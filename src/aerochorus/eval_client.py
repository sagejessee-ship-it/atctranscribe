"""HTTP client for evaluation endpoints (below the leakage line).

Deliberately separate from ``aerochorus.worker.client`` so the transcription
path never imports anything that handles gold references.
"""

from __future__ import annotations

from pydantic import TypeAdapter

from aerochorus.eval_contracts import (
    EvalReport,
    EvalSegmentRow,
    GoldReferenceIn,
    ReferenceImport,
    ReferenceImportResult,
)
from aerochorus.sweep_contracts import ReflagResult
from aerochorus.worker.client import ApiClient


class EvalClient(ApiClient):
    def import_references(
        self, source_key: str, references: list[GoldReferenceIn], chunk: int = 500
    ) -> ReferenceImportResult:
        inserted = unchanged = 0
        for start in range(0, len(references), chunk):
            body = ReferenceImport(
                source_key=source_key, references=references[start : start + chunk]
            )
            data = self._request(
                "POST", "/api/v1/evaluation/references", json=body.model_dump(mode="json")
            )
            result = ReferenceImportResult.model_validate(data)
            inserted += result.inserted
            unchanged += result.unchanged
        return ReferenceImportResult(inserted=inserted, unchanged=unchanged, conflicts=[])

    def evaluate(
        self,
        sweep_id: int,
        *,
        canonical_numbers: bool = False,
        english_only: bool = False,
        split: str | None = None,
    ) -> EvalReport:
        params = {"canonical_numbers": canonical_numbers, "english_only": english_only}
        if split:
            params["split"] = split
        data = self._request("GET", f"/api/v1/evaluation/sweeps/{sweep_id}", params=params)
        return EvalReport.model_validate(data)

    def evaluation_segments(
        self, sweep_id: int, *, model: str | None = None, limit: int = 10, offset: int = 0
    ) -> list[EvalSegmentRow]:
        params: dict = {"limit": limit, "offset": offset}
        if model:
            params["model"] = model
        data = self._request("GET", f"/api/v1/evaluation/sweeps/{sweep_id}/segments", params=params)
        return TypeAdapter(list[EvalSegmentRow]).validate_python(data)

    def reflag(self) -> ReflagResult:
        return ReflagResult.model_validate(self._request("POST", "/api/v1/results/reflag"))
