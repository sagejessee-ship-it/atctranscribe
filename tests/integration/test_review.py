"""Phase 5A: review workbench service (filters, agreement views, annotations, curation)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from aerochorus.api.agreement import refresh_agreement
from aerochorus.db.models import (
    AnnotationThread,
    AnnotationVersion,
    ArchitectureFamily,
    CorpusSource,
    Model,
    ModelSuite,
    Segment,
    SweepRun,
    SweepRunModel,
    TranscriptionResult,
)

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
GND = 121_900_000

MODELS = {
    "canary-a": "canary",
    "canary-b": "canary",
    "parakeet-a": "parakeet",
    "whisper-a": "whisper",
}

# segment name -> {model: (status, text)}
HYPOTHESES = {
    "s1_consensus": {
        "canary-a": ("success", "Delta one two three, taxi via alpha."),
        "parakeet-a": ("success", "delta one two three taxi via alpha"),
        "whisper-a": ("success", "Delta one two three taxi via alpha"),
    },
    "s2_disagree": {
        "canary-a": ("success", "american four hold short runway three three left"),
        "canary-b": ("success", "american four hold short runway three three left"),
        "parakeet-a": ("success", "united nine cleared to land"),
        "whisper-a": ("success", "thank you good day"),
    },
    "s3_near": {
        "canary-a": ("success", "southwest four five six contact tower one one niner point four"),
        "parakeet-a": ("success", "southwest four five six contact tower one one nine point four"),
    },
    "s4_problems": {
        "canary-a": ("error", None),
        "parakeet-a": ("abstained", ""),
        "whisper-a": ("success", "roger"),
    },
    "s5_untranscribed": {},
}


@pytest.fixture
def seeded(db):
    """Two sources (corpus + benchmark), four models in three families, five segments."""
    corpus = CorpusSource(
        logical_key="home_atc_archive",
        name="Home",
        adapter_type="filesystem",
        adapter_config={"filename_timezone": "America/New_York"},
    )
    bench = CorpusSource(
        logical_key="atco2_fixed", name="ATCO2", adapter_type="filesystem", role="benchmark"
    )
    db.add_all([corpus, bench])
    db.add_all(ArchitectureFamily(key=f, display_name=f) for f in sorted(set(MODELS.values())))
    db.flush()
    models = {
        name: Model(
            logical_name=name, architecture_family=family, crisp_backend="x", model_filename=name
        )
        for name, family in MODELS.items()
    }
    db.add_all(models.values())
    suite = ModelSuite(name="all")
    db.add(suite)
    db.flush()
    run = SweepRun(
        suite_id=suite.id,
        status="completed",
        selection_definition={},
        effective_config={},
        config_sha256="0" * 64,
        segments_total=0,
    )
    db.add(run)
    db.flush()
    srms = {}
    for order, model in enumerate(models.values()):
        srm = SweepRunModel(
            run_id=run.id,
            model_id=model.id,
            execution_order=order,
            status="completed",
            segments_total=0,
        )
        db.add(srm)
        srms[model.logical_name] = srm
    db.flush()

    def segment(source, name, index, channel="GND", station="BWI"):
        seg = Segment(
            source_id=source.id,
            relative_path=f"2026/09/08/{name}.mp3",
            relative_dir="2026/09/08",
            capture_start_utc=T0 + timedelta(seconds=20 * index),
            temporal_status="resolved",
            duration_ms=3000,
            file_size=1000,
            file_mtime=T0,
            file_mtime_ns=0,
            frequency_hz=GND,
            channel=channel,
            station=station,
            first_seen_at=T0,
            last_seen_at=T0,
        )
        db.add(seg)
        db.flush()
        return seg

    ids = {}
    for index, (name, hypotheses) in enumerate(HYPOTHESES.items()):
        seg = segment(corpus, name, index)
        ids[name] = seg.id
        for model, (status, words) in hypotheses.items():
            db.add(
                TranscriptionResult(
                    id=uuid.uuid4(),
                    sweep_run_model_id=srms[model].id,
                    segment_id=seg.id,
                    status=status,
                    attempt=1,
                    text=words,
                    error_type="timeout" if status == "error" else None,
                )
            )
    b1 = segment(bench, "b1_benchmark", 0, station="ATCO2")
    ids["b1_benchmark"] = b1.id
    for model in ("canary-a", "parakeet-a"):
        db.add(
            TranscriptionResult(
                id=uuid.uuid4(),
                sweep_run_model_id=srms[model].id,
                segment_id=b1.id,
                status="success",
                attempt=1,
                text="lufthansa one two climb",
            )
        )
    db.flush()
    refresh_agreement(db, list(ids.values()))
    db.commit()
    return ids


def query(http, **filters):
    body = {"filters": filters}
    for key in ("sort", "descending", "offset", "limit"):
        if key in filters:
            body[key] = filters.pop(key)
    response = http.post("/api/v1/review/query", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def names(page, ids):
    by_id = {v: k for k, v in ids.items()}
    return {by_id[row["segment_id"]] for row in page["rows"]}


def preset(http, key):
    views = {v["key"]: v for v in http.get("/api/v1/review/views").json()}
    return views[key]["filters"]


def test_defaults_hide_benchmark_and_untranscribed(http, seeded):
    page = query(http)
    assert names(page, seeded) == {"s1_consensus", "s2_disagree", "s3_near", "s4_problems"}
    assert page["total"] == 4
    assert "s5_untranscribed" in names(query(http, min_models=0), seeded)
    assert "b1_benchmark" in names(query(http, include_benchmark=True), seeded)


def test_agreement_views(http, seeded):
    assert names(query(http, **preset(http, "exact-2-families")), seeded) == {"s1_consensus"}
    assert names(query(http, **preset(http, "near-3-families")), seeded) == {"s1_consensus"}
    assert names(query(http, min_near_families=2), seeded) == {"s1_consensus", "s3_near"}
    # Same-family models count as providers, never as independent families.
    assert names(query(http, min_exact_providers=2), seeded) == {"s1_consensus", "s2_disagree"}
    assert names(query(http, **preset(http, "disagreement")), seeded) == {"s2_disagree"}
    assert names(query(http, **preset(http, "errors-abstentions")), seeded) == {"s4_problems"}
    assert names(query(http, families=["whisper"]), seeded) == {
        "s1_consensus",
        "s2_disagree",
        "s4_problems",
    }


def test_sort_and_pagination(http, seeded):
    page = query(http, sort="exact_families", descending=True, limit=2)
    assert page["total"] == 4 and len(page["rows"]) == 2
    assert page["rows"][0]["segment_id"] == seeded["s1_consensus"]
    rest = query(http, sort="exact_families", descending=True, offset=2, limit=2)
    seen = {r["segment_id"] for r in page["rows"]} | {r["segment_id"] for r in rest["rows"]}
    assert len(seen) == 4


def test_search_scopes(http, seeded):
    assert names(query(http, q="niner"), seeded) == {"s3_near"}
    assert names(query(http, q="NINER", scope="hypotheses"), seeded) == {"s3_near"}
    assert names(query(http, q="niner", scope="model", scope_model="parakeet-a"), seeded) == set()
    assert names(query(http, q="taxi via", scope="consensus"), seeded) == {"s1_consensus"}
    assert names(query(http, q="100%_", scope="any"), seeded) == set()  # wildcards escaped
    assert names(query(http, q="wrong call sign", scope="human"), seeded) == set()
    save(http, seeded["s2_disagree"], text="american four hold short wrong call sign")
    assert names(query(http, q="wrong call sign", scope="human"), seeded) == {"s2_disagree"}


def save(http, segment_id, expected_version=0, **fields):
    body = {
        "review_status": "corrected" if fields.get("text") else "reviewed",
        "training_label": "none",
        "expected_version": expected_version,
    } | fields
    return http.post(f"/api/v1/review/segments/{segment_id}/annotations", json=body)


def test_segment_detail(http, seeded):
    detail = http.get(f"/api/v1/review/segments/{seeded['s2_disagree']}").json()
    assert detail["source_role"] == "corpus"
    assert detail["capture_local"].startswith("2026-09-08T08:00:20")  # EDT from source config
    hyps = {h["model"]: h for h in detail["hypotheses"]}
    assert hyps["canary-a"]["exact_group"] == hyps["canary-b"]["exact_group"] == 0
    assert hyps["parakeet-a"]["exact_group"] != 0
    assert detail["agreement"]["best_exact_provider_count"] == 2
    assert detail["agreement"]["best_exact_family_count"] == 1
    relations = {n["relation"]: n["segment_id"] for n in detail["neighbors"]}
    assert relations["previous"] == seeded["s1_consensus"]
    assert relations["next"] == seeded["s3_near"]
    assert http.get("/api/v1/review/segments/999999").status_code == 404


def test_annotation_versions_are_append_only_with_optimistic_concurrency(http, db, seeded):
    sid = seeded["s1_consensus"]
    first = save(http, sid, text="delta one two three taxi via alpha")
    assert first.status_code == 200, first.text
    assert first.json()["current"]["version"] == 1

    stale = save(http, sid, expected_version=0, text="something else")
    assert stale.status_code == 409

    second = save(http, sid, expected_version=1, text="delta one two three taxi via alpha bravo")
    history = second.json()["history"]
    assert [v["version"] for v in history] == [2, 1]
    assert history[1]["text"] == "delta one two three taxi via alpha"

    # Model hypotheses never change.
    texts = db.scalars(
        select(TranscriptionResult.text).where(TranscriptionResult.segment_id == sid)
    )
    assert "delta one two three taxi via alpha bravo" not in set(texts)


def test_gold_requires_deliberate_human_confirmation(http, db, seeded):
    sid = seeded["s1_consensus"]
    unconfirmed = save(http, sid, text="delta one two three", training_label="gold")
    assert unconfirmed.status_code == 422
    textless = save(http, sid, training_label="gold", confirm_gold=True)
    assert textless.status_code == 422
    gold = save(http, sid, text="delta one two three", training_label="gold", confirm_gold=True)
    assert gold.status_code == 200 and gold.json()["current"]["text_origin"] == "human"

    # The database refuses model-origin gold even if a code path forgot.
    thread = db.scalar(select(AnnotationThread).where(AnnotationThread.segment_id == sid))
    db.add(
        AnnotationVersion(
            thread_id=thread.id,
            version=99,
            text="x",
            text_origin="model_consensus",
            review_status="reviewed",
            training_label="gold",
            action="edit",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_benchmark_segments_never_become_training_data(http, seeded):
    response = save(http, seeded["b1_benchmark"], text="lufthansa", training_label="candidate")
    assert response.status_code == 422
    assert "benchmark" in response.json()["detail"]
    # Reviewing (without a training label) is fine.
    assert save(http, seeded["b1_benchmark"], text="lufthansa one two").status_code == 200


def test_batch_nomination(http, seeded):
    gold_id = seeded["s3_near"]
    assert (
        save(
            http, gold_id, text="southwest four five six", training_label="gold", confirm_gold=True
        ).status_code
        == 200
    )

    outcome = http.post(
        "/api/v1/review/batch",
        json={"action": "silver", "segment_ids": list(seeded.values()), "annotator": "t"},
    ).json()
    assert outcome["skipped"] == {
        "benchmark_source": 1,
        "human_gold_unchanged": 1,
        "no_consensus_text": 1,
        # s2 (medoid of disagreeing models) and s4 (one speaking model) are not agreement.
        "insufficient_agreement": 2,
    }
    assert outcome["segment_ids_applied"] == [seeded["s1_consensus"]]
    detail = http.get(f"/api/v1/review/segments/{seeded['s1_consensus']}").json()
    current = detail["segment_annotation"]["current"]
    assert current["training_label"] == "silver"
    assert current["text_origin"] == "model_consensus"
    assert current["review_status"] == "unreviewed"
    assert current["basis"]["best_exact_family_count"] == 3

    candidates = http.post(
        "/api/v1/review/batch",
        json={"action": "candidate", "segment_ids": [seeded["s2_disagree"], seeded["s4_problems"]]},
    ).json()
    assert candidates["applied"] == 2

    # Gold is not a batch action.
    response = http.post(
        "/api/v1/review/batch", json={"action": "gold", "segment_ids": [seeded["s1_consensus"]]}
    )
    assert response.status_code == 422
    assert names(query(http, **preset(http, "silver-candidates")), seeded) == {
        "s1_consensus",
        "s2_disagree",
        "s4_problems",
    }

    cleared = http.post(
        "/api/v1/review/batch", json={"action": "clear", "segment_ids": [seeded["s1_consensus"]]}
    ).json()
    assert cleared["applied"] == 1
    assert names(query(http, training_label=["gold"]), seeded) == {"s3_near"}
    assert names(query(http, training_label=["silver"]), seeded) == set()


def test_deterministic_samples(http, seeded):
    body = {"filters": {"min_models": 0}, "n": 3, "seed": 42}
    a = http.post("/api/v1/review/samples", json=body).json()
    b = http.post("/api/v1/review/samples", json=body).json()
    assert a["segment_ids"] == b["segment_ids"] and len(a["segment_ids"]) == 3
    assert a["filter_sha256"] == b["filter_sha256"] and a["total_matching"] == 5
    other = http.post("/api/v1/review/samples", json=body | {"seed": 7, "n": 5}).json()
    assert set(other["segment_ids"]) >= set(a["segment_ids"])  # n = all matching
    assert names(query(http, sample_id=a["id"], min_models=0), seeded) == {
        name for name, i in seeded.items() if i in a["segment_ids"]
    }


def test_span_annotations(http, seeded):
    sid = seeded["s2_disagree"]
    span = {"scope": "span", "start_ms": 500, "end_ms": 1500, "text": "american four"}
    ok = save(http, sid, **span, training_label="candidate")
    assert ok.status_code == 200 and ok.json()["current"]["start_ms"] == 500
    assert save(http, sid, scope="span", start_ms=100, end_ms=9000, text="x").status_code == 422
    assert save(http, sid, scope="span", start_ms=900, end_ms=100, text="x").status_code == 422
    assert names(query(http, span_labels=["candidate"]), seeded) == {"s2_disagree"}
    row = next(r for r in query(http)["rows"] if r["segment_id"] == sid)
    assert row["span_count"] >= 1


def test_source_role_switch_guards_training_labels(http, seeded):
    save(http, seeded["s1_consensus"], text="delta", training_label="candidate")
    blocked = http.patch("/api/v1/sources/home_atc_archive/role", json={"role": "benchmark"})
    assert blocked.status_code == 409
    fine = http.patch("/api/v1/sources/atco2_fixed/role", json={"role": "benchmark"})
    assert fine.status_code == 200 and fine.json()["role"] == "benchmark"


def test_agreement_refresh_backfills(http, db, seeded):
    db.execute(text("DELETE FROM segment_agreement"))
    db.commit()
    assert query(http)["total"] == 0
    assert http.post("/api/v1/agreement/refresh").json()["refreshed"] == 5
    assert query(http)["total"] == 4
    assert http.post("/api/v1/agreement/refresh").json()["refreshed"] == 0


def test_airport_profile_resolves_stations_and_channels(http, seeded):
    profile = {
        "icao": "KBWI",
        "faa_id": "BWI",
        "iata": "BWI",
        "name": "Baltimore/Washington Intl Thurgood Marshall",
        "timezone": "America/New_York",
        "runways": [
            {
                "pair": "15R/33L",
                "end_ident": "33L",
                "length_ft": 10502,
                "width_ft": 200,
                "true_alignment": 325.0,
                "spoken": ["runway three three left"],
            },
        ],
        "frequencies": [
            {
                "service": "GND",
                "frequency_hz": GND,
                "facility": "BWI",
                "call": "Baltimore Ground",
                "sectorization": "GND/P",
                "spoken": ["one two one point niner"],
            },
        ],
        "aliases": [{"alias": "BWI", "kind": "station"}, {"alias": "KBWI", "kind": "icao"}],
        "provenance": {"source": "test"},
    }
    assert http.put("/api/v1/airports/KBWI", json=profile).status_code == 200
    page = query(http, airport="KBWI")
    assert page["total"] == 4 and {r["airport"] for r in page["rows"]} == {"KBWI"}
    detail = http.get(f"/api/v1/review/segments/{seeded['s1_consensus']}").json()
    assert detail["channel_service"] == "Baltimore Ground (GND/P)"
    assert detail["airport_profile"]["runways"][0]["end_ident"] == "33L"
    facets = http.get("/api/v1/review/facets").json()
    assert facets["airports"] == ["KBWI"] and "GND" in facets["channels"]


def test_voting_decision_recomputes_agreement_with_evidence(http, db, seeded):
    s1 = seeded["s1_consensus"]
    agreement = lambda: http.get(f"/api/v1/review/segments/{s1}").json()["agreement"]  # noqa: E731
    assert agreement()["best_exact_family_count"] == 3

    evidence = {e["logical_name"]: e for e in http.get("/api/v1/models/evidence").json()}
    whisper = evidence["whisper-a"]
    assert whisper["results"] == 3 and whisper["spoken"] == 3
    # s1 is the only segment where 2+ other families agree exactly: whisper matches it.
    assert (whisper["compared"], whisper["exact_rate"], whisper["near_rate"]) == (1, 1.0, 1.0)

    out = http.post(
        "/api/v1/models/whisper-a/ensemble",
        json={"eligible": False, "reason": "says 'thank you' on noise", "by": "tester"},
    ).json()
    assert out["model"]["ensemble_decision"]["reason"] == "says 'thank you' on noise"
    assert out["segments_refreshed"] == 3
    assert agreement()["best_exact_family_count"] == 2  # whisper no longer votes
    hyps = http.get(f"/api/v1/review/segments/{s1}").json()["hypotheses"]
    assert {h["model"]: h["ensemble_eligible"] for h in hyps}["whisper-a"] is False

    back = http.post(
        "/api/v1/models/whisper-a/ensemble", json={"eligible": True, "reason": "reviewed"}
    )
    assert back.status_code == 200 and agreement()["best_exact_family_count"] == 3
