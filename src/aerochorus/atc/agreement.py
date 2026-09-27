"""Per-segment agreement between model hypotheses (Phase 5, ADR-016).

Pure and deterministic. Inputs are the latest result of each model for one
segment, each tagged with its architecture family *from the model registry*.

* **Exact groups**: hypotheses whose ``normalize_for_evidence`` forms are
  identical. Evidence normalization is deliberately strict (``16L ≠ 16R``,
  ``two seven zero ≠ 270``); the scoring normalizer is never used here.
* **Near group**: for each spoken hypothesis (the anchor), the families that
  have a hypothesis with order-aware LCS similarity >= the threshold to it.
  The best anchor has the most families; its similarity is the *weakest*
  member's similarity to the anchor. This is similarity, not probability.
* Provider count and family count are always separate. Two models of the
  same family (e.g. two Canary decodings) are two providers, one family.
* Abstentions and errors are counted, never treated as disagreement or votes.
* v2 adds **utterance agreement** within the segment (``atc.utterances``):
  agreeing stretches of 3+ content tokens across 2+ families, with time bounds
  from members that report word timestamps.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from aerochorus.atc.align import sequence_similarity
from aerochorus.atc.normalize import evidence_tokens, normalize_for_evidence
from aerochorus.atc.utterances import LOW_CONTENT, TimedHypothesis, utterance_agreement

AGREEMENT_VERSION = 2
DEFAULT_NEAR_THRESHOLD = 0.8


@dataclass(frozen=True)
class Hypothesis:
    model: str
    family: str
    status: str  # success | abstained | error
    text: str | None = None
    flags: tuple[str, ...] = ()
    # (start_ms, end_ms, word) as reported; None if the model gives no word timestamps.
    words: tuple[tuple[int, int, str], ...] | None = None


@dataclass
class Agreement:
    version: int
    near_threshold: float
    results_count: int
    success_count: int
    abstained_count: int
    error_count: int
    models: list[str]
    families: list[str]
    exact_groups: list[dict[str, Any]]
    best_exact_provider_count: int
    best_exact_family_count: int
    near_group: dict[str, Any] | None
    best_near_family_count: int
    best_near_similarity: float | None
    max_pair_similarity: float | None
    representative_text: str | None
    representative_source: str | None
    flags: dict[str, int] = field(default_factory=dict)
    representative_tokens: int = 0
    representative_content_tokens: int = 0
    utterances: list[dict[str, Any]] = field(default_factory=list)
    best_utterance_family_count: int = 0
    best_utterance_tokens: int = 0

    def as_row(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "near_threshold": self.near_threshold,
            "results_count": self.results_count,
            "success_count": self.success_count,
            "abstained_count": self.abstained_count,
            "error_count": self.error_count,
            "models": self.models,
            "families": self.families,
            "exact_groups": self.exact_groups,
            "best_exact_provider_count": self.best_exact_provider_count,
            "best_exact_family_count": self.best_exact_family_count,
            "near_group": self.near_group,
            "best_near_family_count": self.best_near_family_count,
            "best_near_similarity": self.best_near_similarity,
            "max_pair_similarity": self.max_pair_similarity,
            "representative_text": self.representative_text,
            "representative_source": self.representative_source,
            "flags": self.flags,
            "representative_tokens": self.representative_tokens,
            "representative_content_tokens": self.representative_content_tokens,
            "utterances": self.utterances,
            "utterance_count": len(self.utterances),
            "best_utterance_family_count": self.best_utterance_family_count,
            "best_utterance_tokens": self.best_utterance_tokens,
        }


def _spoken(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    return [h for h in hypotheses if h.status == "success" and normalize_for_evidence(h.text)]


def exact_groups(hypotheses: list[Hypothesis]) -> list[dict[str, Any]]:
    groups: dict[str, list[Hypothesis]] = {}
    for h in _spoken(hypotheses):
        groups.setdefault(normalize_for_evidence(h.text), []).append(h)
    out = []
    for normalized, members in groups.items():
        members = sorted(members, key=lambda h: h.model)
        families = sorted({m.family for m in members})
        out.append(
            {
                "normalized": normalized,
                "display_text": members[0].text.strip(),
                "providers": [m.model for m in members],
                "families": families,
                "provider_count": len(members),
                "family_count": len(families),
            }
        )
    out.sort(key=lambda g: (-g["family_count"], -g["provider_count"], g["normalized"]))
    return out


def near_group(
    hypotheses: list[Hypothesis], threshold: float
) -> tuple[dict[str, Any] | None, float | None]:
    """Best anchor-based near group, and the max cross-family pair similarity."""
    spoken = _spoken(hypotheses)
    tokens = {h.model: evidence_tokens(h.text) for h in spoken}
    best: dict[str, Any] | None = None
    best_key: tuple | None = None
    max_pair: float | None = None
    for anchor in sorted(spoken, key=lambda h: h.model):
        closest: dict[str, tuple[float, str]] = {}
        for other in spoken:
            if other.family == anchor.family:
                continue
            similarity = sequence_similarity(tokens[anchor.model], tokens[other.model])
            max_pair = similarity if max_pair is None else max(max_pair, similarity)
            if similarity > closest.get(other.family, (-1.0, ""))[0]:
                closest[other.family] = (similarity, other.model)
        members = {f: v for f, v in closest.items() if v[0] >= threshold}
        family_count = 1 + len(members)
        min_similarity = min((v[0] for v in members.values()), default=None)
        key = (family_count, min_similarity if min_similarity is not None else -1.0)
        if best_key is None or key > best_key:
            best_key = key
            best = {
                "anchor_model": anchor.model,
                "anchor_text": anchor.text.strip(),
                "families": sorted([anchor.family, *members]),
                "providers": sorted([anchor.model, *(v[1] for v in members.values())]),
                "family_count": family_count,
                "min_similarity": round(min_similarity, 4) if min_similarity is not None else None,
                "threshold": threshold,
            }
    return best, (round(max_pair, 4) if max_pair is not None else None)


def _medoid(spoken: list[Hypothesis]) -> Hypothesis | None:
    if not spoken:
        return None
    tokens = {h.model: evidence_tokens(h.text) for h in spoken}

    def centrality(h: Hypothesis) -> tuple[float, str]:
        total = sum(
            sequence_similarity(tokens[h.model], tokens[o.model]) for o in spoken if o is not h
        )
        return (-total, h.model)

    return min(spoken, key=centrality)


def compute_agreement(
    hypotheses: list[Hypothesis],
    *,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
    duration_ms: int | None = None,
) -> Agreement:
    statuses = Counter(h.status for h in hypotheses)
    groups = exact_groups(hypotheses)
    near, max_pair = near_group(hypotheses, near_threshold)
    spoken = _spoken(hypotheses)

    representative_text = representative_source = None
    if groups and groups[0]["family_count"] >= 2:
        representative_text, representative_source = groups[0]["display_text"], "exact"
    elif near and near["family_count"] >= 2:
        representative_text, representative_source = near["anchor_text"], "near"
    else:
        medoid = _medoid(spoken)
        if medoid is not None:
            representative_text = medoid.text.strip()
            representative_source = "single" if len(spoken) == 1 else "medoid"

    flags: Counter = Counter()
    for h in hypotheses:
        flags.update(set(h.flags))
    rep_tokens = evidence_tokens(representative_text)
    utterances = utterance_agreement(
        [TimedHypothesis(h.model, h.family, h.text, h.words) for h in spoken],
        duration_ms=duration_ms,
    )

    return Agreement(
        version=AGREEMENT_VERSION,
        near_threshold=near_threshold,
        results_count=len(hypotheses),
        success_count=statuses.get("success", 0),
        abstained_count=statuses.get("abstained", 0),
        error_count=statuses.get("error", 0),
        models=sorted(h.model for h in hypotheses),
        families=sorted({h.family for h in hypotheses}),
        exact_groups=groups,
        best_exact_provider_count=max((g["provider_count"] for g in groups), default=0),
        best_exact_family_count=max((g["family_count"] for g in groups), default=0),
        near_group=near,
        best_near_family_count=near["family_count"] if near else 0,
        best_near_similarity=near["min_similarity"] if near else None,
        max_pair_similarity=max_pair,
        representative_text=representative_text,
        representative_source=representative_source,
        flags=dict(sorted(flags.items())),
        representative_tokens=len(rep_tokens),
        representative_content_tokens=sum(1 for t in rep_tokens if t not in LOW_CONTENT),
        utterances=utterances,
        best_utterance_family_count=max((u["family_count"] for u in utterances), default=0),
        best_utterance_tokens=max((u["n_tokens"] for u in utterances), default=0),
    )
