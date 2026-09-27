"""Phase 5B: span annotations, partial-usable review, versioned datasets and clip export."""

from __future__ import annotations

import json
import shutil
import wave

import pytest
from corpus_builder import snapshot
from integration_support import SOURCE_KEY, make_scanner

from aerochorus.api.datasets import assign_split
from aerochorus.dataset_contracts import SplitPolicy
from aerochorus.worker.config import SourceMount, WorkerConfig
from aerochorus.worker.dataset_export import ExportError, export_dataset

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


@pytest.fixture
def indexed(api, http, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    rows = http.get(f"/api/v1/sources/{SOURCE_KEY}/segments", params={"limit": 100}).json()
    segments = sorted(
        (r for r in rows if r["capture_start_utc"] and r["duration_ms"]),
        key=lambda r: r["capture_start_utc"],
    )
    return root, segments


def save(http, segment_id, expected_version=0, **fields):
    body = {"review_status": "corrected", "training_label": "none"} | fields
    response = http.post(
        f"/api/v1/review/segments/{segment_id}/annotations",
        json=body | {"expected_version": expected_version},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def labelled(http, indexed):
    root, segs = indexed
    s1, s2, s3, s4 = segs[0], segs[1], segs[2], segs[3]
    save(http, s1["id"], text="delta one two three", training_label="gold", confirm_gold=True)
    gold = {"training_label": "gold", "confirm_gold": True}
    save(http, s1["id"], scope="span", start_ms=100, end_ms=600, text="delta one", **gold)
    save(http, s2["id"], text="american four hold short", training_label="silver")
    save(http, s3["id"], text=None, review_status="reviewed", training_label="rejected")
    save(http, s3["id"], scope="span", start_ms=200, end_ms=900, text="united nine", **gold)
    save(http, s4["id"], text="maybe", training_label="candidate")
    return root, segs


def create(http, **body):
    response = http.post("/api/v1/datasets", json={"name": "bwi-train"} | body)
    assert response.status_code == 201, response.text
    return response.json()


def items(http, dataset_id):
    return http.get(f"/api/v1/datasets/{dataset_id}/items").json()


def test_partial_usable_and_span_search(http, labelled):
    _, segs = labelled
    query = lambda **f: {  # noqa: E731
        r["segment_id"]
        for r in http.post("/api/v1/review/query", json={"filters": f | {"min_models": 0}}).json()[
            "rows"
        ]
    }
    assert query(partial_usable=True) == {segs[2]["id"]}
    assert query(has_spans=True) == {segs[0]["id"], segs[2]["id"]}
    assert query(span_labels=["gold"]) == {segs[0]["id"], segs[2]["id"]}
    assert query(q="united nine", scope="human") == {segs[2]["id"]}  # span text is searchable
    views = {v["key"] for v in http.get("/api/v1/review/views").json()}
    assert "partial-usable" in views


def test_freeze_is_deterministic_versioned_and_immutable(http, labelled):
    _, segs = labelled
    v1 = create(http, labels=["gold", "silver"])
    assert v1["version"] == 1 and v1["status"] == "frozen"
    frozen = items(http, v1["id"])
    # whole s1 (gold), s2 (silver) and the s3 span; s1's nested span and s4 (candidate) are out
    assert [(i["segment_id"], i["scope"]) for i in frozen] == [
        (segs[0]["id"], "segment"),
        (segs[1]["id"], "segment"),
        (segs[2]["id"], "span"),
    ]
    span = frozen[2]
    assert (span["start_ms"], span["end_ms"], span["duration_ms"]) == (200, 900, 700)
    assert all(i["source_sha256"] == s["sha256"] for i, s in zip(frozen, segs, strict=False))

    v2 = create(http, labels=["gold", "silver"])
    assert v2["version"] == 2 and v2["manifest_sha256"] == v1["manifest_sha256"]

    thread = http.get(f"/api/v1/review/segments/{segs[1]['id']}").json()["segment_annotation"]
    save(
        http,
        segs[1]["id"],
        expected_version=thread["current"]["version"],
        text="american four hold short runway three three",
        training_label="silver",
    )
    v3 = create(http, labels=["gold", "silver"])
    assert v3["manifest_sha256"] != v1["manifest_sha256"]
    assert items(http, v1["id"])[1]["text"] == "american four hold short"  # v1 never changes

    nested = create(http, name="nested", labels=["gold"], include_spans_of_included_segments=True)
    assert nested["counts"]["by_scope"] == {"segment": 1, "span": 2}
    candidates = create(http, name="cand", labels=["candidate"])
    assert candidates["item_count"] == 1
    assert (
        http.post("/api/v1/datasets", json={"name": "bad", "labels": ["rejected"]}).status_code
        == 422
    )
    nothing = {"channels": ["NOPE"], "min_models": 0}
    empty = http.post(
        "/api/v1/datasets",
        json={"name": "none", "labels": ["gold"], "scopes": ["segment"], "filters": nothing},
    )
    assert empty.status_code == 422


def test_splits_keep_groups_together_and_are_deterministic():
    policy = SplitPolicy(seed=7)
    groups = [f"1:2026-09-{d:02d}:TWR" for d in range(1, 29)]
    first = [assign_split(g, policy) for g in groups]
    assert first == [assign_split(g, policy) for g in groups]
    assert set(first) <= {"train", "validation", "test"} and "train" in first
    assert first != [assign_split(g, SplitPolicy(seed=8)) for g in groups]
    everything_train = SplitPolicy(train=1, validation=0, test=0)
    assert {assign_split(g, everything_train) for g in groups} == {"train"}


@needs_ffmpeg
def test_export_materializes_clips_and_never_touches_sources(api, http, labelled, tmp_path):
    root, segs = labelled
    before = snapshot(root)
    dataset = create(http, labels=["gold", "silver"])
    config = WorkerConfig(worker_name="t", sources={SOURCE_KEY: SourceMount(root=root)})

    result = export_dataset(api, config, dataset["id"], tmp_path / "exports", ffmpeg=FFMPEG)
    assert snapshot(root) == before  # parent audio untouched
    assert result.dataset.status == "exported" and result.clips == 3
    exported = items(http, dataset["id"])
    for item in exported:
        clip = result.directory / item["clip_path"]
        assert clip.is_file() and item["clip_sha256"]
        with wave.open(str(clip)) as w:
            assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
            duration_ms = w.getnframes() / 16
        expected = item["duration_ms"]
        # Spans are cut sample-exactly; whole-segment probes include MP3 encoder padding
        # that ffmpeg's gapless decoding removes.
        tolerance = 20 if item["scope"] == "span" else 250
        assert abs(duration_ms - expected) <= tolerance, (item["scope"], duration_ms, expected)
    span_clip = next(i for i in exported if i["scope"] == "span")
    assert span_clip["clip_path"].endswith(f"seg{segs[2]['id']}_200-900.wav")

    lines = (result.directory / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    manifest = [json.loads(line) for line in lines]
    assert [m["text"] for m in manifest] == [i["text"] for i in exported]
    assert manifest[2]["source_sha256"] == segs[2]["sha256"]
    meta = json.loads((result.directory / "dataset.json").read_text(encoding="utf-8"))
    assert meta["manifest_sha256"] == dataset["manifest_sha256"]

    again = export_dataset(api, config, dataset["id"], tmp_path / "again", ffmpeg=FFMPEG)
    assert again.dataset.export["clips_sha256"] == result.dataset.export["clips_sha256"]
    summary = http.get("/api/v1/training/summary").json()
    by = {(r["label"], r["scope"]): r["items"] for r in summary["by_label"]}
    assert by[("gold", "segment")] == 1 and by[("gold", "span")] == 2
    assert by[("silver", "segment")] == 1 and by[("rejected", "segment")] == 1


@needs_ffmpeg
def test_export_refuses_changed_sources_and_outputs_inside_sources(api, http, labelled, tmp_path):
    root, segs = labelled
    dataset = create(http, labels=["silver"])
    config = WorkerConfig(worker_name="t", sources={SOURCE_KEY: SourceMount(root=root)})
    with pytest.raises(ExportError, match="inside corpus source"):
        export_dataset(api, config, dataset["id"], root / "exports", ffmpeg=FFMPEG)
    # A test-only change to the fixture copy after freezing.
    (root / segs[1]["relative_path"]).write_bytes(b"different audio")
    with pytest.raises(ExportError, match="changed since the dataset was frozen"):
        export_dataset(api, config, dataset["id"], tmp_path / "out", ffmpeg=FFMPEG)
    assert http.get(f"/api/v1/datasets/{dataset['id']}").json()["status"] == "frozen"
