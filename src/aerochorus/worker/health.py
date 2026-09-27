"""Worker self-report: what this machine can see and reach."""

from __future__ import annotations

import platform
import socket
import sys
import time
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
        "crispasr": None,
        "hardware": _hardware(config),
    }


_INVENTORY: tuple[float, Any] | None = None
INVENTORY_TTL_SECONDS = 600


def _inventory():
    """Hardware changes rarely: probe (nvidia-smi etc.) at most every ten minutes."""
    global _INVENTORY
    from aerochorus.worker.hardware import collect_inventory

    now = time.monotonic()
    if _INVENTORY is None or now - _INVENTORY[0] > INVENTORY_TTL_SECONDS:
        _INVENTORY = (now, collect_inventory())
    return _INVENTORY[1]


def _hardware(config: WorkerConfig) -> dict[str, Any]:
    from aerochorus.worker.hardware import resolve_profile

    try:
        inventory = _inventory()
    except Exception as exc:  # noqa: BLE001 - health must never fail on a probe
        return {"error": str(exc)}
    gpus = [
        {"name": g.name, "compute_capability": g.compute_capability,
         "memory_total_mb": g.memory_total_mb, "driver": g.driver_version}
        for g in inventory.gpus
    ]  # fmt: skip
    return {
        "profile": resolve_profile(config.hardware_profile),
        "os": inventory.os_pretty,
        "kernel": inventory.kernel,
        "cpu": inventory.cpu_model,
        "cpu_cores": inventory.cpu_cores,
        "ram_total_mb": inventory.ram_total_mb,
        "gpus": gpus,
        "cuda_driver": inventory.cuda_driver_version,
    }


def heartbeat_from_health(config: WorkerConfig, health: dict[str, Any]) -> WorkerHeartbeat:
    return WorkerHeartbeat(
        name=config.worker_name,
        hostname=health["hostname"],
        platform=health["platform"],
        version=health["version"],
        health={
            k: health[k] for k in ("status", "machine", "python", "sources", "crispasr", "hardware")
        },
    )
