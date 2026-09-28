# ADR-022: Paid model adjudication of selected segments, capped and never gold

Status: Accepted (2026-09-27)

## Context

The local ensemble (14 families, CrispASR) agrees on a large part of the
corpus. Where it disagrees, or agrees only on part of a segment, a human must
listen. A large audio-capable model, Gemini Pro Latest via OpenRouter
(`~google/gemini-pro-latest`), transcribes ATC unusually well, especially
when it can see what the other models heard and what the airport context
allows.

It is also paid per request. Audio is cheap (32 tokens per second at $2 per
million tokens), but reasoning and output tokens cost $12 per million. That
is about $0.01–0.03 per typical 4-second clip, which is trivial for a sample
and real money across 448k segments.

## Decision

- **Explicit, human-confirmed batches only.** Nothing is adjudicated
  automatically. A batch is created from selected rows, the open segment, or
  a seeded random sample of the current review filters. Creating it requires
  all of these:
  - a preview that prices the exact segments;
  - `confirm: true`;
  - the estimate the human saw, which the server re-prices and compares
    (409 if it moved);
  - a cost cap within a per-batch limit.

  The UI's Send button stays disabled until a checkbox naming the clip count
  and the maximum spend is ticked.
- **A hard cost cap.** Each item carries a worst-case cost (input estimate
  plus `max_tokens` at the output price). A claim reserves it while the item
  is in flight. An item is handed out only if spent + reserved + its worst
  case fits the cap; otherwise the batch stops as `capped`. Spend reported by
  OpenRouter (`usage.cost`, else computed from token counts) is recorded,
  even for unusable answers and late results.
- **Guardrails before sending.** Segments longer than
  `adjudication_max_audio_s` (120 s) are skipped, never sent. So are missing
  audio, segments already queued, and segments already adjudicated with the
  same model and prompt version, unless "redo" is chosen. There is a
  per-batch item limit (500).
- **The runner lives where the audio is, and alone holds the key.** The
  control plane plans, prices (from OpenRouter's public price list, no key;
  configured defaults offline) and records. It never calls the paid API and
  never holds `AEROCHORUS_OPENROUTER_API_KEY`.

  `aerochorus adjudicate run` (on Linux, `aerochorus-adjudicator.service`,
  installed but not enabled) runs on the host that mounts the corpus. For
  each item it:
  1. claims it;
  2. reads the source audio read-only and verifies the indexed sha256;
  3. sends it base64-inline;
  4. posts the result.

  A rejected key or exhausted credit stops the runner and requeues the item.
  Rate limits and outages retry with backoff (at most three attempts per
  item). Stale claims are requeued. Browsers never see the key.
- **One bundle, rendered as text beside the audio**, versioned by
  `PROMPT_VERSION`:
  - the segment (station, channel, service, frequency, UTC and local time,
    duration);
  - every non-superseded hypothesis, with family and a research-only mark,
    plus abstentions and errors;
  - the agreement analysis: exact groups with 2+ families, the near group,
    and agreed utterances with bounds;
  - the airport's runway ends with spoken forms and its voice frequencies;
  - cached ADS-B aircraft, with airline telephony hints (SWA456 = Southwest
    456). The runner never triggers an OpenSky query.
  - up to four nearby transmissions, marked as not part of the clip.

  The system prompt makes the audio authoritative, forbids inserting unspoken
  words, and fixes the transcript conventions: lowercase verbatim words,
  numbers as spoken, no fillers, `[unk]`.
- **Structured output.** A strict JSON schema asks for: speech present,
  transcript, confidence, uncertain words, callsigns, closest hypothesis and
  notes. The answer is validated and normalised. The raw response, usage,
  cost and the rendered request context are stored. The audio is stored only
  as format, size and sha256, never its bytes.
- **Model output, never gold.** An adjudicated transcript can become silver
  with the new `text_origin = model_adjudicated`. Acceptance never replaces
  human text, human gold or a human rejection, and never applies to
  benchmark sources. Batch acceptance can require a minimum confidence and
  independent support: an ASR hypothesis with order-aware similarity ≥ the
  near-match threshold.

  A human may take the text into the correction editor. If they then save it
  as gold, that is their assertion (`text_origin = human`). The existing DB
  check still refuses gold without human origin.

## Consequences

- Hard, human-selected cases (partial agreement, disagreement) can get a
  strong second opinion at a known, bounded price. The spend and the exact
  context sent are auditable per item.
- Selected audio leaves the machine, sent to OpenRouter and its provider
  (Google). That happens only for segments a person chose, after a
  confirmation that says so.
- Silver now has two origins: model consensus and model adjudication. Dataset
  exports keep `text_origin`, so training can weigh or exclude either.
- Transcript conventions (spoken words for numbers) differ from some ASR
  outputs (digits). Comparison uses the shared evidence normalisation, and a
  new prompt version can change the convention without mixing results.

## Verification

- `tests/unit/test_adjudication_prompt.py`: context rendering, strict schema
  payload with inline audio, answer parsing, cost bounds, telephony, error
  classification, and that the key never appears in errors or `repr`.
- `tests/integration/test_adjudication.py`:
  - preview and guardrails; confirmation and estimate checks; limits;
  - worst-case reservation and `capped`;
  - an end-to-end runner against a mocked OpenRouter: audio bytes and
    context sent, results, similarities and spend recorded, no audio stored;
  - changed audio refused; a rejected key stops and requeues;
  - silver acceptance never overrides human work;
  - the DB refuses non-human gold.
- `ui/e2e/adjudication.spec.ts`: priced dialog, confirmation gate, cap
  bound, runner via API, inspector review, "Use as correction", accept as
  silver, batches page.
