from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from aerochorus.api.deps import SessionDep, load_source
from aerochorus.api.indexing import (
    InvalidBatch,
    ScanConflict,
    apply_batch,
    finish_scan,
    start_scan,
)
from aerochorus.contracts import BatchResult, DirectoryBatch, ScanCreate, ScanFinish, ScanRead
from aerochorus.db.models import CorpusScan, Worker

router = APIRouter(tags=["scans"])


def scan_read(scan: CorpusScan) -> ScanRead:
    return ScanRead(
        id=scan.id,
        source_key=scan.source.logical_key,
        worker_name=scan.worker.name if scan.worker else None,
        mode=scan.mode,
        scope_prefix=scan.scope_prefix,
        status=scan.status,
        started_at=scan.started_at,
        updated_at=scan.updated_at,
        finished_at=scan.finished_at,
        counters=scan.counters,
        errors=scan.errors,
        error_message=scan.error_message,
    )


def _locked_scan(session, scan_id: int) -> CorpusScan:
    scan = session.get(CorpusScan, scan_id, with_for_update=True)
    if scan is None:
        raise HTTPException(404, "scan not found")
    return scan


@router.post("/scans", response_model=ScanRead, status_code=201)
def create_scan(body: ScanCreate, request: Request, session: SessionDep) -> ScanRead:
    source = load_source(session, body.source_key)
    worker = session.scalar(select(Worker).where(Worker.name == body.worker_name))
    if worker is None:
        raise HTTPException(404, f"unknown worker {body.worker_name!r}; send a heartbeat first")
    try:
        scan = start_scan(
            session, source, worker, body, request.app.state.settings.scan_stale_after_seconds
        )
    except ScanConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return scan_read(scan)


@router.get("/scans", response_model=list[ScanRead])
def list_scans(
    session: SessionDep, source: str | None = None, limit: int = Query(20, ge=1, le=500)
) -> list[ScanRead]:
    query = select(CorpusScan).order_by(CorpusScan.id.desc()).limit(limit)
    if source is not None:
        query = query.where(CorpusScan.source_id == load_source(session, source).id)
    return [scan_read(s) for s in session.scalars(query)]


@router.get("/scans/{scan_id}", response_model=ScanRead)
def get_scan(scan_id: int, session: SessionDep) -> ScanRead:
    scan = session.get(CorpusScan, scan_id)
    if scan is None:
        raise HTTPException(404, "scan not found")
    return scan_read(scan)


@router.post("/scans/{scan_id}/batches", response_model=BatchResult)
def post_batch(scan_id: int, batch: DirectoryBatch, session: SessionDep) -> BatchResult:
    scan = _locked_scan(session, scan_id)
    try:
        result = apply_batch(session, scan, batch)
    except ScanConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except InvalidBatch as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return result


@router.post("/scans/{scan_id}/finish", response_model=ScanRead)
def post_finish(scan_id: int, body: ScanFinish, session: SessionDep) -> ScanRead:
    scan = _locked_scan(session, scan_id)
    try:
        finish_scan(scan, body)
    except ScanConflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return scan_read(scan)
