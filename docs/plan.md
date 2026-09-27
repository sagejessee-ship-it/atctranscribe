# AeroChorus — Local ATC Corpus Transcription, Ensemble, and Evidence Lab

This is the founding plan, recorded as agreed on 2026-09-26. Decisions made
later are recorded as ADRs in [adr/](adr/README.md).

## 1. Mission

AeroChorus is a local-first system for continuously processing a growing
archive of segmented ATC radio audio through multiple open-source ASR models.

Its first job is deliberately narrow:

> Reliably index existing ATC audio, transcribe selected corpus windows through
> a configurable suite of CrispASR models, preserve every model result with
> complete provenance, survive interruption, and make results inspectable.

At first it is not an ADS-B fusion system, an LLM adjudication system, an ATC
event extractor, or an operational reconstruction tool. Those are future
consumers of a reliable corpus and transcription foundation.

## 2. System context

**Linux ATC collector.** It remains responsible for SDR → radio reception →
segmentation → audio files, and it already works. AeroChorus treats its
network-mounted output as a read-only source corpus. It must never require the
collector to be rewritten, reorganised, renamed, or coupled to the
transcription system.

**RTX 5080 workstation.** The v1 ASR ensemble lives here. It becomes a source
of validated algorithms, regression tests and evaluation knowledge, and a
compatibility reference. It does not become AeroChorus's deployment
architecture.

**M1 MacBook Air (16 GB).** This becomes the primary background processing
host. Priorities, in order: low operational burden, low power, unattended
operation, resumability, correctness, throughput. Throughput is explicitly
secondary.

## 3. Architectural thesis

```
                EXISTING LINUX COLLECTOR
                         │ read-only network mount
                         ▼
                 IMMUTABLE AUDIO CORPUS
                         ▼
                  Corpus Indexer
                         ▼
                    PostgreSQL
                  ┌──────┴──────┐
                  ▼             ▼
              Local API      Scheduler
                                  ▼
                         Native Mac Worker
                                  ▼
                         CrispASR Server
                        [one model loaded]
                                  ▼
                       Transcription Results
                   ┌──────────────┴──────────────┐
                   ▼                             ▼
             PostgreSQL                   Raw Artifact Store (.json.zst)
```

Later, consumers of transcription results: deterministic evidence,
ensemble/consensus, human correction, LLM adjudication, ADS-B context, airport
context, event/operational analysis.

Everything below the source audio is an interpretation of evidence. Nothing
modifies the original evidence.

## 4. Foundational architectural decisions

- **ADR-001** — Create a clean AeroChorus repository; port validated v1
  behaviour selectively.
- **ADR-002** — Source audio is immutable and external. Sources are logical
  (`source_id = home_atc_archive`, `relative_path = 2026/09/26/segment.wav`).
  Machine-local configuration maps `home_atc_archive → /Volumes/ATC`. Absolute
  mount paths never become corpus identity.
- **ADR-003** — PostgreSQL is authoritative for corpus indexing, processing
  state, model registry snapshots, run state, transcription metadata, derived
  results and human annotations. JSONL is export/interchange only. This
  addresses v1's storage amplification of ~580 MB of JSONL per hour of audio.
- **ADR-004** — Raw model output is preserved once. PostgreSQL holds the
  normalised fields plus the artifact URI, SHA-256 and size. The artifact
  store holds `raw/<result_uuid>.json.zst`.
- **ADR-005** — CrispASR runs natively on macOS (Metal). Docker Compose runs
  PostgreSQL, the API and the web UI. The macOS host runs the worker and
  CrispASR. CrispASR is never containerised on the Mac.
- **ADR-006** — Persistent model process, sequential models. Start the server,
  load the model, process the whole requested window, stop, then start the
  next model. Restart CrispASR between models for a deterministic lifecycle,
  clean memory reclamation, simpler fault recovery, and cleaner provenance.
- **ADR-007** — Corpus windows, not infinite runs. A sweep run is corpus
  selection × model suite × run configuration. Every model finishes a window
  before the sweep advances. Window size is configurable.
- **ADR-008** — "All models" means all enabled, `sweep_eligible` ASR models in
  the selected suite, from an AeroChorus-owned registry (`logical_name`,
  `crisp_backend`, `architecture_family`, `model_file`, `model_sha256`,
  `upstream_revision`, `quantization`, `language`, `stable`, `experimental`,
  `sweep_eligible`, word timestamps, token confidence, diarization, Metal
  support, benchmark contamination).
- **ADR-009** — Exactly one architecture-family registry. Everything requiring
  independence uses `architecture_family`. No second family dictionary may
  exist.
- **ADR-010** — Absolute UTC is mandatory. Every segment should eventually
  have `capture_start_utc`/`capture_end_utc`. If it cannot be determined
  trustworthily, then `temporal_status = unresolved`: the segment can be
  transcribed but cannot join temporal fusion. Never fabricate a timestamp.

