"""Audio container probing (duration, rate, channels) without decoding."""

from __future__ import annotations

import io

import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from aerochorus.contracts import AudioProbe

_PROBES = {".mp3": ("mp3", MP3), ".wav": ("wav", WAVE), ".flac": ("flac", FLAC)}


def probe_audio(data: bytes, extension: str) -> AudioProbe:
    fmt, probe_cls = _PROBES.get(extension.lower(), (None, None))
    try:
        audio = probe_cls(io.BytesIO(data)) if probe_cls else mutagen.File(io.BytesIO(data))
        if audio is None or audio.info is None:
            return AudioProbe(format=fmt, error="unrecognized_audio_format")
        info = audio.info
        length = getattr(info, "length", None)
        return AudioProbe(
            format=fmt or type(audio).__name__.lower(),
            duration_ms=round(length * 1000) if length is not None else None,
            sample_rate=getattr(info, "sample_rate", None),
            channels=getattr(info, "channels", None),
            bitrate=getattr(info, "bitrate", None) or None,
        )
    except Exception as exc:  # malformed audio must not stop a scan
        return AudioProbe(format=fmt, error=f"{type(exc).__name__}: {exc}"[:500])
