"""Utterance-level agreement within a segment (agreement v2)."""

from __future__ import annotations

from aerochorus.atc.utterances import (
    TimedHypothesis,
    token_times,
    token_view,
    utterance_agreement,
)


def words(text: str, start: int = 100, step: int = 300):
    return tuple((start + i * step, start + i * step + 250, w) for i, w in enumerate(text.split()))


INSTRUCTION = "Southwest 456, turn left heading 270, contact departure"


def test_leading_agreement_with_a_garbled_trailing_readback():
    hyps = [
        TimedHypothesis("parakeet-a", "parakeet", f"{INSTRUCTION} one two five point three",
                        words(f"{INSTRUCTION} one two five point three")),
        TimedHypothesis("whisper-a", "whisper", f"{INSTRUCTION}. Wanna do my point three?"),
        TimedHypothesis("qwen-a", "qwen3-asr", "southwest 456 turn left heading 270 contact departure, uh"),  # noqa: E501
    ]  # fmt: skip
    utterances = utterance_agreement(hyps, duration_ms=6000)
    best = utterances[0]
    assert best["families"] == ["parakeet", "qwen3-asr", "whisper"]
    assert best["tokens"] == ["southwest", "456", "turn", "left", "heading", "270", "contact", "departure"]  # noqa: E501
    assert best["position"] == "leading" and best["bounds_estimated"] is False
    assert best["timed_by"] == ["parakeet-a"]  # the only member with word timings
    assert (best["start_ms"], best["end_ms"]) == (100, 100 + 7 * 300 + 250)
    assert best["spans"]["whisper-a"] == [0, 8]  # word span in the original text
    assert best["text"] == "Southwest 456, turn left heading 270, contact departure"


def test_same_family_never_makes_agreement():
    hyps = [
        TimedHypothesis("canary-a", "canary", "delta one two three taxi via alpha"),
        TimedHypothesis("canary-b", "canary", "delta one two three taxi via alpha"),
    ]
    assert utterance_agreement(hyps) == []


def test_low_content_runs_do_not_count():
    hyps = [
        TimedHypothesis("p", "parakeet", "thank you good day"),
        TimedHypothesis("w", "whisper", "Thank you, good day."),
    ]
    assert utterance_agreement(hyps) == []


def test_shorter_run_with_more_families_is_kept():
    hyps = [
        TimedHypothesis("p", "parakeet", "united nine cleared to land runway one zero"),
        TimedHypothesis("w", "whisper", "united nine cleared to land runway one zero"),
        TimedHypothesis("q", "qwen3-asr", "jetblue nine is cleared to land"),
    ]
    runs = {tuple(u["tokens"]): u["family_count"] for u in utterance_agreement(hyps)}
    assert runs[tuple(["united", "nine", "cleared", "to", "land", "runway", "one", "zero"])] == 2
    assert runs[("cleared", "to", "land")] == 3


def test_estimated_bounds_without_timings_and_number_contract():
    hyps = [
        TimedHypothesis("p", "parakeet", "american 2669 runway 33 left line up and wait"),
        TimedHypothesis("q", "qwen3-asr", "american two six six nine runway 33 left line up and wait"),  # noqa: E501
    ]  # fmt: skip
    (u,) = utterance_agreement(hyps, duration_ms=5000)
    # "2669" vs "two six six nine" never agree (evidence contract); the rest does.
    assert u["tokens"] == ["runway", "33", "left", "line", "up", "and", "wait"]
    assert u["bounds_estimated"] and u["position"] == "trailing"
    assert 0 < u["start_ms"] < u["end_ms"] == 5000


def test_token_mapping_handles_punctuation_and_mismatched_tokenization():
    tokens, index = token_view("Four fifty nine, (contact)")
    assert tokens == ["four", "fifty", "nine", "contact"] and index == [0, 1, 2, 3]
    times = token_times(tokens, [(80, 240, "Four"), (400, 640, "fifty"), (640, 960, "nine,"),
                                 (1000, 1200, "contact")])  # fmt: skip
    assert times == [(80, 240), (400, 640), (640, 960), (1000, 1200)]
    partial = token_times(tokens, [(80, 240, "Four"), (1000, 1200, "contact")])
    assert partial[0] == (80, 240) and partial[1] is None and partial[3] == (1000, 1200)
    # Whisper-style empty/dash words never become content.
    dashed = [
        TimedHypothesis("p", "parakeet", "- - - roger"),
        TimedHypothesis("w", "whisper", "- - - roger"),
    ]
    assert utterance_agreement(dashed) == []
    assert token_times(tokens, None) is None
