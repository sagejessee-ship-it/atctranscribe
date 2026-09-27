from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aerochorus.api import sweeps as svc
from aerochorus.api.deps import SessionDep
from aerochorus.db.models import Model, ModelSuite, Segment, SweepRun, Worker
from aerochorus.sweep_contracts import (
    CatalogSync,
    CatalogSyncResult,
    ClaimRequest,
    ModelRead,
    ModelRunClaim,
    ModelRunFinish,
    ModelRunStart,
    ModelUpdate,
    PendingBatch,
    PlatformQualificationRead,
    PlatformQualificationWrite,
    QualificationRecord,
    ReflagResult,
    ResultAck,
    ResultPost,
    SuiteRead,
    SuiteWrite,
    SweepCreate,
    SweepPreview,
    SweepRead,
    SweepReport,
    TranscriptionRead,
)

router = APIRouter(tags=["transcription"])


def _call[T](session: Session, fn: Callable[[], T], *, commit: bool = True) -> T:
    """Run a service call, translating domain errors to HTTP and committing on success."""
    try:
        result = fn()
    except svc.NotFound as exc:
        session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except svc.Conflict as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except svc.Invalid as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    if commit:
        session.commit()
    return result


def _worker(session: Session, name: str) -> Worker:
    worker = session.scalar(select(Worker).where(Worker.name == name))
    if worker is None:
        raise HTTPException(404, f"unknown worker {name!r}; send a heartbeat first")
    return worker


# --- model registry -----------------------------------------------------------------


@router.post("/models/sync", response_model=CatalogSyncResult)
def sync_models(body: CatalogSync, session: SessionDep) -> CatalogSyncResult:
    return _call(session, lambda: svc.sync_catalog(session, body))


@router.get("/models", response_model=list[ModelRead])
def list_models(session: SessionDep) -> list[ModelRead]:
    models = session.scalars(select(Model).order_by(Model.architecture_family, Model.logical_name))
    return [svc.model_read(m) for m in models]


@router.get("/models/{name}", response_model=ModelRead)
def get_model(name: str, session: SessionDep) -> ModelRead:
    return svc.model_read(_call(session, lambda: svc.load_model(session, name), commit=False))


@router.patch("/models/{name}", response_model=ModelRead)
def patch_model(name: str, body: ModelUpdate, session: SessionDep) -> ModelRead:
    return svc.model_read(_call(session, lambda: svc.update_model(session, name, body)))


@router.post("/models/{name}/qualification", response_model=ModelRead)
def record_qualification(name: str, body: QualificationRecord, session: SessionDep) -> ModelRead:
    """Record one re-incorporation gate for a converted fine-tune (ADR-019)."""
    return svc.model_read(_call(session, lambda: svc.record_qualification(session, name, body)))


@router.get("/qualifications", response_model=list[PlatformQualificationRead])
def list_platform_qualifications(
    session: SessionDep, profile: str | None = None
) -> list[PlatformQualificationRead]:
    """Model qualification per hardware profile (ADR-021)."""
    return svc.list_platform_qualifications(session, profile)


@router.put("/models/{name}/qualifications/{profile}", response_model=PlatformQualificationRead)
def put_platform_qualification(
    name: str, profile: str, body: PlatformQualificationWrite, session: SessionDep
) -> PlatformQualificationRead:
    return _call(session, lambda: svc.record_platform_qualification(session, name, profile, body))


@router.get("/suites", response_model=list[SuiteRead])
def list_suites(session: SessionDep) -> list[SuiteRead]:
    suites = session.scalars(select(ModelSuite).order_by(ModelSuite.name))
    return [svc.suite_read(session, s) for s in suites]


@router.put("/suites/{name}", response_model=SuiteRead)
def put_suite(name: str, body: SuiteWrite, session: SessionDep) -> SuiteRead:
    suite = _call(session, lambda: svc.write_suite(session, name, body))
    return svc.suite_read(session, suite)


# --- sweeps ---------------------------------------------------------------------------


@router.post("/sweeps", response_model=SweepRead, status_code=201)
def create_sweep(
    body: SweepCreate, session: SessionDep, allow_unqualified: bool = False
) -> SweepRead:
    run = _call(
        session, lambda: svc.create_sweep(session, body, allow_ineligible=allow_unqualified)
    )
    return svc.sweep_read(session, run)


@router.post("/sweeps/preview", response_model=SweepPreview)
def preview_sweep(body: SweepCreate, session: SessionDep) -> SweepPreview:
    """Segment count, audio minutes and a time estimate. Creates nothing."""
    return _call(session, lambda: svc.preview_sweep(session, body), commit=False)


@router.get("/sweeps", response_model=list[SweepRead])
def list_sweeps(session: SessionDep, limit: int = Query(20, ge=1, le=200)) -> list[SweepRead]:
    runs = session.scalars(select(SweepRun).order_by(SweepRun.id.desc()).limit(limit))
    return [svc.sweep_read(session, r) for r in runs]


