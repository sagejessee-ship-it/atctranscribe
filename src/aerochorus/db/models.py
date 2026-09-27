"""Authoritative schema (ADR-003).

Status columns are TEXT with CHECK constraints generated from the enums in
``aerochorus.contracts``, so one Python definition drives both the wire format
and the database.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aerochorus.contracts import (
    IntegrityStatus,
    PresenceStatus,
    ScanMode,
    ScanStatus,
    TemporalStatus,
)
from aerochorus.db.base import Base
from aerochorus.review_contracts import ReviewStatus, TextOrigin, TrainingLabel
from aerochorus.sweep_contracts import ModelRunStatus, ResultStatus, SweepStatus

_EMPTY_OBJECT = text("'{}'::jsonb")
_FALSE = text("false")
_EMPTY_ARRAY = text("'[]'::jsonb")

# A source-relative path: non-empty, '/'-separated, no absolute prefix, no
# backslashes, no empty/'.'/'..' components.
_REL_PATH_RULE = (
    r"{col} <> '' AND left({col}, 1) <> '/' AND strpos({col}, '\') = 0"
    r" AND {col} !~ '(^|/)\.{{0,2}}(/|$)'"
)


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


# --- corpus ----------------------------------------------------------------


class CorpusSource(Base):
    """An external corpus location, identified logically (ADR-002)."""

    __tablename__ = "corpus_source"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    logical_key: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    read_only: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    adapter_type: Mapped[str] = mapped_column(Text)
    adapter_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    # "benchmark" sources (e.g. ATCO2 clips) are evaluation-only: hidden from
    # review by default and never eligible for training labels or export.
    role: Mapped[str] = mapped_column(Text, server_default=text("'corpus'"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # AeroChorus never takes ownership of source audio.
        CheckConstraint("read_only", name="read_only"),
        CheckConstraint("logical_key ~ '^[a-z][a-z0-9_]*$'", name="logical_key_format"),
        CheckConstraint("adapter_type IN ('filesystem')", name="adapter_type"),
        CheckConstraint("role IN ('corpus', 'benchmark')", name="role"),
    )


class Segment(Base):
    """One immutable source audio unit."""

    __tablename__ = "segment"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("corpus_source.id"))
    relative_path: Mapped[str] = mapped_column(Text)
    relative_dir: Mapped[str] = mapped_column(Text)
    source_recording_id: Mapped[str | None] = mapped_column(Text)

    capture_start_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capture_end_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporal_status: Mapped[str] = mapped_column(Text)

    duration_ms: Mapped[int | None] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Exact mtime for change detection; timestamptz only has microseconds.
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(Text)

    frequency_hz: Mapped[int | None] = mapped_column(BigInteger)
    channel: Mapped[str | None] = mapped_column(Text)
    station: Mapped[str | None] = mapped_column(Text)

    presence_status: Mapped[str] = mapped_column(
        Text, server_default=text(f"'{PresenceStatus.PRESENT.value}'")
    )
    integrity_status: Mapped[str] = mapped_column(
        Text, server_default=text(f"'{IntegrityStatus.OK.value}'")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=_EMPTY_OBJECT
    )

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_scan_id: Mapped[int | None] = mapped_column(ForeignKey("corpus_scan.id"))

    source: Mapped[CorpusSource] = relationship()

    __table_args__ = (
        UniqueConstraint("source_id", "relative_path"),
        Index("ix_segment_source_dir", "source_id", "relative_dir"),
        Index("ix_segment_capture_start_utc", "capture_start_utc"),
        Index("ix_segment_source_channel_utc", "source_id", "channel", "capture_start_utc"),
        CheckConstraint(_REL_PATH_RULE.format(col="relative_path"), name="relative_path_format"),
        CheckConstraint(
            "(relative_dir = '' AND strpos(relative_path, '/') = 0)"
            " OR (left(relative_path, length(relative_dir) + 1) = relative_dir || '/'"
            " AND strpos(substr(relative_path, length(relative_dir) + 2), '/') = 0)",
            name="relative_dir_is_parent",
        ),
        CheckConstraint(_in("temporal_status", TemporalStatus), name="temporal_status"),
        # ADR-010: never record a UTC time the evidence does not support.
        CheckConstraint(
            "(capture_start_utc IS NOT NULL) = (temporal_status IN ('resolved', 'unverified'))",
            name="utc_matches_temporal_status",
        ),
        CheckConstraint(
            "capture_end_utc IS NULL"
            " OR (capture_start_utc IS NOT NULL AND capture_end_utc >= capture_start_utc)",
            name="capture_interval",
        ),
        CheckConstraint(_in("presence_status", PresenceStatus), name="presence_status"),
        CheckConstraint(_in("integrity_status", IntegrityStatus), name="integrity_status"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint("file_size >= 0", name="file_size"),
        CheckConstraint("duration_ms >= 0", name="duration_ms"),
    )


class CorpusDirectory(Base):
    """Last settled listing of a source directory, for incremental scans."""

    __tablename__ = "corpus_directory"

    source_id: Mapped[int] = mapped_column(ForeignKey("corpus_source.id"), primary_key=True)
    relative_dir: Mapped[str] = mapped_column(Text, primary_key=True)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    settled: Mapped[bool] = mapped_column(Boolean)
    file_count: Mapped[int] = mapped_column(Integer)
    subdirs: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)
    last_scan_id: Mapped[int] = mapped_column(ForeignKey("corpus_scan.id"))
    last_listed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CorpusScan(Base):
    __tablename__ = "corpus_scan"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("corpus_source.id"))
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("worker.id"))
    mode: Mapped[str] = mapped_column(Text)
    scope_prefix: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counters: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    errors: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)
    error_message: Mapped[str | None] = mapped_column(Text)

    source: Mapped[CorpusSource] = relationship()
    worker: Mapped[Worker | None] = relationship()

    __table_args__ = (
        CheckConstraint(_in("mode", ScanMode), name="mode"),
        CheckConstraint(_in("status", ScanStatus), name="status"),
        # At most one running scan per source.
        Index(
            "uq_corpus_scan_one_running",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )


class Worker(Base):
    """A native processing host that reports to the control plane."""

    __tablename__ = "worker"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    hostname: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


# --- model registry (ADR-008, ADR-009) --------------------------------------


class ArchitectureFamily(Base):
    """The one and only architecture-family registry.

    Anything that reasons about model independence must use
    ``model.architecture_family``. No other family mapping may exist.
    """

    __tablename__ = "architecture_family"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("key ~ '^[a-z][a-z0-9_-]*$'", name="key_format"),)


class Model(Base):
    __tablename__ = "model"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    logical_name: Mapped[str] = mapped_column(Text, unique=True)
    architecture_family: Mapped[str] = mapped_column(ForeignKey("architecture_family.key"))
    crisp_backend: Mapped[str] = mapped_column(Text)

    model_filename: Mapped[str] = mapped_column(Text)
    model_sha256: Mapped[str | None] = mapped_column(Text)
    upstream_model: Mapped[str | None] = mapped_column(Text)
    upstream_revision: Mapped[str | None] = mapped_column(Text)
    quantization: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)

    # Where the pinned artifact can be downloaded (verified against model_sha256).
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # CrispASR request fields this model always needs (language, source_lang, ...).
    request_params: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    # word_timestamps, token_confidence, diarization, metal, ...
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    # training data, benchmark contamination, licence, lineage of fine-tunes...
    pedigree: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    # Re-incorporation gate evidence (ADR-019): smoke, usability, regression.
    qualification: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)

    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sweep_eligible: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    experimental: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("model_sha256 ~ '^[0-9a-f]{64}$'", name="model_sha256_format"),
        # Provenance: a model cannot join a sweep without a pinned artifact.
        CheckConstraint(
            "NOT sweep_eligible OR model_sha256 IS NOT NULL", name="sweep_requires_sha256"
        ),
    )


class ModelSuite(Base):
    __tablename__ = "model_suite"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("name ~ '^[a-z][a-z0-9_.-]*$'", name="name_format"),)


class ModelSuiteMember(Base):
    __tablename__ = "model_suite_member"

    suite_id: Mapped[int] = mapped_column(ForeignKey("model_suite.id"), primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("model.id"), primary_key=True)
    execution_order: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("suite_id", "execution_order"),
        CheckConstraint("execution_order >= 0", name="execution_order"),
    )


# --- sweeps and results (ADR-006, ADR-007) -----------------------------------------


class SweepRun(Base):
    """corpus selection x model suite x configuration."""

    __tablename__ = "sweep_run"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    suite_id: Mapped[int] = mapped_column(ForeignKey("model_suite.id"))
    status: Mapped[str] = mapped_column(Text)
    selection_definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    effective_config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_sha256: Mapped[str] = mapped_column(Text)
    segments_total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    suite: Mapped[ModelSuite] = relationship()

    __table_args__ = (
        CheckConstraint(_in("status", SweepStatus), name="status"),
        CheckConstraint("config_sha256 ~ '^[0-9a-f]{64}$'", name="config_sha256_format"),
    )


class SweepRunSegment(Base):
    """The run's selection, frozen at creation so 'missing work' is well defined."""

    __tablename__ = "sweep_run_segment"

    run_id: Mapped[int] = mapped_column(ForeignKey("sweep_run.id"), primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("segment.id"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("run_id", "ordinal"),)


class SweepRunModel(Base):
    """One model's execution within one sweep run."""

    __tablename__ = "sweep_run_model"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sweep_run.id"))
    model_id: Mapped[int] = mapped_column(ForeignKey("model.id"))
    execution_order: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    claimed_by: Mapped[int | None] = mapped_column(ForeignKey("worker.id"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    segments_total: Mapped[int] = mapped_column(Integer)
    segments_completed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    segments_abstained: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    segments_error: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    audio_ms_total: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    inference_ms_total: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))

    # CrispASR build, launcher, device... recorded when the run first starts.
    runtime: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    runtime_fingerprint: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[SweepRun] = relationship()
    model: Mapped[Model] = relationship()
    worker: Mapped[Worker | None] = relationship()

    __table_args__ = (
        UniqueConstraint("run_id", "model_id"),
        UniqueConstraint("run_id", "execution_order"),
        CheckConstraint(_in("status", ModelRunStatus), name="status"),
    )


class TranscriptionResult(Base):
    """Exactly one result per (sweep_run_model, segment)."""

    __tablename__ = "transcription_result"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    sweep_run_model_id: Mapped[int] = mapped_column(ForeignKey("sweep_run_model.id"))
    segment_id: Mapped[int] = mapped_column(ForeignKey("segment.id"))
    status: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer)

    text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    audio_ms: Mapped[int | None] = mapped_column(Integer)
    inference_ms: Mapped[int | None] = mapped_column(Integer)
    has_word_timestamps: Mapped[bool] = mapped_column(Boolean, server_default=_FALSE)
    has_token_confidence: Mapped[bool] = mapped_column(Boolean, server_default=_FALSE)
    mean_token_confidence: Mapped[float | None] = mapped_column(Double)
    word_count: Mapped[int | None] = mapped_column(Integer)

    # Raw CrispASR output, stored once (ADR-004).
    artifact_uri: Mapped[str | None] = mapped_column(Text)
    artifact_sha256: Mapped[str | None] = mapped_column(Text)
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Hash of the audio bytes actually sent to the model.
    audio_sha256: Mapped[str | None] = mapped_column(Text)

    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Deterministic quality flags computed on ingest (aerochorus.atc.quality).
    quality: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    created_at: Mapped[datetime] = _created_at()

    sweep_run_model: Mapped[SweepRunModel] = relationship()
    segment: Mapped[Segment] = relationship()

    __table_args__ = (
        UniqueConstraint("sweep_run_model_id", "segment_id"),
        Index("ix_transcription_result_segment", "segment_id"),
        Index(
            "ix_transcription_result_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
        CheckConstraint(_in("status", ResultStatus), name="status"),
        # An empty transcript is an abstention, never an error; errors carry a type.
        CheckConstraint(
            "(status = 'success' AND text IS NOT NULL AND btrim(text) <> '')"
            " OR (status = 'abstained' AND coalesce(btrim(text), '') = '')"
            " OR (status = 'error' AND error_type IS NOT NULL)",
            name="status_semantics",
        ),
        CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="artifact_sha256_format"),
        CheckConstraint("audio_sha256 ~ '^[0-9a-f]{64}$'", name="audio_sha256_format"),
    )


