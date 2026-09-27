import pytest
from pydantic import ValidationError

from aerochorus.corpus.config import FilesystemAdapterConfig


def test_parser_requires_explicit_timezone():
    with pytest.raises(ValidationError, match="filename_timezone is required"):
        FilesystemAdapterConfig(filename_parser="rtlsdr_airband")


def test_unknown_timezone_rejected():
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        FilesystemAdapterConfig(filename_parser="rtlsdr_airband", filename_timezone="Mars/Olympus")


def test_extensions_normalized():
    config = FilesystemAdapterConfig(include_extensions=["MP3", ".wav", "mp3"])
    assert config.include_extensions == [".mp3", ".wav"]


def test_sentinels_must_be_relative():
    with pytest.raises(ValidationError):
        FilesystemAdapterConfig(sentinel_paths=["/Volumes/ATC/2026"])


def test_tolerance_must_stay_below_half_an_hour():
    # Otherwise it could not tell the two DST fold candidates apart.
    with pytest.raises(ValidationError):
        FilesystemAdapterConfig(mtime_tolerance_seconds=3600)


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError):
        FilesystemAdapterConfig(root="/Volumes/ATC")
