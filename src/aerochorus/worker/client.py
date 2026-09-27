"""HTTP client for the control plane. The worker's only route to PostgreSQL."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import TypeAdapter

from aerochorus.contracts import (
    BatchResult,
    DirectoryBatch,
    DirectoryState,
    ScanCreate,
    ScanFinish,
    ScanRead,
    SegmentStat,
    SourceCreate,
    SourceRead,
    SourceSummary,
    WorkerHeartbeat,
    WorkerRead,
)


class ApiUnreachable(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ApiClient:
    def __init__(
        self, base_url: str | None = None, *, http: httpx.Client | None = None, timeout=120.0
    ) -> None:
        if http is None and base_url is None:
            raise ValueError("base_url or http is required")
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, path: str, *, json: Any = None, params=None) -> Any:
        try:
            response = self._http.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise ApiUnreachable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, detail)
        return response.json()

    def health(self) -> dict[str, Any]:
        try:
            response = self._http.get("/health")
        except httpx.TransportError as exc:
            raise ApiUnreachable(f"{type(exc).__name__}: {exc}") from exc
        return response.json()

    def heartbeat(self, body: WorkerHeartbeat) -> WorkerRead:
        data = self._request("POST", "/api/v1/workers/heartbeat", json=body.model_dump(mode="json"))
        return WorkerRead.model_validate(data)

    def create_source(self, body: SourceCreate) -> SourceRead:
        data = self._request("POST", "/api/v1/sources", json=body.model_dump(mode="json"))
        return SourceRead.model_validate(data)

    def list_sources(self) -> list[SourceRead]:
        data = self._request("GET", "/api/v1/sources")
        return TypeAdapter(list[SourceRead]).validate_python(data)

    def get_source(self, key: str) -> SourceRead:
        return SourceRead.model_validate(self._request("GET", f"/api/v1/sources/{key}"))

    def source_summary(self, key: str) -> SourceSummary:
        data = self._request("GET", f"/api/v1/sources/{key}/summary")
        return SourceSummary.model_validate(data)

    def directory_states(self, key: str) -> list[DirectoryState]:
        data = self._request("GET", f"/api/v1/sources/{key}/directories")
        return TypeAdapter(list[DirectoryState]).validate_python(data)

    def segment_stats(self, key: str, relative_dir: str) -> list[SegmentStat]:
        data = self._request(
            "GET", f"/api/v1/sources/{key}/segment-stats", params={"dir": relative_dir}
        )
        return TypeAdapter(list[SegmentStat]).validate_python(data)

    def start_scan(self, body: ScanCreate) -> ScanRead:
        data = self._request("POST", "/api/v1/scans", json=body.model_dump(mode="json"))
        return ScanRead.model_validate(data)

    def post_batch(self, scan_id: int, batch: DirectoryBatch) -> BatchResult:
        data = self._request(
            "POST", f"/api/v1/scans/{scan_id}/batches", json=batch.model_dump(mode="json")
        )
        return BatchResult.model_validate(data)

    def finish_scan(self, scan_id: int, body: ScanFinish) -> ScanRead:
        data = self._request(
            "POST", f"/api/v1/scans/{scan_id}/finish", json=body.model_dump(mode="json")
        )
        return ScanRead.model_validate(data)

    def get_scan(self, scan_id: int) -> ScanRead:
        return ScanRead.model_validate(self._request("GET", f"/api/v1/scans/{scan_id}"))

    def list_scans(self, source: str | None = None, limit: int = 20) -> list[ScanRead]:
        params: dict[str, Any] = {"limit": limit}
        if source:
            params["source"] = source
        data = self._request("GET", "/api/v1/scans", params=params)
        return TypeAdapter(list[ScanRead]).validate_python(data)
