"""Historical ADS-B for one airport area from adsb.lol's daily globe_history dumps (ADR-024).

adsb.lol publishes every aircraft trace its network saw on a UTC day as a GitHub
release (``adsblol/globe_history_<year>``, tag ``v<YYYY.MM.DD>-planes-readsb-<pod>``),
openly licensed (ODbL 1.0). A day is a split tar (``.tar.aa``, ``.tar.ab``, ~3.5 GB)
holding readsb's ``traces/<xx>/trace_full_<icao>.json`` (gzip JSON, one per aircraft,
a point every few seconds) plus heatmaps, which are skipped.

This module streams such a day once and keeps only the positions inside an airport
area. Nothing is written to disk. A cheap text pre-filter skips the ~97 % of
aircraft that never come near, so a day costs about its download time. It has no
database access: the API stores what it yields.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import re
import tarfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, BinaryIO

import httpx

GITHUB_API = "https://api.github.com"
PODS = ("prod-0", "prod-1", "staging-0")  # adsb.lol's dump sources, in order of preference
SOURCE = "adsb.lol globe_history (readsb traces)"
LICENSE = "ODbL 1.0, adsb.lol contributors"
FLAG_STALE = 1  # readsb: no position received for 20 s; the point marks a gap


class DayStatus(StrEnum):
    QUEUED = "queued"
    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


class NotPublished(RuntimeError):
    """adsb.lol has no dump for this day (yet: a day is published after it ends)."""


@dataclass(frozen=True)
class Area:
    center_lat: float
    center_lon: float
    radius_nm: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        dlat = self.radius_nm / 60.0
        dlon = self.radius_nm / (60.0 * max(math.cos(math.radians(self.center_lat)), 0.01))
        return (
            self.center_lat - dlat,
            self.center_lat + dlat,
            self.center_lon - dlon,
            self.center_lon + dlon,
        )

    def contains(self, lat: float, lon: float) -> bool:
        lat_min, lat_max, lon_min, lon_max = self.bounds
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            return False
        return distance_nm(self.center_lat, self.center_lon, lat, lon) <= self.radius_nm

    def prefilter(self) -> re.Pattern[bytes]:
        """Matches a trace point ``,<lat>,<lon>,`` whose integer degrees fall in the
        area's box, on the raw JSON. Coarse on purpose: exact checks follow."""
        lat_min, lat_max, lon_min, lon_max = self.bounds

        def whole(lo: float, hi: float) -> str:
            # The integer part as written in JSON: -76.6 is "-76", -0.4 is "-0".
            parts = set()
            v = lo
            while v < hi:
                parts.add(_int_text(v))
                v += 0.25
            parts.add(_int_text(hi))
            return "|".join(sorted(re.escape(p) for p in parts))

        lat, lon = whole(lat_min, lat_max), whole(lon_min, lon_max)
        return re.compile(rf",(?:{lat})(?:\.\d+)?,(?:{lon})(?:\.\d+)?,".encode())


def _int_text(v: float) -> str:
    return ("-" if v < 0 else "") + str(int(abs(v)))


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Position:
    t: int  # unix seconds
    icao24: str
    callsign: str | None
    lat: float
    lon: float
    alt_ft: int | None  # barometric (or geometric when readsb had only that); None on the ground
    on_ground: bool
    gs_kt: float | None
    track_deg: float | None
    vrate_fpm: int | None


@dataclass(frozen=True)
class AircraftInfo:
    icao24: str
    registration: str | None
    type_code: str | None
    description: str | None


def parse_trace(doc: dict[str, Any], area: Area) -> tuple[list[Position], AircraftInfo | None]:
    """The positions of one readsb trace inside ``area``, one per second at most."""
    icao = str(doc.get("icao") or "").lower()
    base = float(doc.get("timestamp") or 0)
    if not icao or not base:
        return [], None
    callsign: str | None = None
    by_second: dict[int, Position] = {}
    for point in doc.get("trace") or ():
        if len(point) < 6:
            continue
        details = point[8] if len(point) > 8 else None
        if isinstance(details, dict) and details.get("flight"):
            callsign = str(details["flight"]).strip() or None
        flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
        lat, lon = point[1], point[2]
        if flags & FLAG_STALE or lat is None or lon is None or not area.contains(lat, lon):
            continue
        alt = point[3]
        on_ground = alt == "ground"
        vrate = point[7] if len(point) > 7 else None
        t = int(round(base + float(point[0])))
        by_second[t] = Position(
            t=t,
            icao24=icao,
            callsign=callsign,
            lat=round(float(lat), 6),
            lon=round(float(lon), 6),
            alt_ft=None if on_ground or not isinstance(alt, int | float) else int(alt),
            on_ground=on_ground,
            gs_kt=None if point[4] is None else float(point[4]),
            track_deg=None if point[5] is None else float(point[5]),
            vrate_fpm=None if not isinstance(vrate, int | float) else int(vrate),
        )
    if not by_second:
        return [], None
    info = AircraftInfo(
        icao24=icao,
        registration=doc.get("r") or None,
        type_code=doc.get("t") or None,
        description=doc.get("desc") or None,
    )
    return [by_second[t] for t in sorted(by_second)], info


def iter_day(stream: BinaryIO, area: Area) -> Iterator[tuple[list[Position], AircraftInfo]]:
    """Every aircraft of one day's tar stream that came inside ``area``."""
    pattern = area.prefilter()
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            name = member.name
            if not member.isfile() or "trace_full_" not in name or "traces/" not in name:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            if data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except (OSError, EOFError):
                    continue
            if not pattern.search(data):
                continue
            try:
                doc = json.loads(data)
            except ValueError:
                continue
            positions, info = parse_trace(doc, area)
            if positions and info is not None:
                yield positions, info


class ChainedStream(io.RawIOBase):
    """Several byte iterables read as one file (a split tar's parts), counting bytes."""

    def __init__(
        self, parts: Iterable[Iterable[bytes]], on_progress: Callable[[int], None] | None = None
    ):
        self._chunks = (chunk for part in parts for chunk in part)
        self._buffer = b""
        self.bytes_read = 0
        self._on_progress = on_progress

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
            except StopIteration:
                return 0
            self.bytes_read += len(self._buffer)
            if self._on_progress is not None:
                self._on_progress(self.bytes_read)
        n = min(len(target), len(self._buffer))
        target[:n] = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return n


@dataclass(frozen=True)
class Release:
    tag: str
    assets: list[tuple[str, int]]  # (download URL, size in bytes), in part order

    @property
    def size(self) -> int:
        return sum(size for _, size in self.assets)


def find_release(day: date, http: httpx.Client) -> Release:
    """The day's dump on GitHub, from the first pod that published one."""
    for pod in PODS:
        tag = f"v{day:%Y.%m.%d}-planes-readsb-{pod}"
        response = http.get(
            f"{GITHUB_API}/repos/adsblol/globe_history_{day.year}/releases/tags/{tag}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code == 404:
            continue
        response.raise_for_status()
        assets = sorted(
            (a["name"], a["browser_download_url"], int(a["size"]))
            for a in response.json().get("assets", [])
            if ".tar" in a["name"]
        )
        if assets:
            return Release(tag, [(url, size) for _, url, size in assets])
    raise NotPublished(f"adsb.lol has no dump for {day} (a day is published after it ends)")


def download_parts(release: Release, http: httpx.Client) -> Iterator[Iterator[bytes]]:
    """The release's parts as streamed byte iterators, opened one after the other."""
    for url, _ in release.assets:

        def part(url: str = url) -> Iterator[bytes]:
            with http.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                yield from response.iter_bytes(1 << 20)

        yield part()
