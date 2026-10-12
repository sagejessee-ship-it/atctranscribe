"""A clip's transmissions, split where the recorder wrote digital silence.

The archive's recorder keeps one file open across a short exchange and writes
digital silence (exact zeros) while the squelch is closed. Each stretch between
such runs is one transmission, often from another speaker (controller, then
pilot). A self-hosted adjudicator gets the whole clip plus each transmission as
its own audio input, with its bounds and median voice pitch (ADR-025).

Pure standard library; ffmpeg decodes from stdin, as in the dataset export, so it
never sees a source path.
"""

from __future__ import annotations

import array
import math
import statistics
import struct
import subprocess
import sys
from dataclasses import dataclass
from operator import mul

RATE = 16_000
SILENCE_DBFS = -90.0  # at 16 bit, about one least-significant bit
MIN_SILENCE_MS = 50
MIN_PIECE_MS = 250  # shorter fragments (squelch flutter) join a neighbour


@dataclass(frozen=True)
class Piece:
    """One transmission: [start, end) in samples, and its median pitch."""

    start: int
    end: int
    rate: int
    f0_hz: float | None = None

    @property
    def start_s(self) -> float:
        return self.start / self.rate

    @property
    def end_s(self) -> float:
        return self.end / self.rate

    def describe(self) -> dict[str, float | None]:
        return {
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
            "f0_hz": self.f0_hz,
        }


