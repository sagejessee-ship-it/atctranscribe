"""ATC domain module. Several cases are v1's highest-value regression tests
(docs/v1-audit/V1_EVALUATION_AND_TESTS.md, Part 3), ported with their real
failure examples."""

import pytest

from aerochorus.atc.align import contains_run, edit_counts, sequence_similarity
from aerochorus.atc.entities import gold_entity_spans
from aerochorus.atc.normalize import (
    evidence_tokens,
    normalize_for_evidence,
    normalize_for_scoring,
    scoring_tokens,
)
from aerochorus.atc.quality import compute_flags, repetition_run

# --- Tier 1: invariants that must never break ----------------------------------------


def _errors(reference: str, hypothesis: str) -> int:
    return edit_counts(scoring_tokens(reference), scoring_tokens(hypothesis)).errors


def test_scoring_is_symmetric_in_case_and_punctuation():
    """v1 DEFECT-2: gold and hypotheses were normalized differently."""
    reference = "oscar kilo papa mike"
    assert _errors(reference, "OSCAR KILO PAPA MIKE") == 0
    assert _errors(reference, "Oscar Kilo, Papa Mike.") == 0
    assert _errors("Oscar Kilo, Papa Mike.", reference) == 0


def test_evidence_comparison_is_conservative():
    """v1 test_evidence_summary.py:47, ported verbatim."""
    assert normalize_for_evidence("  QFA-12, contact 118.5!  ") == "qfa-12 contact 118.5"
    assert normalize_for_evidence("Runway 16L") != normalize_for_evidence("Runway 16R")
    assert normalize_for_evidence("two seven zero") != normalize_for_evidence("270")


# --- Tier 2: compatibility targets -----------------------------------------------------


def test_atco2_markup_is_stripped_from_gold_but_content_kept():
    gold = (
        "[#callsign]Oscar Kilo Papa Mike Bravo[/#callsign] [hes] [#command]descend[/#command] "
        "[#value]flight level one hundred[/#value] <unk> (( )) [ne czech] dekuji [/ne]"
    )
    assert normalize_for_scoring(gold) == (
        "oscar kilo papa mike bravo descend flight level one hundred dekuji"
    )
    assert normalize_for_scoring("IFR flight starts [hes] now time zero five") == (
        "ifr flight starts now time zero five"
    )


def test_language_drift_survives_normalization():
    """Combining vowel signs must survive; a punctuation regex shredded these in v1."""
    devanagari = "मुस्कर के लो पापा माइक ब्रावो"
    assert normalize_for_scoring(devanagari) == devanagari
    assert len(scoring_tokens(devanagari)) == 6
    assert "přeložil" in normalize_for_scoring("Přeložil Andrej Duskarčík, máj pravo.")


def test_canonical_numbers_is_opt_in():
    assert normalize_for_scoring("heading 270, niner") == "heading 270 niner"
    assert normalize_for_scoring("heading 270, niner", canonical_numbers=True) == (
        "heading two seven zero nine"
    )


def test_fluent_phonetic_hallucination_is_not_consensus():
    hallucination = evidence_tokens("Moscar Kilo Papa Mycabrau this is an father Papa for Andrea")
    truth = evidence_tokens("Oscar Kilo Papa Mike Bravo descend flight level one hundred")
    assert sequence_similarity(hallucination, truth) < 0.3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("मुस्कर के लो पापा माइक ब्रावो …", "non_latin_script"),
        ("Přeložil Andrej Duskarčík, máj pravo.", "diacritics"),
        ("Oszker kilópata megbravó, riszalt fájdalba 400.", "diacritics"),
        ("Thank you for watching!", "off_domain_phrase"),
        ("Lovely to see you", "conversational_phrase"),
        ("the hotel is located in the park and rebosch street", "low_atc_content"),
        ("Lovely to see you", "low_atc_content"),
        ("cleared to land cleared to land cleared to land", "repetition_loop"),
        ("uh um", "filler_only"),
        ("", "empty"),
    ],
)
def test_quality_flags_catch_v1_failure_modes(text, expected):
    assert expected in compute_flags(text).flags


def test_clean_atc_transmission_has_no_flags():
    flags = compute_flags(
        "Oscar Kilo Papa Mike Bravo descend flight level one hundred",
        audio_ms=3770,
        reported_language="en",
        expected_language="en",
    )
    assert flags.flags == ()


def test_rate_and_language_flags():
    assert "implausible_rate" in compute_flags("x" * 200, audio_ms=1000).flags
    flagged = compute_flags("bonjour", reported_language="fr", expected_language="en")
    assert "language_mismatch" in flagged.flags
    assert flagged.as_dict()["version"] >= 1000


def test_long_decoder_loops_are_caught():
    """Real canary-1b-v2 output on ATCO2 (2026-09-27): a 9-token phrase, repeated."""
    loop = "So I'm going to go to the first one, " * 20
    flags = compute_flags(loop)
    assert "repetition_loop" in flags.flags
    assert flags.details["repetition"] == {"ngram": 9, "repeats": 20}
    assert "repetition_loop" not in compute_flags("one one").flags  # a spoken "one one" is fine


def test_repetition_run():
    assert repetition_run(["a", "b", "c"]) == (0, 1)
    assert repetition_run(["go", "go", "go", "go"])[1] == 4


# --- alignment and entities ----------------------------------------------------------------


def test_edit_counts_breakdown():
    counts = edit_counts(["a", "b", "c", "d"], ["a", "x", "c", "d", "e"])
    assert (counts.substitutions, counts.deletions, counts.insertions) == (1, 0, 1)
    assert edit_counts([], ["a", "b"]).insertions == 2
    assert edit_counts(["a", "b"], []).deletions == 2
    assert edit_counts(["a", "b"], ["a", "b"]).errors == 0


def test_contains_run_and_similarity():
    assert contains_run(
        ["climb", "flight", "level", "one", "hundred", "now"], ["level", "one", "hundred"]
    )
    assert not contains_run(["level", "one", "now", "hundred"], ["level", "one", "hundred"])
    assert sequence_similarity([], []) == 1.0
    # normalized by the longer side: padding a match with noise is penalized
    assert sequence_similarity(["a", "b"], ["a", "b", "c", "d"]) == 0.5


def test_gold_entity_spans():
    spans = gold_entity_spans(
        "[#callsign]Swiss Two Three[/#callsign] [#command]climb[/#command] "
        "[#value]flight level niner zero[/#value] [#unnamed]thanks[/#unnamed]"
    )
    assert [(s.entity_class, " ".join(s.tokens)) for s in spans] == [
        ("callsign", "swiss two three"),
        ("command", "climb"),
        ("value", "flight level niner zero"),
    ]
    canonical = gold_entity_spans("[#value]niner[/#value]", canonical_numbers=True)
    assert canonical[0].tokens == ("nine",)
