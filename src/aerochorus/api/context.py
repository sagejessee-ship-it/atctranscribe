"""On-demand, cached ADS-B context for one segment (ADR-020).

Nothing here runs on its own: the provider is called only by an explicit
fetch or refresh, and a cached snapshot is reused otherwise. Context never
touches annotations or hypotheses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aerochorus.api.airports import resolve_airport, station_map
from aerochorus.api.sweeps import Invalid, NotFound
from aerochorus.context.opensky import (
    SOURCE,
    AdsbProvider,
    AutoProvider,
    OpenSkyRest,
    OpenSkyTrino,
    ProviderError,
    ProviderUnavailable,
    build_query,
    summarize,
)
from aerochorus.db.models import Airport, ContextSnapshot, Segment
from aerochorus.settings import ControlPlaneSettings


class Unavailable(RuntimeError):
    """No provider configured (or credentials rejected): review continues without ADS-B."""


class Upstream(RuntimeError):
    """The provider failed; nothing was stored."""


class SnapshotView(BaseModel):
    id: int
    segment_id: int
    provider: str
    source: str
    fetched_at: datetime
    t_start: datetime
    t_end: datetime
    radius_nm: float
    row_count: int
    response_sha256: str
    query: dict[str, Any]
    provider_meta: dict[str, Any]
    aircraft: list[dict[str, Any]]
    cached: bool = False


class AdsbStatus(BaseModel):
    configured: bool
    provider: str | None
    message: str | None
    window_before_s: int
    window_after_s: int
    radius_nm: float
    snapshot: SnapshotView | None


def make_provider(settings: ControlPlaneSettings) -> AdsbProvider | None:
    password = settings.opensky_password.get_secret_value() if settings.opensky_password else ""
    secret = (
        settings.opensky_client_secret.get_secret_value() if settings.opensky_client_secret else ""
    )
    trino = (
        OpenSkyTrino(settings.opensky_username, password, timeout_s=settings.opensky_timeout_s)
        if settings.opensky_username and password
        else None
    )
    rest = (
        OpenSkyRest(settings.opensky_client_id, secret, max_tracks=settings.adsb_rest_max_tracks)
        if settings.opensky_client_id and secret
        else None
    )
    if settings.adsb_provider == "trino":
        return trino
    if settings.adsb_provider == "rest":
        return rest
    if trino and rest:
        return AutoProvider(trino, rest)
    return trino or rest


def _view(snapshot: ContextSnapshot, cached: bool = False) -> SnapshotView:
    return SnapshotView(
        id=snapshot.id,
        segment_id=snapshot.segment_id,
        provider=snapshot.provider,
        source=snapshot.source,
        fetched_at=snapshot.fetched_at,
        t_start=snapshot.t_start,
        t_end=snapshot.t_end,
        radius_nm=snapshot.radius_nm,
        row_count=snapshot.row_count,
        response_sha256=snapshot.response_sha256,
        query=snapshot.query,
        provider_meta=snapshot.provider_meta,
        aircraft=snapshot.summary,
        cached=cached,
    )


def latest(session: Session, segment_id: int) -> ContextSnapshot | None:
    return session.scalar(
        select(ContextSnapshot)
        .where(ContextSnapshot.segment_id == segment_id, ContextSnapshot.kind == "adsb")
        .order_by(ContextSnapshot.fetched_at.desc(), ContextSnapshot.id.desc())
        .limit(1)
    )


def status(
    session: Session,
    segment_id: int,
    provider: AdsbProvider | None,
    settings: ControlPlaneSettings,
) -> AdsbStatus:
    """Cached state only; never calls the provider."""
    if session.get(Segment, segment_id) is None:
        raise NotFound(f"unknown segment: {segment_id}")
    snapshot = latest(session, segment_id)
    return AdsbStatus(
        configured=provider is not None,
        provider=provider.name if provider else None,
        message=None
        if provider
        else "ADS-B context not configured (set AEROCHORUS_OPENSKY_USERNAME/PASSWORD for "
        "Trino, or AEROCHORUS_OPENSKY_CLIENT_ID/SECRET for the REST API, on the control plane)",
        window_before_s=settings.adsb_window_before_s,
        window_after_s=settings.adsb_window_after_s,
        radius_nm=settings.adsb_radius_nm,
        snapshot=_view(snapshot, cached=True) if snapshot else None,
    )


def fetch(
    session: Session,
    segment_id: int,
    provider: AdsbProvider | None,
    settings: ControlPlaneSettings,
    *,
    refresh: bool = False,
) -> SnapshotView:
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise NotFound(f"unknown segment: {segment_id}")
    if not refresh and (cached := latest(session, segment_id)) is not None:
        return _view(cached, cached=True)  # reopening never re-queries
    if provider is None:
        raise Unavailable("ADS-B context is not configured on this control plane")
    if segment.capture_start_utc is None:
        raise Invalid("this segment has no absolute UTC time; ADS-B context needs one (ADR-010)")
    icao = resolve_airport(segment.station, station_map(session))
    airport = session.get(Airport, icao) if icao else None
    if airport is None or airport.latitude is None or airport.longitude is None:
        raise Invalid("no airport reference point for this segment; bootstrap its airport profile")

    query = build_query(
        segment.capture_start_utc,
        airport.latitude,
        airport.longitude,
        before_s=settings.adsb_window_before_s,
        after_s=settings.adsb_window_after_s,
        radius_nm=settings.adsb_radius_nm,
    )
    try:
        result = provider.fetch(query)
    except ProviderUnavailable as exc:
        raise Unavailable(str(exc)) from exc
    except ProviderError as exc:
        raise Upstream(str(exc)) from exc
    raw = {"columns": result.columns, "rows": result.rows}
    described = query.describe() | {"airport": icao}
    snapshot = ContextSnapshot(
        segment_id=segment_id,
        kind="adsb",
        provider=result.metadata.get("provider", provider.name),
        source=SOURCE
        if result.metadata.get("provider", provider.name) != "opensky-rest"
        else "opensky REST /flights + /tracks (interpolated)",
        query=described,
        query_sha256=hashlib.sha256(json.dumps(described, sort_keys=True).encode()).hexdigest(),
        t_start=datetime.fromtimestamp(query.t_start, UTC),
        t_end=datetime.fromtimestamp(query.t_end, UTC),
        radius_nm=query.radius_nm,
        row_count=len(result.rows),
        raw=raw,
        response_sha256=hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str).encode()
        ).hexdigest(),
        summary=summarize(result, query),
        provider_meta=result.metadata,
    )
    session.add(snapshot)
    session.flush()
    session.refresh(snapshot)
    return _view(snapshot)
