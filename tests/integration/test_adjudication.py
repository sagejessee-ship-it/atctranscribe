"""Model adjudication (ADR-022): confirmed batches, cost caps, the runner, silver acceptance."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from test_review import HYPOTHESES, save, seeded  # noqa: F401  (fixture)

from aerochorus.adjudication.openrouter import OpenRouterClient
from aerochorus.adjudication_contracts import Pricing
from aerochorus.db.models import AnnotationVersion, Segment
from aerochorus.worker.adjudicator import Adjudicator
from aerochorus.worker.fs import ReadOnlyCorpusReader

# What the fake model hears, keyed by a phrase in the rendered context.
ANSWERS = {
    "Delta one two three": ("delta one two three taxi via alpha", 0.95),
    "american four hold short": ("american four hold short runway three three left", 0.9),
    "southwest four five six contact": (
        "southwest four five six contact tower one one niner point four",
        0.3,
    ),  # noqa: E501
    "lufthansa one two climb": ("lufthansa one two climb", 0.9),
}
COST = 0.011


class FixedPricing:
    def get(self, model: str) -> Pricing:
        return Pricing(model=model, source="test", prompt=2.0, completion=12.0, audio=2.0)


@pytest.fixture
def pricing(app):
    app.state.pricing_book = FixedPricing()  # no network in tests


@pytest.fixture
def audio(db, seeded, tmp_path):  # noqa: F811
    """Real files behind the seeded segments, with their indexed sha256."""
    roots = {"home_atc_archive": tmp_path / "home", "atco2_fixed": tmp_path / "bench"}
    for segment_id in seeded.values():
        seg = db.get(Segment, segment_id)
        key = "atco2_fixed" if "benchmark" in seg.relative_path else "home_atc_archive"
        path = roots[key].joinpath(*seg.relative_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        data = b"ID3" + seg.relative_path.encode() * 20
        path.write_bytes(data)
        seg.sha256 = hashlib.sha256(data).hexdigest()
    db.commit()
    return roots


def fake_openrouter(requests: list[dict], status: int = 200) -> OpenRouterClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "no"}})
        # Only this clip's hypotheses; nearby transmissions are context, not this audio.
        context = payload["messages"][1]["content"][0]["text"].split("Nearby transmissions")[0]
        transcript, confidence = next(
            (answer for phrase, answer in ANSWERS.items() if phrase in context), ("", 0.8)
        )
        answer = {
            "speech_present": bool(transcript),
            "transcript": transcript,
            "confidence": confidence,
            "uncertain_words": [],
            "callsigns": [],
            "closest_hypothesis": "",
            "notes": "",
        }
        return httpx.Response(200, json={
            "id": "gen-1", "model": "google/gemini-pro",
            "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1800, "completion_tokens": 700, "cost": COST},
        })  # fmt: skip

    return OpenRouterClient("sk-test", http=httpx.Client(transport=httpx.MockTransport(handler)))


def runner(api, audio_roots, client) -> Adjudicator:
    readers = {key: ReadOnlyCorpusReader(root) for key, root in audio_roots.items()}
    return Adjudicator(api, readers, client, "test/adjudicator", sleep=lambda s: None,
                       log=lambda m: None)  # fmt: skip


def preview(http, ids, **params):
    body = {"selection": {"segment_ids": ids}, "params": params}
    response = http.post("/api/v1/adjudications/preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def create(http, ids, max_cost=None, acknowledged=None, **params):
    plan = preview(http, ids, **params)
    body = {
        "selection": {"segment_ids": ids},
        "params": params,
        "max_cost_usd": max_cost or plan["suggested_max_cost_usd"],
        "acknowledged_cost_usd": (
            plan["estimated_cost_usd"] if acknowledged is None else acknowledged
        ),
        "confirm": True,
        "created_by": "tester",
    }  # fmt: skip
    return http.post("/api/v1/adjudications", json=body)


def test_preview_prices_and_create_needs_explicit_confirmation(http, db, seeded, pricing):  # noqa: F811
    db.execute(
        update(Segment).where(Segment.id == seeded["s4_problems"]).values(duration_ms=600_000)
    )
    db.commit()
    ids = [seeded[k] for k in ("s1_consensus", "s2_disagree", "s4_problems", "s5_untranscribed")]
    plan = preview(http, ids)
    assert plan["model"] == "~google/gemini-pro-latest" and plan["prompt_version"] == 1
    assert plan["selected"] == 4 and plan["eligible"] == 3  # untranscribed is fine: audio only
    assert plan["skipped"] == {"too_long": 1}
    assert 0 < plan["estimated_cost_usd"] < plan["worst_case_cost_usd"]
    assert plan["suggested_max_cost_usd"] >= plan["worst_case_cost_usd"]
    assert plan["runners"] == [] and plan["pricing"]["source"] == "test"

    body = {"selection": {"segment_ids": ids}, "max_cost_usd": 1.0,
            "acknowledged_cost_usd": plan["estimated_cost_usd"]}  # fmt: skip
    assert http.post("/api/v1/adjudications", json=body).status_code == 422  # confirm missing
    assert create(http, ids, acknowledged=plan["estimated_cost_usd"] + 1).status_code == 409
    assert create(http, ids, max_cost=500).status_code == 422  # above the per-batch limit
    made = create(http, ids)
    assert made.status_code == 201, made.text
    batch = made.json()
    assert batch["status"] == "queued" and batch["item_count"] == 3
    assert batch["counts"] == {"queued": 3} and batch["spent_usd"] == 0
    assert preview(http, ids)["skipped"] == {"too_long": 1, "already_queued": 3}
    # A random sample of a filter is deterministic for a seed.
    sample = {"selection": {"filters": {}, "n": 2, "seed": 7}}
    first = http.post("/api/v1/adjudications/preview", json=sample).json()
    assert first["selected"] == 2
    again = http.post("/api/v1/adjudications/preview", json=sample).json()
    assert again["segment_ids"] == first["segment_ids"]


def test_claims_reserve_worst_case_cost_and_stop_at_the_cap(http, seeded, pricing):  # noqa: F811
    ids = [seeded[k] for k in ("s1_consensus", "s2_disagree", "s3_near")]
    plan = preview(http, ids)
    worst_one = plan["worst_case_cost_usd"] / 3
    batch = create(http, ids, max_cost=round(worst_one * 1.5, 4)).json()
    claim = lambda: http.post(  # noqa: E731
        "/api/v1/adjudication-items/claim", json={"runner": "r1", "limit": 8}
    ).json()
    (first,) = claim()  # room for one worst case only
    assert first["bundle"]["hypotheses"] and first["model"] == "~google/gemini-pro-latest"
    assert claim() == []  # blocked while the first is in flight, not capped
    assert http.get(f"/api/v1/adjudications/{batch['id']}").json()["status"] == "running"
    result = {"runner": "r1", "status": "done", "transcript": "delta one two three",
              "result": {"speech_present": True, "confidence": 0.9},
              "cost_usd": worst_one * 0.9}  # fmt: skip
    http.post(f"/api/v1/adjudication-items/{first['item_id']}/result", json=result)
    assert claim() == []
    detail = http.get(f"/api/v1/adjudications/{batch['id']}").json()
    assert detail["status"] == "capped"
    assert detail["counts"] == {"done": 1, "skipped": 2}
    assert all("cost cap" in i["error"] for i in detail["items"] if i["status"] == "skipped")
    assert detail["spent_usd"] <= detail["max_cost_usd"]
    # The runner shows up in previews and status once it has polled.
    assert [r["runner"] for r in preview(http, ids)["runners"]] == ["r1"]
    status = http.get("/api/v1/adjudication-status").json()
    assert status["default_model"] == "~google/gemini-pro-latest"
    assert [r["runner"] for r in status["runners"]] == ["r1"]


def test_runner_sends_audio_and_context_and_records_results(http, api, seeded, pricing, audio):  # noqa: F811
    ids = [seeded[k] for k in ("s1_consensus", "s2_disagree", "s5_untranscribed")]
    batch = create(http, ids).json()
    requests: list[dict] = []
    summary = runner(api, audio, fake_openrouter(requests)).run()
    assert (summary.done, summary.failed) == (3, 0)
    assert summary.spent_usd == pytest.approx(3 * COST)

    first = requests[0]
    assert first["model"] == "~google/gemini-pro-latest"
    sent = first["messages"][1]["content"][1]["input_audio"]
    s1_path = audio["home_atc_archive"] / "2026/09/08/s1_consensus.mp3"
    assert sent == {"data": base64.b64encode(s1_path.read_bytes()).decode(), "format": "mp3"}
    assert "Delta one two three, taxi via alpha." in first["messages"][1]["content"][0]["text"]

    detail = http.get(f"/api/v1/adjudications/{batch['id']}").json()
    assert detail["status"] == "done" and detail["spent_usd"] == pytest.approx(3 * COST)
    items = {i["segment_id"]: i for i in detail["items"]}
    s1 = items[seeded["s1_consensus"]]
    assert s1["transcript"] == "delta one two three taxi via alpha"
    assert s1["best_hypothesis_similarity"] == 1.0 and s1["representative_similarity"] == 1.0
    assert s1["usage"]["cost_source"] == "openrouter" and s1["cost_usd"] == pytest.approx(COST)
    silent = items[seeded["s5_untranscribed"]]
    assert silent["status"] == "done" and silent["speech_present"] is False
    stored = http.get(f"/api/v1/segments/{seeded['s1_consensus']}/adjudications").json()
    assert stored[0]["id"] == s1["id"]
    # The audio itself is never stored: only its format, size and hash.
    full = http.get(f"/api/v1/adjudications/{batch['id']}").text
    assert sent["data"] not in full


def test_changed_audio_is_refused_and_a_rejected_key_stops_the_runner(
    http,
    api,
    db,
    seeded,  # noqa: F811
    pricing,
    audio,
):
    s1, s2 = seeded["s1_consensus"], seeded["s2_disagree"]
    create(http, [s1])
    (audio["home_atc_archive"] / "2026/09/08/s1_consensus.mp3").write_bytes(b"edited")
    requests: list[dict] = []
    summary = runner(api, audio, fake_openrouter(requests)).run()
    assert summary.failed == 1 and requests == []  # nothing sent
    (item,) = http.get(f"/api/v1/segments/{s1}/adjudications").json()
    assert item["status"] == "failed" and "changed since it was indexed" in item["error"]

    create(http, [s2])
    summary = runner(api, audio, fake_openrouter([], status=401)).run()
    assert summary.stopped and "rejected the API key" in summary.stopped
    assert "sk-test" not in summary.stopped
    (item,) = http.get(f"/api/v1/segments/{s2}/adjudications").json()
    assert item["status"] == "queued" and item["attempts"] == 1  # back in line, nothing spent


def test_accepting_as_silver_never_overrides_human_work(http, api, db, seeded, pricing, audio):  # noqa: F811
    keys = ("s1_consensus", "s2_disagree", "s3_near", "b1_benchmark")
    batch = create(http, [seeded[k] for k in keys]).json()
    runner(api, audio, fake_openrouter([])).run()
    gold = save(http, seeded["s1_consensus"], text="delta one two three taxi via alpha",
                training_label="gold", confirm_gold=True)  # fmt: skip
    assert gold.status_code == 200

    body = {"batch_id": batch["id"], "min_confidence": 0.5, "annotator": "tester"}
    outcome = http.post("/api/v1/adjudication-items/accept", json=body).json()
    assert outcome["segment_ids_applied"] == [seeded["s2_disagree"]]
    assert outcome["skipped"] == {
        "human_gold_unchanged": 1,
        "below_min_confidence": 1,
        "benchmark_source": 1,
    }
    detail = http.get(f"/api/v1/review/segments/{seeded['s2_disagree']}").json()
    current = detail["segment_annotation"]["current"]
    assert current["training_label"] == "silver"
    assert current["text_origin"] == "model_adjudicated"
    assert current["text"] == "american four hold short runway three three left"
    assert current["basis"]["model"] == "~google/gemini-pro-latest"
    assert http.get(f"/api/v1/adjudications/{batch['id']}").json()["accepted"] == 1

    again = http.post("/api/v1/adjudication-items/accept", json=body).json()
    assert again["applied"] == 0 and again["skipped"]["already_accepted"] == 1
    # A human edit afterwards wins; the adjudication cannot be re-applied over it.
    edited = save(http, seeded["s2_disagree"], expected_version=1,
                  text="american four hold short three three left")  # fmt: skip
    assert edited.status_code == 200

    # The database refuses gold that did not come from a human.
    version = db.scalar(
        select(AnnotationVersion).where(AnnotationVersion.text_origin == "model_adjudicated")
    )
    version.training_label, version.review_status = "gold", "reviewed"
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_cancel_stops_queued_work(http, seeded, pricing):  # noqa: F811
    batch = create(http, [seeded["s1_consensus"], seeded["s2_disagree"]]).json()
    cancelled = http.post(f"/api/v1/adjudications/{batch['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["counts"] == {"cancelled": 2}
    claim = http.post("/api/v1/adjudication-items/claim", json={"runner": "r", "limit": 4})
    assert claim.json() == []
    assert set(HYPOTHESES)  # the shared fixture's segments
