"""Versioned ATC lexicon (one copy; v1 had three partial duplicates).

Bump LEXICON_VERSION whenever a list changes: quality flags record the
version they were computed with, so results can be re-flagged consistently.
"""

from __future__ import annotations

LEXICON_VERSION = 1

NATO_PHONETIC = frozenset(
    {
        "alfa",
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliet",
        "juliett",
        "kilo",
        "lima",
        "mike",
        "november",
        "oscar",
        "papa",
        "quebec",
        "romeo",
        "sierra",
        "tango",
        "uniform",
        "victor",
        "whiskey",
        "xray",
        "x-ray",
        "yankee",
        "zulu",
    }
)

# Spoken number words, including ICAO forms.
NUMBER_WORDS = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "niner",
        "tree",
        "fower",
        "fife",
        "ten",
        "eleven",
        "twelve",
        "hundred",
        "thousand",
        "decimal",
        "point",
    }
)

# ICAO pronunciations folded only when a caller explicitly asks for canonical numbers.
ICAO_NUMBER_CANON = {
    "niner": "nine",
    "tree": "three",
    "fife": "five",
    "fower": "four",
    "decimal": "point",
    "dot": "point",
}

DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}

# ATC command/state vocabulary (v1's two lists, merged).
ATC_WORDS = frozenset(
    {
        "affirm",
        "affirmative",
        "altitude",
        "approach",
        "backtrack",
        "cleared",
        "clearance",
        "climb",
        "contact",
        "cross",
        "decimal",
        "departure",
        "descend",
        "direct",
        "expect",
        "flight",
        "frequency",
        "ground",
        "heading",
        "hold",
        "identified",
        "join",
        "knots",
        "land",
        "landing",
        "level",
        "line",
        "maintain",
        "negative",
        "qnh",
        "radar",
        "readback",
        "reduce",
        "report",
        "roger",
        "runway",
        "squawk",
        "speed",
        "startup",
        "takeoff",
        "taxi",
        "tower",
        "traffic",
        "turn",
        "vacate",
        "via",
        "wilco",
        "wind",
    }
)

# Non-informative tokens: "said nothing informative" vs "said nothing".
FILLER_TOKENS = frozenset({"uh", "um", "erm", "hmm", "ah", "eh", "noise", "unk", "unintelligible"})

# Subtitle-corpus / assistant-persona artefacts: a hypothesis containing one is
# a hallucination, not a transcript (v1: explicit_off_domain_phrase -> rejected).
OFF_DOMAIN_PHRASES = (
    "thank you for watching",
    "thanks for watching",
    "like and subscribe",
    "please subscribe",
    "subscribe to",
    "captions by",
    "transcribed by",
    "subtitles by",
    "translated by",
    "as an ai",
    "i'm sorry but",
)

# Conversational framing: a softer signal (v1 used it as a graded penalty).
CONVERSATIONAL_PHRASES = (
    "here is",
    "i think",
    "let's",
    "okay so",
    "you know",
    "lovely to see you",
    "the following is",
)
