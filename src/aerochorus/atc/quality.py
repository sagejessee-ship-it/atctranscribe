"""Deterministic per-hypothesis quality flags.

Computed for every transcription result as it arrives (and recomputable with
``aerochorus results reflag``). They flag; they never edit text. The
thresholds are named, versioned and *unfitted*: Phase 6 refits them on the
ATCO2 calibration split before any of them gates a decision.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from aerochorus.atc.lexicon import (
    ATC_WORDS,
    CONVERSATIONAL_PHRASES,
    FILLER_TOKENS,
    LEXICON_VERSION,
    NATO_PHONETIC,
    NUMBER_WORDS,
    OFF_DOMAIN_PHRASES,
)
from aerochorus.atc.normalize import evidence_tokens, normalize_for_evidence

FLAGS_VERSION = 3 * 1000 + LEXICON_VERSION

# Unfitted defaults (see module docstring).
NON_LATIN_RATIO = 0.15
REPEAT_MIN_RUNS = 3
REPEAT_MIN_TOKENS = 6
MAX_CHARS_PER_SECOND = 30.0
LOW_ATC_MIN_TOKENS = 4
LOW_ATC_RATIO = 0.2


@dataclass(frozen=True)
class QualityFlags:
    flags: tuple[str, ...]
    details: dict

    def as_dict(self) -> dict:
        return {"version": FLAGS_VERSION, "flags": list(self.flags), "details": self.details}


def non_latin_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    non_latin = sum(1 for ch in letters if "LATIN" not in unicodedata.name(ch, ""))
    return non_latin / len(letters)


def has_diacritics(text: str) -> bool:
    """Latin letters with combining marks (č, ř, ó…): a hint of non-English output."""
    return any(
        ch.isalpha() and "LATIN" in unicodedata.name(ch, "") and unicodedata.decomposition(ch)
        for ch in text
    )


def repetition_run(tokens: list[str], max_n: int = 12) -> tuple[int, int]:
    """Longest run of an immediately repeated n-gram: (n, repeats). Whisper-style stuck loops."""
    best = (0, 1)
    for n in range(1, max_n + 1):
        for start in range(len(tokens) - n):
            gram = tokens[start : start + n]
            repeats, cursor = 1, start + n
            while tokens[cursor : cursor + n] == gram:
                repeats += 1
                cursor += n
            if repeats > best[1]:
                best = (n, repeats)
    return best


def compute_flags(
    text: str | None,
    *,
    audio_ms: int | None = None,
    reported_language: str | None = None,
    expected_language: str | None = None,
) -> QualityFlags:
    raw = text or ""
    tokens = evidence_tokens(raw)
    normalized = normalize_for_evidence(raw)
    flags: list[str] = []
    details: dict = {}

    informative = [t for t in tokens if t not in FILLER_TOKENS]
    if not tokens:
        flags.append("empty")
    elif not informative:
        flags.append("filler_only")

    ratio = non_latin_ratio(raw)
    if ratio >= NON_LATIN_RATIO:
        flags.append("non_latin_script")
        details["non_latin_ratio"] = round(ratio, 3)
    elif has_diacritics(raw):
        flags.append("diacritics")

    if (
        expected_language
        and reported_language
        and (reported_language.lower() != expected_language.lower())
    ):
        flags.append("language_mismatch")
        details["reported_language"] = reported_language

    phrase = next((p for p in OFF_DOMAIN_PHRASES if p in normalized), None)
    if phrase:
        flags.append("off_domain_phrase")
        details["off_domain_phrase"] = phrase
    conversational = next((p for p in CONVERSATIONAL_PHRASES if p in normalized), None)
    if conversational:
        flags.append("conversational_phrase")
        details["conversational_phrase"] = conversational

    n, repeats = repetition_run(tokens)
    if repeats >= REPEAT_MIN_RUNS and n * repeats >= REPEAT_MIN_TOKENS:
        flags.append("repetition_loop")
        details["repetition"] = {"ngram": n, "repeats": repeats}

    atc_like = sum(
        1
        for t in informative
        if t in ATC_WORDS or t in NUMBER_WORDS or t in NATO_PHONETIC or any(c.isdigit() for c in t)
    )
    if len(informative) >= LOW_ATC_MIN_TOKENS and atc_like / len(informative) < LOW_ATC_RATIO:
        # Prose with at most an incidental ATC word ("the hotel is located in…").
        flags.append("low_atc_content")
        details["atc_token_ratio"] = round(atc_like / len(informative), 3)

    if audio_ms and audio_ms > 0 and raw.strip():
        cps = len(raw.strip()) / (audio_ms / 1000)
        if cps > MAX_CHARS_PER_SECOND:
            flags.append("implausible_rate")
            details["chars_per_second"] = round(cps, 1)

    return QualityFlags(tuple(flags), details)
