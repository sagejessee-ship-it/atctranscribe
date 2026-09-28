from fastapi import APIRouter, HTTPException, Request

from aerochorus.api import context as svc
from aerochorus.api.deps import SessionDep
from aerochorus.api.routes.sweeps import _call

router = APIRouter(tags=["context"])


@router.get("/context/adsb/{segment_id}", response_model=svc.AdsbStatus)
def adsb_status(segment_id: int, request: Request, session: SessionDep) -> svc.AdsbStatus:
    """Configured? Cached snapshot? Never contacts the provider."""
    provider, settings = request.app.state.adsb_provider, request.app.state.settings
    return _call(session, lambda: svc.status(session, segment_id, provider, settings), commit=False)


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
    except svc.Unavailable as exc:
        session.rollback()
        raise HTTPException(503, str(exc)) from exc
    except svc.Upstream as exc:
        session.rollback()
        raise HTTPException(502, f"ADS-B provider failed: {exc}") from exc
