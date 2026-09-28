# Review workbench

`/review` is where you browse the transcribed corpus, listen, compare
hypotheses, correct, and curate training data.

## Running it

Three processes: the control plane (Docker), the review edge server (native,
where the corpus is mounted), and the built UI (served by the edge).

```bash
docker compose up -d --build                 # control plane, applies migrations (0004+)
cd ui && npm ci && npm run build && cd ..    # once, and after UI changes
uv run aerochorus ui serve                   # http://127.0.0.1:8080/review
```

`ui serve` reads the worker config (`%APPDATA%\aerochorus\worker.toml`,
`~/.config/aerochorus/worker.toml`, or `--config`):

- `[sources]` gives the corpus roots;
- `api_url` gives the control plane (override with `--api`).

Options: `--host`, `--port` (default 8080), `--static` (default `ui/dist`).
There is no authentication. Keep `--host 127.0.0.1` unless the LAN is
trusted.

**UI development:** run `uv run aerochorus ui serve` and, in `ui/`, `npm run
dev`. Vite serves `http://127.0.0.1:5173` with hot reload and proxies
`/api`, `/audio` and `/edge` to the edge server. To use another edge, set
`AEROCHORUS_EDGE_URL`.

**One-time data setup** (the runbook covers each step):

```bash
uv run aerochorus source set-role atco2_fixed benchmark     # keep ATCO2 out of review/training
uv run aerochorus airport bootstrap KBWI --timezone America/New_York --station BWI
uv run aerochorus agreement refresh                         # backfill/refresh derived agreement
```

## Layout

```text
nav:      AeroChorus | Review | Transcribe | Training Sets | Adjudication      annotator  ?
toolbar:  [filters] [saved view ▾] [search … scope ▾ model ▾]   N segments  Sample N  Adjudicate…  cols  ↻  [inspector]
chips:    active filters (each removable) · Clear all
batch:    (when rows are checked) N selected · Add as candidate · Mark silver · Remove candidate/silver · Adjudicate…
body:     filter rail | segment grid (sticky header, virtual rows, pager) | ⋮ | inspector (resizable)
```

The inspector sections, top to bottom:

1. **Identity**: UTC, local time and zone, airport, frequency and the
   channel's service from the airport profile, duration, id and path,
   prev/next.
2. **Waveform** (WaveSurfer.js), with the audio fetched from the edge's
   `/audio/{id}`:
   - timeline, hover time, zoom, rate and volume;
   - drag to select a span; drag the edges to adjust it;
   - click a region to play it, and loop it with `L`;
   - a start/end readout, and "clear selection";
   - agreed utterances (below) shaded on the waveform, darker for 3+ families.

   Looping is on by default: the selected span if there is one, otherwise the
   whole file.

   If the audio fails, the edge's reason is shown with a Retry button.
3. **Agreement**:
   - counts (results, spoken, abstained, errors, families);
   - exact groups (G1…), each with families·providers, text and providers;
   - the near group, with its anchor and weakest similarity;
   - **agreed utterances** within the segment (U1…): maximal runs of 3+ words
     (2+ content words) that 2+ families say identically, even when the whole
     segment disagrees. Each shows families·providers, text, position, and
     bounds from word timestamps (`~` when estimated). "Use as span" (or `U`)
     starts a span with those bounds and the text prefilled. Matching words are
     highlighted in each hypothesis;
   - risk flags;
   - the representative-text source (`exact`, `near`, `medoid` or `single`).
4. **Hypotheses**: every model result, read-only. Each row shows the model,
   family, status, transcript, group and similarity to the representative,
   and model-specific confidence. Row actions: use as starting text, copy,
   and provenance (sweep, runtime, model sha, artifact, timings, errors).
   Superseded results from older sweeps are hidden behind "Show N older".
