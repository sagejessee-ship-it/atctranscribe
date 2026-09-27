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

import base64
import json
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
REST_URL = "https://opensky-network.org/api"
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
        # Log in may use the e-mail address; Trino needs the account username,
        # which the token carries as preferred_username (as pyopensky does).
        claimed = _jwt_claims(body["access_token"]).get("preferred_username")
        if claimed:
            self.trino_user = str(claimed).lower()
        return self._token[0]

    trino_user: str | None = None

    def fetch(self, query: AdsbQuery) -> ProviderResult:
        token = self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Trino-User": self.trino_user or self.username,
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
                if response.status_code in (401, 403):
                    raise ProviderUnavailable(
                        "OpenSky Trino refused this account (historical access not granted yet?)"
                    )
                if response.status_code >= 400:
                    raise ProviderError(f"OpenSky Trino HTTP {response.status_code}")
                payload = response.json()
                if payload.get("error"):
                    message = payload["error"].get("message", "query failed")
                    if "access denied" in message.lower() or "permission" in message.lower():
                        raise ProviderUnavailable(f"OpenSky Trino: {message}")
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
                "trino_user": self.trino_user or self.username,
            },
        )


def _jwt_claims(token: str) -> dict[str, Any]:
    """Payload of a JWT (unverified: used only to read our own username)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


class OpenSkyRest:
    """OpenSky REST API with an OAuth2 client-credentials API client.

    The REST API cannot return historical state vectors (its state-vector
    endpoint reaches back about an hour), so for an archived segment it answers "who was
    arriving/departing KBWI around then, and where were they": flights from
    ``/flights/arrival`` + ``/flights/departure`` around the segment, then each
    active flight's ``/tracks/all`` waypoints, interpolated to the segment time.
    Limits: flights are published after a nightly batch (not for today), tracks
    only for the last 30 days, and only KBWI arrivals/departures are covered
    (no overflights). Each fetch costs API credits (about 30 per call).
    """

    name = "opensky-rest"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        airport: str = "KBWI",
        max_tracks: int = 8,
        http: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self.airport = airport
        self.max_tracks = max_tracks
        self._http = http or httpx.Client(timeout=60.0)
        self._token: tuple[str, float] | None = None

    def __repr__(self) -> str:
        return f"OpenSkyRest(client_id={self.client_id!r})"

    def _access_token(self) -> str:
        now = _time.time()
        if self._token and self._token[1] - 60 > now:
            return self._token[0]
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                },
            )
        except httpx.TransportError as exc:
            raise ProviderError(f"OpenSky auth unreachable: {type(exc).__name__}") from exc
        if response.status_code in (400, 401, 403):
            raise ProviderUnavailable("OpenSky rejected the API client credentials")
        if response.status_code >= 400:
            raise ProviderError(f"OpenSky auth failed: HTTP {response.status_code}")
        body = response.json()
        self._token = (body["access_token"], now + float(body.get("expires_in", 1800)))
        return self._token[0]

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        try:
            response = self._http.get(f"{REST_URL}{path}", params=params, headers=headers)
        except httpx.TransportError as exc:
            raise ProviderError(f"OpenSky REST unreachable: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return []  # "no data for this interval"
        if response.status_code == 429:
            retry = response.headers.get("X-Rate-Limit-Retry-After-Seconds", "?")
            raise ProviderError(f"OpenSky REST credits exhausted (retry after {retry} s)")
        if response.status_code in (401, 403):
            raise ProviderUnavailable("OpenSky REST refused the API client")
        if response.status_code >= 400:
            raise ProviderError(f"OpenSky REST HTTP {response.status_code} on {path}")
        return response.json()

    def fetch(self, query: AdsbQuery) -> ProviderResult:
        t = query.segment_utc
        started = _time.monotonic()
        arrivals = (
            self._get(
                "/flights/arrival", {"airport": self.airport, "begin": t - 1800, "end": t + 3600}
            )
            or []
        )
        departures = (
            self._get(
                "/flights/departure", {"airport": self.airport, "begin": t - 3600, "end": t + 1800}
            )
            or []
        )
        flights = {}
        for flight in [*arrivals, *departures]:
            first, last = flight.get("firstSeen") or 0, flight.get("lastSeen") or 0
            if first - 600 <= t <= last + 600 and flight.get("icao24"):
                flights[flight["icao24"]] = flight
        # Nearest in time first; the credit budget caps the number of tracks.
        ranked = sorted(
            flights.values(),
            key=lambda f: min(
                abs((f.get("firstSeen") or t) - t), abs((f.get("lastSeen") or t) - t)
            ),
        )[: self.max_tracks]
        rows: list[list[Any]] = []
        tracks = 0
        for flight in ranked:
            track = self._get("/tracks/all", {"icao24": flight["icao24"], "time": t})
            if not track or not track.get("path"):
                continue
            tracks += 1
            rows += _track_rows(track, flight, query)
        return ProviderResult(
            columns=list(COLUMNS),
            rows=rows,
            metadata={
                "flights_seen": len(flights),
                "tracks_fetched": tracks,
                "requests": 2 + len(ranked),
                "elapsed_s": round(_time.monotonic() - started, 2),
                "coverage": f"{self.airport} arrivals/departures only; tracks <= 30 days old",
                "interpolated": True,
            },
        )


def _track_rows(track: dict[str, Any], flight: dict[str, Any], query: AdsbQuery) -> list[list]:
    """Waypoints near the window (for trails) + one position interpolated at the segment time."""
    path = [p for p in track["path"] if p[1] is not None and p[2] is not None]
    callsign = (track.get("callsign") or flight.get("callsign") or "").strip() or None
    rows = []
    lo, hi = query.t_start - 240, query.t_end + 240
    for i, (time, lat, lon, alt, heading, ground) in enumerate(path):
        if lo <= time <= hi and query.lat_min - 0.2 <= lat <= query.lat_max + 0.2:
            speed = vrate = None
            if i + 1 < len(path) and path[i + 1][0] > time:
                nxt = path[i + 1]
                dt = nxt[0] - time
                speed = _distance_nm(lat, lon, nxt[1], nxt[2]) * 1852 / dt
                if alt is not None and nxt[3] is not None:
                    vrate = (nxt[3] - alt) / dt
            rows.append(
                [time, track["icao24"], callsign, lat, lon, alt, None, speed, heading, vrate,
                 ground, None, time]
            )  # fmt: skip
    t = query.segment_utc
    for (t0, la0, lo0, a0, h0, g0), (t1, la1, lo1, a1, _h1, _g1) in zip(
        path, path[1:], strict=False
    ):
        if t0 <= t <= t1 and t1 > t0:
            f = (t - t0) / (t1 - t0)
            alt = a0 + f * (a1 - a0) if a0 is not None and a1 is not None else a0
            speed = _distance_nm(la0, lo0, la1, lo1) * 1852 / (t1 - t0)
            vrate = (a1 - a0) / (t1 - t0) if a0 is not None and a1 is not None else None
            rows.append(
                [t, track["icao24"], callsign, la0 + f * (la1 - la0), lo0 + f * (lo1 - lo0), alt,
                 None, speed, h0, vrate, g0, None, t]
            )  # fmt: skip
            break
    return rows


class AutoProvider:
    """Trino when it works (true historical state vectors), else REST (tracks)."""

    name = "opensky-auto"

    def __init__(self, trino: OpenSkyTrino | None, rest: OpenSkyRest | None) -> None:
        self.trino, self.rest = trino, rest

    def fetch(self, query: AdsbQuery) -> ProviderResult:
        notes = []
        if self.trino is not None:
            try:
                result = self.trino.fetch(query)
                result.metadata["provider"] = self.trino.name
                return result
            except ProviderUnavailable as exc:
                if self.rest is None:
                    raise
                notes.append(f"trino unavailable: {exc}")
        if self.rest is None:
            raise ProviderUnavailable("no OpenSky provider configured")
        result = self.rest.fetch(query)
        result.metadata["provider"] = self.rest.name
        if notes:
            result.metadata["fallback"] = notes
        return result


def _distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def _round(value: Any, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def trails(
    columns: list[str], rows: list[list[Any]], segment_utc: int, max_points: int = 24
) -> dict[str, list[list[Any]]]:
    """Per-aircraft position history from the stored rows, for the map.

    ``{icao24: [[offset_s, lat, lon, baro_alt_ft], ...]}`` in time order, evenly
    thinned to ``max_points`` (first and last kept). Derived at view time, so
    snapshots stored before trails existed get them too.
    """
    index = {name: i for i, name in enumerate(columns)}
    if not {"icao24", "time", "lat", "lon"} <= index.keys():
        return {}
    by_icao: dict[str, dict[int, list[Any]]] = {}
    for row in rows:
        icao, t = row[index["icao24"]], row[index["time"]]
        lat, lon = row[index["lat"]], row[index["lon"]]
        if icao is None or t is None or lat is None or lon is None:
            continue
        baro = row[index["baroaltitude"]] if "baroaltitude" in index else None
        point = [
            int(t) - segment_utc,
            round(lat, 5),
            round(lon, 5),
            None if baro is None else round(baro * M_TO_FT),
        ]
        by_icao.setdefault(icao, {})[int(t)] = point
    out = {}
    for icao, points in by_icao.items():
        ordered = [points[t] for t in sorted(points)]
        if len(ordered) > max_points:
            step = (len(ordered) - 1) / (max_points - 1)
            ordered = [ordered[round(i * step)] for i in range(max_points)]
        out[icao] = ordered
    return out


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
