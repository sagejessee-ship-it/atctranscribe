"""Adjudication runner (ADR-022): claim, read audio read-only, ask the model, record.

Runs on the host where the source audio is mounted (the Linux worker/edge box).
It holds the OpenRouter key; the control plane decides what may be sent and
enforces each batch's cost cap when it hands out work. Money spent is always
reported, even when the answer turns out to be unusable.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from aerochorus.adjudication.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterFatal,
    content_of,
    cost_of,
)
from aerochorus.adjudication.prompt import (
    AUDIO_FORMATS,
    MAX_TOKENS,
    SYSTEM_PROMPT,
    build_payload,
    parse_result,
    render_context,
)
from aerochorus.worker.client import ApiClient
from aerochorus.worker.fs import ReadOnlyCorpusReader, SourceUnavailable


@dataclass
class RunSummary:
    done: int = 0
    failed: int = 0
    spent_usd: float = 0.0
    stopped: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)


class Adjudicator:
    def __init__(
        self,
        api: ApiClient,
        readers: dict[str, ReadOnlyCorpusReader],
        openrouter: OpenRouterClient,
        runner: str,
        *,
        retries: int = 2,
        backoff_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.readers = readers
        self.openrouter = openrouter
        self.runner = runner
        self.retries = retries
        self.backoff_s = backoff_s
        self.sleep = sleep
        self.log = log

    def _post(self, item_id: int, **fields: Any) -> dict[str, Any]:
        return self.api.post_adjudication_result(item_id, {"runner": self.runner, **fields})

    def _audio(self, claim: dict[str, Any]) -> tuple[bytes, str]:
        """(bytes, format): read-only, verified against the indexed sha256."""
        reader = self.readers.get(claim["source_key"])
        if reader is None:
            raise LookupError(f"source {claim['source_key']!r} is not mounted on {self.runner}")
        suffix = PurePosixPath(claim["relative_path"]).suffix.lower()
        audio_format = AUDIO_FORMATS.get(suffix)
        if audio_format is None:
            raise ValueError(f"audio format {suffix or '?'} is not supported for adjudication")
        reader.require_available()
        data = reader.read_bytes(claim["relative_path"])
        digest = hashlib.sha256(data).hexdigest()
        if claim.get("sha256") and digest != claim["sha256"]:
            raise ValueError(
                f"source audio changed since it was indexed (sha256 {digest[:12]}… != "
                f"{claim['sha256'][:12]}…)"
            )
        return data, audio_format

    def process(self, claim: dict[str, Any]) -> dict[str, Any]:
        item_id = claim["item_id"]
        params = claim.get("params") or {}
        effort = params.get("reasoning_effort", "low")
        context = render_context(claim["bundle"])
        request: dict[str, Any] = {
            "model": claim["model"],
            "prompt_version": claim["prompt_version"],
            "reasoning_effort": effort,
            "max_tokens": MAX_TOKENS[effort],
            "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "context_text": context,
        }
        try:
            data, audio_format = self._audio(claim)
        except (LookupError, SourceUnavailable) as exc:  # not here / not mounted right now
            return self._post(item_id, status="failed", error=str(exc), request=request,
                              retryable=True)  # fmt: skip
        except (OSError, ValueError) as exc:
            return self._post(item_id, status="failed", error=str(exc), request=request)
        request["audio"] = {
            "format": audio_format,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        payload = build_payload(
            claim["model"],
            claim["bundle"],
            base64.b64encode(data).decode(),
            audio_format,
            effort,
        )
        pricing = claim.get("pricing") or {}
        price = {k: float(pricing.get(k) or 0) for k in ("prompt", "completion", "audio")}
        body = None
        for attempt in range(self.retries + 1):
            try:
                body = self.openrouter.complete(payload)
                break
            except OpenRouterFatal as exc:
                self._post(item_id, status="failed", error=str(exc), request=request,
                           retryable=True)  # fmt: skip
                raise
            except OpenRouterError as exc:
                if exc.retryable and attempt < self.retries:
                    self.log(f"item {item_id}: {exc}; retrying")
                    self.sleep(self.backoff_s * (attempt + 1))
                    continue
                return self._post(item_id, status="failed", error=str(exc), request=request,
                                  retryable=exc.retryable)  # fmt: skip
        cost, cost_source = cost_of(body, price)
        usage = (body.get("usage") or {}) | {"cost_source": cost_source}
        try:
            result = parse_result(content_of(body))
        except ValueError as exc:
            return self._post(item_id, status="failed", error=f"unusable answer: {exc}",
                              request=request, response=body, usage=usage,
                              cost_usd=cost)  # fmt: skip
        return self._post(
            item_id,
            status="done",
            transcript=result["transcript"],
            result=result,
            request=request,
            response=body,
            usage=usage,
            cost_usd=cost,
        )

    def run(
        self,
        *,
        follow: bool = False,
        max_items: int | None = None,
        concurrency: int = 1,
        poll_s: float = 10.0,
    ) -> RunSummary:
        summary = RunSummary()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while max_items is None or summary.done + summary.failed < max_items:
                room = (
                    concurrency
                    if max_items is None
                    else min(concurrency, max_items - summary.done - summary.failed)
                )
                claims = self.api.claim_adjudications(self.runner, limit=room)
                if not claims:
                    if not follow:
                        break
                    self.sleep(poll_s)
                    continue
                futures = [pool.submit(self.process, c) for c in claims]
                for claim, future in zip(claims, futures, strict=True):
                    try:
                        view = future.result()
                    except OpenRouterFatal as exc:
                        summary.stopped = str(exc)
                        summary.failed += 1
                        continue
                    summary.items.append(view)
                    summary.spent_usd += view.get("cost_usd") or 0.0
                    if view["status"] == "done":
                        summary.done += 1
                    else:
                        summary.failed += 1
                    self.log(
                        f"item {claim['item_id']} segment {claim['segment_id']}: {view['status']}"
                        + (f" — {view['transcript']!r}" if view.get("transcript") else "")
                        + (f" — {view['error']}" if view.get("error") else "")
                    )
                if summary.stopped:
                    self.log(f"stopping: {summary.stopped}")
                    break
        return summary
