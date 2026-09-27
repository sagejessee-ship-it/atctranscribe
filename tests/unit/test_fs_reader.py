import os
import stat

import pytest
from corpus_builder import TONE_A, build_bwi_corpus, snapshot

from aerochorus.worker.fs import ReadOnlyCorpusReader


def test_listing_classifies_entries(tmp_path):
    root = tmp_path / "corpus"
    build_bwi_corpus(root)
    (root / "2026" / "09" / "08" / ".DS_Store").write_bytes(b"x")
    reader = ReadOnlyCorpusReader(root)

    top = reader.list_directory("")
    assert top.subdirs == ["2026"]
    assert [f.name for f in top.files] == ["BWI_CLNC_20260716_185205_118050000.mp3"]

    day = reader.list_directory("2026/09/08")
    assert "notes.txt" in [f.name for f in day.files]
    assert day.skipped == [".DS_Store"]
    assert day.mtime_ns == os.stat(root / "2026" / "09" / "08").st_mtime_ns
    assert all(f.size > 0 for f in day.files)


def test_symlinks_are_never_followed(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp3").write_bytes(TONE_A)
    (root / "real.mp3").write_bytes(TONE_A)
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted on this platform/user")
    listing = ReadOnlyCorpusReader(root).list_directory("")
    assert listing.subdirs == []
    assert listing.skipped == ["link"]


def test_availability_checks(tmp_path):
    missing = ReadOnlyCorpusReader(tmp_path / "not-mounted").check_available()
    assert not missing.ok

    empty_mount_point = tmp_path / "ATC"
    empty_mount_point.mkdir()
    empty = ReadOnlyCorpusReader(empty_mount_point).check_available()
    assert not empty.ok and "empty" in empty.reason

    a_file = tmp_path / "file"
    a_file.write_bytes(b"x")
    assert not ReadOnlyCorpusReader(a_file).check_available().ok

    root = tmp_path / "corpus"
    build_bwi_corpus(root)
    assert ReadOnlyCorpusReader(root).check_available().ok
    assert ReadOnlyCorpusReader(root, ["2026"]).check_available().ok
    sentinel = ReadOnlyCorpusReader(root, ["2027"]).check_available()
    assert not sentinel.ok and "sentinel" in sentinel.reason


def test_reading_leaves_read_only_files_untouched(tmp_path):
    root = tmp_path / "corpus"
    placed = build_bwi_corpus(root)
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)
    before = snapshot(root)

    reader = ReadOnlyCorpusReader(root)
    for relative_path in placed:
        assert reader.read_bytes(relative_path)
    assert snapshot(root) == before
