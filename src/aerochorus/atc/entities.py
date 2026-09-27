"""ATC entities for accuracy scoring.

Gold spans come from human word-class tags (``[#callsign]…[/#callsign]``,
``[#value]…[/#value]``, ``[#command]…[/#command]``). An entity is recovered
when its scoring-normalized token run appears contiguously in the hypothesis.
This measures what the product actually promises (the right callsign, the
right level), which plain token error rate hides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aerochorus.atc.normalize import scoring_tokens

ENTITY_CLASSES = ("callsign", "value", "command")
_SPAN = re.compile(r"\[#(?P<cls>[a-z]+)\](?P<body>.*?)\[/#(?P=cls)\]", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class EntitySpan:
    entity_class: str
    tokens: tuple[str, ...]


def gold_entity_spans(raw_reference: str, *, canonical_numbers: bool = False) -> list[EntitySpan]:
    spans = []
    for match in _SPAN.finditer(raw_reference or ""):
        cls = match["cls"].lower()
        if cls not in ENTITY_CLASSES:
            continue
        tokens = tuple(scoring_tokens(match["body"], canonical_numbers=canonical_numbers))
        if tokens:
            spans.append(EntitySpan(cls, tokens))
    return spans
