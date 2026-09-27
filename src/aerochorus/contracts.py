"""Wire contracts between the control plane (API) and workers.

Workers depend on this module and on ``aerochorus.corpus``; they never import
the database layer. Keep this module free of SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aerochorus.corpus.config import FilesystemAdapterConfig
from aerochorus.corpus.paths import normalize_relative_dir, normalize_relative_path


class TemporalStatus(StrEnum):
    # Filename time converted with the source timezone and corroborated by mtime.
    RESOLVED = "resolved"
    # Filename time converted with the source timezone, but not corroborated.
    UNVERIFIED = "unverified"
    # DST fold that the evidence cannot disambiguate. No UTC is recorded.
    AMBIGUOUS = "ambiguous"
    # No trustworthy time. No UTC is recorded.
    UNRESOLVED = "unresolved"


class PresenceStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"


class IntegrityStatus(StrEnum):
    OK = "ok"
    # Content hash differed from an earlier observation of the same path.
    CHANGED = "changed"


class ScanMode(StrEnum):
    # Skip directories whose mtime is unchanged since a settled listing.
    INCREMENTAL = "incremental"
    # List every directory; read only new or stat-changed files.
    FULL = "full"
    # List every directory and re-hash every file.
    VERIFY = "verify"


class ScanStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    SOURCE_UNAVAILABLE = "source_unavailable"
    FAILED = "failed"
    ABANDONED = "abandoned"


# --- sources ---------------------------------------------------------------


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    name: str = Field(min_length=1)
    adapter_type: str = "filesystem"
    adapter_config: FilesystemAdapterConfig = Field(default_factory=FilesystemAdapterConfig)

    @field_validator("adapter_type")
    @classmethod
    def _adapter(cls, value: str) -> str:
        if value != "filesystem":
            raise ValueError("only the 'filesystem' adapter exists")
        return value


class SourceRead(BaseModel):
    logical_key: str
    name: str
    read_only: bool
    adapter_type: str
    adapter_config: FilesystemAdapterConfig
    created_at: datetime


class DirectoryState(BaseModel):
    relative_dir: str
    mtime_ns: int
    settled: bool
    subdirs: list[str]
    file_count: int


class SegmentStat(BaseModel):
    relative_path: str
    file_size: int
    file_mtime_ns: int
    presence_status: PresenceStatus


class SegmentRead(BaseModel):
    id: int
    source_key: str
    relative_path: str
    capture_start_utc: datetime | None
    capture_end_utc: datetime | None
    temporal_status: TemporalStatus
    duration_ms: int | None
    file_size: int
    file_mtime: datetime
    sha256: str | None
    frequency_hz: int | None
    station: str | None
    channel: str | None
    presence_status: PresenceStatus
    integrity_status: IntegrityStatus
    metadata: dict[str, Any]
    first_seen_at: datetime
    last_seen_at: datetime


class DaySummary(BaseModel):
    day_utc: str
    segments: int
    audio_seconds: float


class SourceSummary(BaseModel):
    source_key: str
    segments: int
    audio_hours: float
    by_presence: dict[str, int]
    by_temporal_status: dict[str, int]
    by_integrity: dict[str, int]
    by_utc_day: list[DaySummary]


# --- observations ----------------------------------------------------------


class AudioProbe(BaseModel):
    format: str | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bitrate: int | None = None
    error: str | None = None


class FileObservation(BaseModel):
    """Raw facts a worker observed about one file. No interpretation."""

    relative_path: str
    file_size: int = Field(ge=0)
    file_mtime_ns: int
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    audio: AudioProbe = Field(default_factory=AudioProbe)

    @field_validator("relative_path")
    @classmethod
    def _v_path(cls, value: str) -> str:
        return normalize_relative_path(value)


class DirectoryBatch(BaseModel):
    """Part of one directory listing. The last batch for a directory is final."""

    relative_dir: str = ""
    observations: list[FileObservation] = Field(default_factory=list)
    # Paths confirmed present without re-reading (stat unchanged, or still
    # being written, or unreadable this time).
    seen: list[str] = Field(default_factory=list)
    final: bool = False
    # Only meaningful when final.
    directory_mtime_ns: int | None = None
    settled: bool = False
    subdirs: list[str] = Field(default_factory=list)
    file_count: int = 0

    @field_validator("relative_dir")
    @classmethod
    def _v_dir(cls, value: str) -> str:
        return normalize_relative_dir(value)

    @field_validator("seen")
    @classmethod
    def _v_seen(cls, value: list[str]) -> list[str]:
        return [normalize_relative_path(v) for v in value]

    @field_validator("subdirs")
    @classmethod
    def _v_subdirs(cls, value: list[str]) -> list[str]:
        names = [normalize_relative_path(v) for v in value]
        if any("/" in name for name in names):
            raise ValueError("subdirs must be single path components")
        return names


class BatchResult(BaseModel):
    inserted: int
    updated: int
    content_changed: int
    seen: int
    reappeared: int
    marked_missing: int


# --- scans -----------------------------------------------------------------


class ScanCreate(BaseModel):
    source_key: str
    worker_name: str
    mode: ScanMode = ScanMode.INCREMENTAL
    scope_prefix: str = ""

    @field_validator("scope_prefix")
    @classmethod
    def _v_scope(cls, value: str) -> str:
        return normalize_relative_dir(value)


class ScanFinish(BaseModel):
    status: ScanStatus
    error_message: str | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def _terminal(cls, value: ScanStatus) -> ScanStatus:
        if value in (ScanStatus.RUNNING, ScanStatus.ABANDONED):
            raise ValueError("finish status must be terminal and worker-reported")
        return value


class ScanRead(BaseModel):
    id: int
    source_key: str
    worker_name: str | None
    mode: ScanMode
    scope_prefix: str
    status: ScanStatus
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    counters: dict[str, int]
    errors: list[str]
    error_message: str | None


# --- workers ---------------------------------------------------------------


class WorkerHeartbeat(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    hostname: str
    platform: str
    version: str
    health: dict[str, Any] = Field(default_factory=dict)


class WorkerRead(BaseModel):
    name: str
    hostname: str
    platform: str
    version: str
    health: dict[str, Any]
    last_heartbeat_at: datetime


class HealthRead(BaseModel):
    status: str
    version: str
    database: dict[str, Any]
