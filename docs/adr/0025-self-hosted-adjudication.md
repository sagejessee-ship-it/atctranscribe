# ADR-025: Self-hosted audio-LLM adjudication, profile-driven, gated before paid

Status: Accepted (2026-10-12)

## Context

ADR-022 adjudicates selected segments with a large paid model (Gemini via
OpenRouter): one audio clip plus every hypothesis, the agreement analysis and
the airport/ADS-B context, answered as strict JSON. It is accurate and cheap
per clip, but not cheap across 448k segments, and the audio leaves the house.

Open audio LLMs now run on vLLM's OpenAI-compatible server with audio input
(`input_audio`): Voxtral Mini 3B fits the 16 GB RTX 5080, and Qwen3-Omni 30B-A3B
or Voxtral Small 24B fit H100s. A self-hosted model can read every clip for
nothing per request, and only the hard ones need the paid model.

## Decision

- **Same pipeline, another backend.** The runner gets a backend interface. The
  OpenRouter backend is ADR-022, unchanged. The OpenAI-compatible backend talks to
  any server that accepts `input_audio`. Batches, claims, resumability, results,
  similarities, the review UI and silver acceptance are shared. The prompt,
  schema and `PROMPT_VERSION` are unchanged. Another API (e.g. Gemini or Qwen
  Omni-Plus directly) is one more backend class.
- **Profiles, not code.** `config/adjudicators.toml` names each profile:
  - the backend type and the model id results are recorded under;
  - the server URL, max tokens, temperature, thinking stripping, audio inputs per
    request, timeout, where the instructions go (Voxtral has no system role),
    JSON-schema mode, parse retries and concurrency;
  - the `vllm serve` arguments.

  `AEROCHORUS_ADJUDICATOR_PROFILE` selects one. `AEROCHORUS_ADJUDICATOR_*`
  overrides fields per host. API keys only ever come from the environment.
  `deploy/vllm` starts the server from the same table, so moving from
  `local-small` to `h100-large` changes configuration only, on both sides.
- **Self-hosted ids start with `local/`** and are the server's
  `--served-model-name`. The control plane prices them at $0, allows 5,000 items
  per batch, and hands their items only to a runner configured for that exact
  model. A runner without a model list (OpenRouter, old or new) never gets them.
  Batches for them are created by `aerochorus adjudicate submit` (no cost to
  confirm). Paid batches stay in the review UI behind the price confirmation.
- **The audio, as heard.** The runner reads the source read-only and verifies its
  sha256 (as ADR-022). ffmpeg decodes it from stdin to 16 kHz mono. The whole
  clip goes as WAV, plus each transmission:
  - split where the recorder wrote digital silence (≤ −90 dBFS, ≥ 50 ms);
  - fragments under 250 ms join a neighbour;
  - merged across the shortest gaps to fit the profile's input limit;
  - described in the context with its bounds and median voice pitch.

  The request record stores the profile, endpoint and audio layout, never the
  audio. Without ffmpeg, the original file goes unsplit.
- **Robust answers.** Reasoning blocks are stripped and the JSON object extracted.
  It is validated by the shared parser, which also checks types. One retry with
  a stricter reminder follows; then that item fails, never the batch. A server
  that is down, rejects the key or serves another model stops the runner and
  requeues the item. Timeouts and 5xx retry with backoff.
- **The agreement gate decides what is worth paying for.** An adjudicated
  transcript is accepted when at least N ensemble-voting ASR families (default 2)
  produced the same words. The comparison uses the scoring normalisation with
  canonical numbers, and spells out decimal points, runway sides and `FL240`.
  - A family votes once; research-only models vote only on request.
  - Silence is corroborated by silence.
  - `[unk]` or low confidence escalates.
  - Everything else is escalated: written to a CSV for a paid batch. Nothing
    calls a paid API automatically.
  - Accepting as silver stays explicit (`--accept`) and goes through ADR-022's
    rules (never over human work, never benchmark sources).
- **Measured against gold.** `aerochorus adjudicate eval` runs a profile over the
  ATCO2 benchmark (gold in the isolated `reference` schema, read only by the
  evaluation module). It reports, side by side per batch:
  - token error rate, as produced and with canonical numbers;
  - exact match and callsign recall;
  - calibration (bins, ECE, Brier);
  - the gate's accept/escalate rates with accuracy on each side;
  - every ASR model and the ensemble representative on the same segments.

## Consequences

- Every segment can get an adjudicated reading at the cost of GPU time. The paid
  model sees only what the ensemble does not corroborate.
- Audio stays on hosts we run. When the vLLM server is on another machine, the
  runner sends audio to it over the LAN. Bind it to the LAN only, with a key.
- Results of different models never mix: each profile records its own id, and
  "already adjudicated" is per model and prompt version.
- The gate is only as good as its normalisation. Number formats it does not fold
  (e.g. "four fifty six" vs "456") escalate, which costs money, not accuracy.
- A 16 GB card running sweeps cannot also hold Voxtral Mini in bf16;
  `local-small-fp8` fits beside it, at some cost in accuracy.

## Verification

- `tests/unit/test_adjudication_backends.py`:
  - answer extraction and schema validation;
  - profiles, per-host overrides and every profile's server command;
  - the vLLM request (audio, instructions, schema, key) and the parse retry;
  - error classification and the fail-fast check;
  - squelch-split inputs.
- `tests/unit/test_squelch.py`: silence runs, splitting, flutter, fitting, pitch,
  WAV, ffmpeg decode.
- `tests/unit/test_adjudication_gate.py`: normalisation, family votes,
  research-only, silence, `[unk]`, confidence, near matches.
- `tests/integration/test_adjudication.py`:
  - local batches are free and claimed only by their runner;
  - an end-to-end local run with thinking stripped;
  - the gate endpoint and the gold evaluation, including calibration and the
    baseline.
- On the RTX 5080: `local-small-fp8` against real BWI clips and an ATCO2 sample
  ([LOCAL_ADJUDICATOR.md](../deployment/LOCAL_ADJUDICATOR.md)).
