"""End-to-end corpus indexing: worker scanner -> HTTP API -> PostgreSQL."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from corpus_builder import TONE_A, TONE_A_MS, TONE_B, local, place, snapshot
from integration_support import SOURCE_KEY, make_scanner
from sqlalchemy import func, select, update

from aerochorus.contracts import ScanCreate, ScanMode, ScanStatus
from aerochorus.db.models import CorpusDirectory, CorpusScan, Segment
from aerochorus.worker.client import ApiError
from aerochorus.worker.fs import ReadOnlyCorpusReader

AUDIO_FILES = 7


def segments(db) -> dict[str, Segment]:
    db.expire_all()
    return {s.relative_path: s for s in db.scalars(select(Segment))}


def test_initial_scan_indexes_every_audio_file(api, db, source, corpus):
    root, placed = corpus
    scan = make_scanner(api, root).scan(SOURCE_KEY)

    assert scan.status == ScanStatus.COMPLETED
    assert scan.counters["files_read"] == AUDIO_FILES
    assert scan.counters["files_ignored"] == 1  # notes.txt
    assert scan.counters["segments_inserted"] == AUDIO_FILES

    rows = segments(db)
    assert set(rows) == set(placed)
    for path, fact in placed.items():
        assert rows[path].sha256 == fact.sha256
        assert rows[path].file_mtime_ns == fact.mtime_ns
        assert rows[path].presence_status == "present"

    gnd = rows["2026/09/08/BWI_GND_20260908_000024_121900000.mp3"]
    assert (gnd.station, gnd.channel, gnd.frequency_hz) == ("BWI", "GND", 121_900_000)
    assert gnd.relative_dir == "2026/09/08"
    assert gnd.duration_ms == TONE_A_MS
    assert gnd.temporal_status == "resolved"
    assert gnd.capture_start_utc == datetime(2026, 9, 8, 4, 0, 24, tzinfo=UTC)
    assert gnd.capture_end_utc == gnd.capture_start_utc + timedelta(milliseconds=TONE_A_MS)
    assert gnd.metadata_["audio"]["sample_rate"] == 8000
    assert gnd.metadata_["naming"]["label"] == "BWI_GND"

    assert rows["BWI_CLNC_20260716_185205_118050000.mp3"].relative_dir == ""
    assert rows["2026/09/08/BWI_APP_FS_20260908_101500_119700000.mp3"].channel == "APP_FS"

    fold = rows["2026/11/01/BWI_TWR_20261101_013000_119400000.mp3"]
    assert fold.temporal_status == "resolved"
    assert fold.capture_start_utc == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)

    late = rows["2026/09/08/BWI_TWR_20260908_141532_119400000.mp3"]
    assert late.temporal_status == "unverified"
    assert late.capture_start_utc == local(2026, 9, 8, 14, 15, 32)


def test_rescans_are_idempotent_in_every_mode(api, db, source, corpus):
    root, _ = corpus
    scanner = make_scanner(api, root)
    scanner.scan(SOURCE_KEY)

    again = scanner.scan(SOURCE_KEY, ScanMode.INCREMENTAL)
    assert again.counters.get("files_read", 0) == 0
    assert again.counters.get("dirs_listed", 0) == 0
    # "", 2026, 2026/07, 2026/07/16, 2026/09, 2026/09/08, 2026/11, 2026/11/01
    assert again.counters["dirs_skipped"] == 8

    full = scanner.scan(SOURCE_KEY, ScanMode.FULL)
    assert full.counters.get("files_read", 0) == 0
    assert full.counters["files_unchanged"] == AUDIO_FILES

    verify = scanner.scan(SOURCE_KEY, ScanMode.VERIFY)
    assert verify.counters["files_read"] == AUDIO_FILES
    assert verify.counters.get("segments_content_changed", 0) == 0

    for scan in (again, full, verify):
        assert scan.status == ScanStatus.COMPLETED
        assert scan.counters.get("segments_marked_missing", 0) == 0
    assert db.scalar(select(func.count()).select_from(Segment)) == AUDIO_FILES


def test_source_files_are_never_modified(api, source, corpus):
    root, _ = corpus
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)
    before = snapshot(root)
    scanner = make_scanner(api, root)
    for mode in (ScanMode.INCREMENTAL, ScanMode.FULL, ScanMode.VERIFY, ScanMode.INCREMENTAL):
        assert scanner.scan(SOURCE_KEY, mode).status == ScanStatus.COMPLETED
    assert snapshot(root) == before


def test_incremental_scan_lists_only_changed_directories(api, db, source, corpus):
    root, _ = corpus
    scanner = make_scanner(api, root)
    scanner.scan(SOURCE_KEY)

    place(
        root,
        "2026/09/08/BWI_GND_20260908_120000_121900000.mp3",
        start_utc=local(2026, 9, 8, 12, 0, 0),
    )
    scan = scanner.scan(SOURCE_KEY)
    assert scan.counters["dirs_listed"] == 1
    assert scan.counters["files_read"] == 1
    assert scan.counters["files_unchanged"] == 4

    place(
        root,
        "2026/09/09/BWI_GND_20260909_080000_121900000.mp3",
        start_utc=local(2026, 9, 9, 8, 0, 0),
    )
    scan = scanner.scan(SOURCE_KEY)
    assert scan.counters["dirs_listed"] == 2  # 2026/09 gained a child; 09/09 is new
    assert scan.counters["files_read"] == 1
    assert len(segments(db)) == AUDIO_FILES + 2


def test_unavailable_source_leaves_the_index_alone(api, db, source, corpus, tmp_path):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    before = {p: (s.presence_status, s.last_seen_at) for p, s in segments(db).items()}

    empty_mount_point = tmp_path / "Volumes" / "ATC"
    empty_mount_point.mkdir(parents=True)
    for unavailable_root in (tmp_path / "offline", empty_mount_point):
        for mode in ScanMode:
            scan = make_scanner(api, unavailable_root).scan(SOURCE_KEY, mode)
            assert scan.status == ScanStatus.SOURCE_UNAVAILABLE
            assert scan.error_message

    after = {p: (s.presence_status, s.last_seen_at) for p, s in segments(db).items()}
    assert after == before


class DisconnectingReader(ReadOnlyCorpusReader):
    """Simulates the network share dropping after a number of listings."""

    def __init__(self, root, sentinels=(), *, listings_before_drop=2):
        super().__init__(root, sentinels)
        self.remaining = listings_before_drop
        self.dropped = False

    def check_available(self):
        if self.dropped:
            return type(super().check_available())(False, "share disconnected")
        return super().check_available()

    def dir_mtime_ns(self, relative_dir):
        if self.dropped:
            raise OSError(64, "The specified network name is no longer available")
        return super().dir_mtime_ns(relative_dir)

    def list_directory(self, relative_dir):
        if self.remaining == 0:
            self.dropped = True
            raise OSError(64, "The specified network name is no longer available")
        self.remaining -= 1
        return super().list_directory(relative_dir)


def test_disconnect_mid_scan_reports_source_unavailable(api, db, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)

    scanner = make_scanner(
        api, root, reader_factory=lambda r, s: DisconnectingReader(r, s, listings_before_drop=2)
    )
    scan = scanner.scan(SOURCE_KEY, ScanMode.FULL)
    assert scan.status == ScanStatus.SOURCE_UNAVAILABLE
    assert "disconnected" in scan.error_message
    assert all(s.presence_status == "present" for s in segments(db).values())

    # Progress from a partial first-ever scan is kept, and the next scan completes it.
    db.execute(Segment.__table__.delete())
    db.execute(CorpusDirectory.__table__.delete())
    db.commit()
    partial = make_scanner(
        api, root, reader_factory=lambda r, s: DisconnectingReader(r, s, listings_before_drop=4)
    ).scan(SOURCE_KEY)
    assert partial.status == ScanStatus.SOURCE_UNAVAILABLE
    indexed = len(segments(db))
    assert 0 < indexed < AUDIO_FILES
    resumed = make_scanner(api, root).scan(SOURCE_KEY)
    assert resumed.status == ScanStatus.COMPLETED
    assert resumed.counters["segments_inserted"] == AUDIO_FILES - indexed
    assert len(segments(db)) == AUDIO_FILES


def test_deleted_file_becomes_missing_then_reappears(api, db, source, corpus):
    root, placed = corpus
    scanner = make_scanner(api, root)
    scanner.scan(SOURCE_KEY)
    victim = placed["2026/09/08/BWI_GND_20260908_000024_121900000.mp3"]

    victim.path.unlink()
    scan = scanner.scan(SOURCE_KEY)
    assert scan.counters["segments_marked_missing"] == 1
    assert segments(db)[victim.relative_path].presence_status == "missing"

    victim.path.write_bytes(TONE_A)
    os.utime(victim.path, ns=(victim.mtime_ns, victim.mtime_ns))
    scan = scanner.scan(SOURCE_KEY)
    assert scan.counters["segments_reappeared"] == 1
    row = segments(db)[victim.relative_path]
    assert row.presence_status == "present"
    assert row.integrity_status == "ok"


def test_changed_content_is_flagged_not_silently_replaced(api, db, source, corpus):
    root, placed = corpus
    scanner = make_scanner(api, root)
    scanner.scan(SOURCE_KEY)
    target = placed["2026/09/08/BWI_APP_FS_20260908_101500_119700000.mp3"]

    target.path.write_bytes(TONE_B)
    scan = scanner.scan(SOURCE_KEY, ScanMode.FULL)
    assert scan.counters["segments_content_changed"] == 1
    row = segments(db)[target.relative_path]
    assert row.integrity_status == "changed"
    assert row.metadata_["integrity_history"][0]["previous"]["sha256"] == target.sha256


def test_verify_detects_changes_that_stat_cannot_see(api, db, source, corpus):
    root, placed = corpus
    scanner = make_scanner(api, root)
    scanner.scan(SOURCE_KEY)
    target = placed["2026/07/16/BWI_TWR_20260716_190000_119400000.mp3"]

    data = bytearray(target.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    target.path.write_bytes(bytes(data))
    os.utime(target.path, ns=(target.mtime_ns, target.mtime_ns))

    assert scanner.scan(SOURCE_KEY, ScanMode.FULL).counters.get("segments_content_changed", 0) == 0
    verify = scanner.scan(SOURCE_KEY, ScanMode.VERIFY)
    assert verify.counters["segments_content_changed"] == 1


def test_files_still_being_written_wait_for_a_later_scan(api, db, source, corpus):
    root, _ = corpus
    make_scanner(api, root).scan(SOURCE_KEY)
    fresh = place(
        root,
        "2026/09/08/BWI_TWR_20260908_130000_119400000.mp3",
        start_utc=local(2026, 9, 8, 13, 0, 0),
    )

    just_after = fresh.mtime_ns / 1e9 + 5
    scan = make_scanner(api, root, clock=just_after).scan(SOURCE_KEY)
    assert scan.counters["files_unsettled"] >= 1
    assert fresh.relative_path not in segments(db)
    state = db.get(CorpusDirectory, (1, "2026/09/08"))
    assert state.settled is False

    scan = make_scanner(api, root).scan(SOURCE_KEY)
    assert scan.counters["files_read"] == 1
    assert fresh.relative_path in segments(db)


def test_scope_prefix_limits_the_scan(api, db, source, corpus):
    root, _ = corpus
    scan = make_scanner(api, root).scan(SOURCE_KEY, ScanMode.FULL, "2026/09/08")
    assert scan.status == ScanStatus.COMPLETED
    assert {s.relative_dir for s in segments(db).values()} == {"2026/09/08"}

    missing = make_scanner(api, root).scan(SOURCE_KEY, ScanMode.FULL, "2031/01/01")
    assert missing.status == ScanStatus.FAILED


def test_one_running_scan_per_source(api, db, source, corpus):
    root, _ = corpus
    scanner = make_scanner(api, root)
    scanner.scan(SOURCE_KEY)  # registers the worker
    held = api.start_scan(ScanCreate(source_key=SOURCE_KEY, worker_name="test-worker"))
    with pytest.raises(ApiError) as excinfo:
        scanner.scan(SOURCE_KEY)
    assert excinfo.value.status_code == 409

    # A running scan that stopped reporting is abandoned when the next one starts.
    db.execute(
        update(CorpusScan)
        .where(CorpusScan.id == held.id)
        .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
    )
    db.commit()
    assert scanner.scan(SOURCE_KEY).status == ScanStatus.COMPLETED
    assert api.get_scan(held.id).status == ScanStatus.ABANDONED
