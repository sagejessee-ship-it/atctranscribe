"""CLI: hardware inventory, hardware-profile qualification (ADR-021)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def cmd_worker_inventory(args: argparse.Namespace) -> int:
    from aerochorus.worker.config import load_worker_config
    from aerochorus.worker.hardware import collect_inventory, cuda_compatibility, suggest_profile

    paths: list[Path] = []
    configured_profile = None
    try:
        config = load_worker_config(args.config)
        configured_profile = config.hardware_profile
        paths += [m.root for m in config.sources.values()]
        if config.transcription:
            tc = config.transcription
            paths += [tc.models_dir, tc.artifact_root]
    except FileNotFoundError:
        config = None
    inventory = collect_inventory(paths)
    report = {
        "suggested_profile": suggest_profile(inventory),
        "configured_profile": configured_profile,
        "cuda": cuda_compatibility(inventory),
        "inventory": inventory.as_dict(),
    }
    if config and config.transcription:
        from aerochorus.worker.crispasr import CrispAsrError, make_launcher

        try:
            report["crispasr"] = make_launcher(config.transcription.crispasr).runtime().describe()
        except (CrispAsrError, OSError) as exc:
            report["crispasr"] = {"error": str(exc)}
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def cmd_worker_qualify(args: argparse.Namespace) -> int:
    from aerochorus.cli_transcription import _selected_models, _worker_config
    from aerochorus.worker.client import ApiClient
    from aerochorus.worker.qualify import Thresholds, create_sample, qualify, select_segments

    config = _worker_config(args)
    with ApiClient(config.api_url, timeout=600) as client:
        models = _selected_models(client, args)
        sample_id = args.sample_id
        if sample_id is None:
            filters = {"source_keys": [args.source]}
            if args.utc_from:
                filters["utc_from"] = args.utc_from
            if args.utc_to:
                filters["utc_to"] = args.utc_to
            if args.channels:
                filters["channels"] = args.channels.split(",")
            sample_id = create_sample(
                client, filters, args.n, args.seed, f"qualification {args.source} n={args.n}"
            )
            print(f"qualification sample #{sample_id} (seed {args.seed})", flush=True)
        segments = select_segments(client, sample_id)
        if not segments:
            raise SystemExit("the qualification sample is empty")
        report = qualify(
            client,
            config,
            models,
            segments,
            profile=args.profile,
            thresholds=Thresholds(max_rtf=args.max_rtf, min_non_empty_rate=args.min_non_empty),
            pull=args.pull,
            record=not args.dry_run,
        )
    from aerochorus.cli_transcription import _table

    columns = [
        *("model", "state", "device", "rtf", "load_seconds", "vram_model_mb"),
        *("vram_peak_mb", "host_ram_peak_mb", "non_empty_rate", "errors", "reason"),
    ]
    print(_table(report, columns))
    return 0


def cmd_models_qualifications(args: argparse.Namespace) -> int:
    from aerochorus.cli import _client
    from aerochorus.cli_transcription import _emit, _table

    with _client(args) as client:
        rows = client.platform_qualifications(args.profile)
    table = [
        {
            "profile": r.hardware_profile,
            "model": r.model,
            "family": r.architecture_family,
            "state": r.state.value,
            "device": r.metrics.get("device"),
            "rtf": r.metrics.get("rtf"),
            "vram_mb": r.metrics.get("vram_model_mb"),
            "non_empty": r.metrics.get("non_empty_rate"),
            "updated": r.updated_at.isoformat(timespec="minutes"),
        }
        for r in rows
    ]
    cols = [
        "profile",
        "model",
        "family",
        "state",
        "device",
        "rtf",
        "vram_mb",
        "non_empty",
        "updated",
    ]
    _emit(args, rows, _table(table, cols))
    return 0


def register_worker(worker) -> None:
    p = worker.add_parser(
        "inventory", help="OS/CPU/RAM/GPU/driver/CUDA + suggested hardware profile"
    )
    p.add_argument("--config", type=Path)
    p.add_argument("--out", type=Path, help="also write the JSON report here")
    p.set_defaults(func=cmd_worker_inventory)

    p = worker.add_parser(
        "qualify", help="qualify models on this machine's hardware profile (one at a time)"
    )
    p.add_argument("names", nargs="*", help="models (default: --suite, else all enabled)")
    p.add_argument("--suite")
    p.add_argument("--profile", help="hardware profile id (default: configured, else detected)")
    p.add_argument("--sample-id", type=int, help="reuse a stored review sample")
    p.add_argument("--source", default="home_atc_archive")
    p.add_argument("--utc-from")
    p.add_argument("--utc-to")
    p.add_argument("--channels", help="comma list, e.g. TWR,GND")
    p.add_argument("--n", type=int, default=20, help="segments in the qualification sample")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-rtf", type=float, default=1.0)
    p.add_argument("--min-non-empty", type=float, default=0.3)
    p.add_argument("--pull", action="store_true", help="download missing model artifacts")
    p.add_argument("--dry-run", action="store_true", help="measure but do not record")
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_qualify)


def register_models(models, api_opt, json_opt) -> None:
    p = models.add_parser("qualifications", help="per-hardware-profile qualification states")
    p.add_argument("--profile")
    api_opt(p)
    json_opt(p)
    p.set_defaults(func=cmd_models_qualifications)
