"""Self-hosted adjudication (ADR-025): answer extraction, profiles, the OpenAI-compatible
backend against a mock vLLM server. No GPU, no network."""

from __future__ import annotations

import array
import base64
import json
import math
from pathlib import Path

import httpx
import pytest

from aerochorus.adjudication import squelch
from aerochorus.adjudication.answers import extract_json_object, parse_answer, strip_thinking
from aerochorus.adjudication.backends import OpenAICompatibleBackend, backend_for
from aerochorus.adjudication.errors import BackendError, BackendFatal
from aerochorus.adjudication.openai_compat import ChatCompletionsClient
from aerochorus.adjudication.profiles import (
    AdjudicatorProfile,
    load_profiles,
    resolve_profile,
)
from aerochorus.adjudication.prompt import RESULT_SCHEMA, SYSTEM_PROMPT
from aerochorus.settings import AdjudicatorSettings

REPO = Path(__file__).resolve().parents[2]
ANSWER = {
    "speech_present": True,
    "transcript": "southwest four fifty six runway three three left cleared to land",
    "confidence": 0.82,
    "uncertain_words": [],
    "callsigns": ["swa456"],
    "closest_hypothesis": "parakeet-a",
    "notes": "",
}
CLAIM = {
    "item_id": 1,
    "model": "local/test-audio-llm",
    "prompt_version": 1,
    "params": {},
    "bundle": {
        "segment": {"airport": "KBWI", "duration_s": 2.5},
        "hypotheses": [{"model": "parakeet-a", "family": "parakeet", "text": "southwest 456"}],
    },
}


# --- answers ----------------------------------------------------------------------------


def test_thinking_is_stripped_however_it_is_wrapped():
    assert strip_thinking('<think>runway 33L?</think>\n{"a": 1}') == '{"a": 1}'
    # The chat template opened the block in the prompt: only the close appears.
    assert strip_thinking('I hear "three three left".</think>{"a": 1}') == '{"a": 1}'
    # Cut off while thinking: nothing usable.
    assert strip_thinking("<think>still listening to") == ""
    assert strip_thinking('{"a": 1}') == '{"a": 1}'


def test_the_json_object_is_found_in_prose_fences_and_with_braces_in_strings():
    reply = 'Here you go:\n```json\n{"notes": "a {brace} and \\"quote\\"", "n": 1}\n```\nDone.'
    assert json.loads(extract_json_object(reply)) == {"notes": 'a {brace} and "quote"', "n": 1}
    assert extract_json_object('{broken {"ok": true}') == '{"ok": true}'
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json_object("no braces at all")


def test_answers_are_validated_against_the_schema():
    reply = "<think>two speakers</think>" + json.dumps(ANSWER)
    result = parse_answer(reply)
    assert result["transcript"] == ANSWER["transcript"] and result["callsigns"] == ["SWA456"]
    for broken, why in [
        (ANSWER | {"speech_present": "yes"}, "speech_present"),
        (ANSWER | {"transcript": 42}, "transcript"),
        (ANSWER | {"callsigns": "SWA456"}, "callsigns"),
        (ANSWER | {"confidence": True}, "confidence"),
        ({k: v for k, v in ANSWER.items() if k != "notes"}, "lacks notes"),
    ]:
        with pytest.raises(ValueError, match=why):
            parse_answer(json.dumps(broken))
    with pytest.raises(ValueError, match="empty after removing the reasoning"):
        parse_answer("<think>never finished")


# --- profiles -----------------------------------------------------------------------------


def test_the_shipped_profiles_load_and_self_hosted_ids_are_local():
    book = load_profiles(REPO / "config" / "adjudicators.toml")
    assert {"local-small", "h100-large", "h100-large-fp8", "h100-voxtral"} <= set(book.profiles)
    for profile in book.profiles.values():
        assert profile.model.startswith("local/") and profile.vllm is not None
    small = book.profiles["local-small"]
    assert small.vllm.model == "mistralai/Voxtral-Mini-3B-2507"
    assert small.system_prompt == "user" and small.max_audio_inputs > 1
    large = book.profiles["h100-large"]
    assert large.vllm.gpus == 2 and "--reasoning-parser" in large.vllm.args
    assert book.gate.min_families == 2
    with pytest.raises(ValueError, match="must start with 'local/'"):
        AdjudicatorProfile(name="x", model="gemini", url="http://h/v1", vllm={"model": "m"})


