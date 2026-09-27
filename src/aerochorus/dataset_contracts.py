"""Versioned training datasets (Phase 5B; docs/ui/TRAINING_DATASET_LIFECYCLE.md).

A dataset version is **frozen** by the control plane: the exact annotation
versions, texts, bounds, source hashes and split assignment are fixed at
creation and summarized by ``manifest_sha256``. It is **exported** by a machine
that mounts the corpus (``aerochorus dataset export``), which materializes clip
audio from the immutable parent segment + offsets and reports clip hashes back.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from aerochorus.review_contracts import ReviewFilters, TrainingLabel

# Labels a dataset may include. Rejected/none never; candidate only on purpose.
TRAINABLE = (TrainingLabel.GOLD, TrainingLabel.SILVER, TrainingLabel.CANDIDATE)


class DatasetStatus(StrEnum):
    FROZEN = "frozen"
    EXPORTED = "exported"


class SplitGrouping(StrEnum):
    # Neighbouring exchanges on one channel/day stay in one split (reduces leakage).
    UTC_DAY_CHANNEL = "utc_day_channel"
    UTC_DAY = "utc_day"
    SEGMENT = "segment"


class SplitPolicy(BaseModel):
    group_by: SplitGrouping = SplitGrouping.UTC_DAY_CHANNEL
    seed: int = 0
    train: float = Field(default=0.8, ge=0, le=1)
    validation: float = Field(default=0.1, ge=0, le=1)
    test: float = Field(default=0.1, ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self) -> SplitPolicy:
        if abs(self.train + self.validation + self.test - 1.0) > 1e-6:
            raise ValueError("train + validation + test must equal 1")
        return self


class DatasetCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    description: str | None = None
    labels: list[TrainingLabel] = Field(default_factory=lambda: [TrainingLabel.GOLD])
    scopes: list[Literal["segment", "span"]] = Field(default_factory=lambda: ["segment", "span"])
    # Optional extra constraint on the parent segments (e.g. channels, UTC range).
    filters: ReviewFilters | None = None
    split: SplitPolicy = Field(default_factory=SplitPolicy)
    # A span inside a segment that is itself included would duplicate audio.
    include_spans_of_included_segments: bool = False
    created_by: str | None = None

    @model_validator(mode="after")
    def _trainable(self) -> DatasetCreate:
        bad = [label for label in self.labels if label not in TRAINABLE]
        if bad or not self.labels:
            raise ValueError(f"labels must be a non-empty subset of {[t.value for t in TRAINABLE]}")
        if not self.scopes:
            raise ValueError("scopes must not be empty")
        return self


class DatasetItem(BaseModel):
    ordinal: int
    annotation_version_id: int
    segment_id: int
    scope: str
    start_ms: int | None
    end_ms: int | None
    text: str
    training_label: TrainingLabel
    text_origin: str | None
    source_key: str
    relative_path: str
    source_sha256: str | None
    split: str
    split_group: str
    duration_ms: int | None
    clip_path: str | None = None
    clip_sha256: str | None = None
    clip_bytes: int | None = None


class DatasetView(BaseModel):
    id: int
    name: str
    version: int
    description: str | None
    status: DatasetStatus
    created_at: datetime
    created_by: str | None
    definition: dict[str, Any]
    definition_sha256: str
    manifest_sha256: str
    item_count: int
    audio_ms_total: int
    counts: dict[str, dict[str, int]]
    export: dict[str, Any] | None


class ExportedClip(BaseModel):
    ordinal: int
    clip_path: str
    clip_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clip_bytes: int = Field(ge=44)
    duration_ms: int = Field(ge=0)


class ExportReport(BaseModel):
    exported_by: str
    out_uri: str
    audio_format: str
    tool: str
    manifest_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clips: list[ExportedClip]


class TrainingSummary(BaseModel):
    by_label: list[dict[str, Any]]  # label, scope, items, audio_ms
    by_channel: list[dict[str, Any]]  # channel, label, items
    by_day: list[dict[str, Any]]  # day, label, items