def decode_pcm(data: bytes, ffmpeg: str, rate: int = RATE) -> array.array:
    """Mono 16-bit samples at ``rate``; ValueError if ffmpeg cannot decode the audio."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
             "-ac", "1", "-ar", str(rate), "pipe:1"],
            input=data,
            capture_output=True,
            timeout=120,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"ffmpeg failed: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise ValueError(f"ffmpeg could not decode the audio: {detail or 'no samples'}")
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) - len(proc.stdout) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def silence_runs(
    samples: array.array,
    rate: int = RATE,
    *,
    threshold_dbfs: float = SILENCE_DBFS,
    min_silence_ms: int = MIN_SILENCE_MS,
) -> list[tuple[int, int]]:
    """[start, end) runs at or below the threshold lasting at least ``min_silence_ms``."""
    limit = 32768 * 10 ** (threshold_dbfs / 20)
    min_len = max(1, rate * min_silence_ms // 1000)
    runs: list[tuple[int, int]] = []
    start = None
    for i, x in enumerate(samples):
        if -limit <= x <= limit:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_len:
                runs.append((start, i))
            start = None
    if start is not None and len(samples) - start >= min_len:
        runs.append((start, len(samples)))
    return runs


def _merge(bounds: list[tuple[int, int]], i: int) -> None:
    """Join piece i and i+1 (the silence between them becomes part of the piece)."""
    bounds[i : i + 2] = [(bounds[i][0], bounds[i + 1][1])]


def _absorb_fragments(bounds: list[tuple[int, int]], min_len: int) -> None:
    while len(bounds) > 1:
        shortest = min(range(len(bounds)), key=lambda i: bounds[i][1] - bounds[i][0])
        if bounds[shortest][1] - bounds[shortest][0] >= min_len:
            return
        if shortest == 0:
            _merge(bounds, 0)
        elif shortest == len(bounds) - 1:
            _merge(bounds, shortest - 1)
        else:  # towards the closer neighbour
            before = bounds[shortest][0] - bounds[shortest - 1][1]
            after = bounds[shortest + 1][0] - bounds[shortest][1]
            _merge(bounds, shortest - 1 if before <= after else shortest)


def _fit(bounds: list[tuple[int, int]], max_pieces: int) -> None:
    """Merge across the shortest gaps (least likely a change of speaker) until it fits."""
    while len(bounds) > max(1, max_pieces):
        gap = min(range(len(bounds) - 1), key=lambda i: bounds[i + 1][0] - bounds[i][1])
        _merge(bounds, gap)


def median_f0(
    samples: array.array,
    start: int,
    end: int,
    rate: int = RATE,
    *,
    fmin: float = 70.0,
    fmax: float = 400.0,
    max_frames: int = 40,
) -> float | None:
    """Median voice pitch (Hz) over the loudest frames, by normalised autocorrelation;
    None when fewer than three frames are clearly voiced. A hint, not a measurement:
    radio audio is band-limited, so octave errors happen."""
    step = max(1, rate // 8000)  # about 8 kHz is plenty for 70-400 Hz
    x = [sum(samples[i : i + step]) / step for i in range(start, end - step + 1, step)]
    sr = rate / step
    frame, hop = int(0.04 * sr), int(0.02 * sr)
    lag_min, lag_max = int(sr / fmax), int(sr / fmin)
    if len(x) < frame + lag_max + 1:
        return None
    energies = sorted(
        ((sum(map(mul, x[i : i + frame], x[i : i + frame])), i)
         for i in range(0, len(x) - frame - lag_max, hop)),
        reverse=True,
    )[:max_frames]  # fmt: skip
    if not energies or energies[0][0] <= 0:
        return None
    f0s = []
    for energy, i in energies:
        if energy < 0.1 * energies[0][0]:
            break
        seg = x[i : i + frame]
        scores = []
        for lag in range(lag_min, lag_max + 1):
            other = x[i + lag : i + lag + frame]
            denom = math.sqrt(energy * sum(map(mul, other, other)))
            scores.append(sum(map(mul, seg, other)) / denom if denom else 0.0)
        best = max(scores)
        if best < 0.5:
            continue  # unvoiced or noise
        # The first peak scoring near the best: a multiple of the period scores as well.
        k = next(k for k, s in enumerate(scores) if s >= 0.85 * best)
        while k + 1 < len(scores) and scores[k + 1] > scores[k]:
            k += 1
        f0s.append(sr / (lag_min + k))
    return round(statistics.median(f0s), 1) if len(f0s) >= 3 else None


def split_on_digital_silence(
    samples: array.array,
    rate: int = RATE,
    *,
    threshold_dbfs: float = SILENCE_DBFS,
    min_silence_ms: int = MIN_SILENCE_MS,
    min_piece_ms: int = MIN_PIECE_MS,
    max_pieces: int | None = None,
) -> list[Piece]:
    """The transmissions between digital-silence runs, in order, with median pitch.
    Leading and trailing silence is dropped; fragments shorter than ``min_piece_ms``
    join a neighbour; with ``max_pieces`` the closest pieces are merged until it fits."""
    bounds: list[tuple[int, int]] = []
    position = 0
    for start, end in silence_runs(
        samples, rate, threshold_dbfs=threshold_dbfs, min_silence_ms=min_silence_ms
    ):
        if start > position:
            bounds.append((position, start))
        position = end
    if position < len(samples):
        bounds.append((position, len(samples)))
    _absorb_fragments(bounds, rate * min_piece_ms // 1000)
    if max_pieces is not None:
        _fit(bounds, max_pieces)
    return [Piece(s, e, rate, median_f0(samples, s, e, rate)) for s, e in bounds]


def wav_bytes(samples: array.array, rate: int = RATE) -> bytes:
    """16-bit mono PCM WAV, built in memory (the canonical 44-byte header)."""
    pcm = array.array("h", samples)
    if sys.byteorder == "big":
        pcm.byteswap()
    data = pcm.tobytes()
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,  # PCM, mono, 16 bit
        b"data", len(data),
    )  # fmt: skip
    return header + data


def describe_inputs(pieces: list[Piece], duration_s: float) -> str:
    """The context section that tells the model what each audio input is."""
    lines = [
        f"Audio inputs: audio 1 is the whole clip ({duration_s:.1f} s). The squelch closed "
        f"between transmissions, so it holds {len(pieces)}; audio 2 to {len(pieces) + 1} "
        "are those transmissions on their own, in order. Pitch is the speaker's median "
        "voice pitch: a clearly different pitch usually means a different speaker."
    ]
    for n, piece in enumerate(pieces, 2):
        pitch = f", pitch {piece.f0_hz:.0f} Hz" if piece.f0_hz else ""
        lines.append(f"  audio {n}: {piece.start_s:.2f}-{piece.end_s:.2f} s{pitch}")
    lines.append(
        "Transcribe the whole clip (audio 1) as one transcript, every transmission in order."
    )
    return "\n".join(lines)
