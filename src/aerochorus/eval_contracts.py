"""Wire contracts for evaluation against gold references (ADR-015).

Everything here sits *below* the leakage line: these payloads carry gold text
and must never be produced or consumed by the transcription path.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from aerochorus.corpus.paths import normalize_relative_path


class GoldReferenceIn(BaseModel):
    relative_path: str
    dataset: str
    recording_id: str
    source_segment_index: int
    start_ms: int
    end_ms: int
    speaker: str | None = None
    speaker_label: str | None = None
    text_raw: str
    tags: dict[str, Any] = Field(default_factory=dict)
    split: str
    split_source: str
    non_english: bool = False

    @field_validator("relative_path")
    @classmethod
    def _v_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class ReferenceImport(BaseModel):
    source_key: str
    references: list[GoldReferenceIn]


class ReferenceImportResult(BaseModel):
    inserted: int
    unchanged: int
    conflicts: list[str]


class EntityScore(BaseModel):
    spans: int
    recovered: int
    recall: float | None


class EvalModelMetrics(BaseModel):
    logical_name: str
    architecture_family: str
    status: str
    # Excluded from best-single/oracle claims when trained on this benchmark.
    contaminated: bool
    segments: int
    success: int
    abstained: int
    error: int
    reference_tokens: int
    token_errors: int
    substitutions: int
    deletions: int
    insertions: int
    token_error_rate: float | None
    char_error_rate: float | None
    exact_match_rate: float | None
    entities: dict[str, EntityScore]
    flags: dict[str, int]
    real_time_factor: float | None


class AgreementBin(BaseModel):
    agreement_range: str
    segments: int
    oracle_ter: float | None
    best_single_ter: float | None


class EvalReport(BaseModel):
    sweep_id: int
    suite: str
    source_key: str
    canonical_numbers: bool
    english_only: bool
    split: str | None
    segments_in_sweep: int
    segments_scored: int
    segments_without_gold: int
    reference_tokens: int
    models: list[EvalModelMetrics]
    best_single: dict[str, Any] | None
    oracle: dict[str, Any] | None
    # Phase 6 fills this in; reported beside best-single and oracle from day one.
    ensemble: dict[str, Any] | None
    by_split: dict[str, dict[str, float | None]]
    agreement_bins: list[AgreementBin]
    # How often each quality flag fires on the human gold itself: the base
    # rate a model's flag count should be compared against.
    gold_flag_counts: dict[str, int] = Field(default_factory=dict)
    notes: list[str]


class EvalSegmentRow(BaseModel):
    segment_id: int
    relative_path: str
    split: str
    reference: str
    reference_scoring: str
    results: dict[str, dict[str, Any]]
