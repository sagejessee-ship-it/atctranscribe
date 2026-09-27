from datetime import datetime

import pytest

from aerochorus.corpus.naming import RtlsdrAirbandParser, parse_filename

parser = RtlsdrAirbandParser()


def test_real_collector_name():
    parsed = parser.parse("BWI_GND_20260908_000024_121900000.mp3")
    assert parsed.matched
    assert parsed.wall_clock_start == datetime(2026, 9, 8, 0, 0, 24)
    assert (parsed.station, parsed.channel, parsed.frequency_hz) == ("BWI", "GND", 121_900_000)
    assert parsed.label == "BWI_GND"
    assert parsed.problems == ()


@pytest.mark.parametrize(
    ("name", "station", "channel"),
    [
        ("BWI_APP_FS_20260908_101500_119700000.mp3", "BWI", "APP_FS"),
        ("BWI_GND_ALT_20260908_101500_120200000.mp3", "BWI", "GND_ALT"),
        ("KBWI2_TWR_20260908_101500_119400000.mp3", "KBWI2", "TWR"),
    ],
)
def test_multi_part_labels(name, station, channel):
    parsed = parser.parse(name)
    assert (parsed.station, parsed.channel) == (station, channel)


def test_label_without_station_is_channel_only():
    parsed = parser.parse("tower_20260908_101500.mp3")
    assert parsed.station is None
    assert parsed.channel == "tower"
    assert parsed.frequency_hz is None


def test_invalid_date_is_reported_not_guessed():
    parsed = parser.parse("BWI_GND_20261340_250000_121900000.mp3")
    assert parsed.matched
    assert parsed.wall_clock_start is None
    assert "invalid_datetime_in_name" in parsed.problems


def test_non_matching_name():
    parsed = parser.parse("recording.mp3")
    assert not parsed.matched
    assert parsed.wall_clock_start is None


def test_no_parser_configured():
    assert parse_filename(None, "BWI_GND_20260908_000024_121900000.mp3") is None
