from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from aerochorus.api import agreement as agreement_svc
from aerochorus.api import airports as airport_svc
from aerochorus.api import review as svc
from aerochorus.api.deps import SessionDep
from aerochorus.api.routes.sweeps import _call
from aerochorus.db.models import (
    Airport,
    ArchitectureFamily,
    CorpusSource,
    Model,
    ReviewSample,
    Segment,
    SegmentAgreement,
)
from aerochorus.review_contracts import (
    AirportProfileIn,
    AirportProfileView,
    AirspaceIn,
    AnnotationSave,
    AnnotationThreadView,
    BatchOutcome,
    BatchRequest,
    ReviewPage,
    ReviewQuery,
    SampleRequest,
    SampleView,
    SavedView,
    SegmentReview,
    SourceRole,
)

router = APIRouter(tags=["review"])


class Facets(BaseModel):
    sources: list[dict[str, str]]
    airports: list[str]
    channels: list[str]
    models: list[dict[str, str | bool]]
    families: list[str]
    flags: list[str]
    near_threshold: float


class RefreshResult(BaseModel):
    refreshed: int


@router.post("/review/query", response_model=ReviewPage)
def query_segments(body: ReviewQuery, session: SessionDep) -> ReviewPage:
    return _call(session, lambda: svc.query_segments(session, body), commit=False)


@router.get("/review/views", response_model=list[SavedView])
def saved_views() -> list[SavedView]:
    return svc.SAVED_VIEWS


@router.get("/review/facets", response_model=Facets)
def facets(session: SessionDep) -> Facets:
    """Values the filter rail offers. Channels come from reviewable (non-benchmark) sources."""
    channels = session.scalars(
        select(Segment.channel)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .where(CorpusSource.role == SourceRole.CORPUS, Segment.channel.is_not(None))
        .distinct()
        .order_by(Segment.channel)
    )
    flags = session.scalars(
        select(func.jsonb_object_keys(SegmentAgreement.flags)).distinct().order_by(None)
    )
    return Facets(
        sources=[
            {"key": s.logical_key, "name": s.name, "role": s.role}
            for s in session.scalars(select(CorpusSource).order_by(CorpusSource.logical_key))
        ],
        airports=list(session.scalars(select(Airport.icao).order_by(Airport.icao))),
        channels=list(channels),
        models=[
            {"name": m.logical_name, "family": m.architecture_family, "enabled": m.enabled}
            for m in session.scalars(
                select(Model).order_by(Model.architecture_family, Model.logical_name)
            )
        ],
        families=list(
            session.scalars(select(ArchitectureFamily.key).order_by(ArchitectureFamily.key))
        ),
        flags=sorted(flags),
        near_threshold=agreement_svc.near_threshold(),
    )


@router.get("/review/segments/{segment_id}", response_model=SegmentReview)
def segment_review(segment_id: int, session: SessionDep) -> SegmentReview:
    return _call(session, lambda: svc.segment_review(session, segment_id), commit=False)


@router.post("/review/segments/{segment_id}/annotations", response_model=AnnotationThreadView)
def save_annotation(
    segment_id: int, body: AnnotationSave, session: SessionDep
) -> AnnotationThreadView:
    return _call(session, lambda: svc.save_annotation(session, segment_id, body))


@router.post("/review/batch", response_model=BatchOutcome)
def batch(body: BatchRequest, session: SessionDep) -> BatchOutcome:
    """Nominate candidate/silver or clear nominations. Gold is never batch-assigned."""
    return _call(session, lambda: svc.batch_nominate(session, body))


@router.post("/review/samples", response_model=SampleView, status_code=201)
def create_sample(body: SampleRequest, session: SessionDep) -> SampleView:
    return _call(session, lambda: svc.create_sample(session, body))


@router.get("/review/samples", response_model=list[SampleView])
def list_samples(session: SessionDep, limit: int = Query(50, ge=1, le=500)) -> list[SampleView]:
    rows = session.scalars(select(ReviewSample).order_by(ReviewSample.id.desc()).limit(limit))
    return [svc.sample_view(s) for s in rows]


@router.get("/review/samples/{sample_id}", response_model=SampleView)
def get_sample(sample_id: int, session: SessionDep) -> SampleView:
    sample = session.get(ReviewSample, sample_id)
    if sample is None:
        raise HTTPException(404, f"unknown sample: {sample_id}")
    return svc.sample_view(sample)


@router.post("/agreement/refresh", response_model=RefreshResult)
def refresh_agreement(session: SessionDep, limit: int | None = Query(None, ge=1)) -> RefreshResult:
    """Recompute stale per-segment agreement (commits in batches)."""
    return RefreshResult(refreshed=agreement_svc.refresh_stale(session, limit=limit))


@router.get("/airports", response_model=list[AirportProfileView])
def list_airports(session: SessionDep) -> list[AirportProfileView]:
    icaos = session.scalars(select(Airport.icao).order_by(Airport.icao))
    return [airport_svc.profile_view(session, icao) for icao in icaos]


@router.get("/airports/{icao}", response_model=AirportProfileView)
def get_airport(icao: str, session: SessionDep) -> AirportProfileView:
    view = airport_svc.profile_view(session, icao.upper())
    if view is None:
        raise HTTPException(404, f"no airport profile for {icao}; bootstrap it first")
    return view


@router.put("/airports/{icao}/airspaces", response_model=AirportProfileView)
def put_airspaces(icao: str, body: AirspaceIn, session: SessionDep) -> AirportProfileView:
    """Replace the airport's controlled-airspace polygons (FAA ADDS; map context)."""
    try:
        airport_svc.replace_airspaces(session, icao.upper(), body)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    session.commit()
    return airport_svc.profile_view(session, icao.upper())


@router.put("/airports/{icao}", response_model=AirportProfileView)
def put_airport(icao: str, body: AirportProfileIn, session: SessionDep) -> AirportProfileView:
    if body.icao != icao.upper():
        raise HTTPException(422, "path and body ICAO differ")
    airport_svc.upsert_profile(session, body)
    session.commit()
    return airport_svc.profile_view(session, body.icao)
