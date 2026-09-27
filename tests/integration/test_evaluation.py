"""Phase 4: benchmark corpus, isolated gold, scoring, and the leakage boundary."""

from __future__ import annotations

import json

import pytest
from atco2_builder import GOLD_PHRASES, build_atco2
from fake_crispasr import FakeLauncher, fake_catalog, ok
from sqlalchemy import select

from aerochorus.contracts import SourceCreate
from aerochorus.corpus.config import FilesystemAdapterConfig
from aerochorus.datasets.atco2 import prepare_fixed_clips, read_gold_references
from aerochorus.db.models import TranscriptionResult
from aerochorus.eval_client import EvalClient
from aerochorus.eval_contracts import GoldReferenceIn
from aerochorus.sweep_contracts import ClaimRequest, SweepCreate, SweepSelection
from aerochorus.worker.client import ApiError
from aerochorus.worker.config import SourceMount, TranscriptionConfig, WorkerConfig
from aerochorus.worker.scanner import CorpusScanner
from aerochorus.worker.transcriber import SweepWorker

BENCH = "atco2_fixed"
LKPR = "LKPR_RUZYNE_Radar_120_520MHz_20201025_091112"
LSZH = "LSZH_ZURICH_Tower_118_1MHz_20210412_161248"
# What the fake models "hear" for each clip.
ALPHA = {
    f"{LKPR}/01_0_3770.wav": "Oscar Kilo Papa Mike Bravo, descend flight level one hundred.",
    f"{LKPR}/02_3790_6850.wav": "level one hundred Oscar Kilo Papa Mike",  # 1 deletion
    f"{LSZH}/01_500_2000.wav": "",  # abstains: 6 deletions
    f"{LSZH}/02_2200_4000.wav": "cleared to land Swiss Two Three",
}
BETA = {
    f"{LKPR}/01_0_3770.wav": "Oscar Kilo Papa Mike descend level one hundred",  # 2 deletions
    f"{LKPR}/02_3790_6850.wav": "level one hundred Oscar Kilo Papa Mike Bravo",
    f"{LSZH}/01_500_2000.wav": "Swiss Two Three cleared to land",
    f"{LSZH}/02_2200_4000.wav": "thank you for watching",  # hallucination: 4 subs + 2 del
}


@pytest.fixture
def bench(api, http, tmp_path):
    data = build_atco2(tmp_path / "DATA")
    clips = tmp_path / "clips"
    prepare_fixed_clips(data, clips)
    api.create_source(
        SourceCreate(
            logical_key=BENCH,
            name="ATCO2 fixed",
            adapter_config=FilesystemAdapterConfig(
                include_extensions=[".wav"],
                filename_parser="atco2_clip",
                filename_timezone="UTC",
                mtime_corroboration=False,
                min_file_age_seconds=0,
            ),
        )
    )
    models_dir = tmp_path / "models"
    api.sync_models(fake_catalog(models_dir))
    config = WorkerConfig(
        worker_name="bench-worker",
        api_url="http://testserver",
        sources={BENCH: SourceMount(root=clips)},
        transcription=TranscriptionConfig(
            models_dir=models_dir, artifact_root=tmp_path / "artifacts", artifact_store="bench"
        ),
    )
    CorpusScanner(api, config).scan(BENCH)
    evaluator = EvalClient(http=http)
    rows = [GoldReferenceIn.model_validate(r) for r in read_gold_references(data)]
    evaluator.import_references(BENCH, rows)
    return {"data": data, "config": config, "evaluator": evaluator, "rows": rows}


def script(filename, params, server):
    table = ALPHA if server.backend == "alpha" else BETA
    for path, text in table.items():
        if path.endswith(filename):
            return ok(text)
    raise AssertionError(filename)


def run_benchmark_sweep(api, bench) -> int:
    sweep = api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=BENCH)),
        allow_unqualified=True,
    )
    SweepWorker(api, bench["config"], launcher=FakeLauncher(script=script)).run_until_idle()
    return sweep.id


def test_prepared_clips_index_with_station_frequency_and_utc(api, db, bench):
    from aerochorus.db.models import Segment

    rows = {s.relative_path: s for s in db.scalars(select(Segment))}
    clip = rows[f"{LKPR}/02_3790_6850.wav"]
    assert (clip.station, clip.channel, clip.frequency_hz) == ("LKPR", "RUZYNE_Radar", 120_520_000)
    assert clip.capture_start_utc.isoformat() == "2020-10-25T09:11:15.790000+00:00"
    assert clip.temporal_status == "unverified"  # filename UTC, nothing to corroborate it
    assert clip.metadata_["temporal"]["reason"] == "corroboration_not_applicable"


