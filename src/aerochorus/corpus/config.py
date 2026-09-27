"""Adapter configuration stored with a corpus source.

This describes how the *collector* wrote the corpus (file types, filename
convention, the clock its timestamps use). It is corpus semantics, so it lives
in PostgreSQL with the source. Where the corpus is mounted on a given machine
is not part of it; see ``aerochorus.worker.config``.
"""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aerochorus.corpus.paths import normalize_relative_path

FilenameParserName = Literal["rtlsdr_airband"]


class FilesystemAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_extensions: list[str] = Field(default_factory=lambda: [".mp3", ".wav", ".flac"])
    filename_parser: FilenameParserName | None = None
    # IANA zone of the wall-clock timestamps in filenames. Required whenever a
    # parser is configured: AeroChorus never guesses a collector's clock.
    filename_timezone: str | None = None
    # |file mtime - (filename start + duration)| within which the filename time
    # counts as corroborated. Must stay well below one hour so that it can
    # disambiguate DST fall-back folds.
    mtime_tolerance_seconds: float = Field(default=120.0, gt=0, lt=1800)
    # Files (and directories) modified more recently than this are treated as
    # still being written and are revisited on a later scan.
    min_file_age_seconds: float = Field(default=120.0, ge=0)
    # Source-relative paths that must exist for the source to count as
    # available. Protects against an unmounted, empty mount point.
    sentinel_paths: list[str] = Field(default_factory=list)

    @field_validator("include_extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        out = []
        for ext in value:
            ext = ext.strip().lower()
            if not ext:
                continue
            out.append(ext if ext.startswith(".") else f".{ext}")
        if not out:
            raise ValueError("include_extensions must not be empty")
        return sorted(set(out))

    @field_validator("filename_timezone")
    @classmethod
    def _valid_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @field_validator("sentinel_paths")
    @classmethod
    def _valid_sentinels(cls, value: list[str]) -> list[str]:
        return [normalize_relative_path(v) for v in value]

    @model_validator(mode="after")
    def _parser_needs_zone(self) -> FilesystemAdapterConfig:
        if self.filename_parser is not None and self.filename_timezone is None:
            raise ValueError(
                "filename_timezone is required when filename_parser is set "
                "(use 'UTC' if the collector writes UTC filenames)"
            )
        return self
