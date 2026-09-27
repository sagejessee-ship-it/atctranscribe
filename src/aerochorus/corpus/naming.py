"""Filename conventions: the timestamp extraction framework.

A parser turns a file's basename into structured facts the collector encoded
there. Parsers only report what the name says; converting wall-clock times to
UTC is ``aerochorus.corpus.temporal``'s job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ParsedName:
    parser: str
    matched: bool
    # Naive wall-clock start time as written in the name, in the collector's
    # configured timezone.
    wall_clock_start: datetime | None = None
    label: str | None = None
    station: str | None = None
    channel: str | None = None
    frequency_hz: int | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)

    def evidence(self) -> dict:
        return {
            "parser": self.parser,
            "matched": self.matched,
            "label": self.label,
            "wall_clock_start": self.wall_clock_start.isoformat()
            if self.wall_clock_start
            else None,
            "problems": list(self.problems),
        }


class FilenameParser(Protocol):
    name: str

    def parse(self, basename: str) -> ParsedName: ...


class RtlsdrAirbandParser:
    """RTLSDR-Airband ``split_on_transmission`` output.

    ``<template>_<YYYYMMDD>_<HHMMSS>[_<frequency Hz>].<ext>``, for example
    ``BWI_GND_20260908_000024_121900000.mp3``. The time is the start of the
    transmission in the collector's clock (local time when the collector runs
    with ``localtime = true``). A template of the form ``STATION_POSITION``
    is split into station and channel.
    """

    name = "rtlsdr_airband"
    _PATTERN = re.compile(
        r"^(?P<label>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_(?P<freq>\d+))?\.[A-Za-z0-9]+$"
    )

    def parse(self, basename: str) -> ParsedName:
        match = self._PATTERN.match(basename)
        if match is None:
            return ParsedName(parser=self.name, matched=False, problems=("name_pattern_mismatch",))

        label = match["label"]
        station, _, channel = label.partition("_")
        problems: list[str] = []
        try:
            wall_clock = datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S")
        except ValueError:
            wall_clock = None
            problems.append("invalid_datetime_in_name")

        return ParsedName(
            parser=self.name,
            matched=True,
            wall_clock_start=wall_clock,
            label=label,
            station=station if channel else None,
            channel=channel or station,
            frequency_hz=int(match["freq"]) if match["freq"] else None,
            problems=tuple(problems),
        )


PARSERS: dict[str, FilenameParser] = {
    RtlsdrAirbandParser.name: RtlsdrAirbandParser(),
}


def parse_filename(parser_name: str | None, basename: str) -> ParsedName | None:
    if parser_name is None:
        return None
    return PARSERS[parser_name].parse(basename)
