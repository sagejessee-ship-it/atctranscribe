"""Build synthetic RTLSDR-Airband-style corpora for tests.

Audio fixtures are synthetic tones encoded like the real collector output
(MPEG-2.5 layer III, 8 kHz mono, LAME VBR with a Xing header).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aerochorus.worker.audio import probe_audio

FIXTURES = Path(__file__).parent / "fixtures" / "audio"
TONE_A = (FIXTURES / "tone_a.mp3").read_bytes()
TONE_B = (FIXTURES / "tone_b.mp3").read_bytes()
TONE_A_MS = probe_audio(TONE_A, ".mp3").duration_ms
TONE_B_MS = probe_audio(TONE_B, ".mp3").duration_ms
NEW_YORK = ZoneInfo("America/New_York")


def to_ns(moment: datetime) -> int:
    return int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000


@dataclass(frozen=True)
class Placed:
    relative_path: str
    path: Path
    sha256: str
    mtime_ns: int


def place(
    root: Path,
    relative_path: str,
    data: bytes = TONE_A,
    *,
    start_utc: datetime | None = None,
    mtime_utc: datetime | None = None,
) -> Placed:
    """Write a file whose mtime mimics the collector closing it at start + duration."""
    path = root.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime_utc is None:
        if start_utc is None:
            raise ValueError("start_utc or mtime_utc required")
        duration = probe_audio(data, ".mp3").duration_ms or 0
        mtime_utc = start_utc + timedelta(milliseconds=duration + 700)
    mtime_ns = to_ns(mtime_utc)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return Placed(relative_path, path, hashlib.sha256(data).hexdigest(), mtime_ns)


def local(*args: int) -> datetime:
    """Wall-clock time in America/New_York converted to UTC (fold=0)."""
    return datetime(*args, tzinfo=NEW_YORK).astimezone(UTC)


def build_bwi_corpus(root: Path) -> dict[str, Placed]:
    """A miniature of the real archive's shape and quirks."""
    root.mkdir(parents=True, exist_ok=True)
    files = [
        # Pre-dated-directory files at the root, as on 2026-07-16.
        place(
            root, "BWI_CLNC_20260716_185205_118050000.mp3", start_utc=local(2026, 7, 16, 18, 52, 5)
        ),
        place(
            root,
            "2026/07/16/BWI_TWR_20260716_190000_119400000.mp3",
            TONE_B,
            start_utc=local(2026, 7, 16, 19, 0, 0),
        ),
        place(
            root,
            "2026/09/08/BWI_GND_20260908_000024_121900000.mp3",
            start_utc=local(2026, 9, 8, 0, 0, 24),
        ),
        place(
            root,
            "2026/09/08/BWI_TWR_20260908_092648_119400000.mp3",
            TONE_B,
            start_utc=local(2026, 9, 8, 9, 26, 48),
        ),
        place(
            root,
            "2026/09/08/BWI_APP_FS_20260908_101500_119700000.mp3",
            start_utc=local(2026, 9, 8, 10, 15, 0),
        ),
        # mtime 9.5 hours after the named start: the name cannot be corroborated.
        place(
            root,
            "2026/09/08/BWI_TWR_20260908_141532_119400000.mp3",
            mtime_utc=local(2026, 9, 8, 23, 49, 23),
        ),
        # 01:30 on the DST fall-back day happens twice; the mtime says it was
        # the second (EST) occurrence.
        place(
            root,
            "2026/11/01/BWI_TWR_20261101_013000_119400000.mp3",
            start_utc=datetime(2026, 11, 1, 6, 30, 0, tzinfo=UTC),
        ),
    ]
    (root / "2026" / "09" / "08" / "notes.txt").write_text("not audio")
    return {f.relative_path: f for f in files}


def snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    """sha256, size and mtime of every file under root."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            out[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                st.st_size,
                st.st_mtime_ns,
            )
    return out
