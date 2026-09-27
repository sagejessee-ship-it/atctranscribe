# ADR-014: CrispASR runtimes and the sweep protocol

Status: Accepted (2026-09-27)

## Context

Phases 2–3 need one model resident at a time (ADR-006), processing whole
corpus windows (ADR-007). Every result must be committed as it is produced.
Interruptions must cost nothing, and every hypothesis must trace back to an
exact model artifact and runtime build. CrispASR ships two things we can use:
a server binary (macOS Metal, Windows/Linux CUDA, CPU) and a CUDA server
image (`ghcr.io/crispstrobe/crispasr:main-cuda`, RTX 50xx capable). Both
expose the same HTTP API (`/health`, `/v1/models`,
`/v1/audio/transcriptions`).

## Decision

**Runtimes.** The worker owns CrispASR through a `Launcher` with two
implementations behind the same HTTP client:

- `native` spawns the release binary. This is the Mac's runtime (ADR-005:
  Docker on macOS cannot reach Metal).
- `docker` runs the CUDA image with `--gpus all`, the model directory mounted
  read-only, and loopback-only publishing. This is for NVIDIA hosts such as
  the RTX 5080 box. Pin the image **by digest** in `worker.toml`.

Servers start with `CRISPASR_AUTO_DOWNLOAD=0` and explicit model paths.
Request parameters that would fetch extra models (e.g. Canary's auto-aligner)
are disabled in the catalog, so runs are hermetic.

**Model artifacts.** `config/models.toml` pins each model to a Hugging Face
commit, filename, size and SHA-256, and `aerochorus models sync` loads it into
the registry. A synced model's identity (family, backend, file, hash, request
params) is immutable; a different artifact needs a new `logical_name`. The
worker verifies the full SHA-256 before every model run.

**Sweep protocol** (worker ⇄ API, never worker ⇄ database):

1. `claim`: the next claimable model run in `(sweep id, execution order)`
   order. Claimable means queued/retrying, or active with an expired lease,
   or active and held by this same worker (crash recovery). Attempts are
   counted.
2. The worker verifies the model file, reads the runtime identity, starts
   the server, waits for `/health`, and checks that the reported backend and
   loaded filename match the registry.
3. `start`: records the runtime description and **fingerprint** (CrispASR
   version + build, image digest or binary hash, model SHA-256, backend). If
   results already exist under a different fingerprint, the API refuses and
   the remedy is a new sweep. Results from different builds are never mixed
   in one model run.
4. `pending` → transcribe → `results`, one POST per segment, committed
   immediately, extending the lease. The first request that reaches the
   server is the smoke test: if it fails, the model run fails with no rows
   written.
5. `finish`: the model run completes only when every selected segment has a
   result. Otherwise the worker releases or fails it.

**Result semantics.** There is exactly one row per (model run, segment),
enforced by a unique constraint. `success` has non-empty text. `abstained`
has empty text and is not an error. `error` carries an `error_type`. An error
row is retried once on the next attempt of that model run
(`sweep retry --model`). Success and abstention rows are final. Source audio
is re-hashed before inference; a mismatch against the indexed SHA-256 is a
`source_changed` error, not a transcript. An unreachable source releases the
run without writing anything.

**Raw output** is written once per result as `artifact://<store>/raw/<ab>/<uuid>.json.zst`
(ADR-004). The envelope holds the request, runtime, HTTP status, timing and
the untouched response body. PostgreSQL keeps the URI, SHA-256 and size.

**Selections are frozen.** A sweep materialises its segment list
(`sweep_run_segment`) and `effective_config` at creation, and
`config_sha256` identifies the configuration. The same selection and suite
always yield the same hash. Full sweeps require `sweep_eligible` models;
smoke and qualification sweeps opt in with `--allow-unqualified`.

## Consequences

- Kill -9 at any point loses at most the in-flight segment. Recovery is just
  running the worker again. Orphaned containers are removed by name on the
  next start.
- A CrispASR upgrade mid-sweep is detected, not silently mixed. This matters
  in practice. The same Parakeet file on the same GPU gave different words
  under the Linux container build and the Windows native build of the same
  CrispASR version ("Mary **Two** one thirty two … clear lane **wing**" vs
  "Mary **Tula** one thirty two … clear lane **wind**", 2026-09-27).
- Different model runs of one sweep may be served by different machines. A
  model run that already has results can only be resumed by a runtime with the
  same fingerprint, so in practice the machine that starts a model run
  finishes it.
- No model tested returns token probabilities through the server (0.8.37).
  `mean_token_confidence` stays null rather than being derived from
  incomparable scores.