5. **Model adjudication**: see [below](#model-adjudication-gemini-via-openrouter).
   Each result shows the transcript, a word diff against the representative
   text, the model's confidence, the nearest hypothesis, uncertain words,
   callsigns, cost, and two actions: "Use as correction" (fills the editor)
   and "Accept as silver". "Adjudicate…" prices and sends this segment.
6. **Human annotation**: the correction textarea, a word diff against the
   selected hypothesis, the training label, reason tags and notes, and
   Save/Silver/Gold/Reject. It also shows version history.
7. **Spans**:
   - the saved spans on this segment (bounds, label, text, version);
   - an editor for the selected or new span: exact start/end ms, transcript,
     "prefill from hypothesis", label, notes, and version history;
   - gold needs its own confirmation.
8. **Neighbor context**: previous and next segments on the same channel,
   plus other channels within ±60 s. Each has a relative time, a preview, a
   play button and an open button. This is context only.
9. **Airport context** (collapsed): identifiers, reference point, runway
   ends with spoken forms, voice frequencies (the current channel is
   highlighted), and provenance.
10. **ADS-B context & map** (collapsed, on demand).
    - **Map**: a simplified, true-north map of the airport. It shows runway
      strips from the surveyed NASR runway ends (displaced thresholds
      marked), FAA Class B/C/D airspace with sectional-style
      ceiling/floor labels, range rings, north arrow and scale bar, at
      5/10/20/30 nm. It works without ADS-B. It is not for navigation.
    - **Traffic**: "Fetch ADS-B context" queries OpenSky for this segment's
      UTC ±60 s within 10 nm. Aircraft appear on the map with heading, a
      data block (altitude ×100 ft, climb/descent, speed ×10 kt) and tracks:
      solid before the segment start, dashed after. Ground traffic is muted
      and named on hover. Hovering a table row highlights the aircraft, and
      back. Reopening uses the cached snapshot; "Refresh context" queries
      again.
    - If credentials are not configured, the panel says so and everything
      else works. See [OPENSKY_PROVIDER.md](../context/OPENSKY_PROVIDER.md)
      and [AIRPORT_PROFILE.md](../context/AIRPORT_PROFILE.md).

## Filters and search (all server-side)

| filter | field |
| --- | --- |
| Airport (resolved from station aliases) | `airport` |
| Channel (normalized at ingest) | `channels` |
| UTC range | `utc_from`, `utc_to` |
| Model present, family present | `models`, `families` (JSONB `?|`) |
| Min model results (0 includes untranscribed) | `min_models` (default 1) |
| Min models that produced words | `min_success` |
| Min words in the representative text (hides "thank you") | `min_words` |
| Agreed utterance: families ≥ N, words ≥ N | `min_utterance_families`, `min_utterance_tokens` |
| Partial agreement (utterances agree, the whole segment does not) | `partial_agreement` |
| Exact providers ≥ N, exact families ≥ N, ≤ N | `min_exact_providers`, `min_exact_families`, `max_exact_families` |
| Near families ≥ N, near similarity ≥ X | `min_near_families`, `min_near_similarity` |
| Review status, training label | `review_status`, `training_label` |
| Has a span with label, has spans, partial usable | `span_labels`, `has_spans`, `partial_usable` |
| Error, abstention, flags | `has_error`, `has_abstention`, `has_error_or_abstention`, `flags` |
| Sources, include benchmark | `source_keys`, `include_benchmark` |
| Sample | `sample_id` |

**Search** is a contains match (`ILIKE`, with wildcards escaped) backed by
`pg_trgm` GIN indexes. It runs as `segment.id IN (union of per-scope id
sets)`, so each branch uses its own index.

| scope | searches |
| --- | --- |
| Any | every hypothesis, the representative text, and current human texts |
| Human | current human texts, including spans |
| Consensus/best | the representative text |
| Hypotheses | every hypothesis |
| Specific model | one model's hypotheses. With an empty query it means "has a result from that model". |

**Sort**: UTC, channel, duration, results, exact providers, exact families,
near families, or near similarity. Nulls sort last, with id as the
tie-break.

## Built-in views

| view | filters |
| --- | --- |
| 2+ exact families, 3+ words | `min_exact_families=2`, `min_words=3` |
| Partial agreement (utterances) | `partial_agreement=true` |
| 3+ family utterances | `min_utterance_families=3` |
| 2+ exact families | `min_exact_families=2` |
| 3+ exact providers | `min_exact_providers=3` |
| 3+ near families | `min_near_families=3` |
| High agreement / unreviewed | `min_exact_families=2`, `review_status=[unreviewed]` |
| Human corrected | `review_status=[corrected]` |
| Silver candidates | `training_label=[candidate, silver]` |
| Gold verified | `training_label=[gold]` |
| Disagreement / needs review | `min_success=2`, `max_exact_families=1`, `max_near_families=1` |
| Partial usable (gold/silver spans) | `partial_usable=true`, `min_models=0` |
| ASR errors or abstentions | `has_error_or_abstention=true` |

## Keyboard

Shortcuts work whenever focus is not in a text field. Clicking a row moves
focus to the grid.

| key | action |
| --- | --- |
| Space | play/pause |
| J / ↑ | previous segment (pages back at the top) |
| K / ↓ | next segment (pages forward at the bottom) |
| R | replay the selected span, or from the start |
| L | loop on/off (the selected span, else the whole file) |
| U | start a span from the next agreed utterance (text prefilled) |
| C | focus the correction editor |
| A | use the selected hypothesis as the starting text (keeps focus, so S/K follow) |
| S | mark silver |
| G | gold, after confirmation |
| X | reject |
| Ctrl+Enter | save (in the editor) |
| Esc | leave the editor |
| / | search |
| ? | help |

J/K follow the order in the design brief ("J/K: previous/next").

**Fast path:** pick a view, then K, Space, listen, then A and S (or C to
correct first), then K again.

**Partial gold:**

1. Drag over the clean phrase and press L.
2. Listen, then type the span transcript.
3. Choose Gold, then "Save new span" and confirm.
4. X rejects the parent if it is unusable.

## Sampling

"Sample N" stores:

- the filter definition and its SHA-256;
- the seed;
- the number of matching segments;
- the chosen ids, `ORDER BY md5(seed || ':' || id) LIMIT n`.

The same filters and seed on the same data give the same ids. The grid then
shows `sample_id` with a banner, and batch actions can target the sample.

## Model adjudication (Gemini via OpenRouter)

An adjudication sends one clip to an audio-capable model (default
`~google/gemini-pro-latest`, "Gemini Pro Latest"). The bundle holds the audio,
every model hypothesis, the agreement analysis (exact and near groups, agreed
utterances), the airport profile, cached ADS-B traffic (with airline telephony
hints, e.g. SWA456 = Southwest 456) and nearby transmissions. The model
returns a verbatim transcript as strict JSON, with confidence, uncertain
words, callsigns, the closest hypothesis and a note.
[ADR-022](../adr/0022-model-adjudication.md) has the design.

It costs money, so it is deliberate at every step:

1. **Choose segments**: check rows and press "Adjudicate…" in the batch bar;
   or "Adjudicate…" in the toolbar for a random sample of N from the current
   filters (for example the "Partial agreement" view); or "Adjudicate…" in
   the inspector for the open segment.
2. **Price**: the dialog shows how many segments will be sent (and why others
   are skipped), the audio minutes, the typical and worst-case cost at
   OpenRouter's published prices, and whether a runner is polling.
3. **Cap and confirm**: set a cost cap (the default is the worst case; the
   per-batch limit is `AEROCHORUS_ADJUDICATION_MAX_BATCH_USD`, default $25).
   Tick the confirmation that names the clip count and the maximum spend,
   then "Send N to Gemini". The server re-prices and refuses if the estimate
   moved.
4. **Run**: `aerochorus adjudicate run --follow` on the host with the audio
   and `AEROCHORUS_OPENROUTER_API_KEY`. On Linux this is
   `aerochorus-adjudicator.service`, installed but not enabled. Each item is
   sent only if its worst case still fits the cap.
5. **Review**: in the inspector, "Use as correction" to check and save the
   text yourself (as gold if you listened and it is exact), or "Accept as
   silver". The Adjudication page shows batches, progress, spend against the
   cap, and every item. "Accept as silver…" there takes a minimum confidence
   and, optionally, requires that an ASR hypothesis nearly matches.

Accepted text is silver with `text_origin = model_adjudicated`. It never
replaces human text, human gold or a rejection. It never applies to
benchmark sources, and it is never gold.

## Transcribe page

`/transcribe` queues a transcription run. Choose the portion (a day, a UTC
range or all, plus channels and duration bounds, only untranscribed segments,
optionally a random sample), then choose models (default: every voting
model). A preview shows segments, audio hours and the estimated time per model
from observed speed. Runs show progress, ETA, pause/resume/cancel, and the
worker's status. Nothing starts until you press "Queue transcription run".
A worker must be running to process it: `aerochorus worker transcribe`, or
the `worker` service on the Linux host.

**Model voting** (same page) lists every model that is enabled or has
results. Each row shows whether it votes in agreement or is research-only,
plus its evidence from the latest transcripts (up to 3,000 segments):

- exact and near-match rates against the consensus of 2+ other families
  (never its own vote);
- how often it speaks where the voters all heard nothing.

"Let vote…" / "Make research…" asks for a reason, records the decision
(who, when, why) and recomputes agreement for that model's segments.
Existing silver and gold labels are not changed. A new model stays research
until someone decides otherwise; look at its transcripts first (search scope
"Specific model").

## API

The workbench uses `/api/v1/review/*`, `/api/v1/airports/*`,
`/api/v1/datasets*`, `/api/v1/training/summary`, `/api/v1/context/adsb/*`,
`/api/v1/agreement/refresh`, `/api/v1/sweeps*`, `/api/v1/adjudications*`,
`/api/v1/adjudication-items/*`, `/api/v1/adjudication-status` and
`/api/v1/segments/{id}/adjudications`. See `http://127.0.0.1:8000/docs`.

## Tests

| suite | command |
| --- | --- |
| backend | `uv run pytest tests/integration/test_review.py tests/integration/test_edge.py tests/unit/test_agreement.py tests/unit/test_utterances.py tests/integration/test_adjudication.py tests/unit/test_adjudication_prompt.py` |
| frontend unit | `cd ui && npm test` |
| end-to-end | `cd ui && npm run build && npx playwright test` |

The end-to-end suite starts two `tests/e2e/stack.py` instances:

- :8765 on `aerochorus_e2e`, with a deterministic fake OpenSky provider and
  fixed adjudication prices (the spec plays the runner through the API; no
  OpenRouter call is ever made);
- :8766 on `aerochorus_e2e_plain`, with no ADS-B.

Each recreates its database, seeds a synthetic corpus, and serves everything
from one process. On Windows it uses the installed Edge. Elsewhere, run `npx
playwright install chromium` once, or set `PW_CHANNEL=chrome`.
