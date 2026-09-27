"""OpenSky REST provider (client credentials), auto fallback, token username, FAA airspace."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from aerochorus.context.faa_airspace import parse_features, simplify
from aerochorus.context.opensky import (
    AutoProvider,
    OpenSkyRest,
    OpenSkyTrino,
    ProviderUnavailable,
    build_query,
    summarize,
)

KBWI = (39.1754, -76.6683)
T = int(datetime(2026, 9, 8, 13, 0, tzinfo=UTC).timestamp())


def jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def rest_transport(calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "auth.opensky-network.org":
            return httpx.Response(200, json={"access_token": "rest-token", "expires_in": 1800})
        path = request.url.path
        if path.endswith("/flights/arrival"):
            return httpx.Response(200, json=[
                {"icao24": "a1b2c3", "callsign": "SWA456  ", "firstSeen": T - 3000, "lastSeen": T + 400},  # noqa: E501
                {"icao24": "ffffff", "callsign": "OLD1", "firstSeen": T - 9000, "lastSeen": T - 5000},  # noqa: E501
            ])  # fmt: skip
        if path.endswith("/flights/departure"):
            return httpx.Response(404)  # "no data" is not an error
        if path.endswith("/tracks/all"):
            return httpx.Response(200, json={
                "icao24": "a1b2c3", "callsign": "SWA456", "startTime": T - 3000, "endTime": T + 400,
                "path": [[T - 60, 39.30, -76.70, 1500.0, 170.0, False],
                         [T + 60, 39.24, -76.69, 900.0, 170.0, False]],
            })  # fmt: skip
        return httpx.Response(500)

    return httpx.MockTransport(handler)


def test_rest_provider_reconstructs_positions_from_tracks():
    calls: list[httpx.Request] = []
    provider = OpenSkyRest("client-x", "s3cret", http=httpx.Client(transport=rest_transport(calls)))
    assert "s3cret" not in repr(provider)
    q = build_query(datetime.fromtimestamp(T, UTC), *KBWI, radius_nm=10)
    result = provider.fetch(q)
    form = dict(x.split("=") for x in calls[0].content.decode().split("&"))
    assert form["grant_type"] == "client_credentials" and form["client_id"] == "client-x"
    assert calls[1].headers["authorization"] == "Bearer rest-token"
    track_calls = [c for c in calls if c.url.path.endswith("/tracks/all")]
    assert len(track_calls) == 1  # the flight long gone at T is not tracked
    aircraft = summarize(result, q)
    (swa,) = aircraft
    assert swa["callsign"] == "SWA456" and swa["offset_s"] == 0  # interpolated at T
    assert swa["lat"] == pytest.approx(39.27, abs=1e-3)
    assert swa["baro_altitude_ft"] == round(1200 * 3.28084)
    assert result.metadata["interpolated"] is True and result.metadata["tracks_fetched"] == 1


def test_trino_uses_the_token_username_and_auto_falls_back_to_rest():
    calls: list[httpx.Request] = []

    def trino_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "auth.opensky-network.org":
            token = jwt({"preferred_username": "sagejessee"})
            return httpx.Response(200, json={"access_token": token, "expires_in": 300})
        return httpx.Response(403, text="Access Denied")  # historical access not granted yet

    trino = OpenSkyTrino(
        "Sage@Example.com", "pw", http=httpx.Client(transport=httpx.MockTransport(trino_handler))
    )  # noqa: E501
    q = build_query(datetime.fromtimestamp(T, UTC), *KBWI)
    with pytest.raises(ProviderUnavailable, match="not granted"):
        trino.fetch(q)
    statement = calls[1]
    assert statement.headers["x-trino-user"] == "sagejessee"  # not the e-mail address

    rest = OpenSkyRest("client-x", "s", http=httpx.Client(transport=rest_transport([])))
    result = AutoProvider(trino, rest).fetch(q)
    assert result.metadata["provider"] == "opensky-rest"
    assert "trino unavailable" in result.metadata["fallback"][0]
    with pytest.raises(ProviderUnavailable):
        AutoProvider(trino, None).fetch(q)


def test_faa_airspace_parsing_and_simplification():
    ring = [[-76.7 + i * 0.0001, 39.2 + (i % 2) * 0.00001] for i in range(200)] + [[-76.7, 39.2]]
    assert len(simplify(ring, 0.0015)) < 10
    geojson = {
        "features": [
            {"properties": {"NAME": "WASHINGTON-TRI AREA CLASS B", "CLASS": "B", "LOCAL_TYPE": "CLASS_B",  # noqa: E501
                            "LOWER_VAL": 1500, "LOWER_UOM": "FT", "LOWER_CODE": "MSL",
                            "UPPER_VAL": 10000, "UPPER_UOM": "FT", "UPPER_CODE": "MSL", "GLOBAL_ID": "g1"},  # noqa: E501
             "geometry": {"type": "Polygon", "coordinates": [[[-77, 39], [-76.5, 39], [-76.5, 39.5], [-77, 39.5], [-77, 39]]]}},  # noqa: E501
            {"properties": {"NAME": "CONTIGUOUS US CLASS A", "CLASS": "A", "LOCAL_TYPE": "CLASS_A"},
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
            {"properties": {"NAME": "MARYLAND CLASS E5", "CLASS": "E", "LOCAL_TYPE": "CLASS_E5",
                            "UPPER_VAL": -9998},
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}},
        ]
    }  # fmt: skip
    (b,) = parse_features(geojson)
    assert (b["airspace_class"], b["lower_ft"], b["upper_ft"], b["lower_ref"]) == (
        "B",
        1500,
        10000,
        "MSL",
    )
    assert b["rings"][0][0] == [-77, 39] and b["source_id"] == "g1"


def test_trails_are_ordered_thinned_and_relative_to_the_segment():
    from aerochorus.context.opensky import COLUMNS, trails

    rows = [
        [
            T + dt,
            "a1b2c3",
            "SWA456",
            39.2 + dt / 1e4,
            -76.7,
            300.0,
            None,
            None,
            90.0,
            None,
            False,
            None,
            T + dt,
        ]  # noqa: E501
        for dt in range(-100, 50, 2)
    ] + [[T, "ffffff", None, None, None, None, None, None, None, None, None, None, T]]
    out = trails(list(COLUMNS), list(reversed(rows)), T, max_points=10)
    assert list(out) == ["a1b2c3"]  # an aircraft with no position has no trail
    path = out["a1b2c3"]
    assert len(path) == 10 and path[0][0] == -100 and path[-1][0] == 48
    assert [p[0] for p in path] == sorted(p[0] for p in path)
    assert path[0][3] == round(300 * 3.28084)