def test_a_profile_is_chosen_by_env_and_overridden_per_host(monkeypatch):
    monkeypatch.setenv("AEROCHORUS_ADJUDICATOR_PROFILES", str(REPO / "config/adjudicators.toml"))
    monkeypatch.setenv("AEROCHORUS_ADJUDICATOR_PROFILE", "h100-large")
    monkeypatch.setenv("AEROCHORUS_ADJUDICATOR_URL", "http://gpu-host:8100/v1")
    monkeypatch.setenv("AEROCHORUS_ADJUDICATOR_MAX_TOKENS", "4096")
    profile, gate = resolve_profile(AdjudicatorSettings(_env_file=None))
    assert profile.name == "h100-large" and profile.url == "http://gpu-host:8100/v1"
    assert profile.max_tokens == 4096 and profile.model == "local/qwen3-omni-30b-a3b-thinking"
    with pytest.raises(LookupError, match="unknown adjudicator profile 'nope'"):
        resolve_profile(AdjudicatorSettings(_env_file=None), "nope")
    monkeypatch.delenv("AEROCHORUS_ADJUDICATOR_PROFILE")
    with pytest.raises(LookupError, match="no adjudicator profile"):
        resolve_profile(AdjudicatorSettings(_env_file=None))


# --- the OpenAI-compatible backend against a mock server ----------------------------------------


def profile(**overrides) -> AdjudicatorProfile:
    return AdjudicatorProfile(
        name="test",
        model="local/test-audio-llm",
        url="http://vllm.test:8100/v1",
        **overrides,
    )


class FakeVllm:
    """Records requests; replies from a script (one reply per call, then the last again)."""

    def __init__(self, replies, status: int = 200, served=("local/test-audio-llm",)):
        self.replies = list(replies)
        self.status = status
        self.served = served
        self.requests: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.served]})
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.status != 200:
            return httpx.Response(self.status, json={"object": "error", "message": "nope"})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
        })  # fmt: skip


def backend(server: FakeVllm, *, api_key=None, ffmpeg=None, **overrides):
    return backend_for(
        profile(**overrides),
        api_key=api_key,
        ffmpeg=ffmpeg,
        http=httpx.Client(transport=httpx.MockTransport(server)),
    )


