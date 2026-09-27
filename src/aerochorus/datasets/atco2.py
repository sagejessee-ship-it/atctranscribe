"""ATCO2-ASRdataset-v1_beta adapter (ported from v1, docs/v1-audit).

Leakage boundary (ADR-015):

* ``prepare_fixed_clips`` reads XML **boundaries only** and writes provider-safe
  clips + a manifest. The prepared directory never contains gold text; it is
  registered as an ordinary read-only corpus source and transcribed like any
  other audio.
* ``read_gold_references`` is the only function that returns gold text. Its
  output goes to the control plane's isolated ``reference`` schema and nowhere
  else.
* ``.info`` context and ``.cnet`` alternatives are not read at all.
"""

from __future__ import annotations

import hashlib
import json
import re
import wave
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROFILE = "atco2_fixed_no_vad_gold_boundaries"
MANIFEST_NAME = "aerochorus_atco2_manifest.json"
# atco2 recording ids: <ICAO>_<place/position…>_<freq>MHz_<YYYYMMDD>_<HHMMSS>
_RECORDING_ID = re.compile(
    r"^(?P<icao>[A-Z0-9]{4})_(?P<channel>.+?)_(?P<freq>\d+(?:_\d+)?)MHz_(?P<date>\d{8})_(?P<time>\d{6})$"
)
_CLIP_NAME = re.compile(r"^(?P<index>\d{2,})_(?P<start>\d+)_(?P<end>\d+)\.wav$")


@dataclass(frozen=True)
class Atco2XmlSegment:
    source_index: int
    start_ms: int
    end_ms: int
    speaker: str
    speaker_label: str
    text: str
    tags: dict[str, str]
    split: str | None


@dataclass(frozen=True)
class RecordingName:
    recording_id: str
    station: str
    channel: str
    frequency_hz: int
    start_utc: datetime


def parse_recording_id(recording_id: str) -> RecordingName | None:
    match = _RECORDING_ID.match(recording_id)
    if match is None:
        return None
    try:
        start = datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return RecordingName(
        recording_id=recording_id,
        station=match["icao"],
        channel=match["channel"],
        frequency_hz=round(float(match["freq"].replace("_", ".")) * 1_000_000),
        start_utc=start,
    )


def clip_relative_path(recording_id: str, segment: Atco2XmlSegment) -> str:
    return f"{recording_id}/{segment.source_index:02d}_{segment.start_ms}_{segment.end_ms}.wav"


def parse_clip_path(relative_path: str) -> tuple[RecordingName, int, int, int] | None:
    """Inverse of ``clip_relative_path``: (recording, source_index, start_ms, end_ms)."""
    recording_id, _, name = relative_path.rpartition("/")
    recording = parse_recording_id(recording_id)
    match = _CLIP_NAME.match(name)
    if recording is None or match is None:
        return None
    return recording, int(match["index"]), int(match["start"]), int(match["end"])


def derived_split(recording_id: str) -> str:
    """Deterministic held-out split by recording (the corpus ships none).

    Whole recordings stay on one side so neighbouring transmissions cannot leak
    across the calibration/test boundary.
    """
    digest = hashlib.sha256(recording_id.casefold().encode()).digest()
    return "calibration" if digest[0] % 2 == 0 else "test"


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_xml(xml_path: Path) -> tuple[list[Atco2XmlSegment], list[str]]:
    root = ET.parse(str(xml_path)).getroot()
    segments, warnings = [], []
    for index, element in enumerate(root.findall("segment"), start=1):
        try:
            start = float(element.findtext("start") or "")
            end = float(element.findtext("end") or "")
        except ValueError:
            warnings.append(f"segment_{index}:invalid_start_or_end")
            continue
        if start < 0 or end <= start:
            warnings.append(f"segment_{index}:invalid_time_range")
            continue
        tags_element = element.find("tags")
        tags = {} if tags_element is None else {c.tag: _clean(c.text) for c in tags_element}
        segments.append(
            Atco2XmlSegment(
                source_index=index,
                start_ms=round(start * 1000),
                end_ms=round(end * 1000),
                speaker=_clean(element.findtext("speaker")),
                speaker_label=_clean(element.findtext("speaker_label")),
                text=_clean(element.findtext("text")),
                tags=tags,
                split=_clean(element.findtext("split")) or None,
            )
        )
    return segments, warnings


def discover_recordings(data_root: Path) -> list[tuple[str, Path, Path]]:
    """(recording_id, wav, xml) for every complete pair, sorted case-insensitively."""
    root = Path(data_root)
    if not root.is_dir():
        raise FileNotFoundError(f"ATCO2 data root not found: {root}")
    wavs = {p.stem.casefold(): p for p in root.rglob("*") if p.suffix.lower() == ".wav"}
    pairs = []
    for p in root.rglob("*"):
        if p.suffix.lower() == ".xml" and p.stem.casefold() in wavs:
            pairs.append((p.stem, wavs[p.stem.casefold()], p))
    return sorted(pairs, key=lambda item: item[0].casefold())


