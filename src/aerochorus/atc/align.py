"""Token alignment primitives (dependency-free; adequate for ~13-token ATC phrases)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class EditCounts:
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> EditCounts:
    """Levenshtein alignment with an S/D/I breakdown (minimum total, then fewest S)."""
    rows, cols = len(reference), len(hypothesis)
    # cost[i][j] = (errors, substitutions, deletions, insertions)
    previous = [(j, 0, 0, j) for j in range(cols + 1)]
    for i in range(1, rows + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, cols + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                current.append(previous[j - 1])
                continue
            sub = previous[j - 1]
            dele = previous[j]
            ins = current[j - 1]
            candidates = [
                (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                (dele[0] + 1, dele[1], dele[2] + 1, dele[3]),
                (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
            ]
            current.append(min(candidates))
        previous = current
    _, s, d, n = previous[cols]
    return EditCounts(s, d, n)


def lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for left_token in left:
        current = [0]
        for index, right_token in enumerate(right, start=1):
            if left_token == right_token:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def sequence_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Order-aware similarity in [0, 1], normalized by the LONGER sequence so that
    both insertions and deletions are penalized."""
    if not left and not right:
        return 1.0
    return lcs_length(left, right) / max(len(left), len(right))


def contains_run(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True when ``needle`` occurs as a contiguous run inside ``haystack``."""
    if not needle:
        return True
    width = len(needle)
    return any(
        list(haystack[i : i + width]) == list(needle) for i in range(len(haystack) - width + 1)
    )