@router.get("/sweeps/{sweep_id}", response_model=SweepRead)
def get_sweep(sweep_id: int, session: SessionDep) -> SweepRead:
    run = _call(session, lambda: svc.load_run(session, sweep_id), commit=False)
    return svc.sweep_read(session, run)


@router.post("/sweeps/{sweep_id}/{action}", response_model=SweepRead)
def control_sweep(
    sweep_id: int, action: str, session: SessionDep, model: str | None = None
) -> SweepRead:
    if action not in ("pause", "resume", "cancel", "retry"):
        raise HTTPException(404, f"unknown action {action!r}")

    def act() -> SweepRun:
        run = svc.load_run(session, sweep_id)
        if action == "retry":
            svc.retry_model(session, run, model)
        else:
            svc.set_sweep_status(session, run, action)
        return run

    return svc.sweep_read(session, _call(session, act))


@router.get("/sweeps/{sweep_id}/report", response_model=SweepReport)
def report(sweep_id: int, session: SessionDep) -> SweepReport:
    run = _call(session, lambda: svc.load_run(session, sweep_id), commit=False)
    return svc.sweep_report(session, run)


@router.get("/sweeps/{sweep_id}/transcripts")
def transcripts(
    sweep_id: int,
    session: SessionDep,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    run = _call(session, lambda: svc.load_run(session, sweep_id), commit=False)
    return svc.sweep_transcripts(session, run, limit, offset)


@router.get("/segments/{segment_id}/results", response_model=list[TranscriptionRead])
def segment_results(segment_id: int, session: SessionDep) -> list[TranscriptionRead]:
    if session.get(Segment, segment_id) is None:
        raise HTTPException(404, "segment not found")
    return svc.segment_results(session, segment_id)


# --- worker protocol -----------------------------------------------------------------


@router.post("/sweeps/claim", response_model=ModelRunClaim | None)
def claim(body: ClaimRequest, session: SessionDep, response: Response) -> ModelRunClaim | None:
    worker = _worker(session, body.worker_name)
    srm = _call(
        session,
        lambda: svc.claim(
            session, worker, body.lease_seconds, body.source_keys, body.hardware_profile
        ),
    )
    if srm is None:
        response.status_code = 204
        return None
    return ModelRunClaim(
        sweep_model_id=srm.id,
        sweep_id=srm.run_id,
        attempt=srm.attempts,
        model=svc.model_read(srm.model),
        request_params=svc.request_params(srm.run, srm.model),
        segments_total=srm.segments_total,
        results_recorded=svc.results_recorded(session, srm),
        runtime_fingerprint=srm.runtime_fingerprint,
    )


@router.post("/sweep-models/{sweep_model_id}/start")
def start(sweep_model_id: int, body: ModelRunStart, session: SessionDep) -> dict[str, str]:
    worker = _worker(session, body.worker_name)

    def act():
        return svc.start_model_run(
            session, svc.load_model_run(session, sweep_model_id, lock=True), worker, body
        )

    srm = _call(session, act)
    return {"status": srm.status}


@router.get("/sweep-models/{sweep_model_id}/pending", response_model=PendingBatch)
def get_pending(
    sweep_model_id: int, session: SessionDep, limit: int = Query(25, ge=1, le=1000)
) -> PendingBatch:
    srm = _call(session, lambda: svc.load_model_run(session, sweep_model_id), commit=False)
    return svc.pending(session, srm, limit)


@router.post("/sweep-models/{sweep_model_id}/results", response_model=ResultAck)
def post_result(
    sweep_model_id: int, body: ResultPost, session: SessionDep, worker_name: str
) -> ResultAck:
    worker = _worker(session, worker_name)
    return _call(
        session,
        lambda: svc.record_result(
            session, svc.load_model_run(session, sweep_model_id, lock=True), worker, body
        ),
    )


@router.post("/sweep-models/{sweep_model_id}/finish")
def finish(
    sweep_model_id: int, body: ModelRunFinish, session: SessionDep, worker_name: str
) -> dict[str, str]:
    worker = _worker(session, worker_name)
    srm = _call(
        session,
        lambda: svc.finish_model_run(
            session, svc.load_model_run(session, sweep_model_id, lock=True), worker, body
        ),
    )
    return {"status": srm.status, "sweep_status": srm.run.status}


class ReleaseBody(BaseModel):
    worker_name: str
    reason: str = "released by worker"


@router.post("/sweep-models/{sweep_model_id}/release")
def release(sweep_model_id: int, body: ReleaseBody, session: SessionDep) -> dict[str, str]:
    worker = _worker(session, body.worker_name)

    def act():
        srm = svc.load_model_run(session, sweep_model_id, lock=True)
        svc.release_model_run(session, srm, worker, body.reason)
        return srm

    return {"status": _call(session, act).status}


@router.post("/results/reflag", response_model=ReflagResult)
def reflag(session: SessionDep) -> ReflagResult:
    """Recompute deterministic quality flags where the flags version changed."""
    return _call(session, lambda: svc.reflag_results(session))
