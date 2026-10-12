"""ADR-024: airport-area positions from adsb.lol's daily readsb dumps (no network)."""

from __future__ import annotations

import io
from datetime import date

import httpx
import pytest
from fake_adsblol import BWI, FakeOpener, day_tar, day_traces

from aerochorus.context import adsblol

AREA = adsblol.Area(*BWI, 25.0)
T = 1_790_456_756  # 2026-09-26T21:05:56Z


def test_prefilter_matches_integer_degrees_as_written():
    pattern = AREA.prefilter()
    assert pattern.search(b"[12.3,39.123456,-76.543210,3500,140]")
    assert pattern.search(b"[1,38.9,-77.01,ground,0]")
    assert not pattern.search(b"[1,48.9,-76.5,100,0]")
    assert not pattern.search(b"[1,39.1,76.5,100,0]")  # east, not west
    # Around 0/0 the integer part is written "-0" on one side.
    zero = adsblol.Area(0.05, -0.05, 20).prefilter()
    assert zero.search(b"[1,-0.2,0.1,100,0]") and zero.search(b"[1,0.3,-0.1,100,0]")


def test_trace_keeps_positions_inside_the_area_once_per_second():
    arrival, ramp, outside, europe = day_traces(T)
    positions, info = adsblol.parse_trace(arrival, AREA)
    assert info == adsblol.AircraftInfo("a0f00d", "N456WN", "B737", "BOEING 737-700")
    assert {p.callsign for p in positions} == {"SWA456"}  # carried forward, trimmed
    assert all(not p.on_ground and p.alt_ft is not None for p in positions)
    assert [p.t for p in positions] == sorted({p.t for p in positions})  # one per second
    assert len(positions) == 121  # the stale point is a gap marker, not a position

    ramp_positions, _ = adsblol.parse_trace(ramp, AREA)
    assert all(p.on_ground and p.alt_ft is None for p in ramp_positions)
    assert ramp_positions[0].callsign is None and ramp_positions[-1].callsign == "N123AB"

    assert adsblol.parse_trace(outside, AREA) == ([], None)  # inside the box, beyond 25 nm
    assert adsblol.parse_trace(europe, AREA) == ([], None)


def test_a_split_day_streams_through_in_one_pass():
    release, parts = FakeOpener(T)(date(2026, 9, 26))
    seen = []
    stream = adsblol.ChainedStream(parts, seen.append)
    found = {info.icao24: len(p) for p, info in adsblol.iter_day(io.BufferedReader(stream), AREA)}
    assert found == {"a0f00d": 121, "c0ffee": 21}
    assert stream.bytes_read == release.size == len(day_tar(T))
    assert seen[-1] == release.size  # progress reported as bytes arrive


def test_find_release_tries_each_pod_and_orders_the_parts():
    def handler(request: httpx.Request) -> httpx.Response:
        if "prod-0" in request.url.path:
            return httpx.Response(404)
        tag = request.url.path.rsplit("/", 1)[1]
        assets = [
            {"name": f"{tag}.tar.ab", "browser_download_url": "https://x/ab", "size": 2},
            {"name": f"{tag}.tar.aa", "browser_download_url": "https://x/aa", "size": 3},
            {"name": "notes.txt", "browser_download_url": "https://x/n", "size": 1},
        ]
        return httpx.Response(200, json={"assets": assets})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        release = adsblol.find_release(date(2026, 9, 26), http)
    assert release.tag == "v2026.09.26-planes-readsb-prod-1"
    assert release.assets == [("https://x/aa", 3), ("https://x/ab", 2)] and release.size == 5

    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http,
        pytest.raises(adsblol.NotPublished),
    ):
        adsblol.find_release(date(2026, 9, 26), http)