@dataclass
class PrepareSummary:
    profile: str
    data_root: str
    output_dir: str
    recordings: int = 0
    clips: int = 0
    gold_speech_ms: int = 0
    written: int = 0
    reused: int = 0
    warnings: list[str] = field(default_factory=list)


def _slice_wav(source: Path, target: Path, start_ms: int, end_ms: int) -> None:
    with wave.open(str(source), "rb") as src:
        params = src.getparams()
        rate = src.getframerate()
        first = round(start_ms * rate / 1000)
        last = min(round(end_ms * rate / 1000), src.getnframes())
        src.setpos(first)
        frames = src.readframes(max(0, last - first))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with wave.open(str(tmp), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    tmp.replace(target)


def prepare_fixed_clips(data_root: Path, output_dir: Path) -> PrepareSummary:
    """Clip every gold segment at its exact XML boundaries (no VAD, no padding).

    Deterministic and idempotent: an existing clip with the expected name is
    reused. Clip names are derived from the gold boundaries, so identity does
    not depend on processing order.
    """
    output_dir = Path(output_dir)
    summary = PrepareSummary(PROFILE, str(Path(data_root).resolve()), str(output_dir.resolve()))
    manifest_rows: list[dict[str, Any]] = []
    for recording_id, wav_path, xml_path in discover_recordings(data_root):
        try:
            segments, warnings = parse_xml(xml_path)
        except (ET.ParseError, OSError) as exc:
            summary.warnings.append(f"xml_unreadable:{recording_id}:{type(exc).__name__}")
            continue
        summary.warnings += [f"{recording_id}:{w}" for w in warnings]
        with wave.open(str(wav_path), "rb") as src:
            duration_ms = round(src.getnframes() * 1000 / src.getframerate())
        kept = 0
        for segment in segments:
            if segment.start_ms >= duration_ms:
                summary.warnings.append(
                    f"segment_outside_audio:{recording_id}:{segment.source_index}"
                )
                continue
            end_ms = min(segment.end_ms, duration_ms)
            if end_ms != segment.end_ms:
                summary.warnings.append(
                    f"segment_end_clamped:{recording_id}:{segment.source_index}"
                )
            clamped = Atco2XmlSegment(**(asdict(segment) | {"end_ms": end_ms}))
            relative = clip_relative_path(recording_id, clamped)
            target = output_dir.joinpath(*relative.split("/"))
            if target.exists():
                summary.reused += 1
            else:
                _slice_wav(wav_path, target, clamped.start_ms, end_ms)
                summary.written += 1
            kept += 1
            summary.clips += 1
            summary.gold_speech_ms += end_ms - clamped.start_ms
            # Provider-safe: boundaries and provenance only, never text.
            manifest_rows.append(
                {
                    "relative_path": relative,
                    "recording_id": recording_id,
                    "source_segment_index": segment.source_index,
                    "source_wav": wav_path.name,
                    "clip_start_ms": clamped.start_ms,
                    "clip_end_ms": end_ms,
                    "boundary_pad_ms": 0,
                    "vad_enabled": False,
                }
            )
        summary.recordings += bool(kept)
    manifest = {"profile": PROFILE, "summary": asdict(summary), "clips": manifest_rows}
    (output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return summary


def read_gold_references(data_root: Path) -> list[dict[str, Any]]:
    """Gold rows keyed by clip path. The ONLY reader of ATCO2 transcript text."""
    rows = []
    for recording_id, wav_path, xml_path in discover_recordings(data_root):
        segments, _ = parse_xml(xml_path)
        with wave.open(str(wav_path), "rb") as src:
            duration_ms = round(src.getnframes() * 1000 / src.getframerate())
        for segment in segments:
            if segment.start_ms >= duration_ms:
                continue
            clamped = Atco2XmlSegment(
                **(asdict(segment) | {"end_ms": min(segment.end_ms, duration_ms)})
            )
            rows.append(
                {
                    "relative_path": clip_relative_path(recording_id, clamped),
                    "dataset": "ATCO2-ASRdataset-v1_beta",
                    "recording_id": recording_id,
                    "source_segment_index": segment.source_index,
                    "start_ms": clamped.start_ms,
                    "end_ms": clamped.end_ms,
                    "speaker": segment.speaker,
                    "speaker_label": segment.speaker_label,
                    "text_raw": segment.text,
                    "tags": segment.tags,
                    "split": segment.split or derived_split(recording_id),
                    "split_source": "xml" if segment.split else "derived_recording_hash",
                    "non_english": segment.tags.get("non_english") == "1",
                }
            )
    return rows
