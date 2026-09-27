"""Phase 5C: on-demand, cached ADS-B context behind a provider boundary."""

from __future__ import annotations

import pytest
from integration_support import SOURCE_KEY, make_scanner

from aerochorus.context.opensky import COLUMNS, AdsbQuery, ProviderError, ProviderResult

KBWI = {
    "icao": "KBWI",
    "faa_id": "BWI",
    "name": "Baltimore/Washington Intl Thurgood Marshall",
    "latitude": 39.1754,
    "longitude": -76.6683,
    "timezone": "America/New_York",
    "aliases": [{"alias": "BWI", "kind": "station"}],
}


class FakeProvider:
    name = "fake-opensky"

    def __init__(self, fail: bool = False) -> None:
        self.queries: list[AdsbQuery] = []
        self.fail = fail

    def fetch(self, query: AdsbQuery) -> ProviderResult:
        self.queries.append(query)
        if self.fail:
            raise ProviderError("trino timed out")
        t = query.segment_utc
        row = [
            t + 3,
            "a1b2c3",
            "SWA456",
            39.2,
            -76.7,
            1219.2,
            1250.0,
            102.9,
            330.0,
            -3.2,
            False,
            "1234",
            t,
        ]  # noqa: E501
        return ProviderResult(list(COLUMNS), [row], {"query_id": "fake-1"})


@pytest.fixture
def segment_id(api, http, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    assert http.put("/api/v1/airports/KBWI", json=KBWI).status_code == 200
    rows = http.get(f"/api/v1/sources/{SOURCE_KEY}/segments").json()
    return next(r["id"] for r in rows if r["capture_start_utc"] and r["station"] == "BWI")


def test_unconfigured_provider_keeps_review_working(app, http, segment_id):
    app.state.adsb_provider = None
    status = http.get(f"/api/v1/context/adsb/{segment_id}").json()
    assert status["configured"] is False and "not configured" in status["message"]
    assert http.post(f"/api/v1/context/adsb/{segment_id}").status_code == 503
    assert http.get(f"/api/v1/review/segments/{segment_id}").status_code == 200


def test_fetch_is_explicit_bounded_cached_and_refreshable(app, http, segment_id):
    provider = FakeProvider()
    app.state.adsb_provider = provider
    # Opening a segment and reading status never queries the provider.
    http.get(f"/api/v1/review/segments/{segment_id}")
    status = http.get(f"/api/v1/context/adsb/{segment_id}").json()
    assert status["configured"] is True and status["snapshot"] is None
    assert provider.queries == []

    first = http.post(f"/api/v1/context/adsb/{segment_id}").json()
    assert len(provider.queries) == 1 and first["cached"] is False
    q = provider.queries[0]
    assert q.t_end - q.t_start == 120 and q.radius_nm == 10 and q.hours
    assert first["source"] == "minio.osky.state_vectors_data4"
    assert first["query"]["airport"] == "KBWI" and "hour IN" in first["query"]["sql"]
    assert first["aircraft"][0]["callsign"] == "SWA456" and first["aircraft"][0]["offset_s"] == 3
    assert len(first["response_sha256"]) == 64 and first["row_count"] == 1

    again = http.post(f"/api/v1/context/adsb/{segment_id}").json()
    assert again["cached"] is True and again["id"] == first["id"]
    assert len(provider.queries) == 1  # reopening uses the cache
    cached_status = http.get(f"/api/v1/context/adsb/{segment_id}").json()
    assert cached_status["snapshot"]["id"] == first["id"]

    refreshed = http.post(f"/api/v1/context/adsb/{segment_id}?refresh=true").json()
    assert len(provider.queries) == 2 and refreshed["id"] != first["id"]


def test_provider_failure_stores_nothing_and_loses_no_annotation(app, http, segment_id):
    body = {"review_status": "corrected", "training_label": "none", "expected_version": 0}
    saved = http.post(
        f"/api/v1/review/segments/{segment_id}/annotations", json=body | {"text": "roger"}
    )
    assert saved.status_code == 200
    app.state.adsb_provider = FakeProvider(fail=True)
    response = http.post(f"/api/v1/context/adsb/{segment_id}")
    assert response.status_code == 502 and "trino timed out" in response.json()["detail"]
    status = http.get(f"/api/v1/context/adsb/{segment_id}").json()
    assert status["snapshot"] is None
    detail = http.get(f"/api/v1/review/segments/{segment_id}").json()
    assert detail["segment_annotation"]["current"]["text"] == "roger"


def test_segment_needs_utc_and_airport(app, http, api, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    app.state.adsb_provider = FakeProvider()
    rows = http.get(f"/api/v1/sources/{SOURCE_KEY}/segments").json()
    with_utc = next(r["id"] for r in rows if r["capture_start_utc"])
    response = http.post(f"/api/v1/context/adsb/{with_utc}")  # no airport profile yet
    assert response.status_code == 422 and "airport" in response.json()["detail"]
    no_utc = [r["id"] for r in rows if r["capture_start_utc"] is None]
    if no_utc:
        assert http.put("/api/v1/airports/KBWI", json=KBWI).status_code == 200
        response = http.post(f"/api/v1/context/adsb/{no_utc[0]}")
        assert response.status_code == 422 and "UTC" in response.json()["detail"]


def test_credentials_never_appear_in_responses(database_url):
    from fastapi.testclient import TestClient

    from aerochorus.api.app import create_app
    from aerochorus.settings import ControlPlaneSettings

    settings = ControlPlaneSettings(
        database_url=database_url, opensky_username="Someone", opensky_password="hunter2-secret"
    )
    app = create_app(settings)
    assert app.state.adsb_provider is not None
    with TestClient(app) as client:
        for path in ("/api/v1/review/facets", "/openapi.json", "/health"):
            assert "hunter2-secret" not in client.get(path).text
    assert "hunter2-secret" not in repr(settings) and "hunter2-secret" not in repr(
        app.state.adsb_provider
    )
    app.state.engine.dispose()
