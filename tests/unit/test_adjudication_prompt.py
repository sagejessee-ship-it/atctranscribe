"""Adjudication request building, answer parsing, costs, and the OpenRouter client."""

from __future__ import annotations

import json

import httpx
import pytest

from aerochorus.adjudication.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterFatal,
    content_of,
    cost_of,
    fetch_pricing,
)
from aerochorus.adjudication.prompt import (
    MAX_TOKENS,
    RESULT_SCHEMA,
    build_payload,
    estimate_item_cost,
    parse_result,
    render_context,
)
from aerochorus.atc.telephony import telephony_hint
from aerochorus.settings import ControlPlaneSettings

BUNDLE = {
    "segment": {"airport": "KBWI", "station": "BWI", "channel": "TWR", "service": "Tower",
                "frequency_mhz": 119.4, "utc": "2026-09-08 13:00:05Z", "local": "09:00:05",
                "duration_s": 4.2},
    "airport": {"icao": "KBWI", "name": "Baltimore/Washington Intl",
                "runways": [{"end": "33L", "spoken": ["runway three three left"]}],
                "frequencies": [{"service": "TWR", "mhz": 119.4, "call": "Baltimore Tower"}]},
    "hypotheses": [
        {"model": "parakeet-a", "family": "parakeet", "text": "southwest four five six cleared"},
        {"model": "whisper-a", "family": "whisper", "text": "Thank you.", "research_only": True},
    ],
    "abstained": ["canary-a"],
    "errors": [],
    "agreement": {"exact_groups": [{"text": "southwest four five six", "families": 2}],
                  "near": None,
                  "utterances": [{"text": "four five six", "families": 3, "start_s": 0.4,
                                  "end_s": 1.9}]},
    "neighbors": [{"offset_s": -12.0, "text": "southwest four five six tower"}],
    "adsb": {"aircraft": [{"callsign": "SWA456", "telephony": "Southwest 456", "alt_ft": 1200,
                           "vs_fpm": -700, "on_ground": False, "distance_nm": 3.1}]},
}  # fmt: skip


def test_context_carries_hypotheses_agreement_airport_and_traffic():
    text = render_context(BUNDLE)
    assert "KBWI station BWI channel TWR (Tower, 119.400 MHz)" in text
    assert '1. parakeet-a [parakeet]: "southwest four five six cleared"' in text
    assert "whisper-a [whisper, research-only]" in text
    assert "No speech detected by: canary-a" in text
    assert "identical text from 2 model families" in text
    assert "agreed stretch (3 families, 0.4-1.9 s)" in text
    assert "NOT in this clip" in text and "-12 s" in text
    assert "SWA456 = Southwest 456: 1200 ft, descending, 3.1 nm" in text
    assert "33L (runway three three left)" in text
    # No airport, no hypotheses: still a valid request (audio only).
    bare = render_context({"segment": {"duration_s": 2.0}})
    assert "transcribe from the audio alone" in bare


def test_payload_is_strict_structured_output_with_inline_audio():
    payload = build_payload("~google/gemini-pro-latest", BUNDLE, "QUJD", "mp3", "medium")
    assert payload["model"] == "~google/gemini-pro-latest"
    user = payload["messages"][1]["content"]
    assert user[1] == {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "mp3"}}
    schema = payload["response_format"]["json_schema"]
    assert schema["strict"] is True and schema["schema"] is RESULT_SCHEMA
    assert set(RESULT_SCHEMA["required"]) == set(RESULT_SCHEMA["properties"])
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["max_tokens"] == MAX_TOKENS["medium"]
    assert "temperature" not in payload  # Gemini 3 is tuned for its default


def test_parse_result_normalises_and_rejects_bad_answers():
    answer = {
        "speech_present": True,
        "transcript": "  southwest four five six\n cleared to land  ",
        "confidence": 1.7,
        "uncertain_words": ["six"],
        "callsigns": ["swa456"],
        "closest_hypothesis": "parakeet-a",
        "notes": "",
    }
    parsed = parse_result("```json\n" + json.dumps(answer) + "\n```")
    assert parsed["transcript"] == "southwest four five six cleared to land"
    assert parsed["confidence"] == 1.0 and parsed["callsigns"] == ["SWA456"]
    silent = parse_result(json.dumps(answer | {"transcript": "", "speech_present": True}))
    assert silent["speech_present"] is False  # no words, no speech
    with pytest.raises(ValueError, match="not JSON"):
        parse_result("southwest four five six")
    with pytest.raises(ValueError, match="lacks confidence"):
        parse_result(json.dumps({k: v for k, v in answer.items() if k != "confidence"}))


