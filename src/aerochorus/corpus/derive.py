"""Interpret a raw file observation into segment fields.

Runs in the control plane, so derived fields can be recomputed from stored
observations (for example after a parser fix) without rescanning the source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aerochorus.contracts import FileObservation
from aerochorus.corpus.config import FilesystemAdapterConfig
from aerochorus.corpus.naming import parse_filename
from aerochorus.corpus.paths import basename, parent_dir
from aerochorus.corpus.temporal import resolve_capture_time


def mtime_from_ns(mtime_ns: int) -> datetime:
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1000)


def derive_segment_fields(obs: FileObservation, config: FilesystemAdapterConfig) -> dict[str, Any]:
    file_mtime = mtime_from_ns(obs.file_mtime_ns)
    parsed = parse_filename(config.filename_parser, basename(obs.relative_path))
    temporal = resolve_capture_time(
        wall_clock_start=parsed.wall_clock_start if parsed else None,
        timezone_name=config.filename_timezone,
        duration_ms=obs.audio.duration_ms,
        file_mtime_utc=file_mtime,
        tolerance_seconds=config.mtime_tolerance_seconds,
    )
    metadata: dict[str, Any] = {
        "audio": obs.audio.model_dump(exclude_none=True),
        "temporal": temporal.evidence,
    }
    if parsed is not None:
        metadata["naming"] = parsed.evidence()

    return {
        "relative_path": obs.relative_path,
        "relative_dir": parent_dir(obs.relative_path),
        "file_size": obs.file_size,
        "file_mtime": file_mtime,
        "file_mtime_ns": obs.file_mtime_ns,
        "sha256": obs.sha256,
        "duration_ms": obs.audio.duration_ms,
        "station": parsed.station if parsed else None,
        "channel": parsed.channel if parsed else None,
        "frequency_hz": parsed.frequency_hz if parsed else None,
        "temporal_status": temporal.status.value,
        "capture_start_utc": temporal.capture_start_utc,
        "capture_end_utc": temporal.capture_end_utc,
        "metadata_": metadata,
    }
