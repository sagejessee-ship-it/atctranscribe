import wave
from datetime import UTC, datetime

from atco2_builder import GOLD_PHRASES, build_atco2

from aerochorus.datasets.atco2 import (
    MANIFEST_NAME,
    derived_split,
    parse_clip_path,
    parse_recording_id,
    prepare_fixed_clips,
    read_gold_references,
)


def test_recording_id_carries_station_frequency_and_utc():
    name = parse_recording_id("LSGS_SION_Ground_Control_121_7MHz_20210501_073256")
    assert (name.station, name.channel, name.frequency_hz) == (
        "LSGS",
        "SION_Ground_Control",
        121_700_000,
    )
    assert name.start_utc == datetime(2021, 5, 1, 7, 32, 56, tzinfo=UTC)
    assert parse_recording_id("not_an_atco2_name") is None


def test_prepare_clips_exact_boundaries_without_gold_text(tmp_path):
    data = build_atco2(tmp_path / "DATA")
    out = tmp_path / "clips"
    summary = prepare_fixed_clips(data, out)

    assert (summary.recordings, summary.clips, summary.written) == (2, 4, 4)
    assert any("segment_end_clamped" in w for w in summary.warnings)
    clip = out / "LKPR_RUZYNE_Radar_120_520MHz_20201025_091112" / "02_3790_6850.wav"
    with wave.open(str(clip)) as w:
        assert w.getnframes() == round(6.85 * 16000) - round(3.79 * 16000)
    clamped = out / "LSZH_ZURICH_Tower_118_1MHz_20210412_161248" / "02_2200_4000.wav"
    assert clamped.exists()

    # The leakage boundary: no gold text anywhere in the prepared corpus.
    for path in out.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert not any(p.encode() in content for p in GOLD_PHRASES), path
    assert (out / MANIFEST_NAME).exists()

    again = prepare_fixed_clips(data, out)
    assert (again.written, again.reused) == (0, 4)


def test_gold_references_are_keyed_by_clip_and_split_deterministically(tmp_path):
    data = build_atco2(tmp_path / "DATA")
    rows = read_gold_references(data)
    assert [r["relative_path"] for r in rows] == [
        "LKPR_RUZYNE_Radar_120_520MHz_20201025_091112/01_0_3770.wav",
        "LKPR_RUZYNE_Radar_120_520MHz_20201025_091112/02_3790_6850.wav",
        "LSZH_ZURICH_Tower_118_1MHz_20210412_161248/01_500_2000.wav",
        "LSZH_ZURICH_Tower_118_1MHz_20210412_161248/02_2200_4000.wav",
    ]
    assert rows[0]["text_raw"].startswith("[#callsign]Oscar")
    # one split per recording, stable across calls
    for row in rows:
        assert row["split"] == derived_split(row["recording_id"])
        assert row["split_source"] == "derived_recording_hash"
    recording, index, start, end = parse_clip_path(rows[1]["relative_path"])
    assert (recording.station, index, start, end) == ("LKPR", 2, 3790, 6850)
