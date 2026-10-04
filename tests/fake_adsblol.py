"""A deterministic adsb.lol day: a real split tar in readsb's globe_history layout."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import date

from aerochorus.context import adsblol

BWI = (39.1757, -76.6690)


def _trace(icao: str, t0: int, points: list[list], **extra) -> dict:
    return {"icao": icao, "timestamp": t0, "trace": points, **extra}


def day_traces(t: int) -> list[dict]:
    """Traffic around unix time ``t``: an arrival, an aircraft on the ramp, an overflight
    just outside 25 nm, and one in Europe."""
    lat0, lon0 = BWI
    arrival = []
    for i, dt in enumerate(range(-300, 301, 5)):
        # From 12 nm south-east toward the field, descending.
        f = (dt + 300) / 600
        arrival.append(
            [
                dt + 300,
                round(lat0 - 0.15 + 0.15 * f, 6),
                round(lon0 + 0.15 - 0.15 * f, 6),
                int(4000 - 3500 * f),
                140.0,
                315.0,
                0,
                -700,
                {"flight": "SWA456  "} if i == 0 else None,
            ]
        )
    arrival.insert(
        10, [arrival[10][0] - 1, lat0 - 0.1, lon0 + 0.1, 3000, 140.0, 315.0, 1, None]
    )  # stale
    ramp = [
        [dt + 300, lat0 + 0.002, lon0 - 0.003, "ground", 0.0, 90.0, 0, None]
        for dt in range(-300, 301, 30)
    ]
    ramp[3] += [{"flight": "N123AB"}]  # details may appear mid-trace
    outside = [  # ~27 nm north-east: inside the lat/lon box, outside the 25 nm circle
        [dt + 300, lat0 + 0.35, lon0 + 0.35, 35000, 450.0, 0.0, 0, 0] for dt in range(-300, 301, 10)
    ]
    europe = [[dt + 300, 50.03, 8.57, 12000, 300.0, 90.0, 0, 0] for dt in range(-300, 301, 10)]
    return [
        _trace("a0f00d", t - 300, arrival, r="N456WN", t="B737", desc="BOEING 737-700"),
        _trace("c0ffee", t - 300, ramp, t="C172"),
        _trace("0b0b0b", t - 300, outside, t="A320"),
        _trace("3c6abc", t - 300, europe, t="A321"),
    ]


def day_tar(t: int | None) -> bytes:
    """The tar for one day (traffic around ``t``, or none), with a heatmap member first."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:

        def add(name: str, payload: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        add("./heatmap/00.bin.ttf", b"\x00" * 4096)
        for i, doc in enumerate(day_traces(t) if t is not None else []):
            raw = json.dumps(doc, separators=(",", ":")).encode()
            body = gzip.compress(raw) if i % 2 == 0 else raw  # readsb writes both
            add(f"./traces/{doc['icao'][-2:]}/trace_full_{doc['icao']}.json", body)
    return buffer.getvalue()


class FakeOpener:
    """Stands in for GitHub: serves ``day_tar`` split into two parts, like .tar.aa/.tar.ab."""

    def __init__(self, t: int, *, missing: set[date] | None = None) -> None:
        self.t = t
        self.missing = missing or set()
        self.opened: list[date] = []

    def __call__(self, day: date):
        self.opened.append(day)
        if day in self.missing:
            raise adsblol.NotPublished(f"adsb.lol has no dump for {day}")
        from datetime import UTC, datetime

        has_traffic = datetime.fromtimestamp(self.t, UTC).date() == day
        blob = day_tar(self.t if has_traffic else None)
        cut = len(blob) // 3
        parts = [blob[:cut], blob[cut:]]
        release = adsblol.Release(
            f"v{day:%Y.%m.%d}-planes-readsb-prod-0",
            [(f"https://example.invalid/part{i}", len(p)) for i, p in enumerate(parts)],
        )
        return release, ([p[i : i + 700] for i in range(0, len(p), 700)] for p in parts)