# --- evaluation: gold references (ADR-015) ---------------------------------------------


class GoldSegment(Base):
    """Human reference transcript for one benchmark clip.

    Lives in the separate ``reference`` schema. Only the evaluation service
    reads it; nothing on the transcription path (worker, CrispASR, sweep
    protocol) can see it. Gold is immutable once imported.
    """

    __tablename__ = "gold_segment"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("corpus_source.id"))
    relative_path: Mapped[str] = mapped_column(Text)
    dataset: Mapped[str] = mapped_column(Text)
    recording_id: Mapped[str] = mapped_column(Text)
    source_segment_index: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str | None] = mapped_column(Text)
    speaker_label: Mapped[str | None] = mapped_column(Text)
    text_raw: Mapped[str] = mapped_column(Text)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    split: Mapped[str] = mapped_column(Text)
    split_source: Mapped[str] = mapped_column(Text)
    non_english: Mapped[bool] = mapped_column(Boolean, server_default=_FALSE)
    imported_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("source_id", "relative_path"),
        CheckConstraint("split IN ('calibration', 'test', 'train', 'dev')", name="split"),
        CheckConstraint("end_ms > start_ms", name="interval"),
        {"schema": "reference"},
    )


# --- review workbench (Phase 5, ADR-016/017) --------------------------------------------


