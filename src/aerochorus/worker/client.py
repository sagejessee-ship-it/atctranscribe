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
from aerochorus.dataset_contracts import DatasetItem, DatasetView, ExportReport
from aerochorus.sweep_contracts import (
    CatalogSync,
    CatalogSyncResult,
    ClaimRequest,
    ModelRead,
    ModelRunClaim,
    ModelRunFinish,
    ModelRunStart,
    ModelUpdate,
    PendingBatch,
    PlatformQualificationRead,
    PlatformQualificationWrite,
    QualificationRecord,
    ResultAck,
    ResultPost,
    SuiteRead,
    SuiteWrite,
    SweepCreate,
    SweepRead,
    SweepReport,
    TranscriptionRead,
    WordBackfillAck,
    WordBackfillItem,
    WordBackfillPost,
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

    def set_source_role(self, key: str, role: str) -> SourceRead:
        data = self._request("PATCH", f"/api/v1/sources/{key}/role", json={"role": role})
        return SourceRead.model_validate(data)

    def refresh_agreement(self, limit: int | None = None) -> int:
        params = {"limit": limit} if limit else None
        return self._request("POST", "/api/v1/agreement/refresh", params=params)["refreshed"]

    def put_airport(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/api/v1/airports/{profile['icao']}", json=profile)

    def put_airspaces(self, icao: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/api/v1/airports/{icao}/airspaces", json=body)

    def get_airport(self, icao: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/airports/{icao}")

    def get_dataset(self, dataset_id: int) -> DatasetView:
        return DatasetView.model_validate(self._request("GET", f"/api/v1/datasets/{dataset_id}"))

    def dataset_items(
        self, dataset_id: int, offset: int = 0, limit: int = 1000
    ) -> list[DatasetItem]:
        data = self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/items",
            params={"offset": offset, "limit": limit},
        )
        return TypeAdapter(list[DatasetItem]).validate_python(data)

    def record_dataset_export(self, dataset_id: int, body: ExportReport) -> DatasetView:
        data = self._request(
            "POST", f"/api/v1/datasets/{dataset_id}/export", json=body.model_dump(mode="json")
        )
        return DatasetView.model_validate(data)

    def create_dataset(self, body: dict) -> DatasetView:
        return DatasetView.model_validate(self._request("POST", "/api/v1/datasets", json=body))

    def list_datasets(self) -> list[DatasetView]:
        return TypeAdapter(list[DatasetView]).validate_python(
            self._request("GET", "/api/v1/datasets")
        )

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

    # --- model registry and sweeps -------------------------------------------------

    def sync_models(self, body: CatalogSync) -> CatalogSyncResult:
        data = self._request("POST", "/api/v1/models/sync", json=body.model_dump(mode="json"))
        return CatalogSyncResult.model_validate(data)

    def list_models(self) -> list[ModelRead]:
        return TypeAdapter(list[ModelRead]).validate_python(self._request("GET", "/api/v1/models"))

    def get_model(self, name: str) -> ModelRead:
        return ModelRead.model_validate(self._request("GET", f"/api/v1/models/{name}"))

    def update_model(self, name: str, body: ModelUpdate) -> ModelRead:
        data = self._request(
            "PATCH", f"/api/v1/models/{name}", json=body.model_dump(exclude_none=True)
        )
        return ModelRead.model_validate(data)

    def put_platform_qualification(
        self, name: str, profile: str, body: PlatformQualificationWrite
    ) -> PlatformQualificationRead:
        data = self._request(
            "PUT",
            f"/api/v1/models/{name}/qualifications/{profile}",
            json=body.model_dump(mode="json"),
        )
        return PlatformQualificationRead.model_validate(data)

    def platform_qualifications(
        self, profile: str | None = None
    ) -> list[PlatformQualificationRead]:
        data = self._request(
            "GET", "/api/v1/qualifications", params={"profile": profile} if profile else None
        )
        return TypeAdapter(list[PlatformQualificationRead]).validate_python(data)

    def record_qualification(self, name: str, body: QualificationRecord) -> ModelRead:
        data = self._request(
            "POST", f"/api/v1/models/{name}/qualification", json=body.model_dump(mode="json")
        )
        return ModelRead.model_validate(data)

    def word_backfill_candidates(
        self, limit: int = 500, after: str | None = None
    ) -> list[WordBackfillItem]:
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        data = self._request("GET", "/api/v1/results/word-backfill", params=params)
        return TypeAdapter(list[WordBackfillItem]).validate_python(data)

    def store_word_backfill(self, items: list[WordBackfillPost]) -> WordBackfillAck:
        data = self._request(
            "POST", "/api/v1/results/words", json=[i.model_dump(mode="json") for i in items]
        )
        return WordBackfillAck.model_validate(data)

    def list_suites(self) -> list[SuiteRead]:
        return TypeAdapter(list[SuiteRead]).validate_python(self._request("GET", "/api/v1/suites"))

    def write_suite(self, name: str, body: SuiteWrite) -> SuiteRead:
        data = self._request("PUT", f"/api/v1/suites/{name}", json=body.model_dump(mode="json"))
        return SuiteRead.model_validate(data)

    def create_sweep(self, body: SweepCreate, *, allow_unqualified: bool = False) -> SweepRead:
        data = self._request(
            "POST",
            "/api/v1/sweeps",
            json=body.model_dump(mode="json"),
            params={"allow_unqualified": str(allow_unqualified).lower()},
        )
        return SweepRead.model_validate(data)

    def list_sweeps(self, limit: int = 20) -> list[SweepRead]:
        data = self._request("GET", "/api/v1/sweeps", params={"limit": limit})
        return TypeAdapter(list[SweepRead]).validate_python(data)

    def get_sweep(self, sweep_id: int) -> SweepRead:
        return SweepRead.model_validate(self._request("GET", f"/api/v1/sweeps/{sweep_id}"))

    def control_sweep(self, sweep_id: int, action: str, model: str | None = None) -> SweepRead:
        params = {"model": model} if model else None
        data = self._request("POST", f"/api/v1/sweeps/{sweep_id}/{action}", params=params)
        return SweepRead.model_validate(data)

    def sweep_report(self, sweep_id: int) -> SweepReport:
        data = self._request("GET", f"/api/v1/sweeps/{sweep_id}/report")
        return SweepReport.model_validate(data)

    def sweep_transcripts(self, sweep_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
        return self._request(
            "GET",
            f"/api/v1/sweeps/{sweep_id}/transcripts",
            params={"limit": limit, "offset": offset},
        )

    def segment_results(self, segment_id: int) -> list[TranscriptionRead]:
        data = self._request("GET", f"/api/v1/segments/{segment_id}/results")
        return TypeAdapter(list[TranscriptionRead]).validate_python(data)

    # --- sweep worker protocol ------------------------------------------------------

    def claim_model_run(self, body: ClaimRequest) -> ModelRunClaim | None:
        try:
            response = self._http.post("/api/v1/sweeps/claim", json=body.model_dump(mode="json"))
        except httpx.TransportError as exc:
            raise ApiUnreachable(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.json().get("detail"))
        return ModelRunClaim.model_validate(response.json())

    def start_model_run(self, sweep_model_id: int, body: ModelRunStart) -> None:
        self._request(
            "POST",
            f"/api/v1/sweep-models/{sweep_model_id}/start",
            json=body.model_dump(mode="json"),
        )

    def pending_segments(self, sweep_model_id: int, limit: int) -> PendingBatch:
        data = self._request(
            "GET", f"/api/v1/sweep-models/{sweep_model_id}/pending", params={"limit": limit}
        )
        return PendingBatch.model_validate(data)

    def post_result(self, sweep_model_id: int, worker_name: str, body: ResultPost) -> ResultAck:
        data = self._request(
            "POST",
            f"/api/v1/sweep-models/{sweep_model_id}/results",
            json=body.model_dump(mode="json"),
            params={"worker_name": worker_name},
        )
        return ResultAck.model_validate(data)

    def finish_model_run(
        self, sweep_model_id: int, worker_name: str, body: ModelRunFinish
    ) -> dict[str, str]:
        return self._request(
            "POST",
            f"/api/v1/sweep-models/{sweep_model_id}/finish",
            json=body.model_dump(mode="json"),
            params={"worker_name": worker_name},
        )

    def release_model_run(self, sweep_model_id: int, worker_name: str, reason: str) -> None:
        self._request(
            "POST",
            f"/api/v1/sweep-models/{sweep_model_id}/release",
            json={"worker_name": worker_name, "reason": reason},
        )
