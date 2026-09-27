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
nav:      AeroChorus | Review | Training Sets                       annotator  ?
toolbar:  [filters] [saved view ▾] [search … scope ▾ model ▾]   N segments  Sample N  cols  ↻  [inspector]
chips:    active filters (each removable) · Clear all
batch:    (when rows are checked) N selected · Add as candidate · Mark silver · Remove candidate/silver
body:     filter rail | segment grid (sticky header, virtual rows, pager) | ⋮ | inspector (resizable)
```

The inspector sections, top to bottom:

1. **Identity**: UTC, local time and zone, airport, frequency and the
   channel's service from the airport profile, duration, id and path,
   prev/next.
2. **Audio**: play/pause, replay, seek, time, rate, volume, and "autoplay on
   open". Audio comes from `/audio/{id}` on the edge. If it fails, the edge's
   reason is shown with a Retry button.
3. **Agreement**:
   - counts (results, spoken, abstained, errors, families);
   - exact groups (G1…), each with families·providers, text and providers;
   - the near group, with its anchor and weakest similarity;
   - risk flags;
   - the representative-text source (`exact`, `near`, `medoid` or `single`).
4. **Hypotheses**: every model result, read-only. Each row shows the model,
   family, status, transcript, group and similarity to the representative,
   and model-specific confidence. Row actions: use as starting text, copy,
   and provenance (sweep, runtime, model sha, artifact, timings, errors).
   Superseded results from older sweeps are hidden behind "Show N older".
5. **Human annotation**: the correction textarea, a word diff against the
   selected hypothesis, the training label, reason tags and notes, and
   Save/Silver/Gold/Reject. It also shows version history.
6. **Neighbor context**: previous and next segments on the same channel,
   plus other channels within ±60 s. Each has a relative time, a preview, a
   play button and an open button. This is context only.
7. **Airport context** (collapsed): identifiers, reference point, runway
   ends with spoken forms, voice frequencies (the current channel is
   highlighted), and provenance.

## Filters and search (all server-side)

| filter | field |
| --- | --- |
| Airport (resolved from station aliases) | `airport` |
| Channel (normalized at ingest) | `channels` |
| UTC range | `utc_from`, `utc_to` |
| Model present, family present | `models`, `families` (JSONB `?|`) |
| Min model results (0 includes untranscribed) | `min_models` (default 1) |
| Min models that produced words | `min_success` |
| Exact providers ≥ N, exact families ≥ N, ≤ N | `min_exact_providers`, `min_exact_families`, `max_exact_families` |
| Near families ≥ N, near similarity ≥ X | `min_near_families`, `min_near_similarity` |
| Review status, training label | `review_status`, `training_label` |
| Has a span with label | `span_labels` |
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
| 2+ exact families | `min_exact_families=2` |
| 3+ exact providers | `min_exact_providers=3` |
| 3+ near families | `min_near_families=3` |
| High agreement / unreviewed | `min_exact_families=2`, `review_status=[unreviewed]` |
| Human corrected | `review_status=[corrected]` |
| Silver candidates | `training_label=[candidate, silver]` |
| Gold verified | `training_label=[gold]` |
| Disagreement / needs review | `min_success=2`, `max_exact_families=1`, `max_near_families=1` |
| ASR errors or abstentions | `has_error_or_abstention=true` |

## Keyboard

Shortcuts work whenever focus is not in a text field. Clicking a row moves
focus to the grid.

| key | action |
| --- | --- |
| Space | play/pause |
| J / ↑ | previous segment (pages back at the top) |
| K / ↓ | next segment (pages forward at the bottom) |
| R | replay from the start |
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

## Sampling

"Sample N" stores:

- the filter definition and its SHA-256;
- the seed;
- the number of matching segments;
- the chosen ids, `ORDER BY md5(seed || ':' || id) LIMIT n`.

The same filters and seed on the same data give the same ids. The grid then
shows `sample_id` with a banner, and batch actions can target the sample.

## API

The workbench uses `/api/v1/review/*`, `/api/v1/airports/*` and
`/api/v1/agreement/refresh`. See `http://127.0.0.1:8000/docs`.

## Tests

| suite | command |
| --- | --- |
| backend | `uv run pytest tests/integration/test_review.py tests/integration/test_edge.py tests/unit/test_agreement.py` |
| frontend unit | `cd ui && npm test` |
| end-to-end | `cd ui && npm run build && npx playwright test` |

The end-to-end suite starts `tests/e2e/stack.py`. It recreates the
`aerochorus_e2e` database, seeds a synthetic corpus, and serves everything
on :8765. On Windows it uses the installed Edge. Elsewhere, run `npx
playwright install chromium` once, or set `PW_CHANNEL=chrome`.
