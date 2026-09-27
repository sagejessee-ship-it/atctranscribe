"""Materialize a frozen training dataset into clip audio (Phase 5B).

Runs where the corpus is mounted. For each frozen item it:

1. reads the parent segment through ``ReadOnlyCorpusReader`` and checks its
   SHA-256 against the value frozen in the dataset (a changed source aborts);
2. decodes it with ffmpeg from stdin (ffmpeg never sees a source path) and trims
   spans to ``[start_ms, end_ms)``;
3. writes 16 kHz mono 16-bit PCM WAV with Python's ``wave`` (exact headers);
4. hashes each clip, writes ``manifest.jsonl`` + ``dataset.json`` (export-only
   files, ADR-003), and reports clip hashes to the control plane.

Source audio is never modified; the output directory may not be inside a source.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from aerochorus.dataset_contracts import DatasetItem, DatasetView, ExportedClip, ExportReport
from aerochorus.worker.client import ApiClient
from aerochorus.worker.config import WorkerConfig
from aerochorus.worker.fs import ReadOnlyCorpusReader

SAMPLE_RATE = 16_000
AUDIO_FORMAT = "wav/pcm_s16le/16000Hz/mono"


class ExportError(RuntimeError):
    pass


@dataclass
class ExportResult:
    dataset: DatasetView
    directory: Path
    clips: int
    audio_ms: int


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def find_ffmpeg(explicit: str | None = None) -> str:
    candidate = explicit or os.environ.get("AEROCHORUS_FFMPEG") or shutil.which("ffmpeg")
    if not candidate:
        raise ExportError("ffmpeg not found: install it (brew/winget) or pass --ffmpeg")
    return candidate


def ffmpeg_version(ffmpeg: str) -> str:
    out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, check=False)
    return (out.stdout.splitlines() or ["ffmpeg (unknown version)"])[0]


def decode_pcm(ffmpeg: str, audio: bytes, start_ms: int | None, end_ms: int | None) -> bytes:
    """Source bytes in, mono 16 kHz s16le PCM out. Trimming happens after decoding."""
    filters = []
    if start_ms is not None and end_ms is not None:
        filters.append(f"atrim=start={start_ms / 1000:.3f}:end={end_ms / 1000:.3f}")
        filters.append("asetpts=PTS-STARTPTS")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if filters:
        command += ["-af", ",".join(filters)]
    command += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"]
    result = subprocess.run(command, input=audio, capture_output=True, check=False)
    if result.returncode != 0:
        raise ExportError(f"ffmpeg failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def write_wav(path: Path, pcm: bytes) -> bytes:
    tmp = path.with_suffix(".part")
    with wave.open(str(tmp), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)
    tmp.replace(path)
    return path.read_bytes()


def clip_name(item: DatasetItem) -> str:
    bounds = f"_{item.start_ms}-{item.end_ms}" if item.scope == "span" else ""
    return f"{item.ordinal:06d}_seg{item.segment_id}{bounds}.wav"


def fetch_items(client: ApiClient, dataset_id: int, page: int = 1000) -> list[DatasetItem]:
    items: list[DatasetItem] = []
    while True:
        batch = client.dataset_items(dataset_id, offset=len(items), limit=page)
        items += batch
        if len(batch) < page:
            return items


def export_dataset(
    client: ApiClient,
    config: WorkerConfig,
    dataset_id: int,
    out_dir: Path,
    *,
    ffmpeg: str | None = None,
    exported_by: str | None = None,
) -> ExportResult:
    ffmpeg = find_ffmpeg(ffmpeg)
    dataset = client.get_dataset(dataset_id)
    target = Path(out_dir) / f"{dataset.name}-v{dataset.version}"
    for key, mount in config.sources.items():
        if _is_within(target, mount.root):
            raise ExportError(f"{target} is inside corpus source {key!r}; exports never go there")
    items = fetch_items(client, dataset_id)
    readers: dict[str, ReadOnlyCorpusReader] = {}
    cache: dict[int, bytes] = {}  # several spans can share a parent segment

    clips: list[ExportedClip] = []
    lines: list[str] = []
    for item in items:
        mount = config.sources.get(item.source_key)
        if mount is None:
            raise ExportError(f"source {item.source_key!r} is not mounted on this machine")
        reader = readers.setdefault(item.source_key, ReadOnlyCorpusReader(mount.root))
        if item.segment_id not in cache:
            reader.require_available()
            audio = reader.read_bytes(item.relative_path)
            digest = hashlib.sha256(audio).hexdigest()
            if item.source_sha256 and digest != item.source_sha256:
                raise ExportError(
                    f"{item.source_key}/{item.relative_path} changed since the dataset was frozen "
                    f"({digest[:12]}… != {item.source_sha256[:12]}…)"
                )
            cache = {item.segment_id: audio}  # items are ordered by segment: keep one
        pcm = decode_pcm(ffmpeg, cache[item.segment_id], item.start_ms, item.end_ms)
        relative = f"clips/{item.split}/{clip_name(item)}"
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = write_wav(path, pcm)
        duration_ms = round(len(pcm) / 2 / SAMPLE_RATE * 1000)
        clip = ExportedClip(
            ordinal=item.ordinal,
            clip_path=relative,
            clip_sha256=hashlib.sha256(data).hexdigest(),
            clip_bytes=len(data),
            duration_ms=duration_ms,
        )
        clips.append(clip)
        lines.append(
            json.dumps(
                {
                    "audio": relative,
                    "text": item.text,
                    "split": item.split,
                    "duration_ms": duration_ms,
                    "clip_sha256": clip.clip_sha256,
                    "label": item.training_label.value,
                    "text_origin": item.text_origin,
                    "scope": item.scope,
                    "segment_id": item.segment_id,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "source": f"{item.source_key}/{item.relative_path}",
                    "source_sha256": item.source_sha256,
                    "annotation_version_id": item.annotation_version_id,
                    "split_group": item.split_group,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    manifest = ("\n".join(lines) + "\n").encode()
    (target / "manifest.jsonl").write_bytes(manifest)
    tool = ffmpeg_version(ffmpeg)
    report = ExportReport(
        exported_by=exported_by or socket.gethostname(),
        out_uri=target.resolve().as_uri(),
        audio_format=AUDIO_FORMAT,
        tool=tool,
        manifest_file_sha256=hashlib.sha256(manifest).hexdigest(),
        clips=clips,
    )
    (target / "dataset.json").write_text(
        json.dumps(
            dataset.model_dump(mode="json")
            | {"export": report.model_dump(mode="json", exclude={"clips"})},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    stored = client.record_dataset_export(dataset_id, report)
    return ExportResult(stored, target, len(clips), sum(c.duration_ms for c in clips))
