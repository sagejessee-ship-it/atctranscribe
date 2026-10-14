"""An OpenAI-compatible chat-completions server that accepts ``input_audio`` (vLLM).

Self-hosted (ADR-025): no per-request cost. The optional API key comes from the
environment, is sent as a bearer token, and is never logged, stored or returned.
"""

from __future__ import annotations

from typing import Any

import httpx

from aerochorus.adjudication.errors import BackendError, BackendFatal


def _message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str):
        return error
    return str(body["message"]) if body.get("message") else None  # vLLM's own error shape


class ChatCompletionsClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key or ""
        self._http = http or httpx.Client(timeout=timeout_s)

    def __repr__(self) -> str:  # never print the key
        return f"ChatCompletionsClient(base_url={self.base_url!r})"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def _classify(self, status: int, message: str) -> BackendError:
        where = f"model server {self.base_url}"
        if status in (401, 403):
            return BackendFatal(
                f"{where} rejected the API key ({status}): {message}", status=status
            )
        if status == 404:  # unknown model: the server runs another profile
            return BackendFatal(f"{where} {status}: {message}", status=status)
        if status in (408, 429) or status >= 500:
            return BackendError(f"{where} {status}: {message}", status=status, retryable=True)
        return BackendError(f"{where} {status}: {message}", status=status)  # this item only

    def models(self) -> list[str]:
        """The model ids the server serves (its --served-model-name)."""
        try:
            response = self._http.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=15
            )
        except httpx.TransportError as exc:
            raise BackendFatal(
                f"cannot reach the model server at {self.base_url} ({type(exc).__name__}); "
                "is it running? (deploy/vllm)"
            ) from exc
        if response.status_code >= 400:
            raise self._classify(response.status_code, response.text[:200])
        return [str(m.get("id")) for m in response.json().get("data", []) if isinstance(m, dict)]

    def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise BackendError(
                f"model server {self.base_url} timed out: {exc}", retryable=True
            ) from exc
        except httpx.ConnectError as exc:
            raise BackendFatal(
                f"cannot reach the model server at {self.base_url}; is it running?"
            ) from exc
        except httpx.TransportError as exc:
            raise BackendError(
                f"model server {self.base_url}: {type(exc).__name__}", retryable=True
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:300]}}
        if response.status_code >= 400:
            raise self._classify(response.status_code, _message(body) or response.reason_phrase)
        if not isinstance(body, dict):
            raise BackendError(f"model server {self.base_url}: response is not a JSON object")
        if body.get("error") or body.get("object") == "error":
            error = body.get("error")
            code = body.get("code") or (error.get("code") if isinstance(error, dict) else None)
            raise self._classify(code if isinstance(code, int) else 502, _message(body) or "error")
        return body