def test_cost_estimates_bound_typical_by_worst_case():
    price = {"prompt": 2.0, "completion": 12.0, "audio": 2.0}
    typical, worst = estimate_item_cost(4.0, 6, "low", price)
    assert 0.005 < typical < 0.03 and worst > typical
    long_typical, _ = estimate_item_cost(60.0, 6, "low", price)
    assert long_typical > typical  # audio tokens scale with duration
    high, _ = estimate_item_cost(4.0, 6, "high", price)
    assert high > typical


def test_telephony_hints():
    assert telephony_hint("SWA456") == "Southwest 456"
    assert telephony_hint("jbu1207 ") == "JetBlue 1207"
    assert telephony_hint("N123AB") == "November 123AB"
    assert telephony_hint("XYZ12") is None and telephony_hint(None) is None


def _client(handler) -> OpenRouterClient:
    return OpenRouterClient(
        "sk-or-secret", http=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_openrouter_client_sends_the_key_only_as_a_bearer_token():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1500, "completion_tokens": 600, "cost": 0.0102},
        })  # fmt: skip

    client = _client(handler)
    assert "sk-or-secret" not in repr(client)
    body = client.complete({"model": "m"})
    assert seen[0].headers["authorization"] == "Bearer sk-or-secret"
    assert seen[0].url.path.endswith("/chat/completions")
    assert cost_of(body, {"prompt": 2, "completion": 12}) == (0.0102, "openrouter")
    no_cost = {"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
    assert cost_of(no_cost, {"prompt": 2, "completion": 12}) == (2.0, "estimated from tokens")
    assert content_of(body) == "{}"
    with pytest.raises(ValueError, match="empty answer"):
        content_of({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})


@pytest.mark.parametrize(
    ("status", "fatal", "retryable"),
    [(401, True, False), (402, True, False), (429, False, True), (503, False, True),
     (400, False, False)],
)  # fmt: skip
def test_openrouter_errors_are_classified(status, fatal, retryable):
    client = _client(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(OpenRouterError) as info:
        client.complete({"model": "m"})
    assert isinstance(info.value, OpenRouterFatal) is fatal
    assert info.value.retryable is retryable
    assert "sk-or-secret" not in str(info.value)


def test_provider_error_inside_a_200_is_an_error():
    client = _client(lambda r: httpx.Response(200, json={"error": {"code": 502, "message": "x"}}))
    with pytest.raises(OpenRouterError) as info:
        client.complete({})
    assert info.value.retryable


def test_public_price_list_is_per_million_tokens():
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": "~google/gemini-pro-latest",
             "pricing": {"prompt": "0.000002", "completion": "0.000012", "audio": "0.000002"}},
        ]})  # fmt: skip

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        pricing = fetch_pricing("~google/gemini-pro-latest", http, "https://x/models")
        assert (pricing.prompt, pricing.completion, pricing.audio) == pytest.approx((2, 12, 2))
        assert fetch_pricing("other/model", http, "https://x/models") is None


def test_control_plane_settings_hold_no_openrouter_key():
    assert not any("openrouter_api_key" in name for name in ControlPlaneSettings.model_fields)


def test_chat_prompt_is_short_and_filled_with_the_clip():
    from aerochorus.adjudication.prompt import render_chat_prompt

    bundle = BUNDLE | {
        "airport": BUNDLE["airport"]
        | {
            "runways": [
                {"end": "15R", "pair": "15R/33L", "spoken": []},
                {"end": "33L", "pair": "15R/33L", "spoken": []},
            ]
        }
    }
    text = render_chat_prompt(bundle)
    assert text.startswith("You're transcribing an air traffic control radio clip (attached)")
    assert (
        "Clip: KBWI Tower, 119.400 MHz, 2026-09-08 13:00:05Z (local 09:00:05), 4.2 seconds." in text
    )
    assert '1. parakeet-a: "southwest four five six cleared"' in text
    assert "No speech detected by: canary-a." in text
    assert "Runways 15R/33L." in text
    assert "SWA456 = Southwest 456, 1,200 ft, descending, 3.1 nm" in text
    assert "Write [unk] for any word you can't make out." in text
    assert "NOT in this clip" not in text  # the short form leaves neighbours out
