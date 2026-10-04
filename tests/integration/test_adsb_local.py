"""ADR-024: airport-area ADS-B imported per day from adsb.lol, served from the local store."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fake_adsblol import FakeOpener
from integration_support import SOURCE_KEY, make_scanner
from sqlalchemy import func, select, text

from aerochorus.api import adsb_local
from aerochorus.api.adsb_local import LocalAdsb
from aerochorus.db.models import AdsbAircraft, adsb_position

KBWI = {
    "icao": "KBWI",
    "faa_id": "BWI",
    "name": "Baltimore/Washington Intl Thurgood Marshall",
    "latitude": 39.1757,
    "longitude": -76.6690,
    "timezone": "America/New_York",
    "aliases": [{"alias": "BWI", "kind": "station"}],
}


@pytest.fixture
def segment(app, api, http, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    assert http.put("/api/v1/airports/KBWI", json=KBWI).status_code == 200
    rows = http.get(f"/api/v1/sources/{SOURCE_KEY}/segments").json()
    row = next(r for r in rows if r["capture_start_utc"] and r["station"] == "BWI")
    spawned: list[int] = []
    app.state.adsb_spawn = lambda: spawned.append(1)
    t = int(datetime.fromisoformat(row["capture_start_utc"]).timestamp())
    return row["id"], t, spawned


def _days(http, segment_id):
    return http.get(f"/api/v1/context/adsb/{segment_id}").json()["days"]


def test_local_store_is_the_default_provider(app):
    assert isinstance(app.state.adsb_provider, LocalAdsb)


def test_import_on_request_then_every_aircraft_near_the_segment(app, http, db, segment):
    segment_id, t, spawned = segment
    status = http.get(f"/api/v1/context/adsb/{segment_id}").json()
    assert status["configured"] and status["importable"] and status["provider"] == "adsb.lol"
    days = [d["day"] for d in status["days"]]
    assert {d["status"] for d in status["days"]} == {"not_imported"}
    assert http.post(f"/api/v1/context/adsb/{segment_id}").status_code == 409  # nothing stored

    queued = http.post("/api/v1/context/adsb/days", json={"start": days[0], "end": days[-1]}).json()
    assert [d["status"] for d in queued] == ["queued"] * len(days)
    assert spawned == [1]  # the importer process is started for queued days
    assert adsb_local.run(app.state.engine, FakeOpener(t)) == len(days)

    ready = _days(http, segment_id)
    assert {d["status"] for d in ready} == {"ready"}
    traffic_day = next(d for d in ready if d["aircraft"])
    assert traffic_day["aircraft"] == 2 and traffic_day["positions"] == 121 + 21
    assert traffic_day["release"].endswith("-planes-readsb-prod-0")
    assert traffic_day["bytes_read"] == traffic_day["bytes_total"] > 0
    # Stored in time order (the BRIN index depends on it).
    times = db.scalars(select(adsb_position.c.t)).all()
    assert times == sorted(times)

    snapshot = http.post(f"/api/v1/context/adsb/{segment_id}").json()
    by_icao = {a["icao24"]: a for a in snapshot["aircraft"]}
    assert set(by_icao) == {"a0f00d", "c0ffee"}  # not the one beyond 25 nm, not Europe
    swa = by_icao["a0f00d"]
    assert swa["callsign"] == "SWA456" and swa["type_code"] == "B737"
    assert swa["registration"] == "N456WN"
    assert abs(swa["offset_s"]) <= 5  # a point every few seconds
    assert 500 < swa["baro_altitude_ft"] < 4000 and swa["velocity_kt"] == 140
    assert swa["vertical_rate_fpm"] == -700
    assert by_icao["c0ffee"]["on_ground"] is True
    assert len(snapshot["trails"]["a0f00d"]) > 10  # history for the map
    assert snapshot["provider"] == "adsb.lol"
    assert "adsb.lol" in snapshot["source"] and "prod-0" in snapshot["source"]
    assert snapshot["provider_meta"]["license"].startswith("ODbL")
    assert "aircraft_info" not in snapshot["provider_meta"]
    assert "sql" not in snapshot["query"]
    # Reopening reuses the snapshot, as with any provider.
    assert http.post(f"/api/v1/context/adsb/{segment_id}").json()["id"] == snapshot["id"]


def test_a_failed_day_is_recorded_and_can_be_queued_again(app, http, db, segment):
    segment_id, t, _ = segment
    days = [date.fromisoformat(d["day"]) for d in _days(http, segment_id)]
    http.post("/api/v1/context/adsb/days", json={"start": str(days[0]), "end": str(days[-1])})
    adsb_local.run(app.state.engine, FakeOpener(t, missing=set(days)))
    failed = _days(http, segment_id)
    assert {d["status"] for d in failed} == {"failed"}
    assert "no dump" in failed[0]["error"]
    assert db.scalar(select(func.count()).select_from(adsb_position)) == 0

    again = http.post(
        "/api/v1/context/adsb/days", json={"start": str(days[0]), "end": str(days[-1])}
    )
    assert {d["status"] for d in again.json()} == {"queued"}
    adsb_local.run(app.state.engine, FakeOpener(t))
    assert {d["status"] for d in _days(http, segment_id)} == {"ready"}
    assert db.scalar(select(func.count()).select_from(AdsbAircraft)) == 2


def test_reimport_replaces_a_day_rather_than_duplicating_it(app, http, db, segment):
    segment_id, t, _ = segment
    day = _days(http, segment_id)[0]["day"]
    http.post("/api/v1/context/adsb/days", json={"start": day})
    adsb_local.run(app.state.engine, FakeOpener(t))
    first = db.scalar(select(func.count()).select_from(adsb_position))
    db.execute(text("UPDATE adsb_day SET status = 'queued'"))
    db.commit()
    adsb_local.run(app.state.engine, FakeOpener(t))
    assert db.scalar(select(func.count()).select_from(adsb_position)) == first


def test_one_importer_at_a_time(app, http, segment):
    segment_id, t, spawned = segment
    day = _days(http, segment_id)[0]["day"]
    with app.state.engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": adsb_local.LOCK_KEY})
        http.post("/api/v1/context/adsb/days", json={"start": day})
        assert spawned == []  # an importer is running: not a second one
        assert adsb_local.run(app.state.engine, FakeOpener(t)) == 0
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": adsb_local.LOCK_KEY})
    http.get(f"/api/v1/context/adsb/{segment_id}")
    assert spawned == [1]  # a queued day restarts a stopped importer


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"start": "2026-09-10", "end": "2026-09-01"}, "before the first"),
        ({"start": "2024-01-01", "end": "2026-01-01"}, "at most"),
        ({"start": str(datetime.now(UTC).date())}, "after it ends"),
    ],
)
def test_import_requests_are_bounded(http, segment, body, reason):
    response = http.post("/api/v1/context/adsb/days", json=body)
    assert response.status_code == 422 and reason in response.json()["detail"]


def test_import_needs_the_airport_profile(app, http):
    yesterday = str(datetime.now(UTC).date() - timedelta(days=1))
    response = http.post("/api/v1/context/adsb/days", json={"start": yesterday})
    assert response.status_code == 422 and "airport bootstrap" in response.json()["detail"]
