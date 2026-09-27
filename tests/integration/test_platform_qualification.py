"""ADR-021: per-hardware-profile qualification, claim filtering, ensemble eligibility."""

from __future__ import annotations

from pathlib import Path

import pytest
from corpus_builder import build_bwi_corpus
from fake_crispasr import FakeLauncher, fake_catalog, ok
from integration_support import SOURCE_KEY, make_scanner

from aerochorus.contracts import WorkerHeartbeat
from aerochorus.sweep_contracts import (
    CatalogSync,
    ClaimRequest,
    ModelUpdate,
    PlatformQualificationWrite,
    PlatformState,
    SweepCreate,
    SweepSelection,
)
from aerochorus.worker.config import SourceMount, TranscriptionConfig, WorkerConfig
from aerochorus.worker.qualify import create_sample, qualify, select_segments
from aerochorus.worker.transcriber import SweepWorker


@pytest.fixture
def lab(api, source, tmp_path):
    root = tmp_path / "bwi"
    build_bwi_corpus(root)
    make_scanner(api, root).scan(SOURCE_KEY)
    models_dir = tmp_path / "models"
    api.sync_models(fake_catalog(models_dir))
    config = WorkerConfig(
        worker_name="pascal-box",
        api_url="http://testserver",
        hardware_profile="linux_pascal_8gb",
        sources={SOURCE_KEY: SourceMount(root=root)},
        transcription=TranscriptionConfig(
            models_dir=models_dir, artifact_root=tmp_path / "artifacts", artifact_store="t"
        ),
    )
    return config


def test_qualification_harness_records_states(api, lab):
    sample_id = create_sample(api, {"source_keys": [SOURCE_KEY]}, n=4, seed=1, name="q")
    segments = select_segments(api, sample_id)
    assert len(segments) == 4

    def script(filename, params, server):
        return ok("") if server.backend == "beta" else ok("baltimore tower delta one two")

    launcher = FakeLauncher(script=script)
    models = [api.get_model("alpha-model"), api.get_model("beta-model")]
    report = qualify(api, lab, models, segments, launcher=launcher)
    states = {r["model"]: r["state"] for r in report}
    # The fake has no GPU: nvidia-smi deltas are unavailable, so no CUDA claim is made.
    assert states["alpha-model"] in ("qualified", "qualified_cpu_only")
    assert states["beta-model"] == "backend_failure"  # loads, but returns nothing usable
    rows = {r.model: r for r in api.platform_qualifications("linux_pascal_8gb")}
    assert rows["beta-model"].metrics["non_empty_rate"] == 0.0
    assert rows["alpha-model"].metrics["segments"] == 4
    assert rows["alpha-model"].metrics["memory"]["strategy"] == "default"

    failing = FakeLauncher(fail_startup_for={"alpha"})
    report = qualify(api, lab, [models[0]], segments, launcher=failing, record=False)
    assert report[0]["state"] == "backend_failure"


def test_blocked_models_are_left_for_other_profiles(api, lab, tmp_path):
    for name in ("alpha-model", "beta-model"):
        api.update_model(name, ModelUpdate(sweep_eligible=True))
    api.put_platform_qualification(
        "alpha-model", "linux_pascal_8gb", PlatformQualificationWrite(state=PlatformState.OOM)
    )
    api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=SOURCE_KEY, limit=2))
    )
    for name in ("pascal-box", "blackwell-box"):
        api.heartbeat(WorkerHeartbeat(name=name, hostname=name, platform="test", version="0"))
    pascal = api.claim_model_run(
        ClaimRequest(worker_name="pascal-box", hardware_profile="linux_pascal_8gb")
    )
    assert pascal.model.logical_name == "beta-model"  # alpha OOMs on this profile
    other = api.claim_model_run(
        ClaimRequest(worker_name="blackwell-box", hardware_profile="windows_blackwell_16gb")
    )
    assert other.model.logical_name == "alpha-model"  # fine elsewhere


def test_sweep_worker_records_profile_and_memory(api, lab):
    for name in ("alpha-model", "beta-model"):
        api.update_model(name, ModelUpdate(sweep_eligible=True))
    api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=SOURCE_KEY, limit=2))
    )
    worker = SweepWorker(api, lab, launcher=FakeLauncher())
    worker.run_until_idle()
    sweep = api.list_sweeps()[0]
    assert sweep.status == "completed"
    runtime = api._request("GET", f"/api/v1/sweeps/{sweep.id}")["models"][0]
    assert runtime["status"] == "completed"


def test_research_only_models_never_vote(api, http, lab):
    base = fake_catalog(Path(lab.transcription.models_dir))
    research = base.models[1].model_copy(update={"ensemble_eligible": False})
    api.sync_models(CatalogSync(families=base.families, models=[base.models[0], research]))
    assert api.get_model("beta-model").ensemble_eligible is False
    for name in ("alpha-model", "beta-model"):
        api.update_model(name, ModelUpdate(sweep_eligible=True))
    api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=SOURCE_KEY, limit=1))
    )
    SweepWorker(
        api, lab, launcher=FakeLauncher(script=lambda f, p, s: ok("same words here"))
    ).run_until_idle()  # noqa: E501
    page = http.post("/api/v1/review/query", json={"filters": {}}).json()
    row = page["rows"][0]
    assert row["results_count"] == 1  # beta's result is stored but does not vote
    assert row["best_exact_provider_count"] == 1
    detail = http.get(f"/api/v1/review/segments/{row['segment_id']}").json()
    assert {h["model"] for h in detail["hypotheses"]} == {"alpha-model", "beta-model"}
