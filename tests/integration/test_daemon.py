import threading
import time

from corpus_builder import local, place
from fastapi.testclient import TestClient
from integration_support import SOURCE_KEY

from aerochorus.worker.client import ApiClient
from aerochorus.worker.config import SourceMount, WorkerConfig
from aerochorus.worker.daemon import run


def test_worker_loop_heartbeats_and_scans_until_stopped(app, api, source, tmp_path):
    # The loop uses the real clock, so only use files from the past.
    root = tmp_path / "bwi"
    place(
        root,
        "2026/07/16/BWI_TWR_20260716_190000_119400000.mp3",
        start_utc=local(2026, 7, 16, 19, 0, 0),
    )
    place(
        root,
        "2026/07/16/BWI_GND_20260716_190500_121900000.mp3",
        start_utc=local(2026, 7, 16, 19, 5, 0),
    )
    config = WorkerConfig(
        worker_name="loop-worker",
        api_url="http://testserver",
        heartbeat_interval_seconds=0.2,
        sources={SOURCE_KEY: SourceMount(root=root, auto_scan=True)},
    )
    stop = threading.Event()
    worker = threading.Thread(
        target=run,
        args=(config, stop, lambda _config: ApiClient(http=TestClient(app))),
        daemon=True,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not api.list_scans(SOURCE_KEY):
            time.sleep(0.2)
        while time.monotonic() < deadline and api.list_scans(SOURCE_KEY)[0].status == "running":
            time.sleep(0.2)
    finally:
        stop.set()
        worker.join(timeout=15)

    assert not worker.is_alive()
    scans = api.list_scans(SOURCE_KEY)
    assert [s.status for s in scans] == ["completed"]
    assert scans[0].worker_name == "loop-worker"
    assert api.source_summary(SOURCE_KEY).segments == 2
