"""Adjudication backends: one interface, one class per kind of model API.

The runner (``aerochorus.worker.adjudicator``) claims items, reads and verifies the
audio, retries failed calls and records results. A backend turns one claimed item
and its audio into an answer:

* ``OpenRouterBackend``: the paid hosted model of ADR-022, unchanged.
* ``OpenAICompatibleBackend``: any OpenAI-compatible server that accepts
  ``input_audio`` (vLLM), configured by a profile (ADR-025).

Another API (e.g. Gemini or Qwen Omni-Plus directly) is one more class here, one more
branch in ``backend_for`` and one more ``backend`` value in the profile schema.
"""

from __future__ import annotations

import base64
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from aerochorus.adjudication import squelch
from aerochorus.adjudication.answers import STRICT_REMINDER, parse_answer
from aerochorus.adjudication.errors import BackendFatal
from aerochorus.adjudication.openai_compat import ChatCompletionsClient
from aerochorus.adjudication.openrouter import OpenRouterClient, content_of, cost_of
from aerochorus.adjudication.profiles import AdjudicatorProfile
from aerochorus.adjudication.prompt import (
    MAX_TOKENS,
    RESULT_SCHEMA,
    SYSTEM_PROMPT,
    build_payload,
    parse_result,
    render_context,
)

SYSTEM_PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


@dataclass
class Answer:
    """What one item produced: a validated result, or why it is unusable."""

    result: dict[str, Any] | None
    error: str | None = None
    body: dict[str, Any] | None = None  # the (last) raw response
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None


class AdjudicationBackend(ABC):
    label: str
    # Batch models this backend may take. None: any hosted model, never a local one.
    claims: list[str] | None = None

    def check(self) -> None:  # noqa: B027 - optional hook, nothing to check by default
        """Fail fast (BackendFatal) before claiming anything."""

    @abstractmethod
    def request_record(self, claim: dict[str, Any]) -> dict[str, Any]:
        """What is stored about the request: settings and context, never the audio."""

    @abstractmethod
    def adjudicate(
        self, claim: dict[str, Any], audio: bytes, audio_format: str, request: dict[str, Any]
    ) -> Answer:
        """One answer. Raises BackendError when the call fails (the runner retries);
        may add details to ``request``."""


class OpenRouterBackend(AdjudicationBackend):
    """The paid hosted model (ADR-022): strict structured output, the original audio."""

    label = "openrouter"

    def __init__(self, client: OpenRouterClient) -> None:
        self.client = client

    def request_record(self, claim: dict[str, Any]) -> dict[str, Any]:
        effort = (claim.get("params") or {}).get("reasoning_effort", "low")
        return {
            "model": claim["model"],
            "prompt_version": claim["prompt_version"],
            "reasoning_effort": effort,
            "max_tokens": MAX_TOKENS[effort],
            "system_prompt_sha256": SYSTEM_PROMPT_SHA256,
            "context_text": render_context(claim["bundle"]),
        }

    def adjudicate(
        self, claim: dict[str, Any], audio: bytes, audio_format: str, request: dict[str, Any]
    ) -> Answer:
        payload = build_payload(
            claim["model"],
            claim["bundle"],
            base64.b64encode(audio).decode(),
            audio_format,
            request["reasoning_effort"],
        )
        body = self.client.complete(payload)
        pricing = claim.get("pricing") or {}
        price = {k: float(pricing.get(k) or 0) for k in ("prompt", "completion", "audio")}
        cost, cost_source = cost_of(body, price)
        usage = (body.get("usage") or {}) | {"cost_source": cost_source}
        try:
            result = parse_result(content_of(body))
        except ValueError as exc:
            return Answer(None, f"unusable answer: {exc}", body, usage, cost)
        return Answer(result, None, body, usage, cost)