def test_scores_are_exact_and_silence_is_not_free(api, bench):
    sweep_id = run_benchmark_sweep(api, bench)
    report = bench["evaluator"].evaluate(sweep_id)
    by_model = {m.logical_name: m for m in report.models}

    # gold tokens: 10 + 8 + 6 + 6 = 30
    assert report.reference_tokens == 30
    alpha, beta = by_model["alpha-model"], by_model["beta-model"]
    assert (alpha.token_errors, alpha.deletions, alpha.abstained) == (7, 7, 1)
    assert alpha.token_error_rate == pytest.approx(7 / 30, abs=1e-4)
    assert (beta.token_errors, beta.substitutions) == (8, 4)
    assert beta.flags.get("off_domain_phrase") == 1

    assert report.best_single["model"] == "alpha-model"
    # oracle per segment: 0 + 0 + 0 + 0 (each segment has one perfect model)
    assert report.oracle["token_error_rate"] == 0.0
    assert report.ensemble is None
    assert any("Phase 6" in n for n in report.notes)

    # entity recall from the gold word-class tags
    assert alpha.entities["callsign"].spans == 2
    assert alpha.entities["callsign"].recovered == 1  # abstained on the Swiss clip
    assert alpha.entities["value"].recall == 1.0
    # "descend level one hundred" does not contain the gold "flight level one hundred"
    assert beta.entities["value"].recall == 0.0

    assert set(report.by_split) <= {"calibration", "test"}
    assert sum(v["_segments"] for v in report.by_split.values()) == report.segments_scored
    for name, values in report.by_split.items():
        split_report = bench["evaluator"].evaluate(sweep_id, split=name)
        assert split_report.segments_scored == values["_segments"]

    worst = bench["evaluator"].evaluation_segments(sweep_id, model="beta-model", limit=1)
    assert worst[0].relative_path == f"{LSZH}/02_2200_4000.wav"
    assert worst[0].results["beta-model"]["errors"] == 6


def test_gold_never_reaches_the_transcription_path(api, http, db, bench):
    """v1 Tier 1 #5: gold isolation, checked on every payload the worker can see."""
    sweep = api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=BENCH)),
        allow_unqualified=True,
    )
    worker = SweepWorker(api, bench["config"], launcher=FakeLauncher(script=script))
    worker.run_until_idle(max_model_runs=0)
    claim = api.claim_model_run(ClaimRequest(worker_name="bench-worker", source_keys=[BENCH]))
    seen = [
        claim.model_dump_json(),
        api.get_sweep(sweep.id).model_dump_json(),
        json.dumps(http.get(f"/api/v1/sweeps/{sweep.id}/transcripts").json()),
    ]
    api.start_model_run(
        claim.sweep_model_id,
        __import__("aerochorus.sweep_contracts", fromlist=["ModelRunStart"]).ModelRunStart(
            worker_name="bench-worker", runtime={}, runtime_fingerprint="a" * 64
        ),
    )
    seen.append(api.pending_segments(claim.sweep_model_id, 100).model_dump_json())
    for payload in seen:
        for phrase in GOLD_PHRASES:
            assert phrase.lower() not in payload.lower(), phrase

    # and gold only flows out of the evaluation endpoints
    assert any(
        "descend" in r.reference for r in bench["evaluator"].evaluation_segments(sweep.id, limit=10)
    )


def test_gold_is_immutable(api, bench):
    changed = [r.model_copy(update={"text_raw": "something else"}) for r in bench["rows"][:1]]
    with pytest.raises(ApiError) as excinfo:
        bench["evaluator"].import_references(BENCH, changed)
    assert excinfo.value.status_code == 409
    again = bench["evaluator"].import_references(BENCH, bench["rows"])
    assert (again.inserted, again.unchanged) == (0, len(bench["rows"]))


def test_workers_only_claim_sweeps_for_sources_they_can_read(api, bench):
    api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=BENCH)),
        allow_unqualified=True,
    )
    SweepWorker(api, bench["config"], launcher=FakeLauncher(script=script)).run_until_idle(
        max_model_runs=0
    )
    assert (
        api.claim_model_run(
            ClaimRequest(worker_name="bench-worker", source_keys=["home_atc_archive"])
        )
        is None
    )
    assert api.claim_model_run(ClaimRequest(worker_name="bench-worker", source_keys=[BENCH]))


def test_quality_flags_are_computed_on_ingest_and_reflaggable(api, db, bench):
    sweep_id = run_benchmark_sweep(api, bench)
    db.expire_all()
    flagged = [
        r
        for r in db.scalars(select(TranscriptionResult))
        if "off_domain_phrase" in r.quality.get("flags", [])
    ]
    assert len(flagged) == 1
    report = api.sweep_report(sweep_id)
    assert {m.logical_name: m.flags.get("empty", 0) for m in report.models}["alpha-model"] == 1

    for result in db.scalars(select(TranscriptionResult)):
        result.quality = {}
    db.commit()
    assert bench["evaluator"].reflag().reflagged == 8
    assert bench["evaluator"].reflag().reflagged == 0


def test_missing_results_score_as_errors(api, bench):
    sweep = api.create_sweep(
        SweepCreate(suite="pair", selection=SweepSelection(source_key=BENCH)),
        allow_unqualified=True,
    )
    report = bench["evaluator"].evaluate(sweep.id)  # no results yet: everything is "missing"
    assert all(m.error == m.segments for m in report.models)
