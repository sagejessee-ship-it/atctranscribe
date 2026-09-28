# ADR-016: The review edge server and derived agreement

Status: Accepted (2026-09-27)

## Context

The review workbench must play real source audio. The control plane runs in
Docker and cannot see the SMB archive (ADR-005); only workers read audio,
through `ReadOnlyCorpusReader` (ADR-002). The browser must not receive
filesystem paths or credentials, and AeroChorus must never copy, transcode
or modify source audio.

The workbench also needs agreement values it can filter and sort on across
448k segments. Before Phase 5 they existed only inside evaluation reports.

## Decision

**A review edge server.** `aerochorus ui serve` is a small native FastAPI
process. It runs on a machine that mounts the corpus (the Mac in production,
the 5080 box today) and reads the worker's config for its source roots. It
serves three things on one origin:

| path | what |
| --- | --- |
| `/` | the built UI (`ui/dist`), with SPA fallback |
| `/api/*` | a transparent proxy to the control plane |
| `/audio/{segment_id}` | source bytes read with `ReadOnlyCorpusReader`, checked against the indexed `segment.sha256`, with single-range support for seeking |

Like a worker, it never imports the database layer
(`test_worker_never_loads_the_database_layer` covers it). Errors are explicit
and recoverable:

- `503`: source not mounted or unavailable;
- `404`: file missing;
- `409`: bytes differ from the index, so rescan.

A small in-memory LRU avoids re-reading a segment for each range request.
Nothing is written to disk.

**Derived, versioned agreement.** `segment_agreement` holds one row per
segment. It is computed by `aerochorus.atc.agreement` over the latest result
of each model:

- exact groups use `normalize_for_evidence`, never the scoring normalizer;
- near groups use anchor-based `sequence_similarity` with an explicit
  threshold (`AEROCHORUS_NEAR_MATCH_THRESHOLD`, default 0.8), reported next
  to every value and never called confidence;
- provider count and family count are separate. Families come only from the
  model registry (ADR-009).

The row is refreshed inline when a result is recorded. `aerochorus agreement
refresh` recomputes any row that is missing, or whose version, threshold or
results are out of date.

## Consequences

- The UI works wherever the corpus is mounted. On a machine without it,
  review still works, and audio reports a clear 503.
- There is one extra process to run. There is still no auth, so bind it to
  loopback or a trusted LAN only.
- Agreement is queryable and indexed. Changing the algorithm means bumping
  `AGREEMENT_VERSION` and running `agreement refresh`.
- Results recorded by an older control plane (for example the pre-Phase-5
  API that ran sweep 6) need `agreement refresh` before they appear.
