"""Per-segment agreement semantics (Phase 5, ADR-016)."""

from aerochorus.atc.agreement import Hypothesis, compute_agreement


def H(model, family, text, status="success", flags=()):
    return Hypothesis(model=model, family=family, status=status, text=text, flags=tuple(flags))


def test_exact_groups_use_the_evidence_normalizer():
    agreement = compute_agreement(
        [
            H("parakeet", "parakeet", "Runway 33L, cleared to land."),
            H("whisper", "whisper", "runway 33l cleared to land"),
            H("qwen3", "qwen3-asr", "Runway 33R, cleared to land."),
        ]
    )
    top = agreement.exact_groups[0]
    assert top["providers"] == ["parakeet", "whisper"]
    assert top["family_count"] == 2
    # 33L vs 33R is a disagreement, never folded together (ADR-015).
    assert agreement.best_exact_family_count == 2
    assert agreement.representative_source == "exact"
    assert agreement.representative_text == "Runway 33L, cleared to land."


def test_same_family_models_never_double_count():
    """Required regression: providers and independent families are separate counts."""
    agreement = compute_agreement(
        [
            H("canary-greedy", "canary", "contact ground point niner"),
            H("canary-beam4", "canary", "Contact ground point niner."),
            H("parakeet", "parakeet", "contact ground point nine"),
        ]
    )
    assert agreement.best_exact_provider_count == 2
    assert agreement.best_exact_family_count == 1
    assert agreement.exact_groups[0]["families"] == ["canary"]
    # the near group still counts families, not providers
    assert agreement.best_near_family_count <= 2


def test_number_format_disagreement_stays_disagreement():
    agreement = compute_agreement(
        [H("a", "fa", "heading two seven zero"), H("b", "fb", "heading 270")]
    )
    assert agreement.best_exact_family_count == 1


def test_near_group_threshold_and_weakest_link():
    agreement = compute_agreement(
        [
            H("a", "fa", "american twenty one fifty two runway three three left cleared to land"),
            H("b", "fb", "american twenty one fifty two runway three three left clear to land"),
            H("c", "fc", "thank you"),
        ],
        near_threshold=0.8,
    )
    assert agreement.best_exact_family_count == 1
    assert agreement.best_near_family_count == 2
    assert 0.8 <= agreement.best_near_similarity < 1.0
    assert agreement.near_group["threshold"] == 0.8
    assert agreement.representative_source == "near"

    strict = compute_agreement(
        [H("a", "fa", "one two three four"), H("b", "fb", "one two three five")],
        near_threshold=0.9,
    )
    assert strict.best_near_family_count == 1
    assert strict.max_pair_similarity == 0.75


def test_abstentions_and_errors_are_counted_not_votes():
    agreement = compute_agreement(
        [
            H("a", "fa", "", status="abstained", flags=["empty"]),
            H("b", "fb", None, status="error"),
            H("c", "fc", "Speedbird one two"),
        ]
    )
    assert (agreement.abstained_count, agreement.error_count, agreement.success_count) == (1, 1, 1)
    assert agreement.best_exact_family_count == 1
    assert agreement.best_near_family_count == 1
    assert agreement.representative_source == "single"
    assert agreement.flags == {"empty": 1}


def test_no_spoken_hypotheses():
    agreement = compute_agreement([H("a", "fa", "", status="abstained")])
    assert agreement.exact_groups == []
    assert agreement.best_exact_family_count == 0
    assert agreement.representative_text is None
    assert agreement.near_group is None


def test_medoid_representative_without_agreement():
    agreement = compute_agreement(
        [
            H("a", "fa", "taxi via alpha"),
            H("b", "fb", "taxi via alpha bravo"),
            H("c", "fc", "lovely weather today"),
        ],
        near_threshold=0.95,
    )
    assert agreement.representative_source == "medoid"
    assert agreement.representative_text.startswith("taxi via alpha")
