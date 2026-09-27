"""The adjudication request: system prompt, structured-output schema, context rendering.

``PROMPT_VERSION`` is stored with every batch and item. Change it whenever the
prompt, schema or transcript conventions change, so results stay comparable.
"""

from __future__ import annotations

import json
import math
from typing import Any

PROMPT_VERSION = 1
DEFAULT_MODEL = "~google/gemini-pro-latest"

# Gemini represents audio as 32 tokens per second.
AUDIO_TOKENS_PER_SECOND = 32
# Output budget per reasoning effort (reasoning tokens count as output) and a typical use.
MAX_TOKENS = {"low": 2048, "medium": 4096, "high": 8192}
TYPICAL_OUTPUT_TOKENS = {"low": 900, "medium": 2000, "high": 4500}
PROMPT_OVERHEAD_TOKENS = 1400  # system prompt + schema + context scaffolding
TOKENS_PER_HYPOTHESIS = 45

AUDIO_FORMATS = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".flac": "flac",
    ".ogg": "ogg",
    ".aac": "aac",
    ".aiff": "aiff",
}

SYSTEM_PROMPT = """\
You adjudicate air traffic control (ATC) radio transmissions for a speech-recognition \
training corpus. You receive one audio clip (the authority), independent automatic \
transcripts of the same clip from several ASR systems, an analysis of where they agree, \
and context about the airport and nearby traffic. Produce the most accurate verbatim \
transcript of what is actually spoken in the clip.

Evidence rules
- The audio decides. Hypotheses are evidence, often right, sometimes wrong, empty or \
hallucinated (for example "thank you" on noise). Never copy a hypothesis the audio \
does not support.
- Use the context (runways, frequencies, facility names, callsigns of aircraft in the \
area) only to resolve words you can hear but cannot make out with certainty. Never \
insert words that are not spoken. Context transmissions from nearby times are not part \
of this clip.
- If there is no intelligible speech (noise, carrier, squelch, silence), set \
speech_present to false and transcript to "".

Transcript conventions
- Lowercase words, no punctuation, in the order spoken; include every speaker.
- Numbers, letters and codes exactly as spoken, in words: "runway three three left", \
"one two one point niner", "flight level three five zero", "squawk four two one two", \
"heading two seven zero", "two thousand five hundred". Write "niner" when spoken as niner.
- Phonetic letters as words ("alpha", "bravo"); callsigns as spoken ("southwest four \
fifty six", "november one two three alpha bravo").
- Omit hesitation sounds (uh, um). Keep everything else, including readbacks and \
greetings.
- A word you hear but cannot make out: write [unk] in its place.

Also report
- confidence: your probability (0 to 1) that the transcript is exactly right.
- uncertain_words: words in your transcript you are unsure of.
- callsigns: aircraft callsigns spoken, in ICAO form when you can tell (e.g. "SWA456", \
"N123AB").
- closest_hypothesis: the model name whose hypothesis is closest to your transcript, or "".
- notes: one short sentence on anything a human reviewer should know (overlap, clipped \
start, stepped-on transmission), or "".
Return only the JSON object."""

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "speech_present": {"type": "boolean"},
        "transcript": {"type": "string"},
        "confidence": {"type": "number"},
        "uncertain_words": {"type": "array", "items": {"type": "string"}},
        "callsigns": {"type": "array", "items": {"type": "string"}},
        "closest_hypothesis": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": [
        "speech_present",
        "transcript",
        "confidence",
        "uncertain_words",
        "callsigns",
        "closest_hypothesis",
        "notes",
    ],
    "additionalProperties": False,
}


