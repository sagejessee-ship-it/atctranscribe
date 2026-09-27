"""Wire contracts for transcription: model registry, sweeps and results.

Shared by the control plane and workers, like ``aerochorus.contracts``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aerochorus.corpus.paths import normalize_relative_dir

_SHA256 = r"^[0-9a-f]{64}$"
_SLUG = r"^[a-z][a-z0-9_.-]*$"


class ModelRunStatus(StrEnum):
    QUEUED = "queued"
    LOADING = "loading"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class SweepStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    # Every model finished, but at least one model run failed.
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class ResultStatus(StrEnum):
    SUCCESS = "success"
    # The model produced no words. Not an error, and not disagreement.
    ABSTAINED = "abstained"
    ERROR = "error"


# --- model registry -------------------------------------------------------------


class CatalogFamily(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    display_name: str
    description: str | None = None


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    logical_name: str = Field(pattern=_SLUG)
    architecture_family: str
    crisp_backend: str
    upstream_model: str
    artifact_repo: str
    upstream_revision: str
    model_filename: str
    model_sha256: str = Field(pattern=_SHA256)
    artifact_size_bytes: int = Field(gt=0)
    quantization: str | None = None
    language: str | None = None
    request_params: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    pedigree: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    experimental: bool = True

    @property
    def artifact_uri(self) -> str:
        return (
            f"https://huggingface.co/{self.artifact_repo}/resolve/"
            f"{self.upstream_revision}/{self.model_filename}"
        )


class CatalogSuite(BaseModel):
    name: str = Field(pattern=_SLUG)
    description: str | None = None
    models: list[str] = Field(min_length=1)


class CatalogSync(BaseModel):
    families: list[CatalogFamily] = Field(default_factory=list)
    models: list[CatalogModel] = Field(default_factory=list)
    suites: list[CatalogSuite] = Field(default_factory=list)


class CatalogSyncResult(BaseModel):
    families_created: list[str]
    models_created: list[str]
    models_unchanged: list[str]
    suites_written: list[str]


class ModelRead(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    logical_name: str
    architecture_family: str
    crisp_backend: str
    model_filename: str
    model_sha256: str | None
    artifact_uri: str | None
    artifact_size_bytes: int | None
    upstream_model: str | None
    upstream_revision: str | None
    quantization: str | None
    language: str | None
    request_params: dict[str, str]
    capabilities: dict[str, Any]
    pedigree: dict[str, Any]
    enabled: bool
    sweep_eligible: bool
    experimental: bool


class ModelUpdate(BaseModel):
    enabled: bool | None = None
    sweep_eligible: bool | None = None
    experimental: bool | None = None


class SuiteRead(BaseModel):
    name: str
    description: str | None
    models: list[str]


class SuiteWrite(BaseModel):
    description: str | None = None
    models: list[str] = Field(min_length=1)


# --- sweeps -------------------------------------------------------------------------


class SweepSelection(BaseModel):
    """Which segments a sweep covers. Frozen into the run when it is created."""

    model_config = ConfigDict(extra="forbid")

    source_key: str
    # Half-open UTC window on capture_start_utc (needs resolved/unverified UTC).
    utc_from: datetime | None = None
    utc_to: datetime | None = None
    # A source directory, e.g. "2026/09/08" (direct children only).
    relative_dir: str | None = None
    channels: list[str] = Field(default_factory=list)
    min_duration_ms: int | None = Field(default=None, ge=0)
    max_duration_ms: int | None = Field(default=None, ge=0)
    # Deterministic pseudo-random sample of at most `limit` segments.
    limit: int | None = Field(default=None, ge=1)
    seed: int = 0

    @field_validator("relative_dir")
    @classmethod
    def _v_dir(cls, value: str | None) -> str | None:
        return None if value is None else normalize_relative_dir(value)


class SweepCreate(BaseModel):
    suite: str
    selection: SweepSelection
    name: str | None = None
    # Extra CrispASR request fields applied to every model (e.g. temperature).
    request_overrides: dict[str, str] = Field(default_factory=dict)


class SweepModelRead(BaseModel):
    id: int
    logical_name: str
    architecture_family: str
    execution_order: int
    status: ModelRunStatus
    attempts: int
    claimed_by: str | None
    segments_total: int
    segments_completed: int
    segments_abstained: int
    segments_error: int
    audio_ms_total: int
    inference_ms_total: int
    real_time_factor: float | None
    runtime: dict[str, Any]
    last_error: str | None
    started_at: datetime | None
    completed_at: datetime | None


class SweepRead(BaseModel):
    id: int
    name: str | None
    status: SweepStatus
    suite: str
    selection: dict[str, Any]
    config_sha256: str
    segments_total: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    models: list[SweepModelRead]


# --- worker protocol ------------------------------------------------------------------


class ClaimRequest(BaseModel):
    worker_name: str
    lease_seconds: int = Field(default=600, ge=30, le=86_400)
    # Corpus sources this worker can read. Sweeps over other sources are never
    # offered to it (e.g. the ATCO2 benchmark exists only on the 5080 box).
    source_keys: list[str] | None = None


class ModelRunClaim(BaseModel):
    sweep_model_id: int
    sweep_id: int
    attempt: int
    model: ModelRead
    request_params: dict[str, str]
    segments_total: int
    results_recorded: int
    # Fingerprint recorded when this model run first started. A resumed run
    # must present the same one (same CrispASR build, same model artifact).
    runtime_fingerprint: str | None


class ModelRunStart(BaseModel):
    worker_name: str
    runtime: dict[str, Any]
    runtime_fingerprint: str = Field(pattern=_SHA256)
    lease_seconds: int = Field(default=600, ge=30, le=86_400)


class PendingSegment(BaseModel):
    segment_id: int
    source_key: str
    relative_path: str
    sha256: str | None
    duration_ms: int | None


class PendingBatch(BaseModel):
    sweep_status: SweepStatus
    model_status: ModelRunStatus
    segments: list[PendingSegment]


class ResultPost(BaseModel):
    id: UUID
    segment_id: int
    status: ResultStatus
    text: str | None = None
    language: str | None = None
    audio_ms: int | None = Field(default=None, ge=0)
    inference_ms: int | None = Field(default=None, ge=0)
    has_word_timestamps: bool = False
    has_token_confidence: bool = False
    mean_token_confidence: float | None = None
    word_count: int | None = None
    artifact_uri: str | None = None
    artifact_sha256: str | None = Field(default=None, pattern=_SHA256)
    artifact_size_bytes: int | None = None
    audio_sha256: str | None = Field(default=None, pattern=_SHA256)
    error_type: str | None = None
    error_message: str | None = None
    lease_seconds: int = Field(default=600, ge=30, le=86_400)


class ResultAck(BaseModel):
    replaced_error: bool
    segments_recorded: int
    segments_total: int


class ModelRunFinish(BaseModel):
    status: ModelRunStatus
    error_message: str | None = None

    @field_validator("status")
    @classmethod
    def _terminal(cls, value: ModelRunStatus) -> ModelRunStatus:
        if value not in (ModelRunStatus.COMPLETED, ModelRunStatus.FAILED):
            raise ValueError("finish status must be completed or failed")
        return value


# --- inspection -----------------------------------------------------------------------


class TranscriptionRead(BaseModel):
    id: UUID
    sweep_id: int
    model: str
    architecture_family: str
    segment_id: int
    relative_path: str
    status: ResultStatus
    text: str | None
    language: str | None
    audio_ms: int | None
    inference_ms: int | None
    has_word_timestamps: bool
    has_token_confidence: bool
    mean_token_confidence: float | None
    artifact_uri: str | None
    error_type: str | None
    error_message: str | None
    attempt: int
    created_at: datetime


class SweepReportModel(BaseModel):
    logical_name: str
    architecture_family: str
    status: ModelRunStatus
    results: int
    success: int
    abstained: int
    error: int
    abstain_rate: float | None
    error_rate: float | None
    real_time_factor: float | None
    mean_inference_ms: float | None
    word_timestamp_rate: float | None
    mean_token_confidence: float | None
    # Results whose reported language differs from the requested one.
    language_drift: int
    mean_chars: float | None
    # Counts of deterministic quality flags (aerochorus.atc.quality).
    flags: dict[str, int] = Field(default_factory=dict)


class SweepReport(BaseModel):
    sweep_id: int
    segments_total: int
    models: list[SweepReportModel]


class ReflagResult(BaseModel):
    flags_version: int
    reflagged: int
