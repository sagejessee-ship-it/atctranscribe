"""CLI: model adjudication (ADR-022, ADR-025).

``adjudicate run`` is the runner, on the host with the audio. By default it sends
confirmed batches to OpenRouter (it holds that key). With ``--profile`` (or
``AEROCHORUS_ADJUDICATOR_PROFILE``) it works only on batches for that self-hosted
model and sends them to its server. ``submit`` creates batches for self-hosted
models only (free); paid batches are created in the review UI, where the price is
shown and confirmed. ``list`` / ``show`` report batches.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def _ffmpeg(explicit: str | None) -> str | None:
    from aerochorus.worker.dataset_export import ExportError, find_ffmpeg

    try:
        return find_ffmpeg(explicit)
    except ExportError:
        return None


def _backend(args: argparse.Namespace, settings):
    """(backend, default concurrency, profile or None) for this run."""
    from aerochorus.adjudication.backends import OpenRouterBackend, backend_for
    from aerochorus.adjudication.openrouter import OpenRouterClient
    from aerochorus.adjudication.profiles import resolve_profile

    name = args.profile or settings.adjudicator_profile
    if name:
        profile, _ = resolve_profile(settings, name)
        key = settings.adjudicator_api_key
        ffmpeg = _ffmpeg(args.ffmpeg)
        if ffmpeg is None:
            print(
                "ffmpeg not found (PATH, AEROCHORUS_FFMPEG or --ffmpeg): sending the original "
                "audio, without squelch splitting"
            )
        backend = backend_for(
            profile, api_key=key.get_secret_value() if key else None, ffmpeg=ffmpeg
        )
        return backend, profile.concurrency, profile
    key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else ""
    if not key:
        raise LookupError(
            "AEROCHORUS_OPENROUTER_API_KEY is not set (environment or .env on this host); "
            "the runner needs it to call OpenRouter. For a self-hosted model, pass --profile."
        )
    client = OpenRouterClient(
        key, base_url=settings.openrouter_base_url, timeout_s=settings.adjudication_timeout_s
    )
    return OpenRouterBackend(client), 2, None


def cmd_adjudicate_run(args: argparse.Namespace) -> int:
    from aerochorus.adjudication.errors import BackendFatal
    from aerochorus.cli import _client
    from aerochorus.settings import AdjudicatorSettings
    from aerochorus.worker.adjudicator import Adjudicator
    from aerochorus.worker.config import load_worker_config
    from aerochorus.worker.fs import ReadOnlyCorpusReader

    settings = AdjudicatorSettings()
    try:
        backend, default_concurrency, profile = _backend(args, settings)
    except (LookupError, ValueError) as exc:
        print(exc)
        return 2
    config = load_worker_config(args.config)
    readers = {k: ReadOnlyCorpusReader(m.root) for k, m in config.sources.items()}
    if not readers:
        print("no [sources] in the worker config: the runner needs the audio mounted")
        return 2
    try:
        backend.check()
    except BackendFatal as exc:
        print(exc)
        return 2
    runner = args.runner or (
        f"{config.worker_name}/{profile.name}" if profile else f"{config.worker_name}/adjudicator"
    )
    with _client(args) as api:
        adjudicator = Adjudicator(api, readers, backend, runner)
        print(
            f"{runner} ({backend.label}): "
            + ("waiting for confirmed batches" if args.follow else "draining")
        )
        summary = adjudicator.run(
            follow=args.follow,
            max_items=args.max_items,
            concurrency=args.concurrency or default_concurrency,
            poll_s=args.poll,
        )
    stats = summary.stats()
    print(
        f"done {summary.done}, failed {summary.failed}, spent ${summary.spent_usd:.4f}"
        + (
            f"; latency p50 {stats['latency_ms_p50']} ms, p95 {stats['latency_ms_p95']} ms"
            if stats["latency_ms_p50"] is not None
            else ""
        )
        + "".join(f"; {k.replace('_', ' ')} {v}" for k, v in stats.items() if k.endswith("_total"))
        + (f"; stopped: {summary.stopped}" if summary.stopped else "")
    )
    return 1 if summary.stopped else 0


def _selection(args: argparse.Namespace) -> dict:
    ids: list[int] = []
    for chunk in args.segments or []:
        ids += [int(x) for x in chunk.split(",") if x.strip()]
    if args.segments_file:
        for line in Path(args.segments_file).read_text(encoding="utf-8").splitlines():
            first = line.split(",")[0].strip()
            if first.isdigit():  # also takes the gate's escalation CSV
                ids.append(int(first))
    selection: dict = {"segment_ids": ids}
    if args.sample is not None:
        selection["sample_id"] = args.sample
    if args.source:
        selection |= {
            "filters": {"source_keys": [args.source], "include_benchmark": True},
            "n": args.n,
            "seed": args.seed,
        }
    return selection


def submit_batch(api, profile, selection: dict, args: argparse.Namespace) -> dict | None:
    """Preview and create a batch for a self-hosted profile (no cost to confirm)."""
    from aerochorus.adjudication_contracts import is_local_model

    if not is_local_model(profile.model):
        print(
            f"{profile.model} is not self-hosted: create paid batches in the review UI, "
            "where the price is shown and confirmed (ADR-022)"
        )
        return None
    params = {
        "model": profile.model,
        "include_adsb": not args.no_adsb,
        "include_neighbors": not args.no_neighbors,
        "redo": args.redo,
    }
    plan = api.preview_adjudication({"selection": selection, "params": params})
    skipped = ", ".join(f"{k} {v}" for k, v in sorted(plan["skipped"].items()))
    print(
        f"{profile.model}: {plan['eligible']} of {plan['selected']} selected segments "
        f"({plan['audio_seconds']} s of audio)" + (f"; skipped: {skipped}" if skipped else "")
    )
    if not plan["eligible"]:
        return None
    return api.create_adjudication(
        {
            "selection": selection,
            "params": params,
            "max_cost_usd": plan["suggested_max_cost_usd"],
            "acknowledged_cost_usd": plan["estimated_cost_usd"],
            "confirm": True,
            "note": args.note,
            "created_by": "aerochorus adjudicate submit",
        }
    )


def cmd_adjudicate_submit(args: argparse.Namespace) -> int:
    from aerochorus.adjudication.profiles import resolve_profile
    from aerochorus.cli import _client
    from aerochorus.settings import AdjudicatorSettings

    if args.source and not args.n:
        print("--source needs --n (how many segments to sample)")
        return 2
    try:
        profile, _ = resolve_profile(AdjudicatorSettings(), args.profile)
    except LookupError as exc:
        print(exc)
        return 2
    selection = _selection(args)
    if not (selection["segment_ids"] or "sample_id" in selection or "filters" in selection):
        print("select segments: --segments, --segments-file, --sample or --source with --n")
        return 2
    with _client(args) as api:
        batch = submit_batch(api, profile, selection, args)
    if batch is None:
        return 1
    print(f"batch #{batch['id']} queued: {batch['item_count']} items for {batch['model']}")
    print(f"run it with: aerochorus adjudicate run --profile {profile.name}")
    return 0


def cmd_adjudicate_list(args: argparse.Namespace) -> int:
    from aerochorus.cli import _client

    with _client(args) as api:
        batches = api.list_adjudications()
    for b in batches:
        counts = " ".join(f"{k} {v}" for k, v in sorted(b["counts"].items()))
        print(
            f"#{b['id']} {b['status']:<9} {b['model']} items {b['item_count']} ({counts}) "
            f"spent ${b['spent_usd']:.4f} of ${b['max_cost_usd']:.2f} cap"
        )
    return 0


def cmd_adjudicate_show(args: argparse.Namespace) -> int:
    from aerochorus.cli import _client

    with _client(args) as api:
        batch = api.get_adjudication(args.batch_id)
    print(
        f"#{batch['id']} {batch['status']} {batch['model']} prompt v{batch['prompt_version']} "
        f"spent ${batch['spent_usd']:.4f} / cap ${batch['max_cost_usd']:.2f}"
    )
    for item in batch["items"]:
        text = item["transcript"] or item["error"] or ""
        conf = f"{item['confidence']:.2f}" if item["confidence"] is not None else "—"
        print(f"  segment {item['segment_id']:>8} {item['status']:<9} conf {conf}  {text[:90]}")
    return 0


def _selection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--segments", action="append", help="segment ids, comma separated")
    p.add_argument("--segments-file", help="one segment id per line (or the gate's CSV)")
    p.add_argument("--sample", type=int, default=None, help="a stored review sample id")
    p.add_argument("--source", help="sample this corpus source (benchmarks included)")
    p.add_argument("--n", type=int, default=None, help="how many to sample from --source")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--redo", action="store_true", help="also segments done with this model")
    p.add_argument("--no-adsb", action="store_true", help="leave out cached ADS-B context")
    p.add_argument("--no-neighbors", action="store_true", help="leave out nearby transmissions")
    p.add_argument("--note", default=None)


def register(groups, api_opt) -> None:
    adjudicate = groups.add_parser(
        "adjudicate", help="model adjudication: OpenRouter or a self-hosted audio LLM"
    ).add_subparsers(dest="cmd", required=True)
    p = adjudicate.add_parser(
        "run", help="process confirmed batches: read audio, ask the model, record results"
    )
    p.add_argument("--follow", action="store_true", help="keep polling for new batches")
    p.add_argument("--max-items", type=int, default=None)
    p.add_argument(
        "--concurrency", type=int, default=None, choices=range(1, 9),
        help="requests in flight (default: the profile's, else 2)",
    )  # fmt: skip
    p.add_argument("--poll", type=float, default=10.0, help="seconds between polls (--follow)")
    p.add_argument("--runner", default=None, help="runner name (default <worker>/<profile>)")
    p.add_argument("--profile", default=None, help="self-hosted adjudicator profile")
    p.add_argument("--ffmpeg", default=None, help="ffmpeg binary (squelch splitting)")
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_run)
    p = adjudicate.add_parser(
        "submit", help="queue a batch for a self-hosted profile (free; no paid models)"
    )
    p.add_argument("--profile", default=None, help="adjudicator profile (default: env)")
    _selection_args(p)
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_submit)
    p = adjudicate.add_parser("list", help="recent adjudication batches")
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_list)
    p = adjudicate.add_parser("show", help="one batch with its items")
    p.add_argument("batch_id", type=int)
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_show)
