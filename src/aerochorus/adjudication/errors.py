"""Failures of an adjudication backend, as the runner needs to tell them apart."""

from __future__ import annotations


class BackendError(RuntimeError):
    """A call failed. ``retryable``: worth another attempt (rate limit, outage, timeout)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class BackendFatal(BackendError):
    """Stop the runner: nothing else will work either (rejected key, no credit, a model
    server that is down or serves a different model). The item goes back in line."""
