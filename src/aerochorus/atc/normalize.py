"""Exactly two normalizers, for two different jobs (v1 lesson, ADR-015).

``normalize_for_scoring``: accuracy against gold. Applied to reference AND
hypothesis alike (symmetry; v1 DEFECT-2 used a weaker function on hypotheses,
which inverted its provider ranking). Strips annotation markup, case and all
punctuation. ``canonical_numbers`` optionally folds number *format*
(``niner``->``nine``, ``270``->``two seven zero``); it is off by default so the
headline error rate stays faithful to what each model actually produced.

``normalize_for_evidence``: agreement between models. Deliberately weaker:
case and edge punctuation only. ``16L`` vs ``16R`` and ``two seven zero`` vs
``270`` must stay different, or two models that heard different clearances
would count as corroborating each other.
"""

from __future__ import annotations

import re
import unicodedata

from aerochorus.atc.lexicon import DIGIT_WORDS, ICAO_NUMBER_CANON

# Order matters: strip word-class tags before generic bracket tags, otherwise
# "[#callsign]Hotel[/#callsign]" would lose "Hotel" too.
_WORD_CLASS_TAG = re.compile(r"\[/?#[^\]]*\]")
_DOUBLE_PAREN = re.compile(r"\(\([^)]*\)\)")
_BRACKET_TAG = re.compile(r"\[[^\]]*\]")
_ANGLE_TAG = re.compile(r"<[^>]*>")
# Drop only Unicode punctuation (P*) and symbols (S*). A regex like [^\w\s]
# would also strip combining marks (Devanagari vowel signs), shredding
# non-Latin words; language drift must stay visible and intact.
_DROP_CATEGORIES = ("P", "S")
_WS = re.compile(r"\s+")
_EDGE_PUNCTUATION = "\"'`“”‘’.,;:!?()[]{}<>"


def strip_markup(text: str) -> str:
    """Remove annotation markup (ATCO2/SpokenData, Whisper's [BLANK_AUDIO]) but keep words."""
    text = _WORD_CLASS_TAG.sub(" ", text)
    text = _DOUBLE_PAREN.sub(" ", text)
    text = _BRACKET_TAG.sub(" ", text)
    return _ANGLE_TAG.sub(" ", text)


def _strip_punctuation(text: str) -> str:
    return "".join(" " if unicodedata.category(ch)[0] in _DROP_CATEGORIES else ch for ch in text)


def canonicalize_numbers(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        if token.isdigit():
            out.extend(DIGIT_WORDS.get(ch, ch) for ch in token)
        else:
            out.append(ICAO_NUMBER_CANON.get(token, token))
    return out


def normalize_for_scoring(text: str | None, *, canonical_numbers: bool = False) -> str:
    if not text:
        return ""
    text = strip_markup(str(text)).lower()
    tokens = _WS.sub(" ", _strip_punctuation(text)).strip().split()
    if canonical_numbers:
        tokens = canonicalize_numbers(tokens)
    return " ".join(tokens)


def scoring_tokens(text: str | None, *, canonical_numbers: bool = False) -> list[str]:
    normalized = normalize_for_scoring(text, canonical_numbers=canonical_numbers)
    return normalized.split() if normalized else []


def normalize_for_evidence(text: str | None) -> str:
    if not text:
        return ""
    tokens = [t.strip(_EDGE_PUNCTUATION) for t in _WS.split(str(text).lower().strip())]
    return " ".join(t for t in tokens if t)


def evidence_tokens(text: str | None) -> list[str]:
    normalized = normalize_for_evidence(text)
    return normalized.split() if normalized else []
