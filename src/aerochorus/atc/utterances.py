"""Utterance-level agreement *within* one segment (agreement v2).

A recording often holds a clean instruction followed by a garbled readback,
or trailing squelch. Whole-segment agreement then fails even though most of
the audio is transcribed identically by independent models. This module finds
those agreeing stretches:

* Tokens are **evidence tokens** (``normalize_for_evidence``), the same strict
  contract as whole-segment exact agreement: ``33l != 33r``, ``270 != two seven zero``.
* An *agreed utterance* is a maximal contiguous token run that appears in the
  hypotheses of at least two **architecture families** (from the registry) and
  has at least ``min_tokens`` tokens, of which at least ``min_content`` are not
  low-content words ("thank you good day" never counts).
* A run is dropped if a longer run containing it is supported by the same or
  more families. A shorter run with *more* families is kept ("cleared to land"
  from 3 families inside a 2-family "united nine cleared to land runway ...").
* Time bounds come from members that report word timestamps, most reliable
  first (per ``timing_preference``), as the median over timed members; with
  none, bounds are estimated from token position and flagged as estimated.

Pure and deterministic; nothing here reads the database.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from statistics import median
from typing import Any

from aerochorus.atc.lexicon import FILLER_TOKENS
from aerochorus.atc.normalize import normalize_for_evidence

UTTERANCE_MIN_TOKENS = 3
UTTERANCE_MIN_CONTENT = 2
# Words that carry no transcript-worthy content on their own.
LOW_CONTENT = FILLER_TOKENS | {
    "thank", "thanks", "you", "good", "day", "bye", "okay", "ok", "yeah", "yes", "no",
    "right", "hello", "hi", "the", "a", "an", "and", "so", "sir", "maam", "over", "out",
    "morning", "afternoon", "evening", "night", "gday", "cheers", "have", "nice", "great",
    "-", "--", "...", "roger", "wilco",
}  # fmt: skip


@dataclass(frozen=True)
class TimedHypothesis:
    model: str
    family: str
    text: str
    # (start_ms, end_ms, word) as the model reported them; None when not supported.
    words: tuple[tuple[int, int, str], ...] | None = None


def words_of(text: str) -> list[str]:
    """Whitespace words of the original text (what the UI displays)."""
    return text.split()


def token_view(text: str) -> tuple[list[str], list[int]]:
    """Evidence tokens and, for each token, the index of the original word it came from."""
    tokens, word_index = [], []
    for i, word in enumerate(words_of(text)):
        token = normalize_for_evidence(word)
        if token:
            for part in token.split():
                tokens.append(part)
                word_index.append(i)
    return tokens, word_index


def token_times(tokens: list[str], words) -> list[tuple[int, int] | None] | None:
    """Map reported word timings onto evidence tokens (aligned when tokenizations differ)."""
    if not words:
        return None
    timed_tokens: list[str] = []
    times: list[tuple[int, int]] = []
    for start, end, word in words:
        for part in normalize_for_evidence(word).split():
            timed_tokens.append(part)
            times.append((int(start), int(end)))
    if not timed_tokens:
        return None
    if timed_tokens == tokens:
        return list(times)
    out: list[tuple[int, int] | None] = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(a=tokens, b=timed_tokens, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            out[a + k] = times[b + k]
    return out if any(out) else None


def _maximal_common_runs(a: list[str], b: list[str], min_len: int) -> set[tuple[str, ...]]:
    """All maximal contiguous common runs of length >= min_len (O(n*m) DP)."""
    n, m = len(a), len(b)
    prev = [0] * (m + 1)
    runs: set[tuple[str, ...]] = set()
    for i in range(1, n + 1):
        cur = [0] * (m + 1)
        for j in range(1, m + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
        for j in range(1, m + 1):
            length = cur[j]
            # maximal: cannot be extended to the right in both sequences
            if length >= min_len and not (i < n and j < m and a[i] == b[j]):
                runs.add(tuple(a[i - length : i]))
        prev = cur
    return runs


def _find(tokens: list[str], run: tuple[str, ...]) -> int | None:
    k = len(run)
    for i in range(len(tokens) - k + 1):
        if tuple(tokens[i : i + k]) == run:
            return i
    return None


def _content(run: tuple[str, ...]) -> int:
    return sum(1 for t in run if t.replace("'", "") not in LOW_CONTENT)


def utterance_agreement(
    hypotheses: list[TimedHypothesis],
    *,
    duration_ms: int | None = None,
    min_tokens: int = UTTERANCE_MIN_TOKENS,
    min_content: int = UTTERANCE_MIN_CONTENT,
    timing_preference: tuple[str, ...] = (
        "parakeet",
        "canary",
        "cohere",
        "kyutai-stt",
        "granite-speech",
        "whisper",
    ),  # noqa: E501
) -> list[dict[str, Any]]:
    views = []
    for h in hypotheses:
        tokens, word_index = token_view(h.text or "")
        if tokens:
            views.append((h, tokens, word_index, token_times(tokens, h.words)))
    candidates: set[tuple[str, ...]] = set()
    for i in range(len(views)):
        for j in range(i + 1, len(views)):
            if views[i][0].family != views[j][0].family:
                candidates |= _maximal_common_runs(views[i][1], views[j][1], min_tokens)

    supported = []
    for run in candidates:
        if _content(run) < min_content:
            continue
        members = []
        for h, tokens, word_index, times in views:
            at = _find(tokens, run)
            if at is not None:
                members.append((h, at, tokens, word_index, times))
        families = sorted({m[0].family for m in members})
        if len(families) >= 2:
            supported.append((run, members, families))

    # Keep a run unless a longer run containing it has at least the same families.
    def contains(big: tuple[str, ...], small: tuple[str, ...]) -> bool:
        return len(big) > len(small) and _find(list(big), small) is not None

    kept = [
        (run, members, families)
        for run, members, families in supported
        if not any(
            contains(other, run) and set(fam) >= set(families) for other, _, fam in supported
        )
    ]
    kept.sort(key=lambda x: (-len(x[2]), -len(x[0]), x[0]))

    rank = {family: i for i, family in enumerate(timing_preference)}
    longest = max((len(v[1]) for v in views), default=0)
    out = []
    for run, members, families in kept:
        k = len(run)
        spans: dict[str, list[int]] = {}
        starts, ends, timed_by = [], [], []
        for h, at, _tokens, word_index, times in sorted(
            members, key=lambda m: (rank.get(m[0].family, 99), m[0].model)
        ):
            spans[h.model] = [word_index[at], word_index[at + k - 1] + 1]  # word span [from, to)
            if times:
                first, last = times[at], times[at + k - 1]
                if first and last and last[1] > first[0]:
                    starts.append(first[0])
                    ends.append(last[1])
                    timed_by.append(h.model)
        anchor_h, anchor_at, anchor_tokens, anchor_words, _ = members[0]
        display_words = words_of(anchor_h.text)[spans[anchor_h.model][0] : spans[anchor_h.model][1]]
        start_ms = end_ms = None
        estimated = False
        if starts:
            start_ms, end_ms = int(median(starts)), int(median(ends))
        elif duration_ms:
            # No timed member: place by token position in the anchor (flagged).
            n = len(anchor_tokens)
            start_ms = int(duration_ms * anchor_at / n)
            end_ms = int(duration_ms * (anchor_at + k) / n)
            estimated = True
        position = (
            "leading"
            if anchor_at == 0
            else "trailing"
            if anchor_at + k == len(anchor_tokens)
            else "middle"
        )
        out.append(
            {
                "tokens": list(run),
                "text": " ".join(display_words),
                "n_tokens": k,
                "families": families,
                "family_count": len(families),
                "providers": sorted(m[0].model for m in members),
                "provider_count": len(members),
                "spans": spans,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "bounds_estimated": estimated,
                "timed_by": timed_by,
                "position": position,
                "coverage": round(k / longest, 3) if longest else None,
            }
        )
    return out