## 5. Technology stack

- **Backend:** Python, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL,
  `uv`.
- **Worker:** a small native Python process on macOS. It talks to the API, not
  PostgreSQL, so an M1, a Linux GPU box, the 5080 or a future machine can all
  be workers without control-plane changes.
- **Front end (later):** React, TypeScript, Vite, TanStack Query, TanStack
  Table, shadcn/ui, WaveSurfer.js. One component/design system. The look is a
  technical analysis workstation, not a generic SaaS dashboard.

## 6. Core database model

Start with only the tables needed for transcription.

- `corpus_source` — `id, name, logical_key, read_only, adapter_type,
  created_at`. The filesystem root is machine configuration.
- `segment` — `id, source_id, relative_path, source_recording_id?,
  capture_start_utc?, capture_end_utc?, duration_ms, file_size, file_mtime,
  sha256?, frequency_hz?, channel?, station?, temporal_status, metadata JSONB,
  first_seen_at, last_seen_at`; unique `(source_id, relative_path)`.
- `model` — `id, logical_name, architecture_family, crisp_backend,
  model_filename, model_sha256, upstream_model, upstream_revision,
  quantization, language, capabilities JSONB, pedigree JSONB, enabled,
  sweep_eligible, experimental`.
- `model_suite` — named rosters (`smoke`, `core-diverse`, `full-lab-v1`) with
  ordered members.
- `sweep_run` — `id, status, selection_definition JSONB, effective_config
  JSONB, config_sha256, crispasr_version, created_at, started_at,
  completed_at`.
- `sweep_run_model` — `run_id, model_id, execution_order, status, claimed_by,
  lease_expires_at, segments_total/completed/error/abstained, started_at,
  completed_at`. States: `queued → loading → running → completed`, or
  `failed → retrying`.
- `transcription_result` — exactly one per `sweep_run_model × segment`
  (unique): `status, text, language, inference_ms, has_word_timestamps,
  has_token_confidence, mean_token_confidence?, artifact_uri,
  artifact_sha256, error_type?, error_message?, created_at`. Result states
  distinguish `success`, `abstained` and `error`. An empty transcript is not
  automatically an error.

## 7. Job and resume semantics

No millions of Celery jobs. A worker claims a `sweep_run_model` and asks which
selected segments have no valid result for that run and model. It processes
those, committing each result immediately. Resume means processing the
missing set. Artifact existence is not proof of success. Resume is allowed
only when the effective configuration hash matches. A changed model or
configuration means a new run.

## 8. CrispASR integration contract

For each `sweep_run_model`:

1. Resolve the exact model artifact.
2. Verify its SHA-256.
3. Start the CrispASR server.
4. Verify backend and model identity.
5. Run smoke segments.
6. Process the corpus.
7. Persist every result immediately.
8. Record performance.
9. Verify coverage.
10. Stop CrispASR.
11. Mark the model run complete.
12. Advance to the next model.

CrispASR exposes an OpenAI-compatible transcription endpoint and a persistent
server mode. Auxiliary output (word/token arrays) differs by backend, and
AeroChorus must never assume it is identical.

## 9. Initial CrispASR model

Parakeet TDT 0.6B v3: small, already familiar from v1, a first-class CrispASR
backend (~467 MB), with word timestamps and token confidence. Its purpose is
to validate AeroChorus, not to find the best ATC model. After it, add
architecturally diverse families (Parakeet, Canary, Whisper, Qwen3-ASR,
Granite Speech, Voxtral). Each must pass AeroChorus's own smoke/compatibility
qualification before entering `full-lab`.

## 10. V1 knowledge that becomes v2 law

- Deterministic evidence stays separate from LLM reasoning
  (fusion → deterministic evidence → LLM/nominator/human).
- Abstention is not disagreement.
- Independence is measured by architecture family, never by raw model count.
- Scoring normalisation and agreement normalisation are different:
  `normalize_for_scoring()` and `normalize_for_evidence()`, exactly one
  implementation of each. `16L` vs `16R` must never collapse.
- Raw model confidence is not ensemble confidence. Do not average different
  probability scales. Calibrate later on held-out data.
- LLM-returned IDs use closed enumerations. Typed IDs only.
- Audio enhancement stays experimental (it made 7 of 9 v1 providers worse). It
  is not part of the core schema.

## 11. Evaluation philosophy

ATCO2 survives as a regression system. Gold boundaries may define evaluation
segments, but a gold transcript never reaches the ASR or adjudication path.
References should eventually live in a separate PostgreSQL schema with
restricted access. Every ensemble report shows the ensemble result, the best
individual model, and the per-segment oracle upper bound, because v1's
ensemble lost to its best uncontaminated individual model.

## 12. Phased build

