"""Review edge server (ADR-016): read-only audio with integrity checks, API proxy, UI."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient
from integration_support import SOURCE_KEY, make_scanner

from aerochorus.edge.server import create_edge_app
from aerochorus.worker.config import SourceMount, WorkerConfig

GND = "2026/09/08/BWI_GND_20260908_000024_121900000.mp3"


def api_segments(api):
    rows = api._http.get(f"/api/v1/sources/{SOURCE_KEY}/segments", params={"limit": 1000}).json()
    return {r["relative_path"]: r for r in rows}


def edge_client(app, *, sources=None, static_dir=None):
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cp")
    config = WorkerConfig(worker_name="edge", sources=sources or {})
    edge = create_edge_app(config, api_url="http://cp", static_dir=static_dir, upstream=upstream)
    return TestClient(edge)


def test_audio_is_served_read_only_with_ranges(app, api, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    segment = api_segments(api)[GND]
    original = (root / GND).read_bytes()
    with edge_client(app, sources={SOURCE_KEY: SourceMount(root=root)}) as edge:
        whole = edge.get(f"/audio/{segment['id']}")
        assert whole.status_code == 200
        assert whole.content == original
        assert whole.headers["content-type"] == "audio/mpeg"
        assert whole.headers["x-audio-sha256"] == segment["sha256"]

        part = edge.get(f"/audio/{segment['id']}", headers={"Range": "bytes=10-109"})
        assert part.status_code == 206 and part.content == original[10:110]
        assert part.headers["content-range"] == f"bytes 10-109/{len(original)}"
        tail = edge.get(f"/audio/{segment['id']}", headers={"Range": "bytes=-20"})
        assert tail.content == original[-20:]
        assert (
            edge.get(
                f"/audio/{segment['id']}", headers={"Range": f"bytes={len(original)}-"}
            ).status_code
            == 416
        )
        assert edge.get("/audio/999999").status_code == 404
    assert (root / GND).read_bytes() == original


def test_audio_unavailable_and_changed_sources_fail_clearly(app, api, source, corpus, tmp_path):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    segment_id = api_segments(api)[GND]["id"]

    with edge_client(app) as edge:  # nothing mounted on this machine
        response = edge.get(f"/audio/{segment_id}")
        assert response.status_code == 503 and "not mounted" in response.json()["detail"]

    with edge_client(app, sources={SOURCE_KEY: SourceMount(root=tmp_path / "gone")}) as edge:
        response = edge.get(f"/audio/{segment_id}")
        assert response.status_code == 503 and "unavailable" in response.json()["detail"]

    # A test-only change to the fixture copy: the edge must refuse mismatched audio.
    (root / GND).write_bytes(b"not the indexed audio")
    with edge_client(app, sources={SOURCE_KEY: SourceMount(root=root)}) as edge:
        response = edge.get(f"/audio/{segment_id}")
        assert response.status_code == 409 and "rescan" in response.json()["detail"]


def test_api_proxy_and_spa_fallback(app, api, source, corpus, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>AeroChorus</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    with edge_client(app, static_dir=dist) as edge:
        sources = edge.get("/api/v1/sources")
        assert sources.status_code == 200 and sources.json()[0]["logical_key"] == SOURCE_KEY
        views = edge.get("/api/v1/review/views")
        assert any(v["key"] == "gold" for v in views.json())
        page = edge.post("/api/v1/review/query", json={"filters": {"min_models": 0}})
        assert page.status_code == 200
        assert edge.get("/review/123").text.startswith("<!doctype html>")
        assert edge.get("/assets/app.js").text == "console.log(1)"
        assert edge.get("/../pyproject.toml").text.startswith("<!doctype html>")
        health = edge.get("/edge/health").json()
        assert health["api_reachable"] is True
