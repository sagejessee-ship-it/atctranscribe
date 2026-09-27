"""CLI: review workbench operations (Phase 5).

``airport bootstrap`` fetches FAA NASR data once and stores a profile through
the API; ``agreement refresh`` backfills derived agreement; ``source set-role``
marks evaluation-only sources; ``ui serve`` runs the review edge server.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path


def _client(args: argparse.Namespace):
    from aerochorus.cli import _client as client

    return client(args)


def _default_cache() -> Path:
    return Path.home() / ".aerochorus" / "cache" / "faa_nasr"


def cmd_airport_bootstrap(args: argparse.Namespace) -> int:
    from aerochorus.context import faa_nasr

    faa_id = args.faa_id or (args.icao[1:] if args.icao.startswith("K") else args.icao)
    if args.apt and args.frq:
        files = {"APT": args.apt, "FRQ": args.frq}
        provenance = {
            "source": "FAA NASR 28-Day Subscription (CSV edition), local archives",
            "source_url": faa_nasr.INDEX_URL,
            "files": {
                name: {
                    "file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
                for name, path in files.items()
            },
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        if args.cycle:
            provenance["cycle"] = faa_nasr.cycle_for(args.cycle).isoformat()
    elif args.apt or args.frq:
        print("pass both --apt and --frq, or neither (to download the current cycle)")
        return 2
    else:
        import httpx

        cycle = faa_nasr.cycle_for(args.cycle or date.today())
        with httpx.Client() as http:
            files, provenance = faa_nasr.download_cycle(cycle, args.cache or _default_cache(), http)
    profile = faa_nasr.parse_profile(
        files,
        faa_id,
        timezone=args.timezone,
        station_labels=tuple(args.station or ()),
        provenance=provenance,
    )
    if profile.icao != args.icao:
        print(f"{faa_id} is {profile.icao} in NASR, not {args.icao}")
        return 2
    if args.dry_run:
        print(json.dumps(profile.model_dump(mode="json"), indent=2))
        return 0
    with _client(args) as client:
        stored = client.put_airport(profile.model_dump(mode="json"))
    print(
        f"{stored['icao']} {stored['name']}: {len(stored['runways'])} runway ends, "
        f"{len(stored['frequencies'])} frequencies, {len(stored['aliases'])} aliases "
        f"(NASR effective {stored['provenance'].get('nasr_effective_date')})"
    )
    return 0


def cmd_airport_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        print(json.dumps(client.get_airport(args.icao), indent=2))
    return 0


def cmd_agreement_refresh(args: argparse.Namespace) -> int:
    total = 0
    with _client(args) as client:
        while True:
            refreshed = client.refresh_agreement(limit=args.chunk)
            total += refreshed
            if refreshed:
                print(f"refreshed {total} segments...", flush=True)
            if refreshed < args.chunk:
                break
    print(f"agreement up to date ({total} segments refreshed)")
    return 0


def cmd_source_set_role(args: argparse.Namespace) -> int:
    with _client(args) as client:
        source = client.set_source_role(args.key, args.role)
    print(f"{source.logical_key}: role={source.role}")
    return 0


def cmd_ui_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from aerochorus.edge.server import create_edge_app
    from aerochorus.worker.config import WorkerConfig, load_worker_config

    try:
        config = load_worker_config(args.config)
    except FileNotFoundError as exc:
        print(f"{exc}\nserving without source audio (no mounted sources)")
        config = WorkerConfig()
    from aerochorus.cli import _api_url

    app = create_edge_app(config, api_url=_api_url(args), static_dir=args.static)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def register(groups, api_opt) -> None:
    airport = groups.add_parser("airport", help="airport context profiles").add_subparsers(
        dest="cmd", required=True
    )
    p = airport.add_parser(
        "bootstrap", help="build an airport profile from FAA NASR data and store it"
    )
    p.add_argument("icao", help="e.g. KBWI")
    p.add_argument("--timezone", required=True, help="IANA zone, e.g. America/New_York")
    p.add_argument("--faa-id", help="FAA location id (default: ICAO without the K)")
    p.add_argument(
        "--station", action="append", help="collector station label that means this airport"
    )
    p.add_argument("--cycle", type=date.fromisoformat, help="a date inside the NASR cycle")
    p.add_argument("--cache", type=Path, help="download cache (default ~/.aerochorus/cache)")
    p.add_argument("--apt", type=Path, help="local <cycle>_APT_CSV.zip instead of downloading")
    p.add_argument("--frq", type=Path, help="local <cycle>_FRQ_CSV.zip instead of downloading")
    p.add_argument("--dry-run", action="store_true", help="print the profile; store nothing")
    api_opt(p)
    p.set_defaults(func=cmd_airport_bootstrap)
    p = airport.add_parser("show", help="show a stored airport profile")
    p.add_argument("icao")
    api_opt(p)
    p.set_defaults(func=cmd_airport_show)

    agreement = groups.add_parser("agreement", help="model agreement").add_subparsers(
        dest="cmd", required=True
    )
    p = agreement.add_parser("refresh", help="recompute missing or stale segment agreement")
    p.add_argument("--chunk", type=int, default=5000, help="segments per API call")
    api_opt(p)
    p.set_defaults(func=cmd_agreement_refresh)

    ui = groups.add_parser("ui", help="review workbench").add_subparsers(dest="cmd", required=True)
    p = ui.add_parser(
        "serve", help="serve the review UI, proxy the API and stream source audio (read-only)"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--static", type=Path, help="built UI directory (default: ui/dist)")
    api_opt(p)
    p.set_defaults(func=cmd_ui_serve)


def register_source(source_parsers, api_opt) -> None:
    p = source_parsers.add_parser(
        "set-role", help="corpus (reviewable) or benchmark (evaluation only, never training)"
    )
    p.add_argument("key")
    p.add_argument("role", choices=["corpus", "benchmark"])
    api_opt(p)
    p.set_defaults(func=cmd_source_set_role)
