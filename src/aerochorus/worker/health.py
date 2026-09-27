"""Worker self-report: what this machine can see and reach."""

from __future__ import annotations

import platform
import socket
import sys
from typing import Any

from aerochorus import __version__
from aerochorus.contracts import WorkerHeartbeat
from aerochorus.worker.client import ApiClient, ApiError, ApiUnreachable
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.fs import ReadOnlyCorpusReader


def collect_health(config: WorkerConfig, client: ApiClient | None) -> dict[str, Any]:
    api: dict[str, Any] = {"url": config.api_url, "reachable": False}
    if client is not None:
        try:
            api["health"] = client.health()
            api["reachable"] = True
        except ApiUnreachable as exc:
            api["error"] = str(exc)

    sources: dict[str, Any] = {}
    for key, mount in sorted(config.sources.items()):
        entry: dict[str, Any] = {"root": str(mount.root), "auto_scan": mount.auto_scan}
        sentinels: list[str] = []
        if api["reachable"]:
            try:
                sentinels = client.get_source(key).adapter_config.sentinel_paths
                entry["registered"] = True
            except ApiError as exc:
                entry["registered"] = False if exc.status_code == 404 else None
        availability = ReadOnlyCorpusReader(mount.root, sentinels).check_available()
        entry["available"] = availability.ok
        if availability.reason:
            entry["reason"] = availability.reason
        sources[key] = entry

    api_ok = api["reachable"] and api.get("health", {}).get("status") == "ok"
    sources_ok = all(s["available"] for s in sources.values())
    return {
        "status": "ok" if api_ok and sources_ok else "degraded",
        "worker_name": config.worker_name,
        "version": __version__,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "api": api,
        "sources": sources,
        # Reported once the CrispASR integration exists (Phase 2).
        "crispasr": None,
    }


def heartbeat_from_health(config: WorkerConfig, health: dict[str, Any]) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        name=config.worker_name,
        hostname=health["hostname"],
        platform=health["platform"],
        version=health["version"],
        health={k: health[k] for k in ("status", "machine", "python", "sources", "crispasr")},
    )
