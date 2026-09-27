from datetime import UTC, datetime

from corpus_builder import build_bwi_corpus
from integration_support import SOURCE_KEY, make_scanner

from aerochorus.contracts import ScanCreate, WorkerHeartbeat
from aerochorus.db.migrate import head_revision
from aerochorus.worker.config import SourceMount, WorkerConfig
from aerochorus.worker.health import collect_health


def test_health_reports_database_and_migrations(http):
    response = http.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["reachable"] is True
    assert body["database"]["revision"] == head_revision()
    assert body["database"]["migrations_current"] is True


def test_source_registration_rules(http, source):
    assert source.read_only is True
    assert source.adapter_config.filename_timezone == "America/New_York"

    duplicate = {"logical_key": SOURCE_KEY, "name": "again"}
    assert http.post("/api/v1/sources", json=duplicate).status_code == 409

    no_timezone = {
        "logical_key": "other",
        "name": "Other",
        "adapter_config": {"filename_parser": "rtlsdr_airband"},
    }
    assert http.post("/api/v1/sources", json=no_timezone).status_code == 422

    absolute_identity = {"logical_key": "/Volumes/ATC", "name": "bad"}
    assert http.post("/api/v1/sources", json=absolute_identity).status_code == 422

    assert http.get("/api/v1/sources/nope").status_code == 404


def test_batches_are_validated_against_scope_and_directory(http, api, source):
    api.heartbeat(WorkerHeartbeat(name="w", hostname="h", platform="p", version="v"))
    scan = api.start_scan(
        ScanCreate(source_key=SOURCE_KEY, worker_name="w", scope_prefix="2026/09")
    )
    url = f"/api/v1/scans/{scan.id}/batches"

    outside = {"relative_dir": "2026/08/01", "final": True, "directory_mtime_ns": 1}
    assert http.post(url, json=outside).status_code == 422

    wrong_dir = {"relative_dir": "2026/09/08", "seen": ["2026/09/09/a.mp3"]}
    assert http.post(url, json=wrong_dir).status_code == 422

    traversal = {"relative_dir": "2026/09/../../etc"}
    assert http.post(url, json=traversal).status_code == 422

    final_without_mtime = {"relative_dir": "2026/09/08", "final": True}
    assert http.post(url, json=final_without_mtime).status_code == 422

    finish = {"status": "completed"}
    assert http.post(f"/api/v1/scans/{scan.id}/finish", json=finish).status_code == 200
    assert http.post(f"/api/v1/scans/{scan.id}/finish", json=finish).status_code == 409
    late = {"relative_dir": "2026/09/08"}
    assert http.post(url, json=late).status_code == 409


def test_unknown_worker_cannot_start_scans(http, source):
    body = {"source_key": SOURCE_KEY, "worker_name": "ghost"}
    assert http.post("/api/v1/scans", json=body).status_code == 404


def test_summary_and_segment_browsing(http, api, source, tmp_path):
    root = tmp_path / "bwi"
    build_bwi_corpus(root)
    make_scanner(api, root).scan(SOURCE_KEY)

    summary = api.source_summary(SOURCE_KEY)
    assert summary.segments == 7
    assert summary.by_presence == {"present": 7}
    assert summary.by_temporal_status == {"resolved": 6, "unverified": 1}
    days = {d.day_utc: d.segments for d in summary.by_utc_day}
    assert days["2026-09-08"] == 4
    assert days["2026-11-01"] == 1
    assert summary.audio_hours > 0

    page = http.get(f"/api/v1/sources/{SOURCE_KEY}/segments", params={"dir": "2026/09/08"})
    assert page.status_code == 200
    assert len(page.json()) == 4
    first = page.json()[0]
    assert (
        http.get(f"/api/v1/segments/{first['id']}").json()["relative_path"]
        == (first["relative_path"])
    )
    assert len(api.list_scans(SOURCE_KEY)) == 1


def test_worker_heartbeat_and_health(api, source, tmp_path):
    root = tmp_path / "bwi"
    build_bwi_corpus(root)
    config = WorkerConfig(
        worker_name="mac-air",
        api_url="http://testserver",
        sources={
            SOURCE_KEY: SourceMount(root=root),
            "offline_source": SourceMount(root=tmp_path / "missing"),
        },
    )
    health = collect_health(config, api)
    assert health["api"]["reachable"] is True
    assert health["sources"][SOURCE_KEY]["available"] is True
    assert health["sources"][SOURCE_KEY]["registered"] is True
    assert health["sources"]["offline_source"]["available"] is False
    assert health["sources"]["offline_source"]["registered"] is False
    assert health["status"] == "degraded"

    before = datetime.now(UTC)
    worker = api.heartbeat(
        WorkerHeartbeat(name="mac-air", hostname="air", platform="macOS", version="0.1.0")
    )
    assert worker.last_heartbeat_at >= before
    again = api.heartbeat(
        WorkerHeartbeat(name="mac-air", hostname="air", platform="macOS", version="0.1.1")
    )
    assert again.version == "0.1.1"


def test_subdirectory_names_are_single_components(http, api, source):
    api.heartbeat(WorkerHeartbeat(name="w", hostname="h", platform="p", version="v"))
    scan = api.start_scan(ScanCreate(source_key=SOURCE_KEY, worker_name="w"))
    batch = {"final": True, "directory_mtime_ns": 1, "subdirs": ["2026/../../etc"]}
    assert http.post(f"/api/v1/scans/{scan.id}/batches", json=batch).status_code == 422
