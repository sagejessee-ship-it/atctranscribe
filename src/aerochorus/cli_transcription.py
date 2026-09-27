"""CLI commands for the model registry, sweeps and the transcription worker."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CATALOG = Path("config/models.toml")


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "(none)"
    text = [[("" if r.get(c) is None else str(r.get(c))) for c in columns] for r in rows]
    widths = [max(len(c), *(len(t[i]) for t in text)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(v.ljust(w) for v, w in zip(t, widths, strict=True)) for t in text]
    return "\n".join(lines)


def _emit(args: argparse.Namespace, data: Any, table: str | None = None) -> None:
    if getattr(args, "json", False) or table is None:
        if hasattr(data, "model_dump"):
            data = data.model_dump(mode="json")
        elif isinstance(data, list):
            data = [d.model_dump(mode="json") if hasattr(d, "model_dump") else d for d in data]
        print(json.dumps(data, indent=2, default=str))
    else:
        print(table)


def _client(args: argparse.Namespace):
    from aerochorus.cli import _client as client

    return client(args)


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


# --- models and suites ------------------------------------------------------------------


def load_catalog(path: Path):
    from aerochorus.sweep_contracts import CatalogFamily, CatalogModel, CatalogSuite, CatalogSync

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return CatalogSync(
        families=[CatalogFamily(key=k, **v) for k, v in (raw.get("families") or {}).items()],
        models=[CatalogModel.model_validate(m) for m in raw.get("models") or []],
        suites=[CatalogSuite(name=k, **v) for k, v in (raw.get("suites") or {}).items()],
    )


def cmd_models_sync(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _emit(args, client.sync_models(load_catalog(args.catalog)))
    return 0


def _model_rows(models) -> list[dict[str, Any]]:
    return [
        {
            "model": m.logical_name,
            "family": m.architecture_family,
            "backend": m.crisp_backend,
            "quant": m.quantization,
            "size_mb": round((m.artifact_size_bytes or 0) / 1e6),
            "enabled": m.enabled,
            "eligible": m.sweep_eligible,
        }
        for m in models
    ]


def cmd_models_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        models = client.list_models()
    cols = ["model", "family", "backend", "quant", "size_mb", "enabled", "eligible"]
    _emit(args, models, _table(_model_rows(models), cols))
    return 0


def cmd_models_qualify(args: argparse.Namespace) -> int:
    from aerochorus.sweep_contracts import QualificationRecord

    body = QualificationRecord(
        gate=args.gate,
        passed=args.passed,
        evidence=json.loads(args.evidence) if args.evidence else {},
        by=args.by,
    )
    with _client(args) as client:
        model = client.record_qualification(args.name, body)
    gates = {k: ("pass" if v.get("passed") else "FAIL") for k, v in model.qualification.items()}
    _emit(args, model, f"{model.logical_name}: {gates} eligible={model.sweep_eligible}")
    return 0


def cmd_models_set(args: argparse.Namespace) -> int:
    from aerochorus.sweep_contracts import ModelUpdate

    with _client(args) as client:
        model = client.update_model(
            args.name, ModelUpdate(enabled=args.enabled, sweep_eligible=args.eligible)
        )
    _emit(args, model)
    return 0


def cmd_suite_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        suites = client.list_suites()
    rows = [{"suite": s.name, "models": ", ".join(s.models)} for s in suites]
    _emit(args, suites, _table(rows, ["suite", "models"]))
    return 0


def cmd_suite_set(args: argparse.Namespace) -> int:
    from aerochorus.sweep_contracts import SuiteWrite

    with _client(args) as client:
        suite = client.write_suite(
            args.name, SuiteWrite(description=args.description, models=args.models)
        )
    _emit(args, suite)
    return 0


# --- sweeps -----------------------------------------------------------------------------


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise SystemExit(f"--from/--to need an explicit UTC offset, e.g. {value}Z")
    return moment


def cmd_sweep_create(args: argparse.Namespace) -> int:
    from aerochorus.sweep_contracts import SweepCreate, SweepSelection

    params = dict(p.split("=", 1) for p in args.param or [])
    body = SweepCreate(
        suite=args.suite,
        name=args.name,
        request_overrides=params,
        selection=SweepSelection(
            source_key=args.source,
            relative_dir=args.dir,
            utc_from=_parse_time(args.utc_from),
            utc_to=_parse_time(args.utc_to),
            channels=args.channel or [],
            min_duration_ms=args.min_ms,
            max_duration_ms=args.max_ms,
            limit=args.limit,
            seed=args.seed,
        ),
    )
    with _client(args) as client:
        sweep = client.create_sweep(body, allow_unqualified=args.allow_unqualified)
    _emit(args, sweep, _sweep_table(sweep))
    return 0


def _sweep_table(sweep) -> str:
    header = (
        f"sweep {sweep.id} {sweep.name or ''}  status={sweep.status}  suite={sweep.suite}  "
        f"segments={sweep.segments_total}  config={sweep.config_sha256[:12]}"
    )
    rows = []
    for m in sweep.models:
        done = m.segments_completed + m.segments_abstained + m.segments_error
        rows.append(
            {
                "#": m.execution_order,
                "model": m.logical_name,
                "family": m.architecture_family,
                "status": m.status,
                "progress": f"{done}/{m.segments_total} ({_pct(done, m.segments_total)})",
                "ok": m.segments_completed,
                "abstain": m.segments_abstained,
                "error": m.segments_error,
                "rtf": m.real_time_factor,
                "crispasr": m.runtime.get("crispasr_version"),
                "note": (m.last_error or "")[:60],
            }
        )
    cols = [
        "#",
        "model",
        "family",
        "status",
        "progress",
        "ok",
        "abstain",
        "error",
        "rtf",
        "crispasr",
        "note",
    ]
    return header + "\n" + _table(rows, cols)


def cmd_sweep_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        sweeps = client.list_sweeps(args.limit)
    rows = [
        {
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "suite": s.suite,
            "segments": s.segments_total,
            "models": " ".join(f"{m.logical_name.split('-')[0]}:{m.status}" for m in s.models),
            "created": s.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for s in sweeps
    ]
    cols = ["id", "name", "status", "suite", "segments", "models", "created"]
    _emit(args, sweeps, _table(rows, cols))
    return 0


def cmd_sweep_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        sweep = client.get_sweep(args.id)
    _emit(args, sweep, _sweep_table(sweep))
    return 0


def cmd_sweep_control(args: argparse.Namespace) -> int:
    with _client(args) as client:
        sweep = client.control_sweep(args.id, args.action, getattr(args, "model", None))
    _emit(args, sweep, _sweep_table(sweep))
    return 0


def cmd_sweep_report(args: argparse.Namespace) -> int:
    with _client(args) as client:
        report = client.sweep_report(args.id)
    rows = [
        {
            "model": m.logical_name,
            "family": m.architecture_family,
            "status": m.status,
            "results": f"{m.results}/{report.segments_total}",
            "abstain%": _pct(m.abstained, m.results),
            "error%": _pct(m.error, m.results),
            "rtf": m.real_time_factor,
            "ms/seg": m.mean_inference_ms,
            "word_ts": m.word_timestamp_rate,
            "conf": m.mean_token_confidence,
            "lang_drift": m.language_drift,
            "chars": m.mean_chars,
        }
        for m in report.models
    ]
    cols = [
        "model",
        "family",
        "status",
        "results",
        "abstain%",
        "error%",
        "rtf",
        "ms/seg",
        "word_ts",
        "conf",
        "lang_drift",
        "chars",
    ]
    _emit(args, report, f"sweep {report.sweep_id}\n" + _table(rows, cols))
    return 0


def cmd_sweep_transcripts(args: argparse.Namespace) -> int:
    with _client(args) as client:
        items = client.sweep_transcripts(args.id, args.limit, args.offset)
    if args.json:
        _emit(args, items)
        return 0
    for item in items:
        print(
            f"\n[{item['segment_id']}] {item['relative_path']}  "
            f"{item['capture_start_utc'] or '(no UTC)'}  {item['duration_ms']} ms"
        )
        for model, result in sorted(item["results"].items()):
            text = result["text"] if result["status"] == "success" else f"<{result['status']}>"
            print(f"   {model:<32} {text}")
        if not item["results"]:
            print("   (no results yet)")
    return 0


def cmd_segment_results(args: argparse.Namespace) -> int:
    with _client(args) as client:
        results = client.segment_results(args.id)
    rows = [
        {
            "sweep": r.sweep_id,
            "model": r.model,
            "status": r.status,
            "ms": r.inference_ms,
            "text": r.text if r.status == "success" else (r.error_type or ""),
        }
        for r in results
    ]
    _emit(args, results, _table(rows, ["sweep", "model", "status", "ms", "text"]))
    return 0


# --- worker -----------------------------------------------------------------------------


def _worker_config(args: argparse.Namespace):
    from aerochorus.worker.config import load_worker_config

    config = load_worker_config(args.config)
    if config.transcription is None:
        raise SystemExit("worker config has no [transcription] section (see runbook)")
    return config


def _selected_models(client, args: argparse.Namespace):
    models = {m.logical_name: m for m in client.list_models()}
    if args.suite:
        names = next(s.models for s in client.list_suites() if s.name == args.suite)
    elif args.names:
        names = args.names
    else:
        names = [n for n, m in models.items() if m.enabled]
    missing = [n for n in names if n not in models]
    if missing:
        raise SystemExit(f"unknown models: {', '.join(missing)} (run `aerochorus models sync`)")
    return [models[n] for n in names]


def cmd_worker_models_pull(args: argparse.Namespace) -> int:
    from aerochorus.worker.client import ApiClient
    from aerochorus.worker.model_store import ModelArtifactError, ModelStore

    config = _worker_config(args)
    store = ModelStore(config.transcription.models_dir)
    failures = 0
    with ApiClient(config.api_url) as client:
        models = _selected_models(client, args)
    for model in models:
        size = (model.artifact_size_bytes or 0) / 1e6
        print(f"{model.logical_name} ({size:.0f} MB) <- {model.artifact_uri}", flush=True)
        last = [-1]

        def progress(done: int, total: int | None, last=last) -> None:
            if total:
                pct = int(100 * done / total)
                if pct // 10 != last[0]:
                    last[0] = pct // 10
                    print(f"   {pct}%", flush=True)

        try:
            path = store.pull(model, progress=progress)
            print(f"   verified sha256 {model.model_sha256[:16]}... -> {path}")
        except ModelArtifactError as exc:
            failures += 1
            print(f"   FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


def cmd_worker_models_verify(args: argparse.Namespace) -> int:
    from aerochorus.worker.client import ApiClient
    from aerochorus.worker.model_store import ModelStore

    config = _worker_config(args)
    store = ModelStore(config.transcription.models_dir)
    with ApiClient(config.api_url) as client:
        models = _selected_models(client, args)
    rows, bad = [], 0
    for model in models:
        check = store.verify(model)
        state = "ok" if check.ok else ("missing" if not check.present else "MISMATCH")
        bad += state != "ok"
        rows.append({"model": model.logical_name, "state": state, "path": str(check.path)})
    print(_table(rows, ["model", "state", "path"]))
    return 1 if bad else 0


def cmd_worker_crispasr_check(args: argparse.Namespace) -> int:
    """Readiness check: runtime identity, and optionally a live transcription."""
    from aerochorus.worker.client import ApiClient
    from aerochorus.worker.crispasr import interpret, make_launcher
    from aerochorus.worker.model_store import ModelStore

    config = _worker_config(args)
    tc = config.transcription
    launcher = make_launcher(tc.crispasr)
    runtime = launcher.runtime()
    report: dict[str, Any] = {"runtime": runtime.describe()}
    if args.model:
        with ApiClient(config.api_url) as client:
            model = client.get_model(args.model)
        path = ModelStore(tc.models_dir).require(model)
        log_path = tc.artifact_root.parent / "logs" / f"check-{model.logical_name}.log"
        server = launcher.start(
            path, model.crisp_backend, model.request_params.get("language"), log_path
        )
        try:
            report["health"] = server.wait_ready(tc.crispasr.startup_timeout_seconds)
            report["loaded"] = server.loaded_models()
            if args.audio:
                audio = Path(args.audio).read_bytes()
                params = {"response_format": "verbose_json"} | model.request_params
                response = server.transcribe(audio, Path(args.audio).name, params)
                body = response.json() or {}
                report["transcription"] = {
                    "http_status": response.status_code,
                    "elapsed_ms": response.elapsed_ms,
                    "text": body.get("text"),
                    "interpreted": interpret(body).__dict__ if body else None,
                }
        finally:
            server.stop()
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_worker_transcribe(args: argparse.Namespace) -> int:
    from aerochorus.worker.client import ApiClient
    from aerochorus.worker.transcriber import SweepWorker

    config = _worker_config(args)
    with ApiClient(config.api_url) as client:
        worker = SweepWorker(client, config)
        outcomes = worker.run_until_idle(args.max_model_runs)
    rows = [o.__dict__ for o in outcomes]
    print(_table(rows, ["sweep_id", "model", "status", "processed", "message"]))
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def cmd_worker_artifact(args: argparse.Namespace) -> int:
    from aerochorus.worker.artifacts import ArtifactStore

    config = _worker_config(args)
    tc = config.transcription
    stores = {tc.artifact_store: tc.artifact_root} | tc.artifact_mounts
    store = args.uri.removeprefix("artifact://").split("/", 1)[0]
    if store not in stores:
        raise SystemExit(f"artifact store {store!r} is not mounted here (see artifact_mounts)")
    envelope = ArtifactStore(stores[store], store).read(args.uri)
    print(json.dumps(envelope, indent=2, ensure_ascii=False))
    return 0


# --- parser wiring ----------------------------------------------------------------------


def register(groups: argparse._SubParsersAction, api_opt) -> None:
    def json_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="machine-readable output")

    models = groups.add_parser("models", help="model registry").add_subparsers(
        dest="cmd", required=True
    )
    p = models.add_parser("sync", help="load config/models.toml into the registry")
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    api_opt(p)
    p.set_defaults(func=cmd_models_sync)
    p = models.add_parser("list", help="list registered models")
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_models_list)
    p = models.add_parser(
        "qualify", help="record a re-incorporation gate for a converted fine-tune (ADR-019)"
    )
    p.add_argument("name")
    p.add_argument(
        "gate",
        choices=[
            "conversion", "artifact_hash", "crispasr_load", "smoke", "usability", "regression",
            "pedigree",
        ],
    )  # fmt: skip
    outcome = p.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--passed", dest="passed", action="store_true")
    outcome.add_argument("--failed", dest="passed", action="store_false")
    p.add_argument("--evidence", help="JSON: sweep id, report path, metrics, commit...")
    p.add_argument("--by")
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_models_qualify)
    from aerochorus import cli_deploy

    cli_deploy.register_models(models, api_opt, json_opt)
    p = models.add_parser("set", help="enable/disable a model or mark it sweep-eligible")
    p.add_argument("name")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--enable", dest="enabled", action="store_const", const=True)
    group.add_argument("--disable", dest="enabled", action="store_const", const=False)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--eligible", dest="eligible", action="store_const", const=True)
    group.add_argument("--not-eligible", dest="eligible", action="store_const", const=False)
    api_opt(p)
    p.set_defaults(func=cmd_models_set, enabled=None, eligible=None)

    suite = groups.add_parser("suite", help="model suites").add_subparsers(
        dest="cmd", required=True
    )
    p = suite.add_parser("list")
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_suite_list)
    p = suite.add_parser("set", help="define a suite (ordered models)")
    p.add_argument("name")
    p.add_argument("models", nargs="+")
    p.add_argument("--description")
    api_opt(p)
    p.set_defaults(func=cmd_suite_set)

    sweep = groups.add_parser("sweep", help="sweep runs").add_subparsers(dest="cmd", required=True)
    p = sweep.add_parser("create", help="freeze a corpus selection x suite into a sweep")
    p.add_argument("--suite", required=True)
    p.add_argument("--source", default="home_atc_archive")
    p.add_argument("--dir", help="source directory, e.g. 2026/09/08")
    p.add_argument("--from", dest="utc_from", help="UTC start, e.g. 2026-09-08T12:00:00Z")
    p.add_argument("--to", dest="utc_to", help="UTC end (exclusive)")
    p.add_argument("--channel", action="append", help="e.g. GND, TWR (repeatable)")
    p.add_argument("--min-ms", type=int)
    p.add_argument("--max-ms", type=int)
    p.add_argument("--limit", type=int, help="deterministic sample size")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--name")
    p.add_argument("--param", action="append", help="extra CrispASR request field k=v")
    p.add_argument(
        "--allow-unqualified",
        action="store_true",
        help="include models not yet sweep-eligible (smoke/qualification sweeps)",
    )
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_sweep_create)
    p = sweep.add_parser("list")
    p.add_argument("--limit", type=int, default=20)
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_sweep_list)
    for name, help_ in (("show", "progress per model"), ("report", "qualification metrics")):
        p = sweep.add_parser(name, help=help_)
        p.add_argument("id", type=int)
        api_opt(p)
        json_opt(p)
        p.set_defaults(func=cmd_sweep_show if name == "show" else cmd_sweep_report)
    for action in ("pause", "resume", "cancel"):
        p = sweep.add_parser(action)
        p.add_argument("id", type=int)
        api_opt(p)
        json_opt(p)
        p.set_defaults(func=cmd_sweep_control, action=action)
    p = sweep.add_parser("retry", help="re-queue failed model runs (or one model)")
    p.add_argument("id", type=int)
    p.add_argument("--model")
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_sweep_control, action="retry")
    p = sweep.add_parser("transcripts", help="every model's hypothesis per segment")
    p.add_argument("id", type=int)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_sweep_transcripts)

    segment = groups.add_parser("segment", help="segments").add_subparsers(
        dest="cmd", required=True
    )
    p = segment.add_parser("results", help="all hypotheses recorded for one segment")
    p.add_argument("id", type=int)
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_segment_results)


def register_worker(worker: argparse._SubParsersAction) -> None:
    models = worker.add_parser("models", help="model artifacts on this machine").add_subparsers(
        dest="models_cmd", required=True
    )
    for name, func, help_ in (
        ("pull", cmd_worker_models_pull, "download + verify (resumable)"),
        ("verify", cmd_worker_models_verify, "check presence, size and sha256"),
    ):
        p = models.add_parser(name, help=help_)
        p.add_argument("names", nargs="*")
        p.add_argument("--suite")
        p.add_argument("--config", type=Path)
        p.set_defaults(func=func)

    crisp = worker.add_parser("crispasr", help="CrispASR runtime").add_subparsers(
        dest="crisp_cmd", required=True
    )
    p = crisp.add_parser("check", help="runtime identity; with --model, load it and serve once")
    p.add_argument("--model")
    p.add_argument("--audio", help="audio file to transcribe as a live check")
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_crispasr_check)

    p = worker.add_parser("transcribe", help="process queued sweeps until idle")
    p.add_argument("--max-model-runs", type=int)
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_transcribe)

    p = worker.add_parser("artifact", help="print a stored raw CrispASR response")
    p.add_argument("uri")
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_artifact)


with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8")  # transcripts are UTF-8 on every platform
