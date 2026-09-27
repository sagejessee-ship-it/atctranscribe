"""``aerochorus`` command line.

Control-plane commands (``db``, ``api``) import the database layer lazily so
worker commands never load it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from aerochorus import __version__


def _print_json(data: Any) -> None:
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    elif isinstance(data, list):
        data = [d.model_dump(mode="json") if hasattr(d, "model_dump") else d for d in data]
    print(json.dumps(data, indent=2, default=str))


def _api_url(args: argparse.Namespace) -> str:
    if args.api:
        return args.api
    if url := os.environ.get("AEROCHORUS_API_URL"):
        return url
    from aerochorus.worker.config import load_worker_config

    try:
        return load_worker_config(getattr(args, "config", None)).api_url
    except FileNotFoundError:
        return "http://127.0.0.1:8000"


def _client(args: argparse.Namespace):
    from aerochorus.worker.client import ApiClient

    return ApiClient(_api_url(args))


# --- control plane -------------------------------------------------------------


def cmd_db_upgrade(args: argparse.Namespace) -> int:
    from aerochorus.db.migrate import upgrade
    from aerochorus.settings import get_settings

    upgrade(get_settings().database_url, args.revision)
    return 0


def cmd_api_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "aerochorus.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


# --- sources and scans (via the API) -------------------------------------------


def cmd_source_add(args: argparse.Namespace) -> int:
    from aerochorus.contracts import SourceCreate
    from aerochorus.corpus.config import FilesystemAdapterConfig

    config: dict[str, Any] = {
        "filename_parser": args.parser,
        "filename_timezone": args.timezone,
        "sentinel_paths": args.sentinel or [],
    }
    if args.ext:
        config["include_extensions"] = args.ext
    if args.min_file_age is not None:
        config["min_file_age_seconds"] = args.min_file_age
    if args.mtime_tolerance is not None:
        config["mtime_tolerance_seconds"] = args.mtime_tolerance
    body = SourceCreate(
        logical_key=args.key,
        name=args.name,
        adapter_config=FilesystemAdapterConfig.model_validate(config),
    )
    with _client(args) as client:
        _print_json(client.create_source(body))
    return 0


def cmd_source_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print_json(client.list_sources())
    return 0


def cmd_source_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print_json(client.get_source(args.key))
    return 0


def cmd_source_summary(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print_json(client.source_summary(args.key))
    return 0


def cmd_scan_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        _print_json(client.list_scans(args.source, args.limit))
    return 0


# --- worker ------------------------------------------------------------------------


def cmd_worker_health(args: argparse.Namespace) -> int:
    from aerochorus.worker.client import ApiClient, ApiError, ApiUnreachable
    from aerochorus.worker.config import load_worker_config
    from aerochorus.worker.health import collect_health, heartbeat_from_health

    config = load_worker_config(args.config)
    with ApiClient(config.api_url) as client:
        health = collect_health(config, client)
        if health["api"]["reachable"]:
            try:
                client.heartbeat(heartbeat_from_health(config, health))
                health["heartbeat_sent"] = True
            except (ApiError, ApiUnreachable) as exc:
                health["heartbeat_sent"] = False
                health["heartbeat_error"] = str(exc)
    _print_json(health)
    return 0 if health["status"] == "ok" else 1


def cmd_worker_scan(args: argparse.Namespace) -> int:
    from aerochorus.contracts import ScanMode, ScanStatus
    from aerochorus.worker.client import ApiClient
    from aerochorus.worker.config import load_worker_config
    from aerochorus.worker.power import keep_awake
    from aerochorus.worker.scanner import CorpusScanner

    config = load_worker_config(args.config)
    with ApiClient(config.api_url) as client, keep_awake(f"scan of {args.key}"):
        scan = CorpusScanner(client, config).scan(args.key, ScanMode(args.mode), args.prefix)
    _print_json(scan)
    return 0 if scan.status == ScanStatus.COMPLETED else 2


def cmd_worker_run(args: argparse.Namespace) -> int:
    from aerochorus.worker.config import load_worker_config
    from aerochorus.worker.daemon import run

    with contextlib.suppress(KeyboardInterrupt):
        run(load_worker_config(args.config))
    return 0


# --- parser -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aerochorus", description="AeroChorus ATC corpus transcription lab"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    groups = parser.add_subparsers(dest="group", required=True)

    def api_opt(p: argparse.ArgumentParser) -> None:
        p.add_argument("--api", help="control-plane URL (default: worker config or localhost)")
        p.add_argument("--config", type=Path, help="worker config path (used to find the API)")

    db = groups.add_parser("db", help="database administration").add_subparsers(
        dest="cmd", required=True
    )
    p = db.add_parser("upgrade", help="apply migrations")
    p.add_argument("revision", nargs="?", default="head")
    p.set_defaults(func=cmd_db_upgrade)

    api = groups.add_parser("api", help="control-plane API").add_subparsers(
        dest="cmd", required=True
    )
    p = api.add_parser("serve", help="run the API server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_api_serve)

    source = groups.add_parser("source", help="corpus sources").add_subparsers(
        dest="cmd", required=True
    )
    p = source.add_parser("add", help="register a read-only corpus source")
    p.add_argument("key", help="logical key, e.g. home_atc_archive")
    p.add_argument("--name", required=True)
    p.add_argument("--parser", choices=["rtlsdr_airband"], help="filename convention")
    p.add_argument("--timezone", help="IANA zone of filename timestamps (required with --parser)")
    p.add_argument("--ext", action="append", help="audio extension to include (repeatable)")
    p.add_argument("--sentinel", action="append", help="relative path that must exist")
    p.add_argument("--min-file-age", type=float)
    p.add_argument("--mtime-tolerance", type=float)
    api_opt(p)
    p.set_defaults(func=cmd_source_add)
    for name, func, help_ in (
        ("show", cmd_source_show, "show a source"),
        ("summary", cmd_source_summary, "segment counts and coverage"),
    ):
        p = source.add_parser(name, help=help_)
        p.add_argument("key")
        api_opt(p)
        p.set_defaults(func=func)
    p = source.add_parser("list", help="list sources")
    api_opt(p)
    p.set_defaults(func=cmd_source_list)

    scan = groups.add_parser("scan", help="scan history").add_subparsers(dest="cmd", required=True)
    p = scan.add_parser("list", help="recent scans")
    p.add_argument("--source")
    p.add_argument("--limit", type=int, default=20)
    api_opt(p)
    p.set_defaults(func=cmd_scan_list)

    worker = groups.add_parser("worker", help="native worker").add_subparsers(
        dest="cmd", required=True
    )
    p = worker.add_parser("health", help="report worker health (and send a heartbeat)")
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_health)
    p = worker.add_parser("scan", help="scan a corpus source now")
    p.add_argument("key")
    p.add_argument("--mode", choices=["incremental", "full", "verify"], default="incremental")
    p.add_argument("--prefix", default="", help="limit the scan to this relative directory")
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_scan)
    p = worker.add_parser("run", help="run the worker loop (heartbeats, scheduled scans)")
    p.add_argument("--config", type=Path)
    p.set_defaults(func=cmd_worker_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
