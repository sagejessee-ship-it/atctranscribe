from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aerochorus.api import adsb_local
from aerochorus.api import context as svc
from aerochorus.api.deps import SessionDep
from aerochorus.api.routes.sweeps import _call

router = APIRouter(tags=["context"])


class AdsbImportRequest(BaseModel):
    start: date
    end: date | None = None  # inclusive; one day when omitted


# Before the /{segment_id} routes, which would otherwise match "days".
@router.get("/context/adsb/days", response_model=list[adsb_local.DayView])
def adsb_days(
    request: Request, session: SessionDep, start: date | None = None, end: date | None = None
) -> list[adsb_local.DayView]:
    """Imported (and queued) adsb.lol days. Restarts a stopped importer if days wait."""
    adsb_local.ensure_importer(session, request.app.state.adsb_spawn)
    return adsb_local.list_days(session, start, end)


@router.post("/context/adsb/days", response_model=list[adsb_local.DayView])
def adsb_import(
    body: AdsbImportRequest, request: Request, session: SessionDep
) -> list[adsb_local.DayView]:
    """Queue days for import (an explicit request: each day is a ~3.5 GB download)."""
    settings = request.app.state.settings
    try:
        days = adsb_local.queue(session, body.start, body.end, settings)
    except adsb_local.Invalid as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    adsb_local.ensure_importer(session, request.app.state.adsb_spawn)
    return days


@router.get("/context/adsb/{segment_id}", response_model=svc.AdsbStatus)
def adsb_status(segment_id: int, request: Request, session: SessionDep) -> svc.AdsbStatus:
    """Configured? Cached snapshot? Imported days? Never contacts a provider."""
    provider, settings = request.app.state.adsb_provider, request.app.state.settings
    spawn = request.app.state.adsb_spawn
    return _call(
        session, lambda: svc.status(session, segment_id, provider, settings, spawn), commit=False
    )


@router.post("/context/adsb/{segment_id}", response_model=svc.SnapshotView)
def adsb_fetch(
    segment_id: int, request: Request, session: SessionDep, refresh: bool = False
) -> svc.SnapshotView:
    """Fetch once (then cached); `refresh=true` performs a new, explicit provider query."""
    provider = request.app.state.adsb_provider
    settings = request.app.state.settings
    try:
        return _call(
            session, lambda: svc.fetch(session, segment_id, provider, settings, refresh=refresh)
        )
    except svc.NeedsImport as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except svc.Unavailable as exc:
        session.rollback()
        raise HTTPException(503, str(exc)) from exc
    except svc.Upstream as exc:
        session.rollback()
        raise HTTPException(502, f"ADS-B provider failed: {exc}") from exc
