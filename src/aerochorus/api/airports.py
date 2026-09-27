"""Airport profiles: generic tables, bootstrapped per airport (KBWI from FAA NASR)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from aerochorus.db.models import (
    Airport,
    AirportAirspace,
    AirportAlias,
    AirportFrequency,
    AirportRunway,
)
from aerochorus.review_contracts import (
    AirportFrequencyView,
    AirportProfileIn,
    AirportProfileView,
    AirportRunwayView,
    AirspaceIn,
    AirspaceView,
)

STATION_KINDS = ("station", "faa", "icao")


def upsert_profile(session: Session, body: AirportProfileIn) -> Airport:
    airport = session.get(Airport, body.icao)
    fields = body.model_dump(exclude={"runways", "frequencies", "aliases"})
    if airport is None:
        airport = Airport(**fields)
        session.add(airport)
    else:
        for key, value in fields.items():
            setattr(airport, key, value)
        airport.updated_at = datetime.now(UTC)
    session.flush()
    for table in (AirportRunway, AirportFrequency, AirportAlias):
        session.execute(delete(table).where(table.icao == body.icao))
    session.add_all(AirportRunway(icao=body.icao, **r.model_dump()) for r in body.runways)
    session.add_all(AirportFrequency(icao=body.icao, **f.model_dump()) for f in body.frequencies)
    session.add_all(AirportAlias(icao=body.icao, **a.model_dump()) for a in body.aliases)
    session.flush()
    return airport


def profile_view(session: Session, icao: str) -> AirportProfileView | None:
    airport = session.get(Airport, icao)
    if airport is None:
        return None
    runways = session.scalars(
        select(AirportRunway).where(AirportRunway.icao == icao).order_by(AirportRunway.end_ident)
    )
    frequencies = session.scalars(
        select(AirportFrequency)
        .where(AirportFrequency.icao == icao)
        .order_by(AirportFrequency.service, AirportFrequency.frequency_hz)
    )
    aliases = session.scalars(select(AirportAlias.alias).where(AirportAlias.icao == icao))
    airspaces = list(
        session.scalars(
            select(AirportAirspace)
            .where(AirportAirspace.icao == icao)
            .order_by(AirportAirspace.airspace_class, AirportAirspace.lower_ft)
        )
    )
    return AirportProfileView(
        icao=airport.icao,
        faa_id=airport.faa_id,
        iata=airport.iata,
        name=airport.name,
        city=airport.city,
        latitude=airport.latitude,
        longitude=airport.longitude,
        elevation_ft=airport.elevation_ft,
        magnetic_variation=airport.magnetic_variation,
        timezone=airport.timezone,
        aliases=sorted(set(aliases)),
        runways=[
            AirportRunwayView(
                pair=r.pair,
                end_ident=r.end_ident,
                length_ft=r.length_ft,
                width_ft=r.width_ft,
                true_alignment=r.true_alignment,
                spoken=r.spoken,
                latitude=r.latitude,
                longitude=r.longitude,
                elevation_ft=r.elevation_ft,
                displaced_latitude=r.displaced_latitude,
                displaced_longitude=r.displaced_longitude,
            )
            for r in runways
        ],
        frequencies=[
            AirportFrequencyView(
                service=f.service,
                frequency_hz=f.frequency_hz,
                facility=f.facility,
                call=f.call,
                sectorization=f.sectorization,
                spoken=f.spoken,
            )
            for f in frequencies
        ],
        provenance=airport.provenance,
        airspaces=[
            AirspaceView(
                name=a.name,
                airspace_class=a.airspace_class,
                local_type=a.local_type,
                lower_ft=a.lower_ft,
                lower_ref=a.lower_ref,
                upper_ft=a.upper_ft,
                upper_ref=a.upper_ref,
                rings=a.rings,
                source_id=a.source_id,
            )
            for a in airspaces
        ],
        airspace_provenance=airspaces[0].provenance if airspaces else {},
    )


def replace_airspaces(session: Session, icao: str, body: AirspaceIn) -> int:
    if session.get(Airport, icao) is None:
        raise LookupError(f"no airport profile for {icao}")
    session.execute(delete(AirportAirspace).where(AirportAirspace.icao == icao))
    session.add_all(
        AirportAirspace(icao=icao, provenance=body.provenance, **a.model_dump())
        for a in body.airspaces
    )
    session.flush()
    return len(body.airspaces)


def station_map(session: Session) -> dict[str, str]:
    """Collector station label / FAA / ICAO id -> ICAO, for every known airport."""
    rows = session.execute(
        select(AirportAlias.alias, AirportAlias.icao).where(AirportAlias.kind.in_(STATION_KINDS))
    )
    return {alias: icao for alias, icao in rows}


def stations_for(session: Session, icao: str) -> list[str]:
    """Every station label that resolves to ``icao`` (always includes the ICAO itself)."""
    rows = session.scalars(
        select(AirportAlias.alias).where(
            AirportAlias.icao == icao, AirportAlias.kind.in_(STATION_KINDS)
        )
    )
    return sorted({icao, *rows})


def resolve_airport(station: str | None, mapping: dict[str, str]) -> str | None:
    if not station:
        return None
    return mapping.get(station) or (station if len(station) == 4 and station.isalnum() else None)


def channel_service(session: Session, icao: str | None, frequency_hz: int | None) -> str | None:
    """'Baltimore Tower (LCL/P)' for a channel whose frequency the profile knows."""
    if not icao or not frequency_hz:
        return None
    rows = list(
        session.scalars(
            select(AirportFrequency).where(
                AirportFrequency.icao == icao, AirportFrequency.frequency_hz == frequency_hz
            )
        )
    )
    if not rows:
        return None
    preference = ("TWR", "GND", "CD", "ATIS", "APP", "DEP", "CLASS_B", "PROC", "EMERG", "OTHER")
    best = min(rows, key=lambda f: preference.index(f.service) if f.service in preference else 99)
    return (
        f"{best.call or best.service} ({best.sectorization})"
        if best.sectorization
        else (best.call or best.service)
    )