class SegmentAgreement(Base):
    """Derived, versioned agreement over the latest result of each model.

    Recomputed whenever a result arrives (and by ``aerochorus agreement
    refresh``). Exact groups use normalize_for_evidence; near groups use
    order-aware LCS similarity on evidence tokens. Provider and family counts
    are separate; families come only from the model registry.
    """

    __tablename__ = "segment_agreement"

    segment_id: Mapped[int] = mapped_column(ForeignKey("segment.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    near_threshold: Mapped[float] = mapped_column(Double)
    results_count: Mapped[int] = mapped_column(Integer)
    success_count: Mapped[int] = mapped_column(Integer)
    abstained_count: Mapped[int] = mapped_column(Integer)
    error_count: Mapped[int] = mapped_column(Integer)
    models: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)
    families: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)
    best_exact_provider_count: Mapped[int] = mapped_column(Integer)
    best_exact_family_count: Mapped[int] = mapped_column(Integer)
    best_near_family_count: Mapped[int] = mapped_column(Integer)
    best_near_similarity: Mapped[float | None] = mapped_column(Double)
    max_pair_similarity: Mapped[float | None] = mapped_column(Double)
    representative_text: Mapped[str | None] = mapped_column(Text)
    representative_source: Mapped[str | None] = mapped_column(Text)
    exact_groups: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)
    near_group: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    flags: Mapped[dict[str, int]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_segment_agreement_exact_families", "best_exact_family_count"),
        Index("ix_segment_agreement_exact_providers", "best_exact_provider_count"),
        Index("ix_segment_agreement_near_families", "best_near_family_count"),
        Index("ix_segment_agreement_results", "results_count"),
        Index(
            "ix_segment_agreement_representative_trgm",
            "representative_text",
            postgresql_using="gin",
            postgresql_ops={"representative_text": "gin_trgm_ops"},
        ),
        Index("ix_segment_agreement_flags", "flags", postgresql_using="gin"),
    )


