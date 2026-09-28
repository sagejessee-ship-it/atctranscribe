"""Synthetic ATCO2-format data for tests (stdlib WAV + XML sidecars)."""

from __future__ import annotations

import wave
from pathlib import Path

RECORDINGS = {
    "LKPR_RUZYNE_Radar_120_520MHz_20201025_091112": [
        (
            0.0,
            3.77,
            "A",
            "[#callsign]Oscar Kilo Papa Mike Bravo[/#callsign] "
            "[#command]descend[/#command] [#value]flight level one hundred[/#value]",
        ),
        (3.79, 6.85, "B", "level one hundred Oscar Kilo Papa Mike Bravo"),
    ],
    "LSZH_ZURICH_Tower_118_1MHz_20210412_161248": [
        (
            0.5,
            2.0,
            "A",
            "[#callsign]Swiss Two Three[/#callsign] [hes] [#command]cleared to land[/#command]",
        ),
        # Ends past the audio: must be clamped, as v1 did.
        (2.2, 9.9, "B", "cleared to land Swiss Two Three"),
    ],
}
AUDIO_SECONDS = {
    "LKPR_RUZYNE_Radar_120_520MHz_20201025_091112": 7.0,
    "LSZH_ZURICH_Tower_118_1MHz_20210412_161248": 4.0,
}
GOLD_PHRASES = ("descend", "cleared to land", "Oscar Kilo")


def build_atco2(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for recording_id, segments in RECORDINGS.items():
        with wave.open(str(root / f"{recording_id}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\x01\x00" * int(16000 * AUDIO_SECONDS[recording_id]))
        rows = []
        for start, end, speaker, text in segments:
            rows.append(
                f"<segment><start>{start}</start><end>{end}</end><speaker>{speaker}</speaker>"
                f"<speaker_label>x</speaker_label><text>{text}</text><tags><correct>0</correct>"
                f"<non_english>0</non_english></tags></segment>"
            )
        (root / f"{recording_id}.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?><data>' + "".join(rows) + "</data>",
            encoding="utf-8",
        )
        (root / f"{recording_id}.info").write_text("airport: TEST\n", encoding="utf-8")
    return root
