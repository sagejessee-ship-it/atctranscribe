"""CLI: model adjudication (ADR-022).

``adjudicate run`` is the runner: it works only on batches a human created and
confirmed (review UI or API), on the host with the audio and the OpenRouter key.
``list`` / ``show`` report batches. Nothing here creates a batch or spends money
on its own.
"""

from __future__ import annotations

import argparse


def cmd_adjudicate_run(args: argparse.Namespace) -> int:
    from aerochorus.adjudication.openrouter import OpenRouterClient
    from aerochorus.cli import _client
    from aerochorus.settings import AdjudicatorSettings
    from aerochorus.worker.adjudicator import Adjudicator
    from aerochorus.worker.config import load_worker_config
    from aerochorus.worker.fs import ReadOnlyCorpusReader

    settings = AdjudicatorSettings()
    key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else ""
    if not key:
        print(
            "AEROCHORUS_OPENROUTER_API_KEY is not set (environment or .env on this host); "
            "the runner needs it to call OpenRouter"
        )
        return 2
    config = load_worker_config(args.config)
    readers = {k: ReadOnlyCorpusReader(m.root) for k, m in config.sources.items()}
    if not readers:
        print("no [sources] in the worker config: the runner needs the audio mounted")
        return 2
    runner = args.runner or f"{config.worker_name}/adjudicator"
    openrouter = OpenRouterClient(
        key, base_url=settings.openrouter_base_url, timeout_s=settings.adjudication_timeout_s
    )
    with _client(args) as api:
        adjudicator = Adjudicator(api, readers, openrouter, runner)
        print(f"{runner}: waiting for confirmed batches" if args.follow else f"{runner}: draining")
        summary = adjudicator.run(
            follow=args.follow,
            max_items=args.max_items,
            concurrency=args.concurrency,
            poll_s=args.poll,
        )
    print(
        f"done {summary.done}, failed {summary.failed}, spent ${summary.spent_usd:.4f}"
        + (f"; stopped: {summary.stopped}" if summary.stopped else "")
    )
    return 1 if summary.stopped else 0


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


def register(groups, api_opt) -> None:
    adjudicate = groups.add_parser(
        "adjudicate", help="model adjudication of confirmed batches (OpenRouter)"
    ).add_subparsers(dest="cmd", required=True)
    p = adjudicate.add_parser(
        "run", help="process confirmed batches: read audio, ask the model, record results"
    )
    p.add_argument("--follow", action="store_true", help="keep polling for new batches")
    p.add_argument("--max-items", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=2, choices=range(1, 9))
    p.add_argument("--poll", type=float, default=10.0, help="seconds between polls (--follow)")
    p.add_argument("--runner", default=None, help="runner name (default <worker>/adjudicator)")
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_run)
    p = adjudicate.add_parser("list", help="recent adjudication batches")
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_list)
    p = adjudicate.add_parser("show", help="one batch with its items")
    p.add_argument("batch_id", type=int)
    api_opt(p)
    p.set_defaults(func=cmd_adjudicate_show)
