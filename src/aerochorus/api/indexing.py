"""Applying worker observations to the corpus index.

Invariants:
* one row per ``(source, relative_path)``; rescans update, never duplicate;
* segments are never deleted; unseen files become ``missing`` only after a
  complete listing of their directory within a running scan;
* a content hash that differs from an earlier observation is flagged
  ``integrity_status = changed`` and the previous facts are kept in metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aerochorus.contracts import (
    BatchResult,
    DirectoryBatch,
    IntegrityStatus,
    PresenceStatus,
    ScanCreate,
    ScanFinish,
    ScanStatus,
)
from aerochorus.corpus.config import FilesystemAdapterConfig
from aerochorus.corpus.derive import derive_segment_fields
from aerochorus.corpus.paths import is_within, parent_dir
from aerochorus.db.models import CorpusDirectory, CorpusScan, CorpusSource, Segment, Worker

MAX_STORED_ERRORS = 200


class ScanConflict(Exception):
    """Another scan is running, or this scan is no longer running."""


class InvalidBatch(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def _bump(scan: CorpusScan, increments: dict[str, int], now: datetime) -> None:
    counters = dict(scan.counters)
    for key, value in increments.items():
        counters[key] = counters.get(key, 0) + value
    scan.counters = counters
    scan.updated_at = now


def start_scan(
    session: Session, source: CorpusSource, worker: Worker, body: ScanCreate, stale_after_s: int
) -> CorpusScan:
    now = utcnow()
    session.execute(
        update(CorpusScan)
        .where(
            CorpusScan.source_id == source.id,
            CorpusScan.status == ScanStatus.RUNNING,
            CorpusScan.updated_at < now - timedelta(seconds=stale_after_s),
        )
        .values(
            status=ScanStatus.ABANDONED,
            finished_at=now,
            error_message="no progress reported before the stale timeout",
        )
    )
    scan = CorpusScan(
        source_id=source.id,
        worker_id=worker.id,
        mode=body.mode,
        scope_prefix=body.scope_prefix,
        status=ScanStatus.RUNNING,
        started_at=now,
        updated_at=now,
        counters={},
        errors=[],
    )
    source_key = source.logical_key
    session.add(scan)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ScanConflict(f"a scan of {source_key} is already running") from exc
    return scan


def apply_batch(session: Session, scan: CorpusScan, batch: DirectoryBatch) -> BatchResult:
    if scan.status != ScanStatus.RUNNING:
        raise ScanConflict(f"scan {scan.id} is {scan.status}")
    if not is_within(batch.relative_dir, scan.scope_prefix):
        raise InvalidBatch(f"{batch.relative_dir!r} is outside scan scope {scan.scope_prefix!r}")

    obs_paths = [o.relative_path for o in batch.observations]
    for path in (*obs_paths, *batch.seen):
        if parent_dir(path) != batch.relative_dir:
            raise InvalidBatch(f"{path!r} is not directly inside {batch.relative_dir!r}")
    if len(set(obs_paths) | set(batch.seen)) != len(obs_paths) + len(batch.seen):
        raise InvalidBatch("a path appears more than once in the batch")
    if batch.final and batch.directory_mtime_ns is None:
        raise InvalidBatch("final batch must carry directory_mtime_ns")

    source = scan.source
    config = FilesystemAdapterConfig.model_validate(source.adapter_config)
    now = utcnow()
    inserted = updated = content_changed = reappeared = seen = marked_missing = 0

    existing: dict[str, Segment] = {}
    if obs_paths:
        existing = {
            s.relative_path: s
            for s in session.scalars(
                select(Segment).where(
                    Segment.source_id == source.id, Segment.relative_path.in_(obs_paths)
                )
            )
        }

    new_rows = []
    for obs in batch.observations:
        fields = derive_segment_fields(obs, config)
        row = existing.get(obs.relative_path)
        if row is None:
            new_rows.append(
                fields
                | {
                    "source_id": source.id,
                    "presence_status": PresenceStatus.PRESENT,
                    "integrity_status": IntegrityStatus.OK,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_seen_scan_id": scan.id,
                }
            )
            continue

        metadata = fields.pop("metadata_")
        history = list(row.metadata_.get("integrity_history", []))
        integrity = row.integrity_status
        if row.sha256 and obs.sha256 and row.sha256 != obs.sha256:
            history.append(
                {
                    "observed_at": now.isoformat(),
                    "scan_id": scan.id,
                    "previous": {
                        "sha256": row.sha256,
                        "file_size": row.file_size,
                        "file_mtime_ns": row.file_mtime_ns,
                    },
                }
            )
            integrity = IntegrityStatus.CHANGED
            content_changed += 1
        if history:
            metadata["integrity_history"] = history
        if row.presence_status == PresenceStatus.MISSING:
            reappeared += 1

        for key, value in fields.items():
            setattr(row, key, value)
        row.metadata_ = metadata
        row.integrity_status = integrity
        row.presence_status = PresenceStatus.PRESENT
        row.last_seen_at = now
        row.last_seen_scan_id = scan.id
        updated += 1

    if new_rows:
        session.execute(pg_insert(Segment), new_rows)
        inserted = len(new_rows)
    session.flush()

    if batch.seen:
        in_seen = (Segment.source_id == source.id, Segment.relative_path.in_(batch.seen))
        reappeared += session.scalar(
            select(func.count()).where(*in_seen, Segment.presence_status == PresenceStatus.MISSING)
        )
        seen = session.execute(
            update(Segment)
            .where(*in_seen)
            .values(
                presence_status=PresenceStatus.PRESENT,
                last_seen_at=now,
                last_seen_scan_id=scan.id,
            )
            .execution_options(synchronize_session=False)
        ).rowcount

    if batch.final:
        marked_missing = session.execute(
            update(Segment)
            .where(
                Segment.source_id == source.id,
                Segment.relative_dir == batch.relative_dir,
                Segment.presence_status == PresenceStatus.PRESENT,
                or_(Segment.last_seen_scan_id.is_(None), Segment.last_seen_scan_id != scan.id),
            )
            .values(presence_status=PresenceStatus.MISSING)
            .execution_options(synchronize_session=False)
        ).rowcount
        state = {
            "mtime_ns": batch.directory_mtime_ns,
            "settled": batch.settled,
            "file_count": batch.file_count,
            "subdirs": sorted(batch.subdirs),
            "last_scan_id": scan.id,
            "last_listed_at": now,
        }
        session.execute(
            pg_insert(CorpusDirectory)
            .values(source_id=source.id, relative_dir=batch.relative_dir, **state)
            .on_conflict_do_update(index_elements=["source_id", "relative_dir"], set_=state)
        )

    result = BatchResult(
        inserted=inserted,
        updated=updated,
        content_changed=content_changed,
        seen=seen,
        reappeared=reappeared,
        marked_missing=marked_missing,
    )
    increments = {f"segments_{k}": v for k, v in result.model_dump().items()}
    increments["dirs_recorded"] = int(batch.final)
    _bump(scan, increments, now)
    return result


def finish_scan(scan: CorpusScan, body: ScanFinish) -> None:
    if scan.status != ScanStatus.RUNNING:
        raise ScanConflict(f"scan {scan.id} is already {scan.status}")
    now = utcnow()
    _bump(scan, body.counters, now)
    scan.errors = (list(scan.errors) + body.errors)[:MAX_STORED_ERRORS]
    scan.status = body.status
    scan.error_message = body.error_message
    scan.finished_at = now
