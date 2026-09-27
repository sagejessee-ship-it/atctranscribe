import hashlib
import uuid
from pathlib import Path

import httpx
import pytest

from aerochorus.cli_transcription import load_catalog
from aerochorus.sweep_contracts import ModelRead
from aerochorus.worker.artifacts import ArtifactStore
from aerochorus.worker.crispasr import Runtime, _parse_build_info, interpret
from aerochorus.worker.model_store import ModelArtifactError, ModelStore

CATALOG = Path(__file__).resolve().parents[2] / "config" / "models.toml"

# Shape returned by CrispASR 0.8.37 (parakeet) for a real BWI segment.
PARAKEET = {
    "task": "transcribe",
    "language": "en",
    "duration": 12.606,
    "text": "Taxi via Papa, Echo, Tango.",
    "segments": [
        {
            "id": 0,
            "start": 2.96,
            "end": 12.56,
            "text": "Taxi via Papa, Echo, Tango.",
            "avg_logprob": -0.23,
            "no_speech_prob": 0.0,
            "words": [
                {"word": "Taxi", "start": 2.96, "end": 3.12},
                {"word": "via", "start": 3.2, "end": 3.4},
                {"word": "Papa,", "start": 3.5, "end": 3.9},
                {"word": "Echo,", "start": 4.0, "end": 4.3},
                {"word": "Tango.", "start": 4.4, "end": 4.9},
            ],
        }
    ],
}


def test_interpret_parakeet_response():
    parsed = interpret(PARAKEET)
    assert parsed.text == "Taxi via Papa, Echo, Tango."
    assert parsed.language == "en"
    assert parsed.word_count == 5
    assert parsed.has_word_timestamps
    # avg_logprob is not a token probability; nothing is invented.
    assert not parsed.has_token_confidence and parsed.mean_token_confidence is None


def test_interpret_token_probabilities_and_empty_output():
    body = {
        "text": " hi there ",
        "segments": [{"tokens": [{"text": "hi", "p": 0.9}, {"text": "there", "p": 0.7}]}],
    }
    parsed = interpret(body)
    assert parsed.text == "hi there"
    assert parsed.has_token_confidence and parsed.mean_token_confidence == pytest.approx(0.8)
    assert not parsed.has_word_timestamps

    empty = interpret({"text": "", "segments": [{"text": "", "avg_logprob": 0}]})
    assert empty.text == "" and empty.word_count == 0


def test_build_info_and_fingerprint():
    info = _parse_build_info(
        "=== build info ===\n  version       : 0.8.37\n  git sha       : unknown\n"
        "  ggml backends : cpu,cuda\n  cuda runtime ABI: 13 (matching)\n"
    )
    assert info["version"] == "0.8.37"
    assert info["ggml_backends"] == "cpu,cuda"
    assert info["cuda_runtime_abi"] == "13 (matching)"

    a = Runtime("docker", info, "sha256:image-a")
    same = Runtime("docker", dict(info), "sha256:image-a")
    other_image = Runtime("docker", info, "sha256:image-b")
    assert a.fingerprint("m" * 64, "parakeet") == same.fingerprint("m" * 64, "parakeet")
    assert a.fingerprint("m" * 64, "parakeet") != other_image.fingerprint("m" * 64, "parakeet")
    assert a.fingerprint("m" * 64, "parakeet") != a.fingerprint("n" * 64, "parakeet")


def test_artifact_store_round_trip(tmp_path):
    store = ArtifactStore(tmp_path, "lab")
    result_id = uuid.uuid4()
    stored = store.write(result_id, {"response": PARAKEET})
    assert stored.uri == f"artifact://lab/raw/{str(result_id)[:2]}/{result_id}.json.zst"
    path = tmp_path / "raw" / str(result_id)[:2] / f"{result_id}.json.zst"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == stored.sha256
    assert stored.size_bytes == path.stat().st_size
    assert store.read(stored.uri)["response"] == PARAKEET
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path, "other").read(stored.uri)


def _model(blob: bytes) -> ModelRead:
    return ModelRead(
        logical_name="tiny",
        architecture_family="f",
        crisp_backend="b",
        model_filename="tiny.gguf",
        model_sha256=hashlib.sha256(blob).hexdigest(),
        artifact_uri="https://example.test/tiny.gguf",
        artifact_size_bytes=len(blob),
        upstream_model=None,
        upstream_revision=None,
        quantization=None,
        language=None,
        request_params={},
        capabilities={},
        pedigree={},
        enabled=True,
        sweep_eligible=False,
        experimental=True,
    )


def _server(blob: bytes, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range"))
        if request.headers.get("range"):
            start = int(request.headers["range"].split("=")[1].rstrip("-"))
            return httpx.Response(206, content=blob[start:])
        return httpx.Response(200, content=blob)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_model_pull_verifies_and_resumes(tmp_path):
    blob = b"gguf" * 5000
    model = _model(blob)
    store = ModelStore(tmp_path)
    (tmp_path / "tiny.gguf.part").write_bytes(blob[:7000])  # an interrupted download
    seen: list = []
    path = store.pull(model, http=_server(blob, seen))
    assert seen == ["bytes=7000-"]
    assert path.read_bytes() == blob
    assert store.verify(model).ok
    assert not (tmp_path / "tiny.gguf.part").exists()


def test_model_pull_discards_a_corrupt_download(tmp_path):
    blob = b"gguf" * 100
    model = _model(blob)
    corrupt = b"GGUF" * 100
    with pytest.raises(ModelArtifactError, match="does not match"):
        ModelStore(tmp_path).pull(model, http=_server(corrupt, []))
    assert list(tmp_path.iterdir()) == []

    (tmp_path / "tiny.gguf").write_bytes(corrupt)
    with pytest.raises(ModelArtifactError, match="does not match"):
        ModelStore(tmp_path).require(model)


def test_catalog_is_pinned_and_consistent():
    catalog = load_catalog(CATALOG)
    families = {f.key for f in catalog.families}
    names = {m.logical_name for m in catalog.models}
    for model in catalog.models:
        assert model.architecture_family in families
        assert len(model.upstream_revision) == 40  # a commit, never a moving branch
        assert "/resolve/main/" not in model.artifact_uri
        assert model.request_params.get("language") == "en"
    for suite in catalog.suites:
        assert set(suite.models) <= names
    # Independence comes from families: the qualification roster spans all of them.
    qualification = next(s for s in catalog.suites if s.name == "qualification")
    assert {
        m.architecture_family for m in catalog.models if m.logical_name in qualification.models
    } == families
