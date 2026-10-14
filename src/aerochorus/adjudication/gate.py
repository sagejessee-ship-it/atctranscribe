"""The agreement gate (ADR-025): accept an adjudicated transcript, or escalate it.

A self-hosted model is cheap enough to run on everything, but it is not the last
word. Its transcript is accepted when at least ``min_families`` independent ASR
families produced the same words, compared after the scoring normalisation (case,
punctuation and markup removed; numbers canonical: ``niner`` -> ``nine``, ``270`` ->
``two seven zero``). Independent systems agreeing with a third reading of the audio
is strong evidence; everything else is escalated to a stronger, paid model. Silence
counts too: "no speech" is accepted when enough families also heard nothing.

Research-only models never vote unless asked; a family votes once however many of
its models agree; an ``[unk]`` in the transcript is uncertainty and escalates.
Nothing here calls a paid API.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from aerochorus.adjudication_contracts import GateConfig
from aerochorus.atc.align import sequence_similarity
from aerochorus.atc.normalize import scoring_tokens


@dataclass(frozen=True)
class Vote:
    """One model's hypothesis: its family, its words, and whether it may vote."""

    family: str
    text: str | None
    status: str  # success / abstained / error
    eligible: bool = True
    model: str = ""


@dataclass(frozen=True)
class GateDecision:
    accept: bool
    reason: str
    agreeing_families: tuple[str, ...]
    voting_families: int


_DECIMAL_POINT = re.compile(r"(?<=\d)\.(?=\d)")
_RUNWAY_SIDE = re.compile(r"(?<=\d)([LRC])\b", re.IGNORECASE)
_SIDES = {"l": "left", "r": "right", "c": "center"}
_LETTERS_DIGITS = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def gate_tokens(text: str | None) -> list[str]:
    """Scoring tokens with canonical numbers, after spelling out what ASR systems write
    and the adjudication convention speaks: "119.4" -> "119 point 4", "33L" -> "33 left",
    "FL240" -> "FL 240"."""
    if text:
        text = _DECIMAL_POINT.sub(" point ", text)
        text = _RUNWAY_SIDE.sub(lambda m: " " + _SIDES[m.group(1).lower()], text)
        text = _LETTERS_DIGITS.sub(" ", text)
    return scoring_tokens(text, canonical_numbers=True)


def decide(
    transcript: str | None,
    speech_present: bool | None,
    confidence: float | None,
    votes: Iterable[Vote],
    config: GateConfig,
) -> GateDecision:
    voters = [
        v
        for v in votes
        if v.status in ("success", "abstained") and (v.eligible or config.include_research_only)
    ]
    families = {v.family for v in voters}

    def verdict(agreeing: set[str], reason_no: str) -> GateDecision:
        ok = len(agreeing) >= config.min_families
        return GateDecision(
            ok,
            "families_agree" if ok else reason_no,
            tuple(sorted(agreeing)),
            len(families),
        )

    if (confidence or 0.0) < config.min_confidence:
        return GateDecision(False, "low_confidence", (), len(families))
    if "[unk]" in (transcript or "").lower():
        return GateDecision(False, "unknown_words", (), len(families))
    target = gate_tokens(transcript) if speech_present else []
    if not target:  # no speech: agreed by families that heard nothing either
        silent = {v.family for v in voters if not gate_tokens(v.text)}
        return verdict(silent, "speech_disputed")
    agreeing = set()
    for v in voters:
        words = gate_tokens(v.text)
        if config.min_similarity >= 1.0:
            same = words == target
        else:
            same = bool(words) and sequence_similarity(words, target) >= config.min_similarity
        if same:
            agreeing.add(v.family)
    return verdict(agreeing, "too_few_families")
