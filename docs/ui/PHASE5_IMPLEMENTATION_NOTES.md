# Phase 5 implementation notes

Written before implementation (2026-09-27) from an inspection of the
repository at the end of Phase 4. It records what exists, what Phase 5 needs,
and how each prompt "stop condition" was resolved.

## What exists

| Area | State at start of Phase 5 |
| --- | --- |
| Frontend | **None.** No `package.json`, no UI code. Node 24 LTS installed for this phase. |
| Backend | FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL 17 (Docker Compose, control plane only). Workers are native and reach PostgreSQL only via HTTP (ADR-011). |
| Segments | `segment`: `source_id`, `relative_path`, `capture_start_utc`/`end`, `temporal_status`, `duration_ms`, `station`, `channel`, `frequency_hz`, `sha256`, `presence_status`, `metadata`. 448k BWI + 877 ATCO2 clips. |
| Channel | Normalized at ingest by the filename parser, never in the UI: `BWI_GND_…_121900000.mp3` → `station=BWI, channel=GND, frequency_hz=121900000`. Observed BWI channels: GND, TWR, CLNC, APP_FS, APP_FN, TRSA, GND_ALT. |
| Airport | **No airport field.** `segment.station` holds the collector label (`BWI`, an FAA/IATA id) or, for ATCO2, the ICAO code. |
| Results | `transcription_result`: one row per (sweep model run, segment); `status` success/abstained/error; `text`; `language`; `quality` flags; `mean_token_confidence` (null for every current model); raw artifact URI. A segment can have several results for the same model across sweeps (112 such pairs today). |
| Families | Exactly one registry: `model.architecture_family` → FK `architecture_family.key` (ADR-009). The same family can hold several models (`canary-1b-v2-q8_0` and `…-beam4`). |
| Normalizers | `aerochorus.atc.normalize`: `normalize_for_scoring` (gold accuracy) and `normalize_for_evidence` (agreement). Order-aware similarity: `aerochorus.atc.align.sequence_similarity` (LCS / longer length). |
| Agreement | Computed **only inside evaluation reports**. No per-segment, queryable agreement values. |
| Human annotation | **None.** `reference.gold_segment` is *benchmark gold* for evaluation (isolated schema, ADR-015). It is not the place for human review labels. |
| Training sets | None. |
| Audio serving | **None.** The API runs in Docker and cannot see the SMB archive; only workers read audio, through `ReadOnlyCorpusReader`. |
| Airport data | None. |
| Search | None; `pg_trgm` 1.6 is available in the Postgres image. |

## Stop conditions: all resolved without a stop

| Condition | Resolution |
| --- | --- |
| Versioned human annotations cannot be represented safely | New append-only tables `annotation_thread` + `annotation_version` (ADR-017). Hypotheses stay in `transcription_result`, untouched. |
| Architecture-family mapping duplicated/inconsistent | Not the case: one FK-backed registry. Agreement reads the family from `model` only; a regression test forbids same-family double counting. |
| Source audio not safely streamable read-only | Solved with a **local workbench edge server** (`aerochorus ui serve`, ADR-016). It runs natively on a machine that mounts the corpus, serves the built UI, proxies `/api` to the control plane, and streams audio through `ReadOnlyCorpusReader` with SHA-256 verification. It never touches PostgreSQL. |
| Segment UTC missing for OpenSky | BWI segments are 448,017 `resolved` + 1 `unverified`. The OpenSky provider refuses segments without UTC. |
| Channel cannot be reliably derived | It can; see above. Channel is exposed as stored, never parsed in React. |
| Agreement not matching the evidence-normalization contract | Agreement is computed server-side with `normalize_for_evidence` (exact) and `sequence_similarity` on evidence tokens (near), never the scoring normalizer. |
| Frontend stack conflict | None (no frontend existed). |
| Training labels would overwrite model outputs | They cannot: separate tables, and a test asserts hypotheses are unchanged after corrections. |
| Registry cannot represent a converted fine-tuned model | It can, with a validated `lineage` schema in `model.pedigree` and a new `model.qualification` column (ADR-019). |

## Additional correction found during inspection

**Benchmark contamination.** The ATCO2 clips are an ordinary corpus source
(`atco2_fixed`). Without a guard, a reviewer could annotate them and export
them into a training set, contaminating the regression benchmark. Phase 5
adds `corpus_source.role ∈ {corpus, benchmark}`:

- `atco2_fixed` becomes `benchmark`;
- review hides benchmark sources by default;
- batch nomination and dataset export refuse benchmark segments.

## Changes Phase 5 makes

1. **Derived agreement** (`segment_agreement`, versioned). One row per
   segment, recomputed whenever a result arrives (and via
   `aerochorus agreement refresh`). It is computed over the latest result of
   each model:
   - exact groups;
   - provider and family counts;
   - near groups at a configured threshold;
   - a representative text;
   - error, abstention and flag summaries.
2. **Airport profiles** (`airport`, `airport_runway`, `airport_frequency`,
   `airport_alias`). They are generic, bootstrapped once for KBWI from FAA
   NASR data with provenance, and segment airport is resolved server-side
   from `segment.station`.
3. **Review API** (`/api/v1/review/*`) provides:
   - a server-side filtered, sorted and paginated segment list with `pg_trgm`
     search;
   - segment detail with hypotheses, agreement, annotations, neighbors and
     airport context;
   - built-in views, deterministic samples, and batch nomination.
4. **Annotation API**: versioned saves with optimistic concurrency; gold
   requires explicit confirmation and is never set in batch.
5. **Edge server** (`aerochorus ui serve`): static UI, `/api` proxy, and
   read-only `/audio/{segment_id}`.
6. **Frontend** in `ui/`: React + TypeScript + Vite, TanStack
   Query/Table/Virtual, Radix primitives, CSS-variable tokens, and
   WaveSurfer.js (5B).
7. **5B:** span threads on the same annotation tables, a waveform editor,
   span search/filters, versioned dataset export with materialized clips.
8. **5C:** an OpenSky context provider behind an isolated boundary (Trino
   historical state vectors), plus a cached `context_snapshot`.

## Status: Phase 5A complete (2026-09-27)

| # | 5A acceptance criterion | Evidence |
| --- | --- | --- |
| 1 | Local website starts with a documented workflow | `docs/ui/REVIEW_WORKBENCH.md`, runbook §13 (`aerochorus ui serve`) |
| 2 | Review page loads real data | verified against the live BWI index (448,022 segments; 4,913 transcribed in the day sweep) |
| 3 | Sorts and filters without fetching all rows | server-side `POST /review/query` (offset/limit ≤ 500); 10–35 ms on real data |
| 4 | Search finds text in hypotheses | `test_search_scopes`; E2E "search, correct, confirm gold" |
| 5 | Channel filter uses backend metadata | `channels` filter on `segment.channel`; facets from the DB |
| 6 | `exact family count >= 2` is correct | `test_agreement_views`; E2E "high-agreement review" |
| 7 | Near-family filtering is correct | `test_agreement_views` (`min_near_families`) |
| 8 | No same-family double counting | `test_same_family_models_never_double_count` (unit), `test_agreement_views` |
| 9 | Selected segment's real audio plays | E2E (`currentTime > 0`); real NAS audio checked in the browser |
| 10 | Correction saves and survives reload | E2E "search, correct, confirm gold" (reload) |
| 11 | Original hypotheses unchanged | `test_annotation_versions_are_append_only…`; E2E asserts the hypothesis text |
| 12 | Candidate/silver/gold/rejected are distinct | enums + DB CHECKs; `test_batch_nomination`, `test_gold_requires…` |
| 13 | Batch silver works | `test_batch_nomination`; E2E "batch silver needs agreement" |
| 14 | BWI context renders from the FAA profile | `airport bootstrap KBWI` (NASR 2026-09-03 → 10/28, 15L/33R, 15R/33L); E2E "neighbor and airport context" |
| 15 | Previous/next same-channel context | `test_segment_detail`; E2E |
| 16 | Keyboard navigable | E2E "keyboard navigation…" (J/K, ?, Esc) + axe WCAG 2 A/AA with no serious/critical violations |
| 17 | Unavailable audio gives a clear, recoverable error | `test_audio_unavailable_and_changed_sources_fail_clearly`; E2E "unavailable source audio" |
| 18 | Backend, frontend and E2E tests pass | pytest (including `test_review.py`, `test_edge.py`), vitest, Playwright (6 flows) |

