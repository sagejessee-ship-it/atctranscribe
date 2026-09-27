"""OpenRouter chat completions (audio in, structured JSON out) and its public price list.

The API key is held by the runner process only. It is sent as a bearer token to
OpenRouter and never logged, stored, or returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from aerochorus.adjudication_contracts import Pricing


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class OpenRouterFatal(OpenRouterError):
    """Stop the runner: the key is rejected or the account has no credit."""


def _classify(status: int, message: str) -> OpenRouterError:
    if status in (401, 403):
        return OpenRouterFatal(
            f"OpenRouter rejected the API key ({status}): {message}", status=status
        )
    if status == 402:
        return OpenRouterFatal(f"OpenRouter: insufficient credits ({message})", status=status)
    if status in (408, 429) or status >= 500:
        return OpenRouterError(f"OpenRouter {status}: {message}", status=status, retryable=True)
    return OpenRouterError(f"OpenRouter {status}: {message}", status=status)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        http: httpx.Client | None = None,
        timeout_s: float = 300.0,
    ) -> None:
        if not api_key:
            raise ValueError("an OpenRouter API key is required")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=timeout_s)

    def __repr__(self) -> str:  # never print the key
        return f"OpenRouterClient(base_url={self._base!r})"

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(
                f"{self._base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._key}", "X-Title": "AeroChorus"},
            )
        except httpx.TimeoutException as exc:
            raise OpenRouterError(f"OpenRouter timed out: {exc}", retryable=True) from exc
        except httpx.TransportError as exc:
            raise OpenRouterError(
                f"OpenRouter unreachable: {type(exc).__name__}", retryable=True
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:300]}}
        if response.status_code >= 400:
            message = (body.get("error") or {}).get("message") or response.reason_phrase
            raise _classify(response.status_code, str(message))
        if body.get("error"):  # some provider failures arrive with HTTP 200
            error = body["error"]
            code = error.get("code")
            raise _classify(code if isinstance(code, int) else 502, str(error.get("message")))
        return body


def content_of(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("response has no message content") from exc
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content:
        finish = (body.get("choices") or [{}])[0].get("finish_reason")
        raise ValueError(f"empty answer (finish_reason={finish})")
    return content


def cost_of(body: dict[str, Any], pricing: dict[str, float]) -> tuple[float | None, str]:
    """USD for this call: OpenRouter's own accounting when present, else from token counts."""
    usage = body.get("usage") or {}
    if isinstance(usage.get("cost"), int | float):
        return float(usage["cost"]), "openrouter"
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if prompt is None and completion is None:
        return None, "unknown"
    return (
        ((prompt or 0) * pricing["prompt"] + (completion or 0) * pricing["completion"]) / 1e6,
        "estimated from tokens",
    )


def fetch_pricing(model: str, http: httpx.Client, url: str) -> Pricing | None:
    """Per-million-token prices from OpenRouter's public model list (no key needed)."""
    response = http.get(url, timeout=20)
    response.raise_for_status()
    for entry in response.json().get("data", []):
        if entry.get("id") == model:
            price = entry.get("pricing") or {}

            def per_million(key: str, price: dict = price) -> float:
                return float(price.get(key) or price.get("prompt") or 0) * 1e6

            return Pricing(
                model=model,
                source="OpenRouter public model list",
                prompt=per_million("prompt"),
                completion=per_million("completion"),
                audio=per_million("audio"),
                fetched_at=datetime.now(UTC),
            )
    return None
