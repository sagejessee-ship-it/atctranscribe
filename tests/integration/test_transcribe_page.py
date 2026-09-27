"""The Transcribe page's backend: preview, ad-hoc model lists, untranscribed-only."""

from __future__ import annotations

import pytest
from corpus_builder import build_bwi_corpus
from fake_crispasr import FakeLauncher, fake_catalog
from integration_support import SOURCE_KEY, make_scanner

from aerochorus.worker.config import SourceMount, TranscriptionConfig, WorkerConfig
from aerochorus.worker.transcriber import SweepWorker


@pytest.fixture
def lab(api, source, tmp_path):
    root = tmp_path / "bwi"
    build_bwi_corpus(root)
    make_scanner(api, root).scan(SOURCE_KEY)
    api.sync_models(fake_catalog(tmp_path / "models"))
    return WorkerConfig(
        worker_name="test-worker",
        api_url="http://testserver",
        sources={SOURCE_KEY: SourceMount(root=root)},
        transcription=TranscriptionConfig(
            models_dir=tmp_path / "models", artifact_root=tmp_path / "art", artifact_store="t"
        ),
    )


def body(**selection):
    return {
        "models": ["beta-model", "alpha-model"],
        "selection": {"source_key": SOURCE_KEY} | selection,
    }


def test_preview_counts_without_creating(http, lab):
    preview = http.post("/api/v1/sweeps/preview", json=body()).json()
    assert preview["segments"] == 7 and preview["audio_minutes"] > 0
    assert [m["logical_name"] for m in preview["models"]] == ["beta-model", "alpha-model"]
    assert preview["needs_unqualified"] == ["beta-model", "alpha-model"]
    assert preview["estimated_minutes"] is None  # no speed history yet
    assert http.get("/api/v1/sweeps").json() == []
    narrowed = http.post("/api/v1/sweeps/preview", json=body(channels=["TWR"])).json()
    assert 0 < narrowed["segments"] < 7
    bad = http.post("/api/v1/sweeps/preview", json={"selection": {"source_key": SOURCE_KEY}})
    assert bad.status_code == 422  # neither suite nor models


def test_adhoc_models_and_untranscribed_only(http, lab):
    created = http.post(
        "/api/v1/sweeps?allow_unqualified=true", json=body(limit=3, seed=1) | {"name": "ui"}
    )
    assert created.status_code == 201, created.text
    sweep = created.json()
    assert sweep["suite"].startswith("adhoc-")
    assert [m["logical_name"] for m in sweep["models"]] == ["beta-model", "alpha-model"]
    from aerochorus.worker.client import ApiClient

    SweepWorker(ApiClient(http=http), lab, launcher=FakeLauncher()).run_until_idle()

    after = http.post("/api/v1/sweeps/preview", json=body(untranscribed_only=True)).json()
    assert after["segments"] == 7 - 3  # the three done by both models are skipped
    assert all(m["observed_rtf"] is not None for m in after["models"])
    assert after["estimated_minutes"] is not None
    # Same list again reuses the same recorded ad-hoc suite.
    again = http.post("/api/v1/sweeps?allow_unqualified=true", json=body(limit=1)).json()
    assert again["suite"] == sweep["suite"]
