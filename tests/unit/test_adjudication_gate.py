"""The agreement gate (ADR-025): accept what independent ASR families corroborate."""

from __future__ import annotations

from aerochorus.adjudication.gate import Vote, decide, gate_tokens
from aerochorus.adjudication_contracts import GateConfig

TWO = GateConfig(min_families=2)
SAID = "american four climb flight level two four zero contact one one niner point four"


def test_normalisation_folds_case_punctuation_and_number_format():
    assert gate_tokens("Climb FL 240, contact 119.4.") == gate_tokens(
        "climb fl two four zero contact one one nine point four"
    )
    assert gate_tokens("one one niner point four") == gate_tokens("1 1 9 point 4")
    assert gate_tokens("Runway 33L, FL240") == gate_tokens(
        "runway three three left flight level two four zero".replace("flight level", "fl")
    )
    assert gate_tokens("runway 1C") == ["runway", "one", "center"]
    votes = [
        Vote("canary", "American four, climb flight level 240, contact 119.4.", "success"),
        Vote(
            "parakeet",
            "american four climb flight level two four zero contact one one nine point four",
            "success",
        ),  # fmt: skip
    ]
    d = decide(SAID, True, 0.9, votes, TWO)
    assert d.accept and d.reason == "families_agree"
    assert d.agreeing_families == ("canary", "parakeet") and d.voting_families == 2


def test_a_family_votes_once_and_research_only_models_do_not_vote():
    same = "american four climb flight level two four zero contact one one niner point four"
    votes = [
        Vote("canary", same, "success"),
        Vote("canary", same, "success"),  # a second canary model: still one family
        Vote("whisper", same, "success", eligible=False),  # research-only
        Vote("parakeet", "united nine cleared to land", "success"),
    ]
    d = decide(SAID, True, 0.9, votes, TWO)
    assert not d.accept and d.reason == "too_few_families" and d.agreeing_families == ("canary",)
    assert decide(SAID, True, 0.9, votes, GateConfig(include_research_only=True)).accept
    assert decide(SAID, True, 0.9, votes, GateConfig(min_families=1)).accept


def test_errors_never_vote_and_silence_is_corroborated_by_silence():
    votes = [
        Vote("canary", None, "error"),
        Vote("parakeet", "", "abstained"),
        Vote("whisper", "", "success"),  # spoke nothing
        Vote("qwen", "thank you", "success"),  # hallucinated on noise
    ]
    d = decide("", False, 0.8, votes, TWO)
    assert d.accept and d.agreeing_families == ("parakeet", "whisper")
    assert d.voting_families == 3  # the errored family is not a voter
    lonely = [Vote("parakeet", "", "abstained"), Vote("qwen", "thank you", "success")]
    assert decide("", False, 0.8, lonely, TWO).reason == "speech_disputed"


def test_uncertainty_and_low_confidence_escalate():
    votes = [Vote("canary", SAID, "success"), Vote("parakeet", SAID, "success")]
    assert decide(SAID, True, 0.9, votes, TWO).accept
    unsure = SAID.replace("climb", "[unk]")
    assert decide(unsure, True, 0.9, votes, TWO).reason == "unknown_words"
    assert decide(SAID, True, 0.4, votes, GateConfig(min_confidence=0.5)).reason == (
        "low_confidence"
    )


def test_near_matching_is_optional():
    votes = [
        Vote("canary", SAID.replace("american", "americans"), "success"),
        Vote("parakeet", SAID, "success"),
    ]
    assert not decide(SAID, True, 0.9, votes, TWO).accept  # identical only, by default
    assert decide(SAID, True, 0.9, votes, GateConfig(min_similarity=0.9)).accept
