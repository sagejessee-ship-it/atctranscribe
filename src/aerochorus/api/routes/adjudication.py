from datetime import UTC, datetime

from fastapi import APIRouter, Request

from aerochorus.adjudication_contracts import (
    AdjudicationAccept,
    AdjudicationAcceptOutcome,
    AdjudicationBatchDetail,
    AdjudicationBatchView,
    AdjudicationClaimRequest,
    AdjudicationContext,
    AdjudicationCreate,
    AdjudicationItemView,
    AdjudicationPreview,
    AdjudicationPreviewRequest,
    AdjudicationResultPost,
    AdjudicationStatus,
    ClaimedAdjudication,
)
from aerochorus.api import adjudication as svc
from aerochorus.api.deps import SessionDep
from aerochorus.api.routes.sweeps import _call

router = APIRouter(tags=["adjudication"])


@router.get("/adjudication-status", response_model=AdjudicationStatus)
def status(request: Request) -> AdjudicationStatus:
    """Default model, limits, and which runners are polling (none: batches wait)."""
    state = request.app.state
    s = state.settings
    return AdjudicationStatus(
        default_model=s.adjudication_model,
        runners=state.adjudication_runners.recent(datetime.now(UTC)),
        limits={
            "max_items": s.adjudication_max_items,
            "max_batch_usd": s.adjudication_max_batch_usd,
            "max_audio_s": s.adjudication_max_audio_s,
        },
    )


@router.post("/adjudications/preview", response_model=AdjudicationPreview)
def preview(
    body: AdjudicationPreviewRequest, request: Request, session: SessionDep
) -> AdjudicationPreview:
    """Count, price and check a selection. Sends nothing anywhere."""
    state = request.app.state
    return _call(
        session,
        lambda: svc.preview(
            session, body, state.settings, state.pricing_book, state.adjudication_runners
        ),
        commit=False,
    )


@router.post("/adjudications", response_model=AdjudicationBatchView, status_code=201)
def create(
    body: AdjudicationCreate, request: Request, session: SessionDep
) -> AdjudicationBatchView:
    """Queue a confirmed batch under a hard cost cap (a runner sends it)."""
    state = request.app.state
    return _call(session, lambda: svc.create(session, body, state.settings, state.pricing_book))


@router.get("/adjudications", response_model=list[AdjudicationBatchView])
def list_batches(session: SessionDep) -> list[AdjudicationBatchView]:
    return _call(session, lambda: svc.list_batches(session), commit=False)


@router.get("/adjudications/{batch_id}", response_model=AdjudicationBatchDetail)
def batch_detail(batch_id: int, session: SessionDep) -> AdjudicationBatchDetail:
    return _call(session, lambda: svc.batch_detail(session, batch_id), commit=False)


@router.post("/adjudications/{batch_id}/cancel", response_model=AdjudicationBatchView)
def cancel(batch_id: int, session: SessionDep) -> AdjudicationBatchView:
    return _call(session, lambda: svc.cancel(session, batch_id))


@router.post("/adjudication-items/claim", response_model=list[ClaimedAdjudication])
def claim(
    body: AdjudicationClaimRequest, request: Request, session: SessionDep
) -> list[ClaimedAdjudication]:
    """Runner protocol: hand out queued items within each batch's cost cap."""
    return _call(session, lambda: svc.claim(session, body, request.app.state.adjudication_runners))


@router.post("/adjudication-items/{item_id}/result", response_model=AdjudicationItemView)
def post_result(
    item_id: int, body: AdjudicationResultPost, session: SessionDep
) -> AdjudicationItemView:
    return _call(session, lambda: svc.post_result(session, item_id, body))


@router.post("/adjudication-items/accept", response_model=AdjudicationAcceptOutcome)
def accept(
    body: AdjudicationAccept, request: Request, session: SessionDep
) -> AdjudicationAcceptOutcome:
    """Adjudicated transcripts -> silver (never gold; never over human text or decisions)."""
    return _call(session, lambda: svc.accept(session, body, request.app.state.settings))


@router.get("/segments/{segment_id}/adjudication-context", response_model=AdjudicationContext)
def adjudication_context(segment_id: int, session: SessionDep) -> AdjudicationContext:
    """The prompt text for one segment (manual tests; the audio comes from the edge)."""
    return _call(session, lambda: svc.context_for(session, segment_id), commit=False)


@router.get("/segments/{segment_id}/adjudications", response_model=list[AdjudicationItemView])
def segment_adjudications(segment_id: int, session: SessionDep) -> list[AdjudicationItemView]:
    return _call(session, lambda: svc.segment_items(session, segment_id), commit=False)
