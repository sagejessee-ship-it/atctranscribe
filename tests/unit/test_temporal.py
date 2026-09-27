from datetime import UTC, datetime, timedelta

from aerochorus.contracts import TemporalStatus
from aerochorus.corpus.temporal import resolve_capture_time

NY = "America/New_York"


def resolve(wall, mtime, duration_ms=3000, tz=NY, tolerance=120):
    return resolve_capture_time(
        wall_clock_start=wall,
        timezone_name=tz,
        duration_ms=duration_ms,
        file_mtime_utc=mtime,
        tolerance_seconds=tolerance,
    )


def test_real_segment_in_edt_resolves():
    # BWI_GND_20260908_000024_121900000.mp3 from the archive.
    result = resolve(
        datetime(2026, 9, 8, 0, 0, 24),
        datetime(2026, 9, 8, 4, 0, 26, 986359, tzinfo=UTC),
        duration_ms=2232,
    )
    assert result.status == TemporalStatus.RESOLVED
    assert result.capture_start_utc == datetime(2026, 9, 8, 4, 0, 24, tzinfo=UTC)
    assert result.capture_end_utc == datetime(2026, 9, 8, 4, 0, 26, 232000, tzinfo=UTC)
    assert result.evidence["utc_offset"] == "-0400"
    assert 0 < result.evidence["mtime_delta_s"] < 1


def test_winter_uses_est():
    start = datetime(2026, 12, 15, 15, 0, tzinfo=UTC)
    result = resolve(datetime(2026, 12, 15, 10, 0), start + timedelta(seconds=4))
    assert result.status == TemporalStatus.RESOLVED
    assert result.capture_start_utc == start


def test_dst_fold_resolved_by_mtime_either_way():
    wall = datetime(2026, 11, 1, 1, 30)
    first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # EDT occurrence
    second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # EST occurrence
    for truth in (first, second):
        result = resolve(wall, truth + timedelta(seconds=3.5))
        assert result.status == TemporalStatus.RESOLVED
        assert result.capture_start_utc == truth
        assert result.evidence["fold_resolved_by"] == "mtime"


def test_dst_fold_without_corroboration_is_ambiguous_and_has_no_utc():
    wall = datetime(2026, 11, 1, 1, 30)
    result = resolve(wall, datetime(2026, 11, 3, tzinfo=UTC))
    assert result.status == TemporalStatus.AMBIGUOUS
    assert result.capture_start_utc is None
    assert len(result.evidence["fold_candidates_utc"]) == 2

    unknown_duration = resolve(wall, datetime(2026, 11, 1, 6, 30, 3, tzinfo=UTC), duration_ms=None)
    assert unknown_duration.status == TemporalStatus.AMBIGUOUS


def test_spring_forward_gap_is_unresolved():
    result = resolve(datetime(2027, 3, 14, 2, 30), datetime(2027, 3, 14, 7, 30, tzinfo=UTC))
    assert result.status == TemporalStatus.UNRESOLVED
    assert result.evidence["reason"] == "nonexistent_local_time"
    assert result.capture_start_utc is None


def test_disagreeing_mtime_keeps_name_time_but_flags_it():
    # 2026/07/18/BWI_TWR_20260718_141532: mtime 9.5 hours after the named start.
    result = resolve(
        datetime(2026, 7, 18, 14, 15, 32),
        datetime(2026, 7, 19, 3, 49, 23, tzinfo=UTC),
        duration_ms=216,
    )
    assert result.status == TemporalStatus.UNVERIFIED
    assert result.capture_start_utc == datetime(2026, 7, 18, 18, 15, 32, tzinfo=UTC)
    assert result.evidence["reason"] == "mtime_disagrees"


def test_unknown_duration_is_unverified():
    result = resolve(
        datetime(2026, 9, 8, 0, 0, 24), datetime(2026, 9, 8, 4, 0, 26, tzinfo=UTC), None
    )
    assert result.status == TemporalStatus.UNVERIFIED
    assert result.capture_end_utc is None
    assert result.evidence["reason"] == "duration_unknown"


def test_utc_collector():
    result = resolve(
        datetime(2026, 9, 8, 4, 0, 24), datetime(2026, 9, 8, 4, 0, 28, tzinfo=UTC), tz="UTC"
    )
    assert result.status == TemporalStatus.RESOLVED
    assert result.capture_start_utc == datetime(2026, 9, 8, 4, 0, 24, tzinfo=UTC)


def test_missing_inputs_never_fabricate_a_time():
    mtime = datetime(2026, 9, 8, 4, 0, 28, tzinfo=UTC)
    no_name_time = resolve(None, mtime)
    assert no_name_time.status == TemporalStatus.UNRESOLVED
    assert no_name_time.capture_start_utc is None
    no_zone = resolve(datetime(2026, 9, 8, 0, 0, 24), mtime, tz=None)
    assert no_zone.status == TemporalStatus.UNRESOLVED
    assert no_zone.evidence["reason"] == "timezone_not_configured"