### Decisions made while building 5A

- **Batch silver needs agreement.** A segment qualifies only if its
  representative text came from 2+ families (exact or near), or a human text
  exists. Otherwise it is skipped as `insufficient_agreement`. Candidate has
  no such bar.
- **Keyboard `A` fills the editor without taking focus**, so the fast path
  (listen → A → S → K) needs no Esc. The row button "use as starting text"
  does focus the editor.
- **J = previous, K = next.** This follows the brief literally; ↑/↓ also
  work.
- **shadcn/ui pattern without Tailwind.** Local wrappers over Radix, styled
  with CSS tokens (see DESIGN_SYSTEM.md).
- **Package majors pinned** to known APIs: TanStack Table 8, React Router 7,
  Vite 7, TypeScript 5.9.
- **The E2E stack is one process.** The edge proxies to an in-process
  control plane on a throwaway `aerochorus_e2e` database. A second,
  unmounted source exercises the audio-unavailable path.
- **Results from the running sweep-6 API** (pre-0004 code on :8001) do not
  refresh agreement inline. Run `aerochorus agreement refresh` after that
  sweep ends.

## Status: Phase 5B complete (2026-09-27)

| # | 5B acceptance criterion | Evidence |
| --- | --- | --- |
| 1 | Waveform renders real audio | WaveSurfer on the edge's `/audio`; checked on real NAS audio and in E2E "partial gold" |
| 2 | User can drag a region | E2E: mouse drag on the waveform creates the draft region and readout |
| 3 | Region loops correctly | E2E: with loop on, 8 samples of the playback position all stay within the span |
| 4 | Bounds persist after reload | E2E: reload, then the span list shows `0.30–0.90`, and selecting it restores the region |
| 5 | Gold span coexists with a non-gold parent | E2E: gold span plus rejected parent; the "Partial usable" view finds it; `test_partial_usable_and_span_search` |
| 6 | Span transcript is searchable | `test_partial_usable_and_span_search` ("human" scope) |
| 7 | Export creates derived clipped audio | `test_export_materializes_clips_and_never_touches_sources` (real ffmpeg) |
| 8 | Parent source hash unchanged | same test: corpus snapshot before = after; export refuses a changed source |
| 9 | Optional re-transcription keeps parent/offset provenance | **Deferred** (ADR-018 records the design) |
| 10 | Annotation history is auditable | append-only versions for segments and spans; history in both editors |

Also delivered with 5B:

- versioned datasets (`training_dataset`, `training_dataset_item`,
  migration 0005);
- the `/training` page;
- `dataset create/list/export`;
- ADR-019 lineage plus the qualification gate (`models qualify`,
  `test_model_lineage.py`).

## Status: Phase 5C complete (2026-09-27)

| # | 5C acceptance criterion | Evidence |
| --- | --- | --- |
| 1 | ADS-B context does not query automatically | `test_fetch_is_explicit_bounded_cached_and_refreshable` (status and segment open make no calls); E2E call counter |
| 2 | The button fetches context for the selected segment | E2E "ADS-B fetched on demand…" |
| 3 | Old-data lookup uses Trino | `minio.osky.state_vectors_data4` via the Trino statement protocol; `test_rest_states_all_is_never_used` |
| 4 | Query is tightly bounded in time and space | `test_query_is_tightly_bounded_in_time_space_and_partitions`, `test_broad_scans_are_refused` |
| 5 | Result is cached | `context_snapshot` (migration 0006) |
| 6 | Reopening uses the cache without another call | integration + E2E (reload, same snapshot id, call count unchanged) |
| 7 | Refresh performs a new query | integration + E2E (new snapshot id, call count +1) |
| 8 | Missing credentials do not break review | `test_unconfigured_provider_keeps_review_working`; E2E on the unconfigured stack |
| 9 | Provider failure cannot lose annotations | `test_provider_failure_stores_nothing_and_loses_no_annotation` |
| 10 | Query and source provenance displayed | provenance line: source table, window, radius, hours, vectors, response hash, snapshot id |
| 11 | No map or bulk ADS-B archive | none added (ADR-020) |

**Not verified live:** no OpenSky credentials were available. Auth and
endpoints follow OpenSky's Trino docs and the pyopensky source. The first
real fetch is the remaining check.

