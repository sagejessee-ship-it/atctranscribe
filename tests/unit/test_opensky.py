"""OpenSky Trino provider: bounded historical queries, statement protocol, summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from aerochorus.context import opensky
from aerochorus.context.opensky import (
    COLUMNS,
    OpenSkyTrino,
    ProviderError,
    ProviderResult,
    ProviderUnavailable,
    build_query,
    summarize,
)

KBWI = (39.1754, -76.6683)
T = datetime(2026, 9, 8, 12, 59, 30, tzinfo=UTC)


def test_query_is_tightly_bounded_in_time_space_and_partitions():
    q = build_query(T, *KBWI, before_s=60, after_s=60, radius_nm=10)
    assert q.t_end - q.t_start == 120
    # 12:58:30–13:00:30 spans two hour partitions, both named explicitly.
    assert q.hours == (int(datetime(2026, 9, 8, 12, tzinfo=UTC).timestamp()), q.hours[0] + 3600)
    assert q.lat_max - q.lat_min == pytest.approx(20 / 60)
    assert 0.4 < q.lon_max - q.lon_min < 0.45  # widened by 1/cos(lat)
    sql = q.sql()
    assert "FROM minio.osky.state_vectors_data4" in sql
    assert f"hour IN ({q.hours[0]}, {q.hours[1]})" in sql
    assert f"time BETWEEN {q.t_start} AND {q.t_end}" in sql
    assert "lat BETWEEN" in sql and "lon BETWEEN" in sql and "LIMIT" in sql
    assert "states/all" not in sql
    one_hour = build_query(datetime(2026, 9, 8, 12, 30, tzinfo=UTC), *KBWI)
    assert len(one_hour.hours) == 1


@pytest.mark.parametrize(
    "kwargs",
    [{"before_s": 400, "after_s": 400}, {"radius_nm": 0}, {"radius_nm": 500}, {"before_s": -1}],
)
def test_broad_scans_are_refused(kwargs):
    with pytest.raises(ValueError):
        build_query(T, *KBWI, **kwargs)
    with pytest.raises(ValueError):
        build_query(T.replace(tzinfo=None), *KBWI)


def trino_transport(calls: list[httpx.Request], *, fail: str | None = None, auth_status=200):
    rows = [
        [1788789570, "a1b2c3", "SWA456  ", 39.2, -76.7, 1219.2, 1250.0, 102.9, 330.0, -3.2, False, "1234", 1788789570],  # noqa: E501
        [1788789560, "a1b2c3", "SWA456  ", 39.21, -76.69, 1300.0, 1330.0, 103.0, 330.0, -3.0, False, "1234", 1788789560],  # noqa: E501
        [1788789600, "abcdef", None, 39.18, -76.67, None, None, 5.0, 150.0, 0.0, True, None, 1788789600],  # noqa: E501
    ]  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "auth.opensky-network.org":
            return httpx.Response(auth_status, json={"access_token": "tok", "expires_in": 300})
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": "q1",
                    "nextUri": "https://trino.opensky-network.org/v1/statement/q1/1",
                    "stats": {"state": "QUEUED"},
                },
            )  # noqa: E501
        if fail:
            return httpx.Response(200, json={"id": "q1", "error": {"message": fail}, "stats": {}})
        if request.url.path.endswith("/1"):
            return httpx.Response(
                200,
                json={
                    "id": "q1",
                    "columns": [{"name": c, "type": "varchar"} for c in COLUMNS],
                    "data": rows[:2],
                    "nextUri": "https://trino.opensky-network.org/v1/statement/q1/2",
                    "stats": {"state": "RUNNING"},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "q1",
                "data": rows[2:],
                "stats": {"state": "FINISHED", "processedBytes": 42},
            },
        )

    return httpx.MockTransport(handler)


def test_statement_protocol_auth_and_paging():
    calls: list[httpx.Request] = []
    provider = OpenSkyTrino(
        "SageJ", "s3cret", http=httpx.Client(transport=trino_transport(calls)), poll_interval_s=0
    )
    assert "s3cret" not in repr(provider)
    q = build_query(T, *KBWI)
    result = provider.fetch(q)
    assert len(result.rows) == 3 and result.columns == list(COLUMNS)
    assert result.metadata["query_id"] == "q1" and result.metadata["processed_bytes"] == 42
    token_request = calls[0]
    form = dict(x.split("=") for x in token_request.content.decode().split("&"))
    assert form["client_id"] == "trino-client" and form["grant_type"] == "password"
    assert form["username"] == "sagej"  # lowercase, as OpenSky requires
    statement = calls[1]
    assert statement.headers["authorization"] == "Bearer tok"
    assert statement.headers["x-trino-user"] == "sagej"
    assert statement.headers["x-trino-catalog"] == "minio"
    assert statement.content.decode() == q.sql()

    provider.fetch(q)  # token is reused while valid
    assert sum(c.url.host == "auth.opensky-network.org" for c in calls) == 1


def test_provider_errors_are_explicit():
    bad_login = OpenSkyTrino(
        "u", "p", http=httpx.Client(transport=trino_transport([], auth_status=401))
    )
    with pytest.raises(ProviderUnavailable):
        bad_login.fetch(build_query(T, *KBWI))
    failing = OpenSkyTrino(
        "u",
        "p",
        http=httpx.Client(transport=trino_transport([], fail="scan limit")),
        poll_interval_s=0,
    )
    with pytest.raises(ProviderError, match="scan limit"):
        failing.fetch(build_query(T, *KBWI))


def test_summary_is_nearest_vector_per_aircraft_in_aviation_units():
    q = build_query(datetime.fromtimestamp(1788789565, UTC), *KBWI)
    result = ProviderResult(
        columns=list(COLUMNS),
        rows=[
            [1788789570, "a1b2c3", "SWA456  ", 39.2, -76.7, 1219.2, 1250.0, 102.9, 330.0, -3.2, False, "1234", 0],  # noqa: E501
            [1788789500, "a1b2c3", "SWA456  ", 39.3, -76.7, 1500.0, 1520.0, 103.0, 330.0, -3.0, False, "1234", 0],  # noqa: E501
            [1788789566, "abcdef", None, 39.18, -76.67, None, None, 5.0, 150.0, 0.0, True, None, 0],
        ],
        metadata={},
    )  # fmt: skip
    aircraft = summarize(result, q)
    assert [a["icao24"] for a in aircraft] == ["abcdef", "a1b2c3"]  # sorted by |Δt|
    swa = aircraft[1]
    assert swa["callsign"] == "SWA456" and swa["offset_s"] == 5
    assert swa["baro_altitude_ft"] == 4000 and swa["velocity_kt"] == 200
    assert swa["vertical_rate_fpm"] == -630 and 1.5 < swa["distance_nm"] < 2.5
    assert aircraft[0]["on_ground"] is True and aircraft[0]["baro_altitude_ft"] is None
    json.dumps(aircraft)  # serializable for the snapshot


def test_rest_states_all_is_never_used():
    from pathlib import Path

    code = Path(opensky.__file__).read_text(encoding="utf-8").split('"""', 2)[2]  # skip docstring
    assert "/states/all" not in code and "api/states" not in code
