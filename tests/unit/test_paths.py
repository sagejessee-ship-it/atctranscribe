import pytest

from aerochorus.corpus.paths import (
    InvalidRelativePath,
    is_within,
    join_relative,
    normalize_relative_dir,
    normalize_relative_path,
    parent_dir,
)


def test_valid_relative_path_is_kept():
    path = "2026/09/08/BWI_GND_20260908_000024_121900000.mp3"
    assert normalize_relative_path(path) == path


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/abs/path.mp3",
        "a//b.mp3",
        "a/../b.mp3",
        "./a.mp3",
        "a/.",
        "a\\b.mp3",
        "C:/x.mp3",
        "a/",
        "a\x00b",
    ],
)
def test_invalid_relative_paths_are_rejected(bad):
    with pytest.raises(InvalidRelativePath):
        normalize_relative_path(bad)


def test_unicode_is_nfc_normalized():
    decomposed = "cafe\u0301.mp3"
    assert normalize_relative_path(decomposed) == "caf\u00e9.mp3"


def test_directories_allow_root_and_strip_trailing_slash():
    assert normalize_relative_dir("") == ""
    assert normalize_relative_dir("2026/09/") == "2026/09"
    with pytest.raises(InvalidRelativePath):
        normalize_relative_dir("../outside")


def test_join_and_parent():
    assert join_relative("", "a.mp3") == "a.mp3"
    assert join_relative("2026/09", "08") == "2026/09/08"
    assert parent_dir("2026/09/08/x.mp3") == "2026/09/08"
    assert parent_dir("x.mp3") == ""


def test_scope_containment():
    assert is_within("2026/09/08", "")
    assert is_within("2026/09/08", "2026/09")
    assert is_within("2026/09", "2026/09")
    assert not is_within("2026/090", "2026/09")
    assert not is_within("2026", "2026/09")
