"""Long-running worker loop: heartbeats plus periodic corpus scans."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from aerochorus.contracts import ScanMode
from aerochorus.worker.client import ApiClient, ApiError, ApiUnreachable
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.health import collect_health, heartbeat_from_health
from aerochorus.worker.power import keep_awake
from aerochorus.worker.scanner import CorpusScanner

log = logging.getLogger(__name__)


ClientFactory = Callable[[WorkerConfig], ApiClient]


def _default_client(config: WorkerConfig) -> ApiClient:
    return ApiClient(config.api_url)


def _heartbeat_loop(
    config: WorkerConfig, stop: threading.Event, client_factory: ClientFactory
) -> None:
    with client_factory(config) as client:
        while not stop.is_set():
            try:
                health = collect_health(config, client)
                client.heartbeat(heartbeat_from_health(config, health))
            except (ApiError, ApiUnreachable) as exc:
                log.warning("heartbeat failed: %s", exc)
            stop.wait(config.heartbeat_interval_seconds)


def _extra_slot(config, stop, client_factory, slot, registry) -> None:
    """Another model at the same time, when the GPU has room (max_concurrent_models)."""
    from aerochorus.worker.transcriber import SweepWorker

    with client_factory(config) as client:
        sweeper = SweepWorker(client, config, stop=stop, slot=slot, registry=registry)
        while not stop.is_set():
            try:
                if sweeper.process_next() is not None:
                    continue
            except (ApiError, ApiUnreachable) as exc:
                log.warning("slot %s paused: %s", slot, exc)
            except Exception:
                log.exception("slot %s crashed", slot)
            stop.wait(30.0)  # re-check GPU memory and the queue now and then


def run(
    config: WorkerConfig,
    stop: threading.Event | None = None,
    client_factory: ClientFactory = _default_client,
) -> None:
    stop = stop or threading.Event()
    heartbeats = threading.Thread(
        target=_heartbeat_loop,
        args=(config, stop, client_factory),
        name="heartbeat",
        daemon=True,
    )
    heartbeats.start()

    auto = [key for key, mount in sorted(config.sources.items()) if mount.auto_scan]
    log.info("worker %s running; auto-scan sources: %s", config.worker_name, auto or "none")
    next_incremental = dict.fromkeys(auto, 0.0)
    next_full = {key: time.monotonic() + config.full_scan_interval_seconds for key in auto}

    with client_factory(config) as client:
        scanner = CorpusScanner(client, config)
        sweeper = None
        if config.transcription is not None and config.process_sweeps:
            from aerochorus.worker.transcriber import SlotRegistry, SweepWorker

            registry = SlotRegistry()
            sweeper = SweepWorker(client, config, stop=stop, registry=registry)
            log.info("processing queued sweeps with %s", config.transcription.crispasr.launcher)
            for slot in range(1, config.transcription.max_concurrent_models):
                threading.Thread(
                    target=_extra_slot,
                    args=(config, stop, client_factory, slot, registry),
                    name=f"sweep-slot-{slot}",
                    daemon=True,
                ).start()
                log.info("slot %s: may run a second model when GPU memory allows", slot)
        while not stop.is_set():
            for key in auto:
                now = time.monotonic()
                if now >= next_full[key]:
                    mode = ScanMode.FULL
                elif now >= next_incremental[key]:
                    mode = ScanMode.INCREMENTAL
                else:
                    continue
                try:
                    with keep_awake(f"{mode} scan of {key}"):
                        scan = scanner.scan(key, mode)
                    log.info("scan %s of %s: %s %s", scan.id, key, scan.status, scan.counters)
                except (ApiError, ApiUnreachable) as exc:
                    log.warning("scan of %s not completed: %s", key, exc)
                except Exception:
                    # An unattended worker must outlive any single failed scan.
                    log.exception("scan of %s crashed", key)
                finished = time.monotonic()
                next_incremental[key] = finished + config.incremental_scan_interval_seconds
                if mode == ScanMode.FULL:
                    next_full[key] = finished + config.full_scan_interval_seconds
            if sweeper is not None and not stop.is_set():
                try:
                    if sweeper.process_next() is not None:
                        continue  # more work may be queued; re-check scans first
                except (ApiError, ApiUnreachable) as exc:
                    log.warning("sweep processing paused: %s", exc)
                except Exception:
                    log.exception("sweep processing crashed")
            stop.wait(5.0)
