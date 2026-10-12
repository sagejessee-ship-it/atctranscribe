"""Getting the JSON answer out of a model's reply, whatever wraps it.

Hosted models with strict structured output return clean JSON. Self-hosted ones
may think out loud first (``<think>…</think>``), wrap the object in a code fence
or a sentence, or drift from the schema. The answer is still validated by the
same ``parse_result`` as every other backend.
"""

from __future__ import annotations

import json
import re
from typing import Any

from aerochorus.adjudication.prompt import RESULT_SCHEMA, parse_result

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"

# Sent back, after the unusable answer, for one more try.
STRICT_REMINDER = (
    "Your previous reply could not be used ({error}). Reply again with exactly one JSON "
    "object and nothing else: no reasoning, no markdown, no text before or after it. "
    "Keys: " + ", ".join(RESULT_SCHEMA["required"]) + ". speech_present is true or false; "
    "transcript is a string; confidence is a number from 0 to 1; uncertain_words and "
    "callsigns are arrays of strings; closest_hypothesis and notes are strings."
)


def strip_thinking(text: str) -> str:
    """The reply without reasoning: closed ``<think>`` blocks, and everything up to a stray
    ``</think>`` (chat templates often open the block in the prompt). A reply that opens
    a block and never closes it was cut off while thinking: nothing is left."""
    text = _THINK_BLOCK.sub("", text)
    lower = text.lower()
    if _THINK_CLOSE in lower:
        text = text[lower.rfind(_THINK_CLOSE) + len(_THINK_CLOSE) :]
    elif _THINK_OPEN in lower:
        text = text[: lower.find(_THINK_OPEN)]
    return text.strip()


def extract_json_object(text: str) -> str:
    """The first complete ``{…}`` object that parses, ignoring braces inside strings and
    any prose or code fence around it."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                    except ValueError:
                        break
                    return candidate
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the reply")


def parse_answer(content: str, *, strip: bool = True) -> dict[str, Any]:
    """Validated, normalised answer (``parse_result``); ValueError if there is none."""
    text = strip_thinking(content) if strip else content
    if not text:
        raise ValueError("the reply is empty after removing the reasoning")
    return parse_result(extract_json_object(text))