def _q(text: str | None, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return json.dumps(text[:limit] + ("…" if len(text) > limit else ""), ensure_ascii=False)


def render_context(bundle: dict[str, Any]) -> str:
    """The text part of the request: everything except the audio."""
    seg = bundle.get("segment") or {}
    lines = []
    where = " ".join(
        str(v)
        for v in (
            seg.get("airport"),
            f"station {seg['station']}" if seg.get("station") else None,
            f"channel {seg['channel']}" if seg.get("channel") else None,
        )
        if v
    )
    service = seg.get("service")
    freq = f"{seg['frequency_mhz']:.3f} MHz" if seg.get("frequency_mhz") else None
    when = seg.get("utc") or "time unknown"
    if seg.get("local"):
        when += f" (local {seg['local']})"
    detail = ", ".join(x for x in (service, freq) if x)
    lines.append(
        f"Clip: {where or 'unknown station'}{f' ({detail})' if detail else ''}, {when}, "
        f"{seg.get('duration_s', '?')} s."
    )
    airport = bundle.get("airport")
    if airport:
        runways = "; ".join(
            f"{r['end']} ({' / '.join(r['spoken'])})" if r.get("spoken") else r["end"]
            for r in airport.get("runways", [])
        )
        freqs = "; ".join(
            f"{f['service']} {f['mhz']:.3f} {f.get('call') or ''}".strip()
            for f in airport.get("frequencies", [])
        )
        lines.append(f"Airport: {airport.get('name')} ({airport.get('icao')}).")
        if runways:
            lines.append(f"Runway ends: {runways}.")
        if freqs:
            lines.append(f"Voice frequencies: {freqs}.")
    hyps = bundle.get("hypotheses") or []
    lines.append("")
    lines.append("ASR hypotheses (independent systems; each may be wrong, empty or hallucinated):")
    if hyps:
        for i, h in enumerate(hyps, 1):
            tag = ", research-only" if h.get("research_only") else ""
            lines.append(f"  {i}. {h['model']} [{h.get('family', '?')}{tag}]: {_q(h.get('text'))}")
    else:
        lines.append("  (none: transcribe from the audio alone)")
    if bundle.get("abstained"):
        lines.append(f"  No speech detected by: {', '.join(bundle['abstained'])}.")
    if bundle.get("errors"):
        lines.append(f"  Failed: {', '.join(bundle['errors'])}.")
    agreement = bundle.get("agreement")
    if agreement:
        lines.append("")
        lines.append("Agreement analysis:")
        for g in agreement.get("exact_groups", [])[:4]:
            if g.get("families", 0) >= 2:
                lines.append(
                    f"  identical text from {g['families']} model families: {_q(g.get('text'))}"
                )
        near = agreement.get("near")
        if near and near.get("families", 0) >= 2:
            lines.append(
                f"  {near['families']} families nearly agree (weakest similarity "
                f"{near.get('min_similarity')}), anchored on {near.get('anchor')}"
            )
        for u in agreement.get("utterances", [])[:6]:
            bounds = (
                f"{u['start_s']:.1f}-{u['end_s']:.1f} s"
                if u.get("start_s") is not None and u.get("end_s") is not None
                else "position unknown"
            )
            lines.append(
                f"  agreed stretch ({u.get('families')} families, {bounds}): {_q(u.get('text'))}"
            )
        if not any(
            g.get("families", 0) >= 2 for g in agreement.get("exact_groups", [])
        ) and not agreement.get("utterances"):
            lines.append("  no two model families agree")
    neighbors = bundle.get("neighbors") or []
    if neighbors:
        lines.append("")
        lines.append("Nearby transmissions on this frequency (context only, NOT in this clip):")
        for n in neighbors[:4]:
            off = n.get("offset_s")
            when_n = f"{off:+.0f} s" if off is not None else "nearby"
            lines.append(f"  {when_n}: {_q(n.get('text'), 200)}")
    adsb = bundle.get("adsb")
    if adsb and adsb.get("aircraft"):
        lines.append("")
        lines.append(
            "Aircraft near the airport at this time (ADS-B; callsigns are usually spoken "
            "with the airline's telephony name):"
        )
        for a in adsb["aircraft"][:15]:
            name = a.get("callsign") or a.get("icao24")
            hint = f" = {a['telephony']}" if a.get("telephony") else ""
            if a.get("on_ground"):
                state = "on the ground"
            else:
                alt = a.get("alt_ft")
                vs = a.get("vs_fpm") or 0
                trend = ", climbing" if vs >= 300 else ", descending" if vs <= -300 else ""
                state = f"{alt if alt is not None else '?'} ft{trend}"
            dist = a.get("distance_nm")
            where_a = f", {dist} nm" if dist is not None else ""
            lines.append(f"  {name}{hint}: {state}{where_a}")
    return "\n".join(lines)


def build_payload(
    model: str,
    bundle: dict[str, Any],
    audio_b64: str,
    audio_format: str,
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    """An OpenRouter chat-completions request with the audio inline (base64)."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": render_context(bundle)},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": audio_b64, "format": audio_format},
                    },
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "atc_adjudication", "strict": True, "schema": RESULT_SCHEMA},
        },
        "reasoning": {"effort": reasoning_effort},
        "max_tokens": MAX_TOKENS[reasoning_effort],
        "usage": {"include": True},
    }


def parse_result(content: str) -> dict[str, Any]:
    """The model's JSON answer, validated and normalised; ValueError if unusable."""
    text = content.strip()
    if text.startswith("```"):  # tolerate a fenced answer
        text = text.strip("`")
        text = text[text.find("{") :]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"model answer is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("model answer is not a JSON object")
    missing = [k for k in RESULT_SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"model answer lacks {', '.join(missing)}")
    transcript = " ".join(str(data["transcript"] or "").split())
    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence is not a number") from exc
    if math.isnan(confidence):
        raise ValueError("confidence is NaN")
    return {
        "speech_present": bool(data["speech_present"]) and bool(transcript),
        "transcript": transcript,
        "confidence": min(max(confidence, 0.0), 1.0),
        "uncertain_words": [str(w) for w in data.get("uncertain_words") or []][:50],
        "callsigns": [str(c).strip().upper() for c in data.get("callsigns") or []][:20],
        "closest_hypothesis": str(data.get("closest_hypothesis") or ""),
        "notes": str(data.get("notes") or "")[:500],
    }


def estimate_item_cost(
    duration_s: float, n_hypotheses: int, effort: str, pricing: dict[str, float]
) -> tuple[float, float]:
    """(typical, worst case) USD for one item; pricing is USD per million tokens."""
    text_in = PROMPT_OVERHEAD_TOKENS + TOKENS_PER_HYPOTHESIS * n_hypotheses
    audio_in = AUDIO_TOKENS_PER_SECOND * max(duration_s, 1.0)
    input_cost = (text_in * pricing["prompt"] + audio_in * pricing["audio"]) / 1e6
    typical = input_cost + TYPICAL_OUTPUT_TOKENS[effort] * pricing["completion"] / 1e6
    worst = input_cost * 1.25 + MAX_TOKENS[effort] * pricing["completion"] / 1e6
    return typical, worst