def test_request_carries_audio_context_schema_and_profile_settings():
    server = FakeVllm([json.dumps(ANSWER)])
    b = backend(server, api_key="vllm-secret", system_prompt="user", temperature=0.2,
                extra_body={"top_p": 0.95})  # fmt: skip
    request = b.request_record(CLAIM)
    answer = b.adjudicate(CLAIM, b"ID3fake", "mp3", request)
    assert answer.result["transcript"] == ANSWER["transcript"] and answer.error is None
    (sent,) = server.requests
    assert sent["model"] == "local/test-audio-llm"
    assert sent["temperature"] == 0.2 and sent["top_p"] == 0.95 and sent["max_tokens"] == 1024
    assert sent["response_format"]["json_schema"]["schema"] == RESULT_SCHEMA
    (user,) = sent["messages"]  # no system role: the instructions lead the user turn
    audio, text = user["content"]
    assert audio == {"type": "input_audio",
                     "input_audio": {"data": base64.b64encode(b"ID3fake").decode(), "format": "mp3"}}  # noqa: E501  # fmt: skip
    assert text["text"].startswith(SYSTEM_PROMPT) and "parakeet-a [parakeet]" in text["text"]
    assert server.headers[0]["authorization"] == "Bearer vllm-secret"
    assert answer.usage["calls"] == 1 and answer.usage["completion_tokens"] == 120
    assert answer.cost_usd is None and "self-hosted" in answer.usage["cost_source"]
    assert request["profile"] == "test" and request["parse_retries"] == 0
    assert request["audio_inputs"]["count"] == 1
    assert "vllm-secret" not in repr(b.client) and "vllm-secret" not in json.dumps(request)

    with_system = backend(FakeVllm([json.dumps(ANSWER)]), system_prompt="system",
                          structured_output=False)  # fmt: skip
    with_system.adjudicate(CLAIM, b"x", "mp3", with_system.request_record(CLAIM))
    sent = with_system.client._http._transport.handler.requests[0]
    assert sent["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "response_format" not in sent


def test_an_unusable_answer_is_retried_once_with_a_stricter_reminder():
    thinking = "<think>hmm</think>" + json.dumps(ANSWER)
    server = FakeVllm(["Sure! The pilot says runway three three left.", thinking])
    b = backend(server)
    request = b.request_record(CLAIM)
    answer = b.adjudicate(CLAIM, b"x", "mp3", request)
    assert answer.result["confidence"] == 0.82 and request["parse_retries"] == 1
    retry = server.requests[1]["messages"]
    assert retry[-2] == {"role": "assistant",
                         "content": "Sure! The pilot says runway three three left."}  # fmt: skip
    assert "no JSON object" in retry[-1]["content"] and "exactly one JSON" in retry[-1]["content"]
    assert answer.usage["calls"] == 2 and answer.usage["prompt_tokens"] == 1800

    hopeless = FakeVllm(["still prose"])
    answer = backend(hopeless).adjudicate(CLAIM, b"x", "mp3", {})
    assert answer.result is None and "unusable answer" in answer.error
    assert len(hopeless.requests) == 2  # one retry, then the item fails (not the batch)
    assert backend(FakeVllm(["prose"]), parse_retries=0).adjudicate(CLAIM, b"x", "mp3", {}).error


@pytest.mark.parametrize(
    ("status", "fatal", "retryable"),
    [(401, True, False), (404, True, False), (400, False, False), (503, False, True)],
)
def test_server_errors_are_classified(status, fatal, retryable):
    b = backend(FakeVllm([""], status=status))
    with pytest.raises(BackendError) as excinfo:
        b.adjudicate(CLAIM, b"x", "mp3", {})
    assert isinstance(excinfo.value, BackendFatal) is fatal
    assert excinfo.value.retryable is retryable


def test_check_fails_fast_when_the_server_is_down_or_serves_another_model():
    assert backend(FakeVllm([""])).check() is None
    with pytest.raises(BackendFatal, match="not local/test-audio-llm"):
        backend(FakeVllm([""], served=("local/other",))).check()

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    down = OpenAICompatibleBackend(
        profile(), ChatCompletionsClient("http://vllm.test/v1",
                                         http=httpx.Client(transport=httpx.MockTransport(refuse)))
    )  # fmt: skip
    with pytest.raises(BackendFatal, match="is it running"):
        down.check()
    with pytest.raises(BackendFatal, match="cannot reach"):
        down.adjudicate(CLAIM, b"x", "mp3", {})


def test_squelch_split_clips_become_extra_audio_inputs(monkeypatch):
    rate = squelch.RATE
    tone = [int(5000 * math.sin(2 * math.pi * 150 * i / rate)) for i in range(rate)]
    two = array.array("h", tone + [0] * (rate // 5) + tone)
    monkeypatch.setattr(squelch, "decode_pcm", lambda data, ffmpeg, rate=rate: two)
    server = FakeVllm([json.dumps(ANSWER)])
    b = backend(server, ffmpeg="ffmpeg", max_audio_inputs=5)
    request = b.request_record(CLAIM)
    b.adjudicate(CLAIM, b"ID3", "mp3", request)
    parts = server.requests[0]["messages"][-1]["content"]
    audio = [p["input_audio"] for p in parts if p["type"] == "input_audio"]
    assert [a["format"] for a in audio] == ["wav", "wav", "wav"]  # whole clip + 2 transmissions
    text = parts[-1]["text"]
    assert "audio 2: 0.00-1.00 s" in text and "audio 3: 1.20-2.20 s" in text
    assert request["audio_inputs"]["count"] == 3
    assert [t["start_s"] for t in request["audio_inputs"]["transmissions"]] == [0.0, 1.2]
    assert "Audio inputs:" in request["context_text"]

    single = FakeVllm([json.dumps(ANSWER)])
    b1 = backend(single, ffmpeg="ffmpeg", max_audio_inputs=1)
    b1.adjudicate(CLAIM, b"ID3", "mp3", b1.request_record(CLAIM))
    parts = single.requests[0]["messages"][-1]["content"]
    assert sum(p["type"] == "input_audio" for p in parts) == 1  # whole clip only

    def undecodable(data, ffmpeg, rate=rate):
        raise ValueError("ffmpeg could not decode the audio: junk")

    monkeypatch.setattr(squelch, "decode_pcm", undecodable)
    answer = backend(FakeVllm(["{}"]), ffmpeg="ffmpeg").adjudicate(CLAIM, b"?", "mp3", {})
    assert answer.result is None and "could not be prepared" in answer.error
