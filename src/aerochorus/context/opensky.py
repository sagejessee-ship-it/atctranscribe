"""On-demand historical ADS-B context from OpenSky's Trino interface (Phase 5C, ADR-020).

Historical state vectors come from ``minio.osky.state_vectors_data4``. The
REST ``/states/all`` endpoint is effectively current-only and is never used for
archived segments. Every query is bounded by:

* the ``hour`` partition(s) covering the window (required by OpenSky's Trino);
* an exact ``time`` window around the segment's UTC;
* a latitude/longitude box around the airport reference point.

Credentials come from the control plane's environment only and never reach
the browser. This module has no database access; the API caches snapshots.

References: https://openskynetwork.github.io/opensky-api/trino.html
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
TRINO_URL = "https://trino.opensky-network.org"
CATALOG, SCHEMA, TABLE = "minio", "osky", "state_vectors_data4"
SOURCE = f"{CATALOG}.{SCHEMA}.{TABLE}"
COLUMNS = (
    "time",
    "icao24",
    "callsign",
    "lat",
    "lon",
    "baroaltitude",
    "geoaltitude",
    "velocity",
    "heading",
    "vertrate",
    "onground",
    "squawk",
    "lastcontact",
)
MAX_WINDOW_S = 600
MAX_RADIUS_NM = 40.0
ROW_LIMIT = 20_000
M_TO_FT = 3.28084
MS_TO_KT = 1.943844
MS_TO_FPM = 196.8504


class ProviderUnavailable(RuntimeError):
    """Not configured, or credentials rejected."""


class ProviderError(RuntimeError):
    """The provider answered with an error or could not be reached."""


@dataclass(frozen=True)
class AdsbQuery:
    segment_utc: int  # unix seconds
    t_start: int
    t_end: int
    center_lat: float
    center_lon: float
    radius_nm: float
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    hours: tuple[int, ...] = field(default=())

    def sql(self) -> str:
        # Only validated numbers are interpolated; no text from requests reaches SQL.
        hours = ", ".join(str(int(h)) for h in self.hours)
        return (
            f"SELECT {', '.join(COLUMNS)} FROM {SOURCE} "
            f"WHERE hour IN ({hours}) "
            f"AND time BETWEEN {int(self.t_start)} AND {int(self.t_end)} "
            f"AND lat BETWEEN {self.lat_min:.5f} AND {self.lat_max:.5f} "
            f"AND lon BETWEEN {self.lon_min:.5f} AND {self.lon_max:.5f} "
            "AND time - lastcontact <= 15 "
            f"ORDER BY time LIMIT {ROW_LIMIT}"
        )

    def describe(self) -> dict[str, Any]:
        return asdict(self) | {"source": SOURCE, "sql": self.sql()}


def build_query(
    segment_utc: datetime,
    center_lat: float,
    center_lon: float,
    *,
    before_s: int = 60,
    after_s: int = 60,
    radius_nm: float = 10.0,
) -> AdsbQuery:
    if segment_utc.tzinfo is None:
        raise ValueError("segment time must be timezone-aware UTC")
    if before_s < 0 or after_s < 0 or before_s + after_s > MAX_WINDOW_S:
        raise ValueError(f"window must be within {MAX_WINDOW_S}s in total")
    if not 0 < radius_nm <= MAX_RADIUS_NM:
        raise ValueError(f"radius must be in (0, {MAX_RADIUS_NM}] nm")
    t = int(segment_utc.timestamp())
    t_start, t_end = t - before_s, t + after_s
    dlat = radius_nm / 60.0
    dlon = radius_nm / (60.0 * max(math.cos(math.radians(center_lat)), 0.01))
    hours = tuple(range(t_start - t_start % 3600, t_end - t_end % 3600 + 1, 3600))
    return AdsbQuery(
        segment_utc=t,
        t_start=t_start,
        t_end=t_end,
        center_lat=center_lat,
        center_lon=center_lon,
        radius_nm=radius_nm,
        lat_min=center_lat - dlat,
        lat_max=center_lat + dlat,
        lon_min=center_lon - dlon,
        lon_max=center_lon + dlon,
        hours=hours,
    )


@dataclass
class ProviderResult:
    columns: list[str]
    rows: list[list[Any]]
    metadata: dict[str, Any]


class AdsbProvider(Protocol):
    name: str

    def fetch(self, query: AdsbQuery) -> ProviderResult: ...


class OpenSkyTrino:
    """Trino REST statement protocol over httpx, with an OpenSky OAuth2 password-grant token."""

    name = "opensky-trino"

    def __init__(
        self,
        username: str,
        password: str,
        *,
        http: httpx.Client | None = None,
        timeout_s: float = 120.0,
        poll_interval_s: float = 0.5,
    ) -> None:
        self.username = username.lower()  # OpenSky stores usernames in lowercase
        self._password = password
        self._http = http or httpx.Client(timeout=30.0)
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._token: tuple[str, float] | None = None

    def __repr__(self) -> str:  # never print the password
        return f"OpenSkyTrino(username={self.username!r})"

    def _access_token(self) -> str:
        now = _time.time()
        if self._token and self._token[1] - 60 > now:
            return self._token[0]
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": "trino-client",
                    "grant_type": "password",
                    "username": self.username,
                    "password": self._password,
                },
            )
        except httpx.TransportError as exc:
            raise ProviderError(f"OpenSky auth unreachable: {type(exc).__name__}") from exc
        if response.status_code in (400, 401, 403):
            raise ProviderUnavailable("OpenSky rejected the configured credentials")
        if response.status_code >= 400:
            raise ProviderError(f"OpenSky auth failed: HTTP {response.status_code}")
        body = response.json()
        self._token = (body["access_token"], now + float(body.get("expires_in", 300)))
        return self._token[0]

    def fetch(self, query: AdsbQuery) -> ProviderResult:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "X-Trino-User": self.username,
            "X-Trino-Catalog": CATALOG,
            "X-Trino-Schema": SCHEMA,
            "X-Trino-Source": "aerochorus",
        }
        started = _time.monotonic()
        columns: list[str] = []
        rows: list[list[Any]] = []
        try:
            response = self._http.post(
                f"{TRINO_URL}/v1/statement", content=query.sql(), headers=headers
            )
            while True:
                if response.status_code >= 400:
                    raise ProviderError(f"OpenSky Trino HTTP {response.status_code}")
                payload = response.json()
                if payload.get("error"):
                    message = payload["error"].get("message", "query failed")
                    raise ProviderError(f"OpenSky Trino: {message}")
                if payload.get("columns") and not columns:
                    columns = [c["name"] for c in payload["columns"]]
                rows += payload.get("data") or []
                next_uri = payload.get("nextUri")
                if not next_uri:
                    break
                if _time.monotonic() - started > self.timeout_s:
                    raise ProviderError(f"OpenSky Trino query exceeded {self.timeout_s:.0f}s")
                if not payload.get("data"):
                    _time.sleep(self.poll_interval_s)
                response = self._http.get(next_uri, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderError(f"OpenSky Trino unreachable: {type(exc).__name__}") from exc
        stats = payload.get("stats", {})
        return ProviderResult(
            columns=columns or list(COLUMNS),
            rows=rows,
            metadata={
                "query_id": payload.get("id"),
                "state": stats.get("state"),
                "processed_bytes": stats.get("processedBytes"),
                "elapsed_s": round(_time.monotonic() - started, 2),
                "trino_user": self.username,
            },
        )


def _distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def _round(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def summarize(result: ProviderResult, query: AdsbQuery) -> list[dict[str, Any]]:
    """One row per aircraft: the state vector nearest in time to the segment start."""
    index = {name: i for i, name in enumerate(result.columns)}
    best: dict[str, list[Any]] = {}
    for row in result.rows:
        icao = row[index["icao24"]]
        t = row[index["time"]]
        if icao is None or t is None:
            continue
        current = best.get(icao)
        if current is None or abs(t - query.segment_utc) < abs(
            current[index["time"]] - query.segment_utc
        ):
            best[icao] = row
    aircraft = []
    for icao, row in best.items():
        get = lambda name, row=row: row[index[name]] if name in index else None  # noqa: E731
        lat, lon = get("lat"), get("lon")
        baro = get("baroaltitude")
        velocity, vertrate = get("velocity"), get("vertrate")
        aircraft.append(
            {
                "icao24": icao,
                "callsign": (get("callsign") or "").strip() or None,
                "time": get("time"),
                "offset_s": get("time") - query.segment_utc,
                "lat": _round(lat, 5),
                "lon": _round(lon, 5),
                "distance_nm": None
                if lat is None or lon is None
                else round(_distance_nm(query.center_lat, query.center_lon, lat, lon), 2),
                "baro_altitude_ft": None if baro is None else round(baro * M_TO_FT),
                "geo_altitude_ft": None
                if get("geoaltitude") is None
                else round(get("geoaltitude") * M_TO_FT),
                "heading_deg": _round(get("heading"), 1),
                "velocity_kt": None if velocity is None else round(velocity * MS_TO_KT),
                "vertical_rate_fpm": None if vertrate is None else round(vertrate * MS_TO_FPM),
                "on_ground": get("onground"),
                "squawk": get("squawk"),
            }
        )
    aircraft.sort(key=lambda a: (abs(a["offset_s"]), a["distance_nm"] or 0.0))
    return aircraft
