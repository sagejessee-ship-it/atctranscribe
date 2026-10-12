"""Squelch splitting: digital-silence runs, transmissions, median pitch, WAV inputs."""

from __future__ import annotations

import array
import io
import math
import shutil
import wave

import pytest

from aerochorus.adjudication import squelch

RATE = squelch.RATE


def voice(seconds: float, f0: float, amplitude: int = 6000) -> list[int]:
    """A harmonic-rich tone, closer to a voice than a sine (and to band-limited radio)."""
    n = int(seconds * RATE)
    return [
        int(amplitude * sum(math.sin(2 * math.pi * f0 * k * i / RATE) / k for k in (1, 2, 3)))
        for i in range(n)
    ]


def zeros(seconds: float) -> list[int]:
    return [0] * int(seconds * RATE)


def pcm(*parts: list[int]) -> array.array:
    return array.array("h", [x for part in parts for x in part])


def test_silence_runs_need_digital_silence_for_at_least_50_ms():
    samples = pcm(voice(0.2, 150), zeros(0.06), voice(0.2, 150), zeros(0.03), voice(0.2, 150))
    runs = squelch.silence_runs(samples)
    assert len(runs) == 1  # 30 ms is too short to be a squelch gap
    start, end = runs[0]
    assert abs(start / RATE - 0.2) < 0.005 and abs((end - start) / RATE - 0.06) < 0.005
    # One least-significant bit of dither is still digital silence (-90 dBFS); two is not.
    dithered = pcm(voice(0.2, 150), [1, -1] * 800, voice(0.2, 150))
    assert len(squelch.silence_runs(dithered)) == 1
    noisy = pcm(voice(0.2, 150), [2, -2] * 800, voice(0.2, 150))
    assert squelch.silence_runs(noisy) == []


def test_transmissions_are_split_with_bounds_and_pitch():
    samples = pcm(zeros(0.1), voice(1.0, 200), zeros(0.15), voice(1.2, 120), zeros(0.2))
    pieces = squelch.split_on_digital_silence(samples)
    assert len(pieces) == 2
    first, second = pieces
    assert first.start_s == pytest.approx(0.1, abs=0.01)  # leading silence dropped
    assert first.end_s == pytest.approx(1.1, abs=0.01)
    assert second.start_s == pytest.approx(1.25, abs=0.01)
    assert second.end_s == pytest.approx(2.45, abs=0.01)  # trailing silence dropped
    assert first.f0_hz == pytest.approx(200, rel=0.06)
    assert second.f0_hz == pytest.approx(120, rel=0.06)
    assert first.describe() == {"start_s": 0.1, "end_s": 1.1, "f0_hz": first.f0_hz}


def test_squelch_flutter_joins_a_neighbour_and_pieces_fit_the_input_limit():
    # A 100 ms fragment between two gaps is flutter, not a transmission.
    samples = pcm(voice(1.0, 180), zeros(0.08), voice(0.1, 180), zeros(0.3), voice(1.0, 110))
    pieces = squelch.split_on_digital_silence(samples)
    assert len(pieces) == 2
    assert pieces[0].end_s == pytest.approx(1.18, abs=0.01)  # joined the closer neighbour

    four = pcm(
        voice(0.5, 180), zeros(0.3), voice(0.5, 110), zeros(0.06), voice(0.5, 110),
        zeros(0.3), voice(0.5, 180),
    )  # fmt: skip
    assert len(squelch.split_on_digital_silence(four)) == 4
    fitted = squelch.split_on_digital_silence(four, max_pieces=3)
    assert len(fitted) == 3  # merged across the shortest gap (60 ms)
    assert fitted[1].start_s == pytest.approx(0.8, abs=0.01)
    assert fitted[1].end_s == pytest.approx(1.86, abs=0.01)
    assert len(squelch.split_on_digital_silence(four, max_pieces=1)) == 1


def test_pitch_is_none_without_voice():
    assert squelch.median_f0(pcm(zeros(1.0)), 0, RATE) is None
    assert squelch.median_f0(pcm(voice(0.02, 150)), 0, int(0.02 * RATE)) is None  # too short


def test_wav_inputs_and_their_description():
    samples = pcm(voice(0.5, 150))
    with wave.open(io.BytesIO(squelch.wav_bytes(samples)), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, RATE)
        back = array.array("h", wav.readframes(wav.getnframes()))
    assert back == samples
    pieces = [squelch.Piece(0, RATE, RATE, 182.0), squelch.Piece(int(1.2 * RATE), 3 * RATE, RATE)]
    text = squelch.describe_inputs(pieces, 3.0)
    assert "audio 1 is the whole clip (3.0 s)" in text
    assert "audio 2: 0.00-1.00 s, pitch 182 Hz" in text
    assert "audio 3: 1.20-3.00 s\n" in text  # no pitch, no claim


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_decode_through_ffmpeg_keeps_the_silence():
    source = squelch.wav_bytes(pcm(voice(0.5, 150), zeros(0.2), voice(0.5, 150)))
    samples = squelch.decode_pcm(source, shutil.which("ffmpeg"))
    assert len(samples) == pytest.approx(1.2 * RATE, rel=0.01)
    assert len(squelch.split_on_digital_silence(samples)) == 2
    with pytest.raises(ValueError, match="could not decode"):
        squelch.decode_pcm(b"not audio at all", shutil.which("ffmpeg"))