- **Phase 0 — Foundation.** Docs/ADRs, project config, Compose skeleton,
  Postgres migrations, API skeleton, worker skeleton, model registry schema,
  tests. Acceptance: clean start, migration succeeds, API and worker report
  health, source audio cannot be modified, one family registry, no JSONL
  datastore. Non-goals: ASR, UI, ensemble, LLM, ADS-B.
- **Phase 1 — Corpus index.** Read-only filesystem adapter with incremental,
  idempotent scanning, stable identity, source-relative paths, metadata and
  UTC extraction. Acceptance: index a tiny fixture, one real day, then the
  full history. Repeat scans without duplicates. Verify source files are
  unchanged. A disconnected share reports `source_unavailable` without
  corrupting or deleting segment state.
- **Phase 2 — Single-model transcription.** Native CrispASR + Parakeet on
  ~100 representative segments. The model starts once. Results and raw
  artifacts are persisted with exact model, hash, quantization, CrispASR
  version and timing. The worker is killable and resumable with no
  duplicates. A config change prevents incompatible resume.
- **Phase 3 — Automated model sweeps.** `model_suite`, `sweep_run`,
  `sweep_run_model`, automatic model advancement. Test with 1 hour × 3 models,
  then 1 day × core-diverse. Populate `full-lab-v1` only after that passes.
- **Phase 4 — Evaluation and regression.** ATCO2 fixed-boundary evaluation,
  scoring/evidence normalisers, family-independence, empty ≠ disagreement,
  language-drift, gold-leakage tests, provider usability predicates, a
  best-single baseline. Do not copy v1's hand-set thresholds.
- **Phase 5 — Minimal research UI.** Corpus coverage, run progress, and a
  segment view (audio, waveform, metadata, all hypotheses, eventual human
  correction). No chatbot, map or ADS-B.
- **Phase 6 — Deterministic evidence and ensemble.** Source gating,
  abstention, family agreement, consensus core, disputed spans, length
  anomalies, language drift, critical ATC entity disagreement, named risk
  flags. Refit every threshold against evaluation data.
- **Phase 7 — Human correction and selective LLM adjudication.** Only
  ambiguous or high-value segments go to an LLM. LLM output is another derived
  interpretation, not truth. Human correction supersedes without deleting.
- **Phase 8 — Context providers.** A generic contract for ADS-B, airport,
  procedure, weather and document providers.

## 13. Explicitly not built yet

Kafka, Redis, Celery, Kubernetes, distributed scheduling, vector databases,
RAG, ADS-B ingestion, airport documents, LLM adjudication, semantic chat,
automated event detection, audio enhancement, diarization, real-time
transcription, elaborate authentication, multi-user permissions.

## 14. Power-aware worker behaviour

Eventually: `pause_on_battery`, `minimum_battery_percent`,
`pause_on_thermal_pressure`, `optional_processing_hours`. Initially: prevent
sleep while work is active and allow normal sleep when idle, record wall-clock
inference time and real-time factor, and do not maximise concurrency. Thermal
throttling is acceptable.

## 15. First major success ("V2 Foundation Complete")

Select one day from the mounted archive, choose a multi-model suite, press
Start, and walk away. AeroChorus indexes, loads model 1, processes every
segment, checkpoints every result, stops model 1, loads model 2, … It survives
interruptions, records complete provenance, preserves raw output, reports
progress, and finishes. No 5080, no manual model-server management, no JSONL
orchestration, no collector changes.

## 16. First coding milestone

Phase 0 + Phase 1 only. Once that works against the real network mount,
freeze and tag it, then write a separate implementation prompt for Phase 2.

## 17. Resolved foundational decisions

Clean v2 repository; name AeroChorus; collector unchanged; external read-only
audio; PostgreSQL; JSONL export-only; native CrispASR; Mac containers are
control plane only; sequential models, one persistent server per model;
worker coupling via API; experiment unit = corpus window × model suite;
immutable/versioned results; single family registry; UTC-first; ground truth
isolated from inference; raw output preserved once; best individual model
always reported beside the ensemble; LLM later, bounded, never the source of
truth.

## 18. To resolve during Phase 1

- **Linux corpus adapter:** directory structure, filename convention,
  sidecars, frequency/channel source, UTC encoding. Resolved: see
  [corpus/home_atc_archive.md](corpus/home_atc_archive.md) and ADR-012.
- **Artifact location** on the Mac (configuration; Phase 2).
- **Initial full-sweep roster:** benchmark candidates automatically after the
  single-model slice (loads, memory, speed, output validity, timestamps,
  confidence, language drift, failure rate). Only qualified models enter
  `full-lab-v1`.

## Guiding rule

> Does this help us produce, preserve, evaluate, or inspect trustworthy ASR
> hypotheses?

If not, put it in the backlog. AeroChorus should first become a boringly
reliable transcription laboratory.
