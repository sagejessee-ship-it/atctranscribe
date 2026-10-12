"""Adjudication runner (ADR-022, ADR-025): claim, read audio read-only, ask, record.

Runs on the host where the source audio is mounted. A backend does the asking: the
paid hosted model (it holds the OpenRouter key; the control plane enforces each
batch's cost cap when it hands out work) or a self-hosted model server named by a
profile. Money spent is always reported, even when the answer turns out unusable.
Work is resumable by construction: finished items are never handed out again, and
stale claims go back in line.
"""

from __future__ import annotations

import hashlib
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from aerochorus.adjudication.backends import AdjudicationBackend, Answer
from aerochorus.adjudication.errors import BackendError, BackendFatal
from aerochorus.adjudication.prompt import AUDIO_FORMATS
from aerochorus.worker.client import ApiClient
from aerochorus.worker.fs import ReadOnlyCorpusReader, SourceUnavailable


@dataclass
class RunSummary:
    done: int = 0
    failed: int = 0
    spent_usd: float = 0.0
    stopped: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    latencies_ms: list[int] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        return {
            "items": self.done + self.failed,
            "done": self.done,
            "failed": self.failed,
            "spent_usd": round(self.spent_usd, 4),
            "latency_ms_p50": round(statistics.median(lat)) if lat else None,
            "latency_ms_p95": lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] if lat else None,
            "latency_ms_mean": round(statistics.fmean(lat)) if lat else None,
            **{f"{k}_total": v for k, v in sorted(self.tokens.items())},
        }


class Adjudicator:
    def __init__(
        self,
        api: ApiClient,
        readers: dict[str, ReadOnlyCorpusReader],
        backend: AdjudicationBackend,
        runner: str,
        *,
        retries: int = 2,
        backoff_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.readers = readers
        self.backend = backend
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
        request = self.backend.request_record(claim)
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
        answer: Answer | None = None
        started = time.monotonic()
        for attempt in range(self.retries + 1):
            try:
                answer = self.backend.adjudicate(claim, data, audio_format, request)
                break
            except BackendFatal as exc:
                self._post(item_id, status="failed", error=str(exc), request=request,
                           retryable=True)  # fmt: skip
                raise
            except BackendError as exc:
                if exc.retryable and attempt < self.retries:
                    self.log(f"item {item_id}: {exc}; retrying")
                    self.sleep(self.backoff_s * (attempt + 1))
                    continue
                return self._post(item_id, status="failed", error=str(exc), request=request,
                                  retryable=exc.retryable)  # fmt: skip
        assert answer is not None
        answer.usage.setdefault("latency_ms", round((time.monotonic() - started) * 1000))
        recorded = {
            "request": request,
            "response": answer.body,
            "usage": answer.usage,
            "cost_usd": answer.cost_usd,
        }
        if answer.result is None:
            return self._post(item_id, status="failed", error=answer.error, **recorded)
        return self._post(
            item_id,
            status="done",
            transcript=answer.result["transcript"],
            result=answer.result,
            **recorded,
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
                claims = self.api.claim_adjudications(
                    self.runner, limit=room, models=self.backend.claims
                )
                if not claims:
                    if not follow:
                        break
                    self.sleep(poll_s)
                    continue
                futures = [pool.submit(self.process, c) for c in claims]
                for claim, future in zip(claims, futures, strict=True):
                    try:
                        view = future.result()
                    except BackendFatal as exc:
                        summary.stopped = str(exc)
                        summary.failed += 1
                        continue
                    summary.items.append(view)
                    summary.spent_usd += view.get("cost_usd") or 0.0
                    usage = view.get("usage") or {}
                    if isinstance(usage.get("latency_ms"), int):
                        summary.latencies_ms.append(usage["latency_ms"])
                    for key in ("prompt_tokens", "completion_tokens"):
                        if isinstance(usage.get(key), int):
                            summary.tokens[key] = summary.tokens.get(key, 0) + usage[key]
                    if view["status"] == "done":
                        summary.done += 1
                    else:
                        summary.failed += 1
                    self.log(
                        f"item {claim['item_id']} segment {claim['segment_id']}: {view['status']}"
                        + (f" ({usage['latency_ms']} ms)" if "latency_ms" in usage else "")
                        + (f" — {view['transcript']!r}" if view.get("transcript") else "")
                        + (f" — {view['error']}" if view.get("error") else "")
                    )
                if summary.stopped:
                    self.log(f"stopping: {summary.stopped}")
                    break
        return summary
