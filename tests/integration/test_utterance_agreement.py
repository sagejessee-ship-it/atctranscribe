"""Agreement v2: utterance agreement within segments, short-text filter, word backfill."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from aerochorus.api.agreement import refresh_agreement
from aerochorus.db.models import (
    ArchitectureFamily,
    CorpusSource,
    Model,
    ModelSuite,
    Segment,
    SweepRun,
    SweepRunModel,
    TranscriptionResult,
)

T0 = datetime(2026, 9, 8, 12, tzinfo=UTC)
LEAD = "Southwest 456, turn left heading 270, contact departure"


def timed(text: str, step: int = 300):
    return [[100 + i * step, 100 + i * step + 250, w] for i, w in enumerate(text.split())]


# segment -> {model: (text, words or None)}
CASES = {
    "partial": {  # clean instruction agreed, trailing readback garbled
        "parakeet-a": (f"{LEAD} one two five point three", timed(f"{LEAD} one two five point three")),  # noqa: E501
        "whisper-a": (f"{LEAD}. Wanna do my point three?", None),
        "qwen-a": ("southwest 456 turn left heading 270 contact departure uh", None),
    },
    "whole": {
        "parakeet-a": ("delta one two three taxi via alpha", timed("delta one two three taxi via alpha")),  # noqa: E501
        "whisper-a": ("Delta one two three, taxi via alpha.", None),
    },
    "short": {"parakeet-a": ("Thank you.", None), "whisper-a": ("thank you", None)},
}  # fmt: skip
FAMILIES = {"parakeet-a": "parakeet", "whisper-a": "whisper", "qwen-a": "qwen3-asr"}


@pytest.fixture
def seeded(db):
    source = CorpusSource(logical_key="home_atc_archive", name="Home", adapter_type="filesystem")
    db.add(source)
    db.add_all(ArchitectureFamily(key=f, display_name=f) for f in sorted(set(FAMILIES.values())))
    db.flush()
    models = {n: Model(logical_name=n, architecture_family=f, crisp_backend="x", model_filename=n)
              for n, f in FAMILIES.items()}  # fmt: skip
    db.add_all(models.values())
    suite = ModelSuite(name="all")
    db.add(suite)
    db.flush()
    run = SweepRun(suite_id=suite.id, status="completed", selection_definition={},
                   effective_config={}, config_sha256="0" * 64, segments_total=0)  # fmt: skip
    db.add(run)
    db.flush()
    srms = {}
    for i, m in enumerate(models.values()):
        srms[m.logical_name] = SweepRunModel(run_id=run.id, model_id=m.id, execution_order=i,
                                             status="completed", segments_total=0)  # fmt: skip
    db.add_all(srms.values())
    db.flush()
    ids = {}
    for i, (name, hyps) in enumerate(CASES.items()):
        seg = Segment(source_id=source.id, relative_path=f"d/{name}.mp3", relative_dir="d",
                      capture_start_utc=T0 + timedelta(seconds=30 * i), temporal_status="resolved",
                      duration_ms=6000, file_size=1, file_mtime=T0, file_mtime_ns=0, channel="TWR",
                      station="BWI", first_seen_at=T0, last_seen_at=T0)  # fmt: skip
        db.add(seg)
        db.flush()
        ids[name] = seg.id
        for model, (text, words) in hyps.items():
            db.add(TranscriptionResult(
                id=uuid.uuid4(), sweep_run_model_id=srms[model].id, segment_id=seg.id,
                status="success", attempt=1, text=text, words=words,
                has_word_timestamps=words is not None, artifact_uri=f"artifact://t/{model}/{name}",
            ))  # fmt: skip
    db.flush()
    refresh_agreement(db, list(ids.values()))
    db.commit()
    return ids


def query(http, **filters):
    rows = http.post("/api/v1/review/query", json={"filters": filters}).json()["rows"]
    return {r["segment_id"]: r for r in rows}


def test_partial_agreement_is_found_and_filterable(http, seeded):
    partial = query(http, partial_agreement=True)
    assert set(partial) == {seeded["partial"]}  # "whole" already agrees as a whole
    row = partial[seeded["partial"]]
    assert row["best_utterance_family_count"] == 3 and row["best_utterance_tokens"] == 8
    assert set(query(http, min_utterance_families=3)) == {seeded["partial"]}

    detail = http.get(f"/api/v1/review/segments/{seeded['partial']}").json()
    (u, *_) = detail["agreement"]["utterances"]
    assert u["families"] == ["parakeet", "qwen3-asr", "whisper"]
    assert u["timed_by"] == ["parakeet-a"] and u["bounds_estimated"] is False
    assert (u["start_ms"], u["end_ms"]) == (100, 100 + 7 * 300 + 250)
    hyps = {h["model"]: h for h in detail["hypotheses"]}
    assert hyps["whisper-a"]["highlights"][0] | {} == {
        "start": 0,
        "end": 8,
        "utterance": 0,
        "family_count": 3,
    }
    assert hyps["parakeet-a"]["has_word_times"] is True
    assert row["representative_highlights"], "the grid preview is highlighted too"


def test_short_agreed_segments_can_be_hidden(http, seeded):
    two_families = query(http, min_exact_families=2)
    assert {seeded["whole"], seeded["short"]} <= set(two_families)
    substantive = query(http, min_exact_families=2, min_words=3)
    assert seeded["short"] not in substantive and seeded["whole"] in substantive
    views = {v["key"]: v["filters"] for v in http.get("/api/v1/review/views").json()}
    assert views["exact-2-families-3-words"]["min_words"] == 3
    # "thank you" never becomes an agreed utterance either.
    detail = http.get(f"/api/v1/review/segments/{seeded['short']}").json()
    assert detail["agreement"]["utterances"] == []


def test_word_backfill_attaches_timings_and_re_agrees(http, db, seeded):
    # Pretend the whisper result reported timings that were never stored.
    result = db.scalar(
        select(TranscriptionResult).where(
            TranscriptionResult.segment_id == seeded["partial"],
            TranscriptionResult.text.like("%Wanna%"),
        )
    )
    result.has_word_timestamps = True
    db.commit()
    candidates = http.get("/api/v1/results/word-backfill").json()
    assert [c["id"] for c in candidates] == [str(result.id)]
    words = [[3000, 3300, w] for w in f"{LEAD}. Wanna do my point three?".split()]
    ack = http.post("/api/v1/results/words", json=[{"id": str(result.id), "words": words}]).json()
    assert ack == {"updated": 1, "segments_refreshed": 1}
    assert http.get("/api/v1/results/word-backfill").json() == []
    detail = http.get(f"/api/v1/review/segments/{seeded['partial']}").json()
    assert detail["agreement"]["utterances"][0]["timed_by"] == ["parakeet-a", "whisper-a"]
