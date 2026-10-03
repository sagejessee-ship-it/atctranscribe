"""Phases 2-3: model registry, sweeps, resumable multi-model transcription."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from corpus_builder import build_bwi_corpus
from fake_crispasr import FakeLauncher, default_script, fake_catalog, ok
from integration_support import SOURCE_KEY, make_scanner
from sqlalchemy import func, select

from aerochorus.db.models import TranscriptionResult
from aerochorus.sweep_contracts import (
    ClaimRequest,
    ModelRunStart,
    ModelUpdate,
    ResultPost,
    ResultStatus,
    SweepCreate,
    SweepSelection,
)
from aerochorus.worker.artifacts import ArtifactStore
from aerochorus.worker.client import ApiError
from aerochorus.worker.config import SourceMount, TranscriptionConfig, WorkerConfig
from aerochorus.worker.crispasr import TranscribeResponse
from aerochorus.worker.transcriber import SweepWorker

SEGMENTS = 7


@dataclass
class Lab:
    root: Path
    placed: dict
    models_dir: Path
    artifacts: Path


@pytest.fixture
def lab(api, source, tmp_path) -> Lab:
    root = tmp_path / "bwi"
    placed = build_bwi_corpus(root)
    make_scanner(api, root).scan(SOURCE_KEY)
    models_dir = tmp_path / "lab" / "models"
    api.sync_models(fake_catalog(models_dir))
    return Lab(root, placed, models_dir, tmp_path / "lab" / "artifacts")


def sweeper(api, lab: Lab, launcher: FakeLauncher, **overrides) -> SweepWorker:
    config = WorkerConfig(
        worker_name="test-worker",
        api_url="http://testserver",
        sources={SOURCE_KEY: SourceMount(root=lab.root)},
        transcription=TranscriptionConfig(
            models_dir=lab.models_dir,
            artifact_root=lab.artifacts,
            artifact_store="test",
            pending_batch=2,
        ),
    )
    return SweepWorker(api, config, launcher=launcher, **overrides)


def new_sweep(api, suite="pair", name=None, **selection):
    return api.create_sweep(
        SweepCreate(
            suite=suite, name=name, selection=SweepSelection(source_key=SOURCE_KEY, **selection)
        ),
        allow_unqualified=True,
    )


def result_count(db, sweep_id=None) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(TranscriptionResult))


def test_catalog_sync_is_idempotent_and_identity_is_immutable(api, lab):
    again = api.sync_models(fake_catalog(lab.models_dir))
    assert again.models_created == []
    assert sorted(again.models_unchanged) == ["alpha-model", "beta-model"]

    tampered = fake_catalog(lab.models_dir)
    tampered.models[0].model_sha256 = "f" * 64
    with pytest.raises(ApiError) as excinfo:
        api.sync_models(tampered)
    assert excinfo.value.status_code == 409


def test_real_catalog_loads(api, source):
    from aerochorus.cli_transcription import load_catalog

    catalog = load_catalog(Path(__file__).resolve().parents[2] / "config" / "models.toml")
    result = api.sync_models(catalog)
    assert len(result.models_created) == 18
    models = api.list_models()
    families = {m.architecture_family for m in models}
    assert len(families) == 14  # variants (beam search, q4_k, 1.7b) share their family
    # Only the established roster votes; new candidates are research-only (ADR-021).
    assert sum(m.ensemble_eligible for m in models) == 7
    suites = {s.name: s.models for s in api.list_suites()}
    assert suites["smoke"] == ["parakeet-tdt-0.6b-v3-q8_0"]
    assert len(suites["qualification"]) == 6
    assert suites["smoke-linux1070"] == ["whisper-large-v3-turbo-q8_0", "parakeet-tdt-0.6b-v3-q8_0"]
    assert len(suites["qualify-linux1070"]) == 17


def test_full_sweeps_require_qualified_models(api, lab):
    with pytest.raises(ApiError) as excinfo:
        api.create_sweep(SweepCreate(suite="pair", selection=SweepSelection(source_key=SOURCE_KEY)))
    assert excinfo.value.status_code == 409

    for name in ("alpha-model", "beta-model"):
        api.update_model(name, ModelUpdate(sweep_eligible=True))
    sweep = api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=SOURCE_KEY))
    )
    assert sweep.segments_total == SEGMENTS
    assert [m.logical_name for m in sweep.models] == ["alpha-model", "beta-model"]


def test_selection_filters_and_frozen_config(api, lab):
    by_dir = new_sweep(api, relative_dir="2026/09/08")
    assert by_dir.segments_total == 4
    gnd = new_sweep(api, channels=["GND"])
    assert gnd.segments_total == 1
    window = new_sweep(
        api,
        utc_from=datetime(2026, 9, 8, 0, tzinfo=UTC),
        utc_to=datetime(2026, 9, 9, 0, tzinfo=UTC),
    )
    assert window.segments_total == 4
    sample = new_sweep(api, limit=3, seed=7)
    assert sample.segments_total == 3
    assert new_sweep(api, limit=3, seed=7).config_sha256 == sample.config_sha256
    assert new_sweep(api, limit=3, seed=8).config_sha256 != sample.config_sha256
    with pytest.raises(ApiError):
        new_sweep(api, channels=["NOPE"])


def test_two_model_sweep_end_to_end(api, db, lab):
    empty = "2026/09/08/BWI_APP_FS_20260908_101500_119700000.mp3"

    def script(filename, params, server):
        if filename == empty.rsplit("/", 1)[1]:
            return ok("")
        return default_script(filename, params, server)

    launcher = FakeLauncher(script=script)
    sweep = new_sweep(api, name="e2e")
    outcomes = sweeper(api, lab, launcher).run_until_idle()

    assert [(o.model, o.status, o.processed) for o in outcomes] == [
        ("alpha-model", "completed", SEGMENTS),
        ("beta-model", "completed", SEGMENTS),
    ]
    # One server per model, each serving the whole window, then stopped.
    assert [len(s.requests) for s in launcher.servers] == [SEGMENTS, SEGMENTS]
    assert all(not s.alive for s in launcher.servers)

    done = api.get_sweep(sweep.id)
    assert done.status == "completed"
    for model in done.models:
        assert model.status == "completed"
        assert (model.segments_completed, model.segments_abstained, model.segments_error) == (
            6,
            1,
            0,
        )
        assert model.runtime["crispasr_version"] == "0.0.0-fake"
        assert model.real_time_factor is not None
    assert result_count(db) == 2 * SEGMENTS

    segment_id = api.sweep_transcripts(sweep.id, limit=1)[0]["segment_id"]
    results = api.segment_results(segment_id)
    assert {r.model for r in results} == {"alpha-model", "beta-model"}
    first = results[0]
    assert first.status == "success" and first.has_word_timestamps
    envelope = ArtifactStore(lab.artifacts, "test").read(first.artifact_uri)
    assert envelope["response"]["text"] == first.text
    assert envelope["model_sha256"] and envelope["audio_sha256"]

    report = api.sweep_report(sweep.id)
    assert [m.abstained for m in report.models] == [1, 1]
    assert all(m.language_drift == 0 for m in report.models)


def test_interrupted_run_resumes_only_missing_work(api, db, lab):
    sweep = new_sweep(api)
    launcher = FakeLauncher()
    worker = sweeper(api, lab, launcher)

    def stop_after_three(filename, params, server):
        if len(server.requests) == 3:
            worker.stop.set()
        return default_script(filename, params, server)

    launcher.script = stop_after_three
    outcomes = worker.run_until_idle()
    assert outcomes[0].status == "released" and outcomes[0].processed == 3
    assert result_count(db) == 3
    assert api.get_sweep(sweep.id).models[0].status == "queued"

    fresh = FakeLauncher()
    outcomes = sweeper(api, lab, fresh).run_until_idle()
    assert [(o.model, o.processed) for o in outcomes] == [
        ("alpha-model", SEGMENTS - 3),
        ("beta-model", SEGMENTS),
    ]
    assert result_count(db) == 2 * SEGMENTS
    assert api.get_sweep(sweep.id).status == "completed"


def test_hard_kill_is_recovered_by_the_same_worker(api, db, lab):
    sweep = new_sweep(api)
    sweeper(api, lab, FakeLauncher()).run_until_idle(max_model_runs=0)  # registers worker
    claim = api.claim_model_run(ClaimRequest(worker_name="test-worker"))
    api.start_model_run(
        claim.sweep_model_id,
        ModelRunStart(
            worker_name="test-worker",
            runtime={"crispasr_version": "0.0.0-fake"},
            runtime_fingerprint=FakeLauncher()
            .runtime()
            .fingerprint(claim.model.model_sha256, claim.model.crisp_backend),
        ),
    )
    first = api.pending_segments(claim.sweep_model_id, 1).segments[0]
    api.post_result(
        claim.sweep_model_id,
        "test-worker",
        ResultPost(
            id=__import__("uuid").uuid4(),
            segment_id=first.segment_id,
            status=ResultStatus.SUCCESS,
            text="recorded before the crash",
        ),
    )
    # ...the process dies here without releasing its claim...

    outcomes = sweeper(api, lab, FakeLauncher()).run_until_idle()
    assert outcomes[0].processed == SEGMENTS - 1
    assert result_count(db) == 2 * SEGMENTS
    assert api.get_sweep(sweep.id).status == "completed"


def test_exactly_one_result_per_segment(api, lab):
    new_sweep(api)
    worker = sweeper(api, lab, FakeLauncher())
    worker.run_until_idle(max_model_runs=0)
    claim = api.claim_model_run(ClaimRequest(worker_name="test-worker"))
    api.start_model_run(
        claim.sweep_model_id,
        ModelRunStart(worker_name="test-worker", runtime={}, runtime_fingerprint="a" * 64),
    )
    segment = api.pending_segments(claim.sweep_model_id, 1).segments[0]
    body = ResultPost(
        id=__import__("uuid").uuid4(),
        segment_id=segment.segment_id,
        status=ResultStatus.SUCCESS,
        text="once",
    )
    api.post_result(claim.sweep_model_id, "test-worker", body)
    with pytest.raises(ApiError) as excinfo:
        api.post_result(
            claim.sweep_model_id,
            "test-worker",
            body.model_copy(update={"id": __import__("uuid").uuid4()}),
        )
    assert excinfo.value.status_code == 409


def test_changed_runtime_cannot_resume(api, db, lab):
    sweep = new_sweep(api)
    worker = sweeper(api, lab, FakeLauncher(version="1.0"))

    def stop_after_two(filename, params, server):
        if len(server.requests) == 2:
            worker.stop.set()
        return default_script(filename, params, server)

    worker.launcher.script = stop_after_two
    worker.run_until_idle()
    assert result_count(db) == 2

    outcomes = sweeper(api, lab, FakeLauncher(version="2.0")).run_until_idle()
    alpha = outcomes[0]
    assert alpha.model == "alpha-model" and alpha.status == "failed"
    assert "differs" in alpha.message
    state = api.get_sweep(sweep.id)
    assert state.models[0].status == "failed"
    assert state.models[1].status == "completed"  # other models are unaffected
    assert state.status == "partial"

    rerun = new_sweep(api)  # the remedy: a new sweep with the new runtime
    sweeper(api, lab, FakeLauncher(version="2.0")).run_until_idle()
    assert api.get_sweep(rerun.id).status == "completed"


def test_model_failure_keeps_other_models_and_can_be_retried(api, db, lab):
    sweep = new_sweep(api)
    outcomes = sweeper(api, lab, FakeLauncher(fail_startup_for={"alpha"})).run_until_idle()
    assert [(o.model, o.status) for o in outcomes] == [
        ("alpha-model", "failed"),
        ("beta-model", "completed"),
    ]
    assert result_count(db) == SEGMENTS
    assert api.get_sweep(sweep.id).status == "partial"

    retried = api.control_sweep(sweep.id, "retry")
    assert retried.models[0].status == "retrying"
    sweeper(api, lab, FakeLauncher()).run_until_idle()
    done = api.get_sweep(sweep.id)
    assert done.status == "completed"
    assert result_count(db) == 2 * SEGMENTS


def test_failed_smoke_request_fails_the_model_run_without_results(api, db, lab):
    def broken(filename, params, server):
        return TranscribeResponse(500, b'{"error": "boom"}', 5)

    new_sweep(api, relative_dir="2026/09/08")
    outcomes = sweeper(api, lab, FakeLauncher(script=broken)).run_until_idle(max_model_runs=1)
    assert outcomes[0].status == "failed" and "smoke" in outcomes[0].message
    assert result_count(db) == 0


def test_segment_errors_are_recorded_then_retried(api, db, lab):
    flaky_name = "BWI_TWR_20260908_092648_119400000.mp3"

    def flaky(filename, params, server):
        if filename == flaky_name:
            return TranscribeResponse(503, b"busy", 5)
        return default_script(filename, params, server)

    sweep = new_sweep(api, suite="pair", relative_dir="2026/09/08")
    sweeper(api, lab, FakeLauncher(script=flaky)).run_until_idle()
    state = api.get_sweep(sweep.id)
    assert state.status == "completed"  # coverage is complete; errors are results too
    assert [m.segments_error for m in state.models] == [1, 1]

    api.control_sweep(sweep.id, "retry", model="alpha-model")
    outcomes = sweeper(api, lab, FakeLauncher()).run_until_idle()
    assert [(o.model, o.processed) for o in outcomes] == [("alpha-model", 1)]
    state = api.get_sweep(sweep.id)
    assert state.models[0].segments_error == 0
    assert state.models[0].segments_completed == 4
    assert state.models[1].segments_error == 1
    assert result_count(db) == 8


def test_pause_releases_and_resume_continues(api, db, lab):
    sweep = new_sweep(api)

    def pause_after_two(filename, params, server):
        if len(server.requests) == 2:
            api.control_sweep(sweep.id, "pause")
        return default_script(filename, params, server)

    outcomes = sweeper(api, lab, FakeLauncher(script=pause_after_two)).run_until_idle()
    assert outcomes[0].status == "released"
    assert api.get_sweep(sweep.id).status == "paused"
    assert sweeper(api, lab, FakeLauncher()).run_until_idle() == []  # nothing claimable

    api.control_sweep(sweep.id, "resume")
    sweeper(api, lab, FakeLauncher()).run_until_idle()
    assert api.get_sweep(sweep.id).status == "completed"
    assert result_count(db) == 2 * SEGMENTS


def test_changed_source_audio_is_an_error_not_a_transcript(api, db, lab):
    sweep = new_sweep(api, relative_dir="2026/09/08")
    target = lab.placed["2026/09/08/BWI_GND_20260908_000024_121900000.mp3"]
    data = bytearray(target.path.read_bytes())
    data[-1] ^= 0xFF
    target.path.write_bytes(bytes(data))

    sweeper(api, lab, FakeLauncher()).run_until_idle(max_model_runs=1)
    errors = [
        r
        for r in api.segment_results(
            next(
                i["segment_id"]
                for i in api.sweep_transcripts(sweep.id)
                if i["relative_path"] == target.relative_path
            )
        )
    ]
    assert errors[0].status == "error" and errors[0].error_type == "source_changed"


def test_artifact_root_inside_a_source_is_refused(api, lab):
    from aerochorus.worker.crispasr import CrispAsrError

    config = WorkerConfig(
        worker_name="w",
        sources={SOURCE_KEY: SourceMount(root=lab.root)},
        transcription=TranscriptionConfig(
            models_dir=lab.models_dir, artifact_root=lab.root / "artifacts", artifact_store="x"
        ),
    )
    with pytest.raises(CrispAsrError, match="never"):
        SweepWorker(api, config, launcher=FakeLauncher())


def test_artifacts_are_compressed_json(api, lab):
    new_sweep(api, relative_dir="2026/09/08")
    sweeper(api, lab, FakeLauncher()).run_until_idle(max_model_runs=1)
    files = list(lab.artifacts.rglob("*.json.zst"))
    assert len(files) == 4
    assert not any(p.suffix == ".jsonl" for p in lab.artifacts.rglob("*"))
    raw = ArtifactStore(lab.artifacts, "test")
    uri = "artifact://test/" + files[0].relative_to(lab.artifacts).as_posix()
    assert json.dumps(raw.read(uri))


def crash_script(nth: int):
    """A backend that aborts (like a GGML assertion) on whichever file is the nth request."""
    state = {"requests": 0, "poison": None}

    def script(filename, params, server):
        state["requests"] += 1
        if state["poison"] is None and state["requests"] == nth:
            state["poison"] = filename
        if filename == state["poison"]:
            server.alive = False
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return default_script(filename, params, server)

    return script, state


@pytest.mark.parametrize("nth", [1, 3], ids=["as-the-smoke-test", "mid-run"])
def test_a_segment_that_crashes_the_backend_costs_only_that_segment(api, db, lab, nth):
    script, state = crash_script(nth)
    launcher = FakeLauncher(script=script)
    sweep = new_sweep(api, relative_dir="2026/09/08")
    outcomes = sweeper(api, lab, launcher).run_until_idle()
    assert [o.status for o in outcomes] == ["completed", "completed"]
    done = api.get_sweep(sweep.id)
    assert done.status == "completed"
    assert [m.segments_error for m in done.models] == [1, 1]  # only the poison segment
    assert result_count(db) == 2 * done.segments_total  # every other segment transcribed
    assert len(launcher.servers) == 4  # each model: started, crashed once, restarted
    errors = db.scalars(
        select(TranscriptionResult.error_type).where(TranscriptionResult.status == "error")
    ).all()
    assert errors == ["server_unavailable", "server_unavailable"]


def test_a_backend_that_crashes_on_everything_fails_without_results(api, db, lab):
    def always(filename, params, server):
        server.alive = False
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    new_sweep(api, relative_dir="2026/09/08")
    launcher = FakeLauncher(script=always)
    outcomes = sweeper(api, lab, launcher).run_until_idle(max_model_runs=1)
    assert outcomes[0].status == "failed" and "smoke" in outcomes[0].message
    assert result_count(db) == 0  # a broken backend leaves nothing behind
    assert len(launcher.servers) == 3  # started, restarted twice, then judged broken


def test_a_voting_decision_survives_catalog_sync(api, http, lab):
    decided = http.post(
        "/api/v1/models/alpha-model/ensemble",
        json={"eligible": False, "reason": "hallucinates on noise", "by": "tester"},
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["model"]["ensemble_eligible"] is False
    assert body["model"]["ensemble_decision"]["previous"] is True
    # The catalog still says alpha votes; the recorded decision wins, and says so.
    result = api.sync_models(fake_catalog(lab.models_dir))
    assert result.ensemble_kept == ["alpha-model"]
    assert api.get_model("alpha-model").ensemble_eligible is False
    assert api.get_model("beta-model").ensemble_eligible is True
    # Voting never changes through the plain update, and never without a reason.
    assert (
        http.patch("/api/v1/models/beta-model", json={"ensemble_eligible": False}).status_code
        == 422
    )
    short = http.post("/api/v1/models/beta-model/ensemble", json={"eligible": False, "reason": ""})
    assert short.status_code == 422
    same = http.post(
        "/api/v1/models/alpha-model/ensemble", json={"eligible": False, "reason": "again"}
    )
    assert same.status_code == 422


def test_selections_beyond_the_bind_parameter_limit(api, http, db, lab):
    """A month of archive is 100k+ segments: previews and sweeps must not pass ids as
    bind parameters (PostgreSQL allows 65,535), and work fetching must use the cursor."""
    from sqlalchemy import text

    from aerochorus.api.sweeps import canonical_sha256
    from aerochorus.db.models import SweepRun, SweepRunSegment

    extra = 70_000
    db.execute(
        text(
            """
            INSERT INTO segment (source_id, relative_path, relative_dir, capture_start_utc,
                temporal_status, duration_ms, file_size, file_mtime, file_mtime_ns,
                first_seen_at, last_seen_at)
            SELECT s.id, 'bulk/' || lpad(g::text, 6, '0') || '.mp3', 'bulk',
                   timestamptz '2026-10-01 00:00Z' + g * interval '1 second', 'resolved',
                   3000, 1000, now(), 0, now(), now()
            FROM corpus_source s, generate_series(1, :n) g
            WHERE s.logical_key = :key
            """
        ),
        {"n": extra, "key": SOURCE_KEY},
    )
    db.commit()
    body = {"suite": "pair", "selection": {"source_key": SOURCE_KEY}}
    preview = http.post("/api/v1/sweeps/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["segments"] == SEGMENTS + extra
    assert preview.json()["audio_minutes"] >= extra * 3 / 60

    sweep = new_sweep(api)
    assert sweep.segments_total == SEGMENTS + extra
    db.expire_all()
    ordinals = db.scalars(
        select(SweepRunSegment.ordinal)
        .where(SweepRunSegment.run_id == sweep.id)
        .order_by(SweepRunSegment.ordinal)
    ).all()
    assert ordinals == list(range(SEGMENTS + extra))  # contiguous, from the database
    ordered = db.scalars(
        select(SweepRunSegment.segment_id)
        .where(SweepRunSegment.run_id == sweep.id)
        .order_by(SweepRunSegment.ordinal)
    ).all()
    run = db.get(SweepRun, sweep.id)
    assert run.effective_config["segment_ids_sha256"] == canonical_sha256(list(ordered))

    # The worker's cursor: the next batch starts right after it (for a claimed run).
    srm_id = sweep.models[0].id
    db.execute(text("UPDATE sweep_run SET status = 'running' WHERE id = :id"), {"id": sweep.id})
    db.execute(
        text("UPDATE sweep_run_model SET status = 'running', attempts = 1 WHERE id = :id"),
        {"id": srm_id},
    )
    db.commit()
    first = http.get(f"/api/v1/sweep-models/{srm_id}/pending", params={"limit": 3}).json()
    assert [s["ordinal"] for s in first["segments"]] == [0, 1, 2]
    later = http.get(
        f"/api/v1/sweep-models/{srm_id}/pending", params={"limit": 2, "after_ordinal": 50_000}
    ).json()
    assert [s["ordinal"] for s in later["segments"]] == [50_001, 50_002]


def test_run_health_progress_and_estimates(api, db, lab, monkeypatch, caplog):
    import logging

    from sqlalchemy import text

    from aerochorus.worker import transcriber

    monkeypatch.setattr(transcriber, "PROGRESS_EVERY_S", 0.0)  # log progress on every result
    finished = new_sweep(api)  # the whole test corpus: enough audio for a speed history
    with caplog.at_level(logging.INFO, logger="aerochorus.worker.transcriber"):
        sweeper(api, lab, FakeLauncher()).run_until_idle()
    assert "segments/s" in caplog.text and "left for this model" in caplog.text

    done = api.get_sweep(finished.id)
    # Processed counts spoken, empty and errors alike; a finished run has no estimate.
    assert all(m.processed == m.segments_total for m in done.models)
    assert done.eta_seconds is None

    queued = api.get_sweep(new_sweep(api).id)  # the whole corpus, not started
    assert all(m.eta_seconds is not None for m in queued.models)  # from the finished run
    assert queued.eta_seconds == pytest.approx(sum(m.eta_seconds for m in queued.models), abs=2)
    assert not queued.eta_partial

    # A running model run that has recorded nothing for 20 minutes is flagged.
    srm = queued.models[0]
    db.execute(text("UPDATE sweep_run SET status = 'running' WHERE id = :id"), {"id": queued.id})
    db.execute(
        text(
            "UPDATE sweep_run_model SET status = 'running', attempts = 1, "
            "started_at = now() - interval '20 minutes', "
            "lease_expires_at = now() + interval '5 minutes' WHERE id = :id"
        ),
        {"id": srm.id},
    )
    db.commit()
    running = api.get_sweep(queued.id).models[0]
    assert running.stalled and running.last_result_at is None
    assert running.recent_per_min == 0


def test_vram_estimates_prefer_measurements():
    from types import SimpleNamespace

    from aerochorus.api.sweeps import estimated_vram_mb

    model = SimpleNamespace(artifact_size_bytes=1000 * 2**20)  # a 1000 MiB model file
    assert estimated_vram_mb(model, None) == 2000  # 1000 * 1.3 + 700
    measured = {"vram_peak_mb": 4776, "vram_before_mb": 3221, "vram_model_mb": 1326}
    assert estimated_vram_mb(model, measured) == 4776 - 3221 + 200
    assert estimated_vram_mb(model, {"vram_model_mb": 1326}) == int(1326 * 1.25 + 200)


def test_claims_for_a_second_slot_exclude_held_runs_and_respect_gpu_memory(api, lab):
    from aerochorus.sweep_contracts import ClaimRequest

    sweep = new_sweep(api)
    worker = sweeper(api, lab, FakeLauncher())
    worker.run_until_idle(max_model_runs=0)  # sends the heartbeat that registers the worker
    base = {"worker_name": "test-worker", "lease_seconds": 600}
    first = api.claim_model_run(ClaimRequest(**base))
    assert first.model.logical_name == "alpha-model"
    # The same worker asking again would normally get its own run back (crash recovery);
    # a second slot excludes it and gets the next model instead.
    second = api.claim_model_run(ClaimRequest(**base, exclude_model_runs=[first.sweep_model_id]))
    assert second.model.logical_name == "beta-model"
    # Only models whose estimated footprint fits are offered (the fake models need ~700 MB).
    both = [first.sweep_model_id, second.sweep_model_id]
    assert api.claim_model_run(ClaimRequest(**base, exclude_model_runs=both)) is None
    assert (
        api.claim_model_run(
            ClaimRequest(**base, exclude_model_runs=[first.sweep_model_id], max_vram_mb=500)
        )
        is None
    )
    assert api.get_sweep(sweep.id).status == "running"


def test_two_slots_transcribe_two_models_at_once(api, db, lab, monkeypatch):
    import threading
    import time

    from aerochorus.worker import hardware
    from aerochorus.worker.transcriber import SlotRegistry

    monkeypatch.setattr(hardware, "gpu_memory_free_mb", lambda index=0: 12_000)
    overlap = threading.Event()
    active: set[str] = set()
    lock = threading.Lock()

    def slow(filename, params, server):
        with lock:
            active.add(server.backend)
            if len(active) == 2:
                overlap.set()
        time.sleep(0.05)
        with lock:
            active.discard(server.backend)
        return default_script(filename, params, server)

    sweep = new_sweep(api)
    registry = SlotRegistry()
    first = sweeper(api, lab, FakeLauncher(script=slow))
    first.registry = registry
    first.run_until_idle(max_model_runs=0)  # register the worker
    second = sweeper(api, lab, FakeLauncher(script=slow))
    second.slot, second.registry = 1, registry
    registry.hold(0, 999_999)  # slot 0 is loading a model: its memory is not visible yet
    assert second.process_next() is None  # so slot 1 waits instead of over-committing
    registry.release(0)

    outcomes = {}
    t0 = threading.Thread(target=lambda: outcomes.__setitem__(0, first.process_next()))
    t0.start()
    deadline = time.monotonic() + 10
    while not any(loaded for _, loaded in registry.others(1)) and time.monotonic() < deadline:
        time.sleep(0.01)
    outcomes[1] = second.process_next()
    t0.join(timeout=30)
    assert {outcomes[0].model, outcomes[1].model} == {"alpha-model", "beta-model"}
    assert overlap.is_set()  # both servers answered requests at the same time
    assert api.get_sweep(sweep.id).status == "completed"
    assert result_count(db) == 2 * SEGMENTS