class AnnotationThread(Base):
    """One annotated object: the whole segment, or one bounded span of it."""

    __tablename__ = "annotation_thread"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("segment.id"))
    scope: Mapped[str] = mapped_column(Text)
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("annotation_version.id", use_alter=True)
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("scope IN ('segment', 'span')", name="scope"),
        Index("ix_annotation_thread_segment", "segment_id"),
        # At most one whole-segment thread per segment.
        Index(
            "uq_annotation_thread_one_segment_scope",
            "segment_id",
            unique=True,
            postgresql_where=text("scope = 'segment'"),
        ),
    )


class AnnotationVersion(Base):
    """Append-only annotation history. The thread points at its current version."""

    __tablename__ = "annotation_version"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("annotation_thread.id"))
    version: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    text_origin: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(Text)
    training_label: Mapped[str] = mapped_column(Text)
    reason_tags: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)
    notes: Mapped[str | None] = mapped_column(Text)
    annotator: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    basis: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    created_at: Mapped[datetime] = _created_at()

    thread: Mapped[AnnotationThread] = relationship(foreign_keys=[thread_id])

    __table_args__ = (
        UniqueConstraint("thread_id", "version"),
        CheckConstraint(_in("review_status", ReviewStatus), name="review_status"),
        CheckConstraint(_in("training_label", TrainingLabel), name="training_label"),
        CheckConstraint(
            "text_origin IS NULL OR " + _in("text_origin", TextOrigin), name="text_origin"
        ),
        CheckConstraint("(start_ms IS NULL) = (end_ms IS NULL)", name="span_bounds_paired"),
        CheckConstraint("end_ms IS NULL OR end_ms > start_ms", name="span_bounds_order"),
        # Gold is a human assertion with text (ADR-017).
        CheckConstraint(
            "training_label <> 'gold' OR (text_origin = 'human' AND coalesce(btrim(text), '') <> ''"
            " AND review_status IN ('reviewed', 'corrected'))",
            name="gold_is_human",
        ),
        Index(
            "ix_annotation_version_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
    )


class ReviewSample(Base):
    """A reproducible QC sample: filter definition + seed + the chosen ids."""

    __tablename__ = "review_sample"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    filter_sha256: Mapped[str] = mapped_column(Text)
    seed: Mapped[int] = mapped_column(BigInteger)
    n: Mapped[int] = mapped_column(Integer)
    total_matching: Mapped[int] = mapped_column(Integer)
    segment_ids: Mapped[list[int]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


# --- airport context (generic; KBWI bootstrapped from FAA NASR) ---------------------------


class Airport(Base):
    __tablename__ = "airport"

    icao: Mapped[str] = mapped_column(Text, primary_key=True)
    faa_id: Mapped[str | None] = mapped_column(Text)
    iata: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    elevation_ft: Mapped[float | None] = mapped_column(Double)
    magnetic_variation: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("icao ~ '^[A-Z0-9]{4}$'", name="icao_format"),)


class AirportRunway(Base):
    __tablename__ = "airport_runway"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    icao: Mapped[str] = mapped_column(ForeignKey("airport.icao", ondelete="CASCADE"))
    pair: Mapped[str] = mapped_column(Text)
    end_ident: Mapped[str] = mapped_column(Text)
    length_ft: Mapped[int | None] = mapped_column(Integer)
    width_ft: Mapped[int | None] = mapped_column(Integer)
    true_alignment: Mapped[float | None] = mapped_column(Double)
    spoken: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)

    __table_args__ = (UniqueConstraint("icao", "end_ident"),)


