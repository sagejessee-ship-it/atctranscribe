"""Airport-area ADS-B from adsb.lol, imported per day on request, served locally (ADR-024).

A day is imported only when someone asks (the UI's Import button or
``aerochorus adsb import``). The importer runs as its own process next to the API
(``python -m aerochorus.api.adsb_local``), so a multi-gigabyte download never slows
the API. A PostgreSQL advisory lock keeps it to one importer. Queued days survive
restarts: the next request that sees one starts the importer again.

``LocalAdsb`` answers segment queries from the stored positions: no network, no
credits, every aircraft the adsb.lol network saw.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import Engine, delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from aerochorus.context import adsblol
from aerochorus.context.adsblol import Area, DayStatus
from aerochorus.context.opensky import COLUMNS, AdsbQuery, ProviderResult, ProviderUnavailable
from aerochorus.db.models import AdsbAircraft, AdsbDay, Airport, adsb_position
from aerochorus.db.session import make_engine, make_sessionmaker
from aerochorus.settings import ControlPlaneSettings

log = logging.getLogger(__name__)

LOCK_KEY = 0x41445342  # "ADSB": the importer's session-level advisory lock
PROGRESS_EVERY_S = 3.0
TRAIL_PAD_S = 120  # positions this far beyond the window are kept for the map's trails
TRAIL_STEP_S = 8  # ...thinned to one per this many seconds per aircraft
MAX_DAYS_PER_REQUEST = 400
NOT_IMPORTED = "not_imported"  # a day with no row yet (not a stored status)

Opener = Callable[[date], tuple[adsblol.Release, Iterable[Iterable[bytes]]]]
Spawner = Callable[[], None]


class Invalid(ValueError):
    """A request that cannot be served as asked."""


class NotImported(ProviderUnavailable):
    """The days a query needs are not (yet) in the local store."""

    def __init__(self, days: list[date]) -> None:
        self.days = days
        listed = ", ".join(d.isoformat() for d in days)
        super().__init__(f"ADS-B for {listed} is not imported yet (adsb.lol, one download per day)")


class DayView(BaseModel):
    day: date
    status: str  # queued | importing | ready | failed | not_imported
    source: str | None = None
    release: str | None = None
    radius_nm: float | None = None
    bytes_total: int | None = None
    bytes_read: int = 0
    aircraft: int = 0
    positions: int = 0
    error: str | None = None
    requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def view(row: AdsbDay) -> DayView:
    return DayView(
        day=row.day,
        status=row.status,
        source=row.source,
        release=row.release,
        radius_nm=row.radius_nm,
        bytes_total=row.bytes_total,
        bytes_read=row.bytes_read,
        aircraft=row.aircraft,
        positions=row.positions,
        error=row.error,
        requested_at=row.requested_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def utc_days(t_start: int, t_end: int) -> list[date]:
    first = datetime.fromtimestamp(t_start, UTC).date()
    last = datetime.fromtimestamp(t_end, UTC).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def day_views(session: Session, days: list[date]) -> list[DayView]:
    rows = {r.day: r for r in session.scalars(select(AdsbDay).where(AdsbDay.day.in_(days)))}
    return [view(rows[d]) if d in rows else DayView(day=d, status=NOT_IMPORTED) for d in days]


def list_days(
    session: Session, start: date | None = None, end: date | None = None
) -> list[DayView]:
    query = select(AdsbDay).order_by(AdsbDay.day)
    if start is not None:
        query = query.where(AdsbDay.day >= start)
    if end is not None:
        query = query.where(AdsbDay.day <= end)
    return [view(r) for r in session.scalars(query)]


def import_area(session: Session, settings: ControlPlaneSettings) -> Area:
    airport = session.get(Airport, settings.adsb_airport)
    if airport is None or airport.latitude is None or airport.longitude is None:
        raise Invalid(
            f"no profile for {settings.adsb_airport}; run `aerochorus airport bootstrap` first"
        )
    return Area(airport.latitude, airport.longitude, settings.adsb_import_radius_nm)


def queue(
    session: Session, start: date, end: date | None, settings: ControlPlaneSettings
) -> list[DayView]:
    """Queue the days from ``start`` to ``end`` (inclusive). Ready days are kept as they are;
    failed ones are queued again."""
    end = end or start
    if end < start:
        raise Invalid("the last day is before the first")
    count = (end - start).days + 1
    if count > MAX_DAYS_PER_REQUEST:
        raise Invalid(f"at most {MAX_DAYS_PER_REQUEST} days per request")
    if end >= datetime.now(UTC).date():
        raise Invalid("adsb.lol publishes a day after it ends; pick days before today (UTC)")
    area = import_area(session, settings)
    days = [start + timedelta(days=i) for i in range(count)]
    existing = {r.day: r for r in session.scalars(select(AdsbDay).where(AdsbDay.day.in_(days)))}
    now = datetime.now(UTC)
    for day in days:
        row = existing.get(day)
        if row is None:
            session.add(
                AdsbDay(
                    day=day,
                    status=DayStatus.QUEUED,
                    source=adsblol.SOURCE,
                    center_lat=area.center_lat,
                    center_lon=area.center_lon,
                    radius_nm=area.radius_nm,
                    requested_at=now,
                )
            )
        elif row.status == DayStatus.FAILED:
            row.status, row.error, row.bytes_read, row.requested_at = DayStatus.QUEUED, None, 0, now
            row.center_lat, row.center_lon, row.radius_nm = (
                area.center_lat,
                area.center_lon,
                area.radius_nm,
            )
    session.flush()
    return day_views(session, days)


def importer_running(session: Session) -> bool:
    return bool(
        session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = 0 AND objid = :key AND granted)"
            ),
            {"key": LOCK_KEY},
        )
    )


def ensure_importer(session: Session, spawn: Spawner | None) -> None:
    """Start the importer if days are waiting and none is running."""
    if spawn is None:
        return
    waiting = session.scalar(
        select(AdsbDay.day)
        .where(AdsbDay.status.in_([DayStatus.QUEUED, DayStatus.IMPORTING]))
        .limit(1)
    )
    if waiting is not None and not importer_running(session):
        spawn()


def spawn_importer(database_url: str) -> None:
    """The importer as a separate process on the API's own database (it exits when no
    day is queued)."""
    extra: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
    env = os.environ | {"AEROCHORUS_DATABASE_URL": database_url}
    subprocess.Popen([sys.executable, "-m", "aerochorus.api.adsb_local"], env=env, **extra)  # noqa: S603


# --- the importer --------------------------------------------------------------------------


def github_opener(http: httpx.Client) -> Opener:
    def open_day(day: date) -> tuple[adsblol.Release, Iterable[Iterable[bytes]]]:
        release = adsblol.find_release(day, http)
        return release, adsblol.download_parts(release, http)

    return open_day


def _set(sm: sessionmaker[Session], day: date, **values: Any) -> None:
    with sm() as session:
        session.execute(update(AdsbDay).where(AdsbDay.day == day).values(**values))
        session.commit()


def import_day(sm: sessionmaker[Session], day: date, open_day: Opener) -> None:
    """Download, filter and store one day. All or nothing: positions become visible
    together with the 'ready' status."""
    with sm() as session:
        row = session.get(AdsbDay, day)
        if row is None:
            return
        area = Area(row.center_lat, row.center_lon, row.radius_nm)
    _set(
        sm, day, status=DayStatus.IMPORTING, started_at=datetime.now(UTC), error=None, bytes_read=0
    )
    started = time.monotonic()
    try:
        release, parts = open_day(day)
        _set(sm, day, release=release.tag, bytes_total=release.size)
        found: dict[str, adsblol.AircraftInfo] = {}
        last_report = [0.0]

        def progress(read: int) -> None:
            now = time.monotonic()
            if now - last_report[0] >= PROGRESS_EVERY_S:
                last_report[0] = now
                _set(sm, day, bytes_read=read, aircraft=len(found))

        stream = adsblol.ChainedStream(parts, progress)
        positions: list[adsblol.Position] = []
        for aircraft_positions, info in adsblol.iter_day(io.BufferedReader(stream, 1 << 20), area):
            positions += aircraft_positions
            found[info.icao24] = info
        positions.sort(key=lambda p: (p.t, p.icao24))  # time order keeps the BRIN index tight
        with sm() as session:
            session.execute(delete(adsb_position).where(adsb_position.c.day == day))
            session.execute(delete(AdsbAircraft).where(AdsbAircraft.day == day))
            _copy_positions(session, day, positions)
            session.add_all(
                AdsbAircraft(
                    day=day,
                    icao24=i.icao24,
                    registration=i.registration,
                    type_code=i.type_code,
                    description=i.description,
                )
                for i in found.values()
            )
            session.execute(
                update(AdsbDay)
                .where(AdsbDay.day == day)
                .values(
                    status=DayStatus.READY,
                    bytes_read=stream.bytes_read,
                    aircraft=len(found),
                    positions=len(positions),
                    finished_at=datetime.now(UTC),
                )
            )
            session.commit()
        log.info(
            "adsb %s: %d aircraft, %d positions within %.0f nm (%s, %.0f MB in %.0f s)",
            day,
            len(found),
            len(positions),
            area.radius_nm,
            release.tag,
            stream.bytes_read / 2**20,
            time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the day; the importer moves on
        log.warning("adsb %s failed: %s", day, exc)
        _set(
            sm,
            day,
            status=DayStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}"[:2000],
            finished_at=datetime.now(UTC),
        )


def _copy_positions(session: Session, day: date, positions: list[adsblol.Position]) -> None:
    columns = "day, t, icao24, callsign, lat, lon, alt_ft, on_ground, gs_kt, track_deg, vrate_fpm"
    driver = session.connection().connection.driver_connection
    with (
        driver.cursor() as cursor,
        cursor.copy(f"COPY adsb_position ({columns}) FROM STDIN") as copy,
    ):
        for p in positions:
            copy.write_row(
                (
                    day,
                    p.t,
                    p.icao24,
                    p.callsign,
                    p.lat,
                    p.lon,
                    p.alt_ft,
                    p.on_ground,
                    p.gs_kt,
                    p.track_deg,
                    p.vrate_fpm,
                )
            )


def run(engine: Engine, open_day: Opener | None = None) -> int:
    """Import queued days until none is left. Returns the number of days processed, or 0
    at once if another importer holds the lock."""
    sm = make_sessionmaker(engine)
    with engine.connect() as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
            return 0
        lock.commit()  # the lock is session-level; no transaction stays open
        try:
            with sm() as session:  # left 'importing' by an importer that died
                session.execute(
                    update(AdsbDay)
                    .where(AdsbDay.status == DayStatus.IMPORTING)
                    .values(status=DayStatus.QUEUED)
                )
                session.commit()
            with httpx.Client(
                timeout=httpx.Timeout(60.0, read=300.0), headers={"User-Agent": "aerochorus"}
            ) as http:
                opener = open_day or github_opener(http)
                done = 0
                while True:
                    with sm() as session:
                        day = session.scalar(
                            select(AdsbDay.day)
                            .where(AdsbDay.status == DayStatus.QUEUED)
                            .order_by(AdsbDay.requested_at, AdsbDay.day)
                            .limit(1)
                        )
                    if day is None:
                        return done
                    import_day(sm, day, opener)
                    done += 1
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
            lock.commit()


def main() -> int:
    from aerochorus.settings import get_settings

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    engine = make_engine(get_settings().database_url)
    try:
        done = run(engine)
    finally:
        engine.dispose()
    log.info("adsb importer: %d day(s) processed", done)
    return 0


# --- the provider ---------------------------------------------------------------------------

M_PER_FT, MS_PER_KT, MS_PER_FPM = 0.3048, 0.514444, 0.00508


class LocalAdsb:
    """Segment queries answered from imported adsb.lol days."""

    name = "adsb.lol"

    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def __repr__(self) -> str:
        return "LocalAdsb()"

    @staticmethod
    def days_needed(query: AdsbQuery) -> list[date]:
        return utc_days(query.t_start - TRAIL_PAD_S, query.t_end + TRAIL_PAD_S)

    def fetch(self, query: AdsbQuery) -> ProviderResult:
        needed = self.days_needed(query)
        p = adsb_position.c
        with self._sm() as session:
            days = {
                r.day: r for r in session.scalars(select(AdsbDay).where(AdsbDay.day.in_(needed)))
            }
            missing = [d for d in needed if d not in days or days[d].status != DayStatus.READY]
            if missing:
                raise NotImported(missing)
            found = session.execute(
                select(
                    p.t.label("ts"),  # Row.t is taken (rows are tuples)
                    p.icao24,
                    p.callsign,
                    p.lat,
                    p.lon,
                    p.alt_ft,
                    p.gs_kt,
                    p.track_deg,
                    p.vrate_fpm,
                    p.on_ground,
                )  # fmt: skip
                .where(
                    p.t.between(query.t_start - TRAIL_PAD_S, query.t_end + TRAIL_PAD_S),
                    p.lat.between(query.lat_min, query.lat_max),
                    p.lon.between(query.lon_min, query.lon_max),
                )
                .order_by(p.icao24, p.t)
            ).all()
            # Aircraft present in the window itself; the padding only draws their trails.
            present = {r.icao24 for r in found if query.t_start <= r.ts <= query.t_end}
            info = {
                a.icao24: {"type_code": a.type_code, "registration": a.registration}
                for a in session.scalars(
                    select(AdsbAircraft).where(
                        AdsbAircraft.day.in_(needed), AdsbAircraft.icao24.in_(present or {""})
                    )
                )
            }
        rows: list[list[Any]] = []
        last_kept: dict[str, int] = {}
        for r in found:
            if r.icao24 not in present:
                continue
            near_segment = abs(r.ts - query.segment_utc) <= TRAIL_STEP_S
            in_window = query.t_start <= r.ts <= query.t_end
            if (
                not near_segment
                and not in_window
                and r.ts - last_kept.get(r.icao24, -(10**9)) < TRAIL_STEP_S
            ):
                continue
            last_kept[r.icao24] = r.ts
            rows.append(
                [
                    r.ts,
                    r.icao24,
                    r.callsign,
                    round(r.lat, 5),
                    round(r.lon, 5),
                    None if r.alt_ft is None else r.alt_ft * M_PER_FT,
                    None,
                    None if r.gs_kt is None else r.gs_kt * MS_PER_KT,
                    r.track_deg,
                    None if r.vrate_fpm is None else r.vrate_fpm * MS_PER_FPM,
                    r.on_ground,
                    None,
                    r.ts,
                ]
            )
        rows.sort(key=lambda row: row[0])
        releases = sorted({days[d].release for d in needed if days[d].release})
        return ProviderResult(
            columns=list(COLUMNS),
            rows=rows,
            metadata={
                "provider": self.name,
                "source": f"{adsblol.SOURCE}, {', '.join(releases)}",
                "license": adsblol.LICENSE,
                "days": [d.isoformat() for d in needed],
                "import_radius_nm": min(days[d].radius_nm for d in needed),
                "aircraft_info": info,
            },
        )


if __name__ == "__main__":
    sys.exit(main())
