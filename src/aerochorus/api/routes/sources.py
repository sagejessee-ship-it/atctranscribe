from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aerochorus.api.deps import SessionDep, load_source
from aerochorus.contracts import (
    DaySummary,
    DirectoryState,
    SegmentRead,
    SegmentStat,
    SourceCreate,
    SourceRead,
    SourceSummary,
)
from aerochorus.corpus.config import FilesystemAdapterConfig
from aerochorus.corpus.paths import InvalidRelativePath, normalize_relative_dir
from aerochorus.db.models import CorpusDirectory, CorpusSource, Segment

router = APIRouter(tags=["corpus"])


def source_read(source: CorpusSource) -> SourceRead:
    return SourceRead(
        logical_key=source.logical_key,
        name=source.name,
        read_only=source.read_only,
        adapter_type=source.adapter_type,
        adapter_config=FilesystemAdapterConfig.model_validate(source.adapter_config),
        created_at=source.created_at,
    )


def segment_read(segment: Segment, source_key: str) -> SegmentRead:
    return SegmentRead(
        id=segment.id,
        source_key=source_key,
        relative_path=segment.relative_path,
        capture_start_utc=segment.capture_start_utc,
        capture_end_utc=segment.capture_end_utc,
        temporal_status=segment.temporal_status,
        duration_ms=segment.duration_ms,
        file_size=segment.file_size,
        file_mtime=segment.file_mtime,
        sha256=segment.sha256,
        frequency_hz=segment.frequency_hz,
        station=segment.station,
        channel=segment.channel,
        presence_status=segment.presence_status,
        integrity_status=segment.integrity_status,
        metadata=segment.metadata_,
        first_seen_at=segment.first_seen_at,
        last_seen_at=segment.last_seen_at,
    )


def _dir_param(value: str) -> str:
    try:
        return normalize_relative_dir(value)
    except InvalidRelativePath as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sources", response_model=SourceRead, status_code=201)
def create_source(body: SourceCreate, session: SessionDep) -> SourceRead:
    source = CorpusSource(
        logical_key=body.logical_key,
        name=body.name,
        read_only=True,
        adapter_type=body.adapter_type,
        adapter_config=body.adapter_config.model_dump(mode="json"),
    )
    session.add(source)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"corpus source already exists: {body.logical_key}") from exc
    session.refresh(source)
    return source_read(source)


@router.get("/sources", response_model=list[SourceRead])
def list_sources(session: SessionDep) -> list[SourceRead]:
    sources = session.scalars(select(CorpusSource).order_by(CorpusSource.logical_key))
    return [source_read(s) for s in sources]


@router.get("/sources/{key}", response_model=SourceRead)
def get_source(key: str, session: SessionDep) -> SourceRead:
    return source_read(load_source(session, key))


@router.get("/sources/{key}/directories", response_model=list[DirectoryState])
def list_directories(key: str, session: SessionDep) -> list[DirectoryState]:
    source = load_source(session, key)
    rows = session.scalars(
        select(CorpusDirectory)
        .where(CorpusDirectory.source_id == source.id)
        .order_by(CorpusDirectory.relative_dir)
    )
    return [
        DirectoryState(
            relative_dir=r.relative_dir,
            mtime_ns=r.mtime_ns,
            settled=r.settled,
            subdirs=r.subdirs,
            file_count=r.file_count,
        )
        for r in rows
    ]


@router.get("/sources/{key}/segment-stats", response_model=list[SegmentStat])
def segment_stats(key: str, session: SessionDep, dir: str = "") -> list[SegmentStat]:
    """Stored stat facts for every segment directly inside ``dir`` (for scanners)."""
    source = load_source(session, key)
    rows = session.execute(
        select(
            Segment.relative_path,
            Segment.file_size,
            Segment.file_mtime_ns,
            Segment.presence_status,
        ).where(Segment.source_id == source.id, Segment.relative_dir == _dir_param(dir))
    )
    return [
        SegmentStat(
            relative_path=r.relative_path,
            file_size=r.file_size,
            file_mtime_ns=r.file_mtime_ns,
            presence_status=r.presence_status,
        )
        for r in rows
    ]


@router.get("/sources/{key}/segments", response_model=list[SegmentRead])
def list_segments(
    key: str,
    session: SessionDep,
    dir: str | None = None,
    after_id: int = 0,
    limit: int = Query(100, ge=1, le=1000),
) -> list[SegmentRead]:
    source = load_source(session, key)
    query = select(Segment).where(Segment.source_id == source.id, Segment.id > after_id)
    if dir is not None:
        query = query.where(Segment.relative_dir == _dir_param(dir))
    rows = session.scalars(query.order_by(Segment.id).limit(limit))
    return [segment_read(r, key) for r in rows]


@router.get("/sources/{key}/summary", response_model=SourceSummary)
def summarize_source(key: str, session: SessionDep) -> SourceSummary:
    source = load_source(session, key)
    in_source = Segment.source_id == source.id

    total, duration_ms = session.execute(
        select(func.count(), func.coalesce(func.sum(Segment.duration_ms), 0)).where(in_source)
    ).one()

    def counts(column) -> dict[str, int]:
        rows = session.execute(select(column, func.count()).where(in_source).group_by(column))
        return {value: n for value, n in rows}

    utc_day = func.date(func.timezone("UTC", Segment.capture_start_utc))
    days = session.execute(
        select(utc_day, func.count(), func.coalesce(func.sum(Segment.duration_ms), 0))
        .where(in_source, Segment.capture_start_utc.is_not(None))
        .group_by(utc_day)
        .order_by(utc_day)
    )
    return SourceSummary(
        source_key=key,
        segments=total,
        audio_hours=round(duration_ms / 3_600_000, 3),
        by_presence=counts(Segment.presence_status),
        by_temporal_status=counts(Segment.temporal_status),
        by_integrity=counts(Segment.integrity_status),
        by_utc_day=[
            DaySummary(day_utc=day.isoformat(), segments=n, audio_seconds=round(ms / 1000, 3))
            for day, n, ms in days
        ],
    )


@router.get("/segments/{segment_id}", response_model=SegmentRead)
def get_segment(segment_id: int, session: SessionDep) -> SegmentRead:
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(404, "segment not found")
    return segment_read(segment, segment.source.logical_key)