class AirportFrequency(Base):
    __tablename__ = "airport_frequency"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    icao: Mapped[str] = mapped_column(ForeignKey("airport.icao", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(Text)
    frequency_hz: Mapped[int] = mapped_column(BigInteger)
    facility: Mapped[str | None] = mapped_column(Text)
    call: Mapped[str | None] = mapped_column(Text)
    sectorization: Mapped[str | None] = mapped_column(Text)
    spoken: Mapped[list[str]] = mapped_column(JSONB, server_default=_EMPTY_ARRAY)

    __table_args__ = (Index("ix_airport_frequency_hz", "icao", "frequency_hz"),)


class AirportAlias(Base):
    """Identifiers and spoken names. kind='station' maps collector labels (BWI) to ICAO."""

    __tablename__ = "airport_alias"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    icao: Mapped[str] = mapped_column(ForeignKey("airport.icao", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("icao", "alias", "kind"),
        CheckConstraint(
            "kind IN ('icao', 'faa', 'iata', 'station', 'name', 'spoken')", name="kind"
        ),
        Index("ix_airport_alias_lookup", "kind", "alias"),
    )


# --- training datasets (Phase 5B) ------------------------------------------------------------


class TrainingDataset(Base):
    """A frozen, versioned selection of human annotations (never edited after creation)."""

    __tablename__ = "training_dataset"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'frozen'"))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    definition_sha256: Mapped[str] = mapped_column(Text)
    manifest_sha256: Mapped[str] = mapped_column(Text)
    item_count: Mapped[int] = mapped_column(Integer)
    audio_ms_total: Mapped[int] = mapped_column(BigInteger)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_OBJECT)
    export: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("name", "version"),
        CheckConstraint("status IN ('frozen', 'exported')", name="status"),
        CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="manifest_sha256_format"),
    )


class TrainingDatasetItem(Base):
    """One example: an annotation version, frozen with its text, bounds and split."""

    __tablename__ = "training_dataset_item"

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("training_dataset.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    annotation_version_id: Mapped[int] = mapped_column(ForeignKey("annotation_version.id"))
    segment_id: Mapped[int] = mapped_column(ForeignKey("segment.id"))
    scope: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    training_label: Mapped[str] = mapped_column(Text)
    text_origin: Mapped[str | None] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(Text)
    split: Mapped[str] = mapped_column(Text)
    split_group: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    clip_path: Mapped[str | None] = mapped_column(Text)
    clip_sha256: Mapped[str | None] = mapped_column(Text)
    clip_bytes: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("dataset_id", "annotation_version_id"),
        CheckConstraint("split IN ('train', 'validation', 'test')", name="split"),
        CheckConstraint("scope IN ('segment', 'span')", name="scope"),
        CheckConstraint(
            "training_label IN ('gold', 'silver', 'candidate')", name="trainable_label"
        ),
        Index("ix_training_dataset_item_segment", "segment_id"),
    )