class OpenAICompatibleBackend(AdjudicationBackend):
    """A self-hosted audio LLM behind an OpenAI-compatible API (vLLM), per a profile."""

    def __init__(
        self,
        profile: AdjudicatorProfile,
        client: ChatCompletionsClient,
        *,
        ffmpeg: str | None = None,
    ) -> None:
        self.profile = profile
        self.client = client
        self.ffmpeg = ffmpeg  # None: send the original file, unsplit
        self.label = f"{profile.name} ({profile.model} at {client.base_url})"
        self.claims = [profile.model]

    def check(self) -> None:
        served = self.client.models()
        if self.profile.model not in served:
            raise BackendFatal(
                f"{self.client.base_url} serves {served or 'nothing'}, not "
                f"{self.profile.model}: start the {self.profile.name} server (deploy/vllm) "
                "or fix the profile's url/model"
            )

    def request_record(self, claim: dict[str, Any]) -> dict[str, Any]:
        p = self.profile
        return {
            "model": claim["model"],
            "prompt_version": claim["prompt_version"],
            "profile": p.name,
            "backend": p.backend,
            "endpoint": self.client.base_url,
            "max_tokens": p.max_tokens,
            "temperature": p.temperature,
            "system_prompt": p.system_prompt,
            "structured_output": p.structured_output,
            "system_prompt_sha256": SYSTEM_PROMPT_SHA256,
            "context_text": render_context(claim["bundle"]),
        }

    def _audio_inputs(
        self, audio: bytes, audio_format: str
    ) -> tuple[list[tuple[bytes, str]], list[squelch.Piece], float | None]:
        """[(bytes, format)]: the whole clip, then its transmissions when split."""
        if not self.ffmpeg:
            return [(audio, audio_format)], [], None
        pcm = squelch.decode_pcm(audio, self.ffmpeg)
        duration = len(pcm) / squelch.RATE
        pieces: list[squelch.Piece] = []
        if self.profile.max_audio_inputs > 1:
            pieces = squelch.split_on_digital_silence(
                pcm, max_pieces=self.profile.max_audio_inputs - 1
            )
            if len(pieces) < 2:
                pieces = []  # one transmission: the whole clip says it all
        inputs = [(squelch.wav_bytes(pcm), "wav")]
        inputs += [(squelch.wav_bytes(pcm[p.start : p.end]), "wav") for p in pieces]
        return inputs, pieces, duration

    def _payload(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        p = self.profile
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "max_tokens": p.max_tokens,
        }
        if p.temperature is not None:
            payload["temperature"] = p.temperature
        if p.structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "atc_adjudication",
                    "strict": True,
                    "schema": RESULT_SCHEMA,
                },
            }
        return payload | p.extra_body

    def _call(self, payload: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        body = self.client.complete(payload)
        usage["calls"] = usage.get("calls", 0) + 1
        usage["latency_ms"] = usage.get("latency_ms", 0) + round(
            (time.monotonic() - started) * 1000
        )
        for key, value in (body.get("usage") or {}).items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
        return body

    def adjudicate(
        self, claim: dict[str, Any], audio: bytes, audio_format: str, request: dict[str, Any]
    ) -> Answer:
        p = self.profile
        usage: dict[str, Any] = {"cost_source": "self-hosted (no per-request cost)"}
        try:
            inputs, pieces, duration = self._audio_inputs(audio, audio_format)
        except ValueError as exc:
            return Answer(None, f"audio could not be prepared: {exc}", usage=usage)
        context = render_context(claim["bundle"])
        if pieces:
            context += "\n\n" + squelch.describe_inputs(pieces, duration or 0.0)
        request["context_text"] = context
        request["audio_inputs"] = {
            "count": len(inputs),
            "format": inputs[0][1],
            "duration_s": round(duration, 2) if duration is not None else None,
            "transmissions": [piece.describe() for piece in pieces],
        }
        text = context if p.system_prompt == "system" else f"{SYSTEM_PROMPT}\n\n{context}"
        content = [
            {"type": "input_audio",
             "input_audio": {"data": base64.b64encode(data).decode(), "format": fmt}}
            for data, fmt in inputs
        ] + [{"type": "text", "text": text}]  # fmt: skip
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        if p.system_prompt == "system":
            messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

        body = self._call(self._payload(messages), usage)
        retries = 0
        while True:
            reply = ""
            try:
                reply = content_of(body)
                result = parse_answer(reply, strip=p.strip_thinking)
            except ValueError as exc:
                if retries >= p.parse_retries:
                    request["parse_retries"] = retries
                    return Answer(None, f"unusable answer: {exc}", body, usage)
                retries += 1
                messages = messages + [
                    {"role": "assistant", "content": reply[:4000] or "(no answer)"},
                    {"role": "user", "content": STRICT_REMINDER.format(error=exc)},
                ]
                body = self._call(self._payload(messages), usage)
                continue
            request["parse_retries"] = retries
            return Answer(result, None, body, usage)


def backend_for(
    profile: AdjudicatorProfile,
    *,
    api_key: str | None = None,
    ffmpeg: str | None = None,
    http: Any = None,
) -> AdjudicationBackend:
    """The backend a profile describes."""
    if profile.backend == "openai_compatible":
        client = ChatCompletionsClient(
            profile.url, api_key=api_key, timeout_s=profile.timeout_s, http=http
        )
        return OpenAICompatibleBackend(profile, client, ffmpeg=ffmpeg)
    raise ValueError(f"unknown adjudicator backend {profile.backend!r}")  # pragma: no cover
