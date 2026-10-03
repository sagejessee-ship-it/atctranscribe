# AeroChorus runbook

This runbook covers checking the system, running the tests, and processing
real data from the CLI. Every command below has been run on the RTX 5080 box
(JesseePC, Windows 11). Differences for the M1 MacBook Air are called out
inline.

> Shell note: commands are shown for bash-style shells. On Windows use Git
> Bash, or PowerShell with the same commands. Every `aerochorus …` command is
> run from the repository root as `uv run aerochorus …`.

- [0. Where things stand](#0-where-things-stand)
- [1. One-time setup](#1-one-time-setup)
- [2. Is everything up?](#2-is-everything-up)
- [3. Tests](#3-tests)
- [4. Corpus indexing](#4-corpus-indexing)
- [5. Models and the CrispASR runtime](#5-models-and-the-crispasr-runtime)
- [6. Transcribing a subset](#6-transcribing-a-subset)
- [7. Evaluating against ATCO2](#7-evaluating-against-atco2)
- [8. Qualifying models into `full-lab-v1`](#8-qualifying-models-into-full-lab-v1)
- [9. Unattended operation](#9-unattended-operation)
- [10. Moving the control plane to the M1](#10-moving-the-control-plane-to-the-m1)
- [11. Troubleshooting](#11-troubleshooting)
- [12. Reference](#12-reference)
- [13. Review workbench (Phase 5)](#13-review-workbench-phase-5)

---

## 0. Where things stand

| Phase | State | Evidence |
| --- | --- | --- |
| 0 Foundation | done | Compose stack, migrations, `/health`, worker health, ADRs, architecture tests |
| 1 Corpus index | done | Full history: 448,018 segments / 559.5 h of audio in 52 min. 448,017 UTC-resolved, 1 flagged `unverified`, 0 probe errors. Incremental rescan 3.2 s, no duplicates; 0 of 447,999 source files changed; offline share → `source_unavailable` |
| 2 Single-model transcription | done | Sweep 1: Parakeet over 100 real segments (80 transcribed, 20 abstained, 0 errors) |
| 3 Automated model sweeps | done (CLI) | Sweep 2: six families × 100 segments, with kill -9 mid-run and a clean resume. Sweep 3: one hour (333 segments) × 4 families. Model runs and sweeps advance automatically; 0 errors |
| 4 Evaluation / regression | done | ATCO2 benchmark (877 clips, gold in an isolated schema, derived calibration/test split); one symmetric scorer; entity recall; best-single + oracle; background quality flags on every result; v1 hypotheses re-scored for comparison |
| 5 Research UI | not started | The CLI (`sweep transcripts`, `eval segments`, `segment results`) is the interim inspector |
| 6+ | not started | |

Components:

```
Linux collector ─SMB─▶ worker (native Python) ─HTTP─▶ API (Docker) ─▶ PostgreSQL (Docker)
                           │
                           └─▶ CrispASR server, one model at a time
                               · RTX 5080 box: CUDA container (ghcr.io/crispstrobe/crispasr, pinned digest)
                               · M1 Air: native Metal binary
```

Data locations on the 5080 box:

| What | Where |
| --- | --- |
| Source audio (read-only) | `\\192.168.68.84\bwi` |
| Worker config | `%APPDATA%\aerochorus\worker.toml` |
| Model artifacts | `C:\Users\sagej\aerochorus-data\models` (9.2 GB, six models) |
| Raw CrispASR output | `C:\Users\sagej\aerochorus-data\artifacts\raw\…\*.json.zst` |
| CrispASR server logs | `C:\Users\sagej\aerochorus-data\logs\` |
| ATCO2 benchmark clips (derived, no gold) | `C:\Users\sagej\aerochorus-data\corpora\atco2_fixed` |
| Native CrispASR build (optional) | `C:\Users\sagej\aerochorus-data\crispasr\v0.8.37-windows-cuda13\…\crispasr.exe` |
| Database | Docker volume `aerochorus_pgdata` |

---

## 1. One-time setup

### 1.1 Control plane (the machine that runs Docker)

```bash
uv sync
docker compose up -d --build        # postgres + migrate + api on 127.0.0.1:8000
curl http://127.0.0.1:8000/health   # {"status":"ok", ... "migrations_current":true}
```

`migrate` runs `aerochorus db upgrade` on every `up`. After pulling new code,
run `docker compose up -d --build` again.

### 1.2 Register the corpus source (once per database)

```bash
uv run aerochorus source add home_atc_archive --name "Home ATC archive (BWI)" \
    --parser rtlsdr_airband --timezone America/New_York --sentinel 2026
```

### 1.3 Worker on the RTX 5080 box (Windows)

1. Create the worker config at `%APPDATA%\aerochorus\worker.toml` (template:
   [config/worker.example.toml](../config/worker.example.toml)):

   ```toml
   worker_name = "jesseepc-5080"
   api_url = "http://127.0.0.1:8000"
   read_concurrency = 8

   [sources.home_atc_archive]
   root = '\\192.168.68.84\bwi'

   [sources.atco2_fixed]          # benchmark clips, this box only (§7)
   root = 'C:\Users\sagej\aerochorus-data\corpora\atco2_fixed'

   [transcription]
   models_dir = 'C:\Users\sagej\aerochorus-data\models'
   artifact_root = 'C:\Users\sagej\aerochorus-data\artifacts'
   artifact_store = "jesseepc"

   [transcription.crispasr]
   launcher = "docker"
   image = "ghcr.io/crispstrobe/crispasr@sha256:bc53f7a834140c86923c7ccb5f60867ec12b978e8ef12d8d8776d8034eb0eec0"
   gpus = "all"
   host_port = 8090
   ```

2. Pull the CrispASR image. It is 2.3 GB, CUDA 13, and needs NVIDIA driver
   R580+; this box has 610.88.

   ```bash
   docker pull ghcr.io/crispstrobe/crispasr@sha256:bc53f7a834140c86923c7ccb5f60867ec12b978e8ef12d8d8776d8034eb0eec0
   ```

   To move to a newer CrispASR, pull the new tag, read its digest with
   `docker image inspect ghcr.io/crispstrobe/crispasr:main-cuda --format '{{.Id}}'`,
   and update `image`. Model runs already started under the old digest will
   refuse to resume on the new one (ADR-014). Finish them or start new sweeps.

3. Load the model catalog and download the models (§5).

To use the native Windows build instead of the container, set
`launcher = "native"` and
`binary = 'C:\Users\sagej\aerochorus-data\crispasr\v0.8.37-windows-cuda13\crispasr-windows-x86_64-cuda13\crispasr.exe'`.

### 1.4 Worker on the M1 MacBook Air

```bash
# 1. Mount the archive read-only (use an SMB account with read-only rights).
mkdir -p /Volumes/ATC
mount_smbfs -o rdonly //USER@192.168.68.84/bwi /Volumes/ATC

# 2. Get the native CrispASR build and verify it against the release digest.
mkdir -p ~/aerochorus-data/crispasr && cd ~/aerochorus-data/crispasr
gh release download v0.8.37 -R CrispStrobe/CrispASR -p crispasr-macos.tar.gz
shasum -a 256 crispasr-macos.tar.gz
#   expect 4d3525c26d52cb0cfdfa0888b810b4b4e98e2d5c7035ced43e333a8ecc72377a
tar xzf crispasr-macos.tar.gz && ./crispasr --version     # ggml backends should list metal

# 3. Worker code and config.
git clone https://github.com/sagejessee-ship-it/atctranscribe.git && cd atctranscribe
uv sync
mkdir -p ~/.config/aerochorus && cp config/worker.example.toml ~/.config/aerochorus/worker.toml
# edit: worker_name, api_url (the control plane), root = "/Volumes/ATC",
#       models_dir / artifact_root under ~/aerochorus-data, launcher = "native",
#       binary = path to crispasr
```

If the control plane runs on the Mac too, do §1.1 there. Otherwise point
`api_url` at the machine that runs it. The API binds to 127.0.0.1 by
default; change the Compose port mapping deliberately if you want it on the
LAN.

---

## 2. Is everything up?

Run these in order. Each one is safe to repeat.

```bash
docker compose ps                         # postgres + api "Up (healthy)"; migrate "Exited (0)"
curl -s http://127.0.0.1:8000/health      # "status":"ok", "migrations_current":true
uv run aerochorus worker health           # exit 0; sources.*.available = true; api.reachable = true
uv run aerochorus source summary home_atc_archive   # segment counts by presence/temporal status/day
uv run aerochorus models list             # six models, enabled
uv run aerochorus worker models verify    # every model "ok" (full SHA-256 check, ~8 s for 9.2 GB)
uv run aerochorus worker crispasr check   # runtime: launcher, crispasr_version, image digest, ggml backends
```

Live GPU check. This loads a model in CrispASR and transcribes one real file
(about 15 s):

```bash
uv run aerochorus worker crispasr check --model parakeet-tdt-0.6b-v3-q8_0 \
    --audio //192.168.68.84/bwi/2026/09/08/BWI_TWR_20260908_111051_119400000.mp3
# health {"status":"ok","backend":"parakeet"}, loaded [/models/parakeet-…gguf],
# transcription 200 in ~360 ms: "…runway three three left, clear lane wing two three zero six…"
```

Health semantics: `worker health` exits 1 with `"status": "degraded"` if the
API is unreachable, migrations are pending, or a configured source root is
unavailable. An unmounted or empty mount point counts as unavailable.

---

## 3. Tests

```bash
uv run pytest tests/unit                  # ~1 s, no database, no GPU, no network
AEROCHORUS_TEST_DATABASE_URL=postgresql+psycopg://aerochorus:aerochorus@127.0.0.1:5432/aerochorus_test \
    uv run pytest                         # full suite, ~25 s; needs the Compose Postgres
uv run ruff check . && uv run ruff format --check .
cd ui && npm test                          # frontend unit tests (vitest)
cd ui && npm run build && npx playwright test   # browser E2E against tests/e2e/stack.py (aerochorus_e2e DB)
```

The integration suite rebuilds the `aerochorus_test` database from the
migrations on every run. No test needs a GPU, the network or real audio:
CrispASR is replaced by a scriptable fake (`tests/fake_crispasr.py`), and
corpora are synthetic collector-format MP3s.

| Area | What is proven |
| --- | --- |
| Architecture | the worker never loads the DB layer; no mutating filesystem calls outside the model/artifact/log writers; one architecture-family registry; no JSONL datastore |
| Schema | the models match the migrations; migrations round-trip; CHECKs for read-only sources, UTC ⇔ temporal status, result semantics |
| Corpus | idempotent scans in every mode; read-only fixtures unchanged; disconnect mid-scan → `source_unavailable`; missing/reappearing files; content changes; unsettled files; DST fold/gap |
| ATC domain | v1 regression cases ported: symmetric scoring, `16L ≠ 16R`, markup stripping, language drift kept intact, fluent hallucination ≠ consensus, loop/off-domain/low-ATC flags |
| Evaluation | exact TER/S/D/I arithmetic, abstentions as deletions, entity recall, best-single/oracle, splits; gold never in any worker payload or import graph; gold immutable; source-aware claims; flags on ingest + reflag |
| Review (Phase 5) | filters/views/sort/paging, trigram search per scope (wildcards escaped), same-family never double-counted, append-only versions + 409 on stale saves, gold needs confirmation (and a DB CHECK), benchmark never trainable, batch silver needs 2-family agreement, deterministic samples, span bounds, airport resolution, agreement backfill |
| Edge server | audio bytes identical to source, Range/416, not-mounted → 503, changed bytes → 409, API proxy, SPA fallback without path traversal |
| Sweeps | two-model sweep end to end; one server per model; kill mid-run → resume only missing work; hard kill recovered by the same worker; exactly one result per segment; a changed runtime blocks resume; a failed model does not affect others and can be retried; smoke failure writes nothing; segment errors retried on the next attempt; pause/resume; changed source audio → `source_changed`; artifacts never inside a source |

---

## 4. Corpus indexing

```bash
uv run aerochorus worker scan home_atc_archive --prefix 2026/09/08 --mode full   # one day
uv run aerochorus worker scan home_atc_archive                                    # everything, incremental
uv run aerochorus worker scan home_atc_archive --mode verify --prefix 2026/09/08  # re-hash; proves files unchanged
uv run aerochorus scan list --source home_atc_archive
uv run aerochorus source summary home_atc_archive
```

| Mode | Lists | Reads |
| --- | --- | --- |
| `incremental` (default) | only directories whose mtime changed since a settled listing | new/changed files |
| `full` | every directory | new/changed files (stat comparison) |
| `verify` | every directory | every file (SHA-256) |

Exit code 0 = `completed`, 2 = `source_unavailable` or `failed` (message in
the JSON). A scan never deletes segments. A file absent from a completely
listed directory becomes `presence_status = missing` and returns to
`present` if it reappears.

Measured on the 5080 box over SMB: about 82 files/s with
`read_concurrency = 8`, about 170 files/s with 16. The first index of all
~448k files took about an hour. Later incremental scans relist only the
current day's directory (seconds).

Spot checks (psql via `docker compose exec postgres psql -U aerochorus -d aerochorus`):

```sql
SELECT temporal_status, count(*) FROM segment GROUP BY 1;
SELECT channel, frequency_hz, count(*) FROM segment GROUP BY 1, 2 ORDER BY 3 DESC;
SELECT relative_path, metadata->'temporal' FROM segment WHERE temporal_status <> 'resolved' LIMIT 5;
```

---

## 5. Models and the CrispASR runtime

The catalog lives in [config/models.toml](../config/models.toml): one model
per architecture family, pinned to a Hugging Face commit and SHA-256.

```bash
uv run aerochorus models sync                        # load/refresh the catalog (idempotent)
uv run aerochorus models list
uv run aerochorus suite list                         # smoke, core-diverse, qualification
uv run aerochorus worker models pull                 # download all enabled models (resumable)
uv run aerochorus worker models pull --suite smoke   # …or just a suite / named models
uv run aerochorus worker models verify
```

| Model | Family | Backend | File | Size | Load on 5080 |
| --- | --- | --- | --- | --- | --- |
| parakeet-tdt-0.6b-v3-q8_0 | parakeet | parakeet | q8_0 | 674 MB | 6–7 s |
| canary-1b-v2-q8_0 | canary | canary | q8_0 | 1.05 GB | 12 s |
| whisper-large-v3-turbo-q8_0 | whisper | whisper | q8_0 | 874 MB | 7 s |
| qwen3-asr-0.6b-q8_0 | qwen3-asr | qwen3 | q8_0 | 1.0 GB | 14 s |
| granite-speech-4.1-2b-q4_k | granite-speech | granite-4.1 | q4_k | 2.94 GB | 26 s |
| voxtral-mini-3b-2507-q4_k | voxtral | voxtral | q4_k | 2.65 GB | 20 s |

Rules:

- A synced model's file, hash, backend, family and request parameters are
  immutable. Changing any of them requires a new `logical_name`, which keeps
  results comparable.
- `pull` streams to `<file>.part`, resumes with HTTP Range, and verifies the
  SHA-256 before renaming. A bad download is deleted, never kept.
- Every model run re-verifies the file's SHA-256 before loading it.

---

## 6. Transcribing a subset

This is the core workflow: freeze a selection × suite into a sweep, then let
a worker drain it.

### 6.1 Create a sweep

```bash
# 100 deterministic-random segments from one day, Parakeet only (the smoke suite)
uv run aerochorus sweep create --suite smoke --dir 2026/09/08 --limit 100 --seed 1 \
    --name smoke-0908-100 --allow-unqualified
```

Selection options, all combinable:

| Flag | Meaning |
| --- | --- |
| `--dir 2026/09/08` | one source directory |
| `--from 2026-09-08T12:00:00Z --to 2026-09-08T13:00:00Z` | UTC window on `capture_start_utc` (needs resolved/unverified UTC) |
| `--channel GND --channel TWR` | positions |
| `--min-ms 1500 --max-ms 30000` | duration bounds |
| `--limit 100 --seed 1` | deterministic sample; the same seed gives the same segments |
| `--param temperature=0` | extra CrispASR request field for every model |
| `--allow-unqualified` | include models not yet `sweep_eligible` (smoke/qualification sweeps only) |

The sweep freezes its segment list and configuration, and prints a
`config=` hash. Identical inputs give an identical hash.

### 6.2 Run the worker

```bash
uv run aerochorus worker transcribe          # drains every queued sweep, then exits
```

In a second terminal, watch progress:

```bash
uv run aerochorus sweep show 1               # per-model progress, ok/abstain/error, RTF, CrispASR version
uv run aerochorus sweep list
docker ps --filter name=aerochorus-crispasr  # the one resident model server
```

For each model run the worker does the following:
- claims the run and verifies the model's SHA-256;
- starts CrispASR with that model only;
- checks `/health` reports the expected backend and file;
- smoke-tests the first request;
- transcribes every pending segment, committing each result and its
  `.json.zst` artifact immediately;
- confirms coverage, stops CrispASR, and moves to the next model.

Reference numbers on the 5080. RTF is inference time ÷ audio time; lower is
faster.

| Model | RTF, sweep 3 (1 h, 333 segs) | RTF, sweep 2 (100 segs) | Abstain % (sweep 2) | Notes |
| --- | --- | --- | --- | --- |
| parakeet | 0.016 | 0.07 | 20% | fastest; word timestamps; abstains on noise |
| canary | 0.077 | 0.19 | 9% | word timestamps |
| whisper-turbo | 0.110 | 0.13 | 0% | word timestamps; never abstains (fills noise with "Thank you.") |
| qwen3-asr | 0.210 | 0.22 | 1% | |
| granite 4.1 | — | 0.28 | 0% | lowercase; hallucinates prose on short/noisy clips |
| voxtral | — | 0.38 | 1% | slowest; ATC-shaped output |

Sweep 3 (one busy hour, 30.9 min of audio, four models) took about 20 minutes
end to end, including model loads. On the M1, expect several times slower,
which is acceptable: throughput is explicitly secondary.

### 6.3 Inspect results

```bash
uv run aerochorus sweep transcripts 2 --limit 5     # every model's hypothesis, per segment
uv run aerochorus segment results <segment_id>      # all results for one segment, across sweeps
uv run aerochorus sweep report 2                    # per-model metrics (see §8)
uv run aerochorus worker artifact artifact://jesseepc/raw/ab/<uuid>.json.zst   # the raw CrispASR response
```

Result states: `success` (non-empty text), `abstained` (the model returned no
words; not an error and not disagreement), `error` (with `error_type`:
`http_503`, `timeout`, `server_unavailable`, `source_changed`,
`source_read_error`, …).

### 6.4 Interruption drill (safe to run any time)

```bash
uv run aerochorus worker transcribe     # then Ctrl-C (or kill the process outright) mid-run
uv run aerochorus sweep show <id>       # completed results are kept; that model run is queued/running
uv run aerochorus worker transcribe     # resumes: only segments without a result are processed
```

This was verified with a hard `Stop-Process -Force` after 44 results. The
restart reclaimed the run, removed the orphaned container, processed the
remaining 56, and wrote no duplicates (a unique constraint enforces
this).

### 6.5 Controls

```bash
uv run aerochorus sweep pause <id>      # the worker finishes its current segment and releases the run
uv run aerochorus sweep resume <id>
uv run aerochorus sweep cancel <id>
uv run aerochorus sweep retry <id>                     # re-queue failed model runs
uv run aerochorus sweep retry <id> --model canary-1b-v2-q8_0   # re-attempt that model's error rows
```

A sweep ends `completed`, or `partial` if a model run failed. Other models'
results are never affected by one model's failure.

---

## 7. Evaluating against ATCO2

ATCO2-ASRdataset-v1_beta is the regression benchmark: 560 recordings, 877
human-transcribed segments, 59.5 minutes of mostly non-native European ATC.
It lives only on the 5080 box
(`C:\Users\sagej\Projects\atc-asr-corpus-studio\Data\ATCO2-ASRdataset-v1_beta\DATA`),
so this machine is the benchmark worker (see ADR-015).

### 7.1 One-time setup (done on this box)

```bash
# 1. Clip every gold segment at its exact XML boundaries (no VAD, no padding).
#    Writes audio + a manifest, NEVER gold text. Idempotent.
uv run aerochorus eval atco2 prepare \
    --data "C:/Users/sagej/Projects/atc-asr-corpus-studio/Data/ATCO2-ASRdataset-v1_beta/DATA" \
    --out  "C:/Users/sagej/aerochorus-data/corpora/atco2_fixed"
#    -> 560 recordings, 877 clips, 26 end-clamp warnings (identical to v1)

# 2. Register the clips as an ordinary read-only source, and map it in worker.toml:
uv run aerochorus source add atco2_fixed --name "ATCO2 v1_beta, fixed gold boundaries (benchmark)" \
    --parser atco2_clip --timezone UTC --ext .wav --min-file-age 0 --no-mtime-corroboration
#    worker.toml:  [sources.atco2_fixed]  root = 'C:\Users\sagej\aerochorus-data\corpora\atco2_fixed'
uv run aerochorus worker scan atco2_fixed --mode full          # 877 segments, UTC from ATCO2 names

# 3. Load gold into the isolated `reference` schema (immutable; re-import is a no-op).
uv run aerochorus eval atco2 import-references \
    --data "C:/Users/sagej/Projects/atc-asr-corpus-studio/Data/ATCO2-ASRdataset-v1_beta/DATA" \
    --source atco2_fixed
#    -> 877 references; split calibration 456 / test 421 (derived by recording)
```

### 7.2 Evaluate models

```bash
uv run aerochorus sweep create --suite qualification --source atco2_fixed \
    --name atco2-877-qualification --allow-unqualified
uv run aerochorus worker transcribe            # ~40 min for all six models on the 5080
uv run aerochorus eval report <sweep_id>       # the scoreboard
uv run aerochorus eval report <sweep_id> --split test           # held-out half only
uv run aerochorus eval report <sweep_id> --canonical-numbers    # ignore number *format*
uv run aerochorus eval segments <sweep_id> --model canary-1b-v2-q8_0 --limit 5   # worst segments
```

What the report means:

| Column | Meaning |
| --- | --- |
| TER, S/D/I | micro token error rate, from one symmetric scorer (`normalize_for_scoring` on gold **and** hypotheses) |
| CER | the same, at character level |
| exact | share of segments transcribed perfectly |
| callsign / value / command | recall of the human-tagged gold entities (`[#callsign]…`), i.e. did the model get the callsign/level/instruction right |
| abstain / error | empty or failed outputs; both score as full deletions, so silence is never free |
| top flags | deterministic quality flags (loops, off-domain phrases, non-Latin script…) |
| best single / oracle | the bars any Phase 6 ensemble must beat; the oracle picks the best model per segment |
| family agreement vs error | how inter-model agreement predicts correctness (the evidence Phase 6 builds on) |

Results on this box: sweep 4 (all six families) and sweep 5 (Canary with beam search),
877 segments / 10,834 reference tokens, CrispASR 0.8.37 CUDA container,
2026-09-27:

| Model | TER | test-split TER | CER | callsign | value | command | loops | RTF |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **parakeet-tdt-0.6b-v3-q8_0** | **0.565** | **0.561** | 0.348 | 0.361 | 0.632 | 0.454 | 0 | 0.013 |
| qwen3-asr-0.6b-q8_0 | 0.597 | 0.589 | 0.349 | **0.374** | **0.642** | 0.421 | 0 | 0.058 |
| whisper-large-v3-turbo-q8_0 | 0.636 | 0.635 | 0.419 | 0.222 | 0.329 | **0.538** | 3 | 0.026 |
| canary-1b-v2-q8_0-beam4 | 0.715 | — | 0.476 | 0.157 | 0.257 | 0.385 | — | 0.063 |
| voxtral-mini-3b-2507-q4_k | 0.766 | 0.771 | 0.497 | 0.207 | 0.314 | 0.527 | 7 | 0.093 |
| canary-1b-v2-q8_0 (greedy) | 0.804 | 0.855 | 0.513 | 0.165 | 0.277 | 0.419 | 12 | 0.037 |
| granite-speech-4.1-2b-q4_k | 1.046 | 0.934 | 0.667 | 0.116 | 0.259 | 0.294 | 0 | 0.106 |

- **Best single model:** Parakeet v3, TER 0.565. **Per-segment oracle:**
  0.425. There is real headroom for Phase 6.
- **Agreement predicts correctness.** Oracle TER is 0.64 where the families
  agree least and 0.17 where they agree most. The evidence layer can
  exploit this.
- **Canary-1b-v2 falls into decoder loops** under greedy decoding
  ("so I'm going to go to the first one, …" ×20). Beam search
  (`beam_size=4`) cuts its insertions from 1,675 to 703. It is still behind
  v1's `canary-1b-flash` (0.586), so that checkpoint is worth cataloguing.
- **Granite 4.1 hallucinates prose** (3,373 insertions; `low_atc_content` on
  455 results vs 25 in the human gold). Do not qualify it without a fix.
- These are ATCO2 numbers: mostly non-native European English. They are
  not a proxy for BWI traffic accuracy, which has no gold yet.

### 7.3 v1 numbers are not comparable, but can be made so

v1 scored hypotheses with a weaker normalizer than gold (DEFECT-2). Its
stored hypotheses, re-scored with the v2 scorer on the same 877 segments:

| v1 provider (original audio) | v1 published | v2 symmetric |
| --- | --- | --- |
| qwen (Qwen3-ASR-1.7B) | 0.6360 | **0.5012** |
| parakeet_tdt_0_6b_v3 | 0.6236 | 0.5354 |
| parakeet_tdt_0_6b_v2 | 0.6371 | 0.5498 |
| canary (canary-1b-**flash**) | **0.5811** | 0.5856 |
| whisper_vanilla (large-v3) | 0.7516 | 0.6737 |
| parakeet (1.1b NIM) | 0.7453 | 0.7005 |
| canary_qwen_2_5b | 0.7121 | 0.7154 |
| granite_speech (4.1-2b-plus) | 0.9285 | 0.9406 |

v1's "Canary is the best fair provider" conclusion came from the
asymmetric scorer. Punctuating models were penalized for commas.

### 7.4 Leakage rules (enforced by tests)

- Prepared clips and their manifest contain no gold text.
- Gold is only in `reference.gold_segment` and only leaves via `/api/v1/evaluation/*`.
- The worker/CrispASR/scanner path cannot import gold-handling code
  (`test_transcription_path_cannot_see_gold`).
- No worker payload (claim, pending, sweep views) contains gold phrases
  (`test_gold_never_reaches_the_transcription_path`).
- Only workers that mount `atco2_fixed` are ever offered ATCO2 sweeps.

---

## 8. Qualifying models into `full-lab-v1`

Full sweeps refuse models that are not `sweep_eligible`. Qualification is an
explicit, recorded decision, not an assumption.

```bash
uv run aerochorus sweep create --suite qualification --dir 2026/09/08 --limit 100 --seed 1 \
    --name qualify-0908-100 --allow-unqualified
uv run aerochorus worker transcribe
uv run aerochorus sweep report <id>
```

The report shows, per model:
- results, abstain %, error %;
- RTF, and mean ms per segment;
- word-timestamp rate, and token confidence (when a backend provides it);
- language drift: successful results whose reported language ≠ the
  requested `en`;
- mean characters per segment.

Suggested gates (to be replaced by ATCO2-fitted criteria in Phase 4):

1. The model loads and every request answers: `error% = 0`.
2. Language drift is 0 (English-only traffic).
3. Its throughput is acceptable for the window sizes you plan on the M1.
4. A spot read of `sweep transcripts` shows ATC-shaped output, not
   hallucinated prose.

Then promote the models and define the roster:

```bash
uv run aerochorus models set parakeet-tdt-0.6b-v3-q8_0 --eligible
# …repeat per qualified model…
uv run aerochorus suite set full-lab-v1 parakeet-tdt-0.6b-v3-q8_0 canary-1b-v2-q8_0 \
    whisper-large-v3-turbo-q8_0 qwen3-asr-0.6b-q8_0 --description "qualified 2026-09-27"
uv run aerochorus sweep create --suite full-lab-v1 --from 2026-09-08T04:00:00Z --to 2026-09-09T04:00:00Z \
    --name "2026-09-08 local day"      # no --allow-unqualified needed
```

---

## 9. Unattended operation

```bash
uv run aerochorus worker run
```

This runs heartbeats, incremental scans every 15 minutes for sources with
`auto_scan = true`, a daily full scan, and queued sweeps (when
`[transcription]` is present and `process_sweeps = true`). A sweep is
processed one model run at a time, with scans re-checked between model runs.
On macOS the worker holds a `caffeinate -i` assertion only while a scan or
model run is active, so the Mac sleeps normally when idle. Stop it with
Ctrl-C: the active model run is released and resumes later.

---

## 10. Moving the control plane to the M1

> **Superseded (2026-09-27, ADR-021, ADR-023).** The Mac is an Intel client
> with 8 GB of RAM, not a server. The primary host is the Linux desktop with
> the GTX 1070, which runs **containers only**, built on this PC and shipped
> as a bundle. See
> [deployment/LINUX_DEPLOYMENT_RUNBOOK.md](deployment/LINUX_DEPLOYMENT_RUNBOOK.md)
> and [deployment/MIGRATION.md](deployment/MIGRATION.md). §14 is the
> pre-migration check on this PC. The rest of this section is kept for
> history.

The target layout: the **M1 runs the control plane and the archive worker**,
and **this PC stays a GPU/benchmark worker** pointed at the M1. Nothing in
the code assumes which machine is which. Workers are identified by name, and
each claims only sweeps over sources it mounts.

```
M1 MacBook Air                                   RTX 5080 box (this PC)
  docker compose: postgres + api (LAN-bound) ◀── worker "jesseepc-5080"
  worker "m1-air": native CrispASR (Metal)          sources: atco2_fixed (+ archive, optional)
    sources: home_atc_archive (/Volumes/ATC, ro)    CrispASR: CUDA container
```

### 10.1 Stand up the M1

1. Install Docker Desktop (Apple silicon), `uv`, `git` and `gh`. The Compose
   images (`postgres:17`, `python:3.12-slim`, `ghcr.io/astral-sh/uv`) are
   multi-arch, so `docker compose up -d --build` builds natively on arm64.
2. Mount the archive read-only and install the native CrispASR build (§1.4).
3. Move the database from this PC. The dump carries the corpus index,
   registry, sweeps, results and gold.

   ```bash
   # on this PC
   docker compose exec -T postgres pg_dump -U aerochorus -Fc aerochorus > aerochorus.dump
   # copy aerochorus.dump to the M1, then on the M1 (in the repo):
   docker compose up -d postgres
   docker compose exec -T postgres pg_restore -U aerochorus -d aerochorus --clean --if-exists < aerochorus.dump
   AEROCHORUS_API_BIND=<m1-lan-ip> docker compose up -d --build     # migrate is a no-op; api on the LAN
   ```

   Or start fresh on the M1 and re-index: about an hour over SMB, but you
   lose earlier sweeps and gold.
4. On the M1, write `~/.config/aerochorus/worker.toml` from
   `config/worker.example.toml` (`launcher = "native"`,
   `root = "/Volumes/ATC"`, `artifact_store = "m1-air"`). Then:

   ```bash
   uv run aerochorus worker models pull        # 9.2 GB, or copy the models dir; both are SHA-verified
   uv run aerochorus worker health
   uv run aerochorus worker crispasr check --model parakeet-tdt-0.6b-v3-q8_0 --audio /Volumes/ATC/2026/09/08/<file>.mp3
   ```

### 10.2 Point this PC at the M1

In `%APPDATA%\aerochorus\worker.toml` on this PC, set
`api_url = "http://<m1-lan-ip>:8000"`. Keep `[sources.atco2_fixed]`. Keep
`[sources.home_atc_archive]` only if this PC should also take archive work.
Model runs never mix machines, because each model run keeps the runtime
fingerprint it started with. Then run `uv run aerochorus worker run` (or
`worker transcribe`) here as before.

### 10.3 Raw artifacts

Raw CrispASR outputs stay on the machine that produced them:
`artifact://jesseepc/…` here, `artifact://m1-air/…` on the Mac. To read the
PC's artifacts on the Mac, copy (or share) the directory and map it:

```toml
[transcription.artifact_mounts]
jesseepc = "/Users/me/aerochorus-data/jesseepc-artifacts"
```

### 10.4 Checklist before switching

- [ ] `aerochorus worker health` exits 0 on both machines.
- [ ] `aerochorus sweep list` on the M1 shows the migrated sweeps.
- [ ] A 100-segment smoke sweep on the M1 completes with the native runtime.
- [ ] An ATCO2 sweep created on the M1 is claimed by this PC and not by the M1.
- [ ] The Mac sleeps normally when idle (the worker holds `caffeinate` only while working).

---

## 11. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `/health` → `degraded`, `migrations_current: false` | Code and database disagree. `docker compose up -d --build` (runs `migrate`). |
| `worker health` → source `available: false` | Share unmounted or unreachable. Remount; scans report `source_unavailable` and change nothing meanwhile. |
| `sweep create` → 409 "not sweep-eligible" | Qualify the models (§8) or add `--allow-unqualified` for a smoke/qualification sweep. |
| model run `failed`: "does not match the registered size/sha256" | Corrupt or partial model file. `worker models pull <name>` re-downloads and verifies it. |
| model run `failed`: "runtime differs from the one this model run started with" | CrispASR build or image changed mid-run. Finish on the original runtime, or create a new sweep. |
| model run `failed`: "smoke segment …" | The first request failed (model/backend mismatch, GPU problem). Check `C:\Users\sagej\aerochorus-data\logs\sweep<id>-<model>.log`, fix, then `sweep retry <id>`. |
| "CrispASR not ready after 900s" | Model too large for the device, or a driver issue. Run `docker run --rm --gpus all --entrypoint crispasr <image> --diagnostics`. |
| Leftover container after a crash | Harmless: the next start removes it. Or remove it by hand with `docker rm -f aerochorus-crispasr-8090`. |
| Windows: `uv run …` fails with "aerochorus.exe is being used by another process" | A long-running `aerochorus` (scan/worker) holds the launcher. Wait, or use `UV_NO_SYNC=1 uv run …`. |
| Nothing claimable, but the sweep isn't finished | The sweep is `paused`, or another worker holds a live lease (default 10 min). `sweep show` shows `claimed_by`. |

---

## 12. Reference

API docs: `http://127.0.0.1:8000/docs`.

```sql
-- Where is a sweep?
SELECT m.logical_name, r.status, r.segments_completed, r.segments_abstained, r.segments_error,
       round(r.inference_ms_total::numeric / nullif(r.audio_ms_total, 0), 3) AS rtf
FROM sweep_run_model r JOIN model m ON m.id = r.model_id WHERE r.run_id = 2 ORDER BY r.execution_order;

-- All hypotheses for one segment
SELECT m.logical_name, t.status, t.text
FROM transcription_result t
JOIN sweep_run_model r ON r.id = t.sweep_run_model_id JOIN model m ON m.id = r.model_id
WHERE t.segment_id = 123 ORDER BY r.execution_order;

-- Segments where the architecture families disagree on emptiness
SELECT t.segment_id, count(*) FILTER (WHERE t.status = 'abstained') AS abstained,
       count(*) FILTER (WHERE t.status = 'success') AS spoke
FROM transcription_result t JOIN sweep_run_model r ON r.id = t.sweep_run_model_id
WHERE r.run_id = 2 GROUP BY 1
HAVING count(*) FILTER (WHERE t.status = 'abstained') > 0
   AND count(*) FILTER (WHERE t.status = 'success') > 0;
```

---

## 13. Review workbench (Phase 5)

Details: [docs/ui/REVIEW_WORKBENCH.md](ui/REVIEW_WORKBENCH.md).

```bash
docker compose up -d --build                        # migrations 0004–0010 (agreement v2, map, adjudication)
uv run aerochorus source set-role atco2_fixed benchmark
uv run aerochorus airport bootstrap KBWI --timezone America/New_York --station BWI
uv run aerochorus agreement refresh                 # after upgrades, or results posted by an older API
cd ui && npm ci && npm run build && cd ..           # Node 20.19+ (24 LTS used here)
uv run aerochorus ui serve                          # → http://127.0.0.1:8080/review
```

- `ui serve` runs natively where the corpus is mounted (it reuses the worker
  config's `[sources]`). On the M1 that is the Mac itself.
- `ui serve --api http://<control-plane>:8000` points it at a remote control
  plane.
- Audio problems appear in the inspector with the edge's reason:
  - 503: not mounted or unavailable;
  - 404: file missing;
  - 409: changed since indexing, so run `worker scan --mode verify`.
- `curl http://127.0.0.1:8080/edge/health` shows source availability and
  whether the API is reachable.
- Agreement is refreshed automatically when results are recorded. Run
  `agreement refresh` after a threshold change
  (`AEROCHORUS_NEAR_MATCH_THRESHOLD`), an algorithm version bump, or results
  recorded by a pre-0004 API.

### Training datasets (Phase 5B)

Details: [docs/ui/TRAINING_DATASET_LIFECYCLE.md](ui/TRAINING_DATASET_LIFECYCLE.md).

```bash
uv run aerochorus dataset create bwi-atc --labels gold,silver     # freeze a version (grouped, seeded split)
uv run aerochorus dataset list
uv run aerochorus dataset export <id> --out ~/aerochorus-data/datasets   # needs the corpus + ffmpeg
```

### Custom fine-tuned models (ADR-019)

```bash
uv run aerochorus models sync                                     # models.toml entry with pedigree.lineage + artifact_url
uv run aerochorus models qualify <name> smoke --passed --evidence '{"sweep": 12}'
uv run aerochorus models set <name> --eligible                    # refused until all 7 gates pass
```

### ADS-B context (Phase 5C, optional)

Details: [docs/context/OPENSKY_PROVIDER.md](context/OPENSKY_PROVIDER.md).

```bash
# ./.env (gitignored) — read by docker compose for the api service
AEROCHORUS_OPENSKY_USERNAME=<opensky user>
AEROCHORUS_OPENSKY_PASSWORD=<opensky password>
# optional: AEROCHORUS_ADSB_WINDOW_BEFORE_S=60 AEROCHORUS_ADSB_WINDOW_AFTER_S=60 AEROCHORUS_ADSB_RADIUS_NM=10
```

Then run `docker compose up -d`. Without these, the inspector's ADS-B panel
says "not configured" and nothing else changes. The container needs outbound
HTTPS to `auth.opensky-network.org` and `trino.opensky-network.org`.

Trino works only once OpenSky has granted historical access to the account.
Until then the panel shows "Access Denied". For the REST fallback, create an
API client on the OpenSky account page and add
`AEROCHORUS_OPENSKY_CLIENT_ID` / `AEROCHORUS_OPENSKY_CLIENT_SECRET`.

Map geometry, once per airport (runway ends come with the bootstrap):

```bash
uv run aerochorus airport airspace KBWI --radius-nm 30 --classes B,C,D
```

### Model adjudication (optional, paid)

Details: [ADR-022](adr/0022-model-adjudication.md) and
[REVIEW_WORKBENCH.md](ui/REVIEW_WORKBENCH.md#model-adjudication-gemini-via-openrouter).

Batches are created only in the review UI (or API), each priced, capped and
confirmed. The runner holds the key and runs where the audio is mounted:

```bash
# on the audio host, in its environment or ./.env (never the API container):
AEROCHORUS_OPENROUTER_API_KEY=<openrouter key>

uv run aerochorus adjudicate run --follow          # waits for confirmed batches
uv run aerochorus adjudicate list                  # batches, progress, spend vs cap
uv run aerochorus adjudicate show <batch>          # items and transcripts
```

On the Linux host, put the key in `/etc/aerochorus/aerochorus.env`, then
run `sudo systemctl enable --now aerochorus-adjudicator.service`. The
control plane's limits:

| setting | default |
| --- | --- |
| `AEROCHORUS_ADJUDICATION_MODEL` | `~google/gemini-pro-latest` |
| `AEROCHORUS_ADJUDICATION_MAX_ITEMS` | 500 per batch |
| `AEROCHORUS_ADJUDICATION_MAX_BATCH_USD` | $25 per batch |
| `AEROCHORUS_ADJUDICATION_MAX_AUDIO_S` | 120 s per clip |

Prices come from OpenRouter's public model list. If it is unreachable, the
`AEROCHORUS_ADJUDICATION_PRICE_*` defaults are used.

## 14. A full day on this PC (Windows, RTX 5080): the pre-migration check

This runs everything end to end on the dev PC before the Linux move:
transcribe one day, review it, look at ADS-B, and optionally adjudicate a
few segments. Nothing here touches the Linux host.

### 14.1 Start (each session)

In PowerShell, from `C:\Users\sagej\Projects\atctranscribe`:

```powershell
docker compose up -d --build          # Postgres + API; picks up .env (OpenSky keys) and new code
uv run aerochorus ui serve            # review UI → http://127.0.0.1:8080/review (leave this window open)
```

If `ui serve` is still running from an earlier session, stop it (Ctrl-C)
and start it again after pulling new code. `docker compose up -d --build`
also applies database migrations. After editing `.env`, recreate the API
with `docker compose up -d --force-recreate api`.

### 14.2 Make sure the day is indexed

```powershell
uv run aerochorus worker scan home_atc_archive --prefix 2026/09/26
```

This is an incremental scan of that day's directory, so it is cheap if
nothing changed. The Review page's day filter then shows the segments.

### 14.3 Queue the run

On the **Transcribe** page (`http://127.0.0.1:8080/transcribe`):

1. Portion: **One day**, then Day (archive folder) `2026-09-26`. Keep all
   channels, and tick **only segments these models have not transcribed**.
2. Models: **Voting models**, then untick `canary-1b-v2-q8_0`. It is the
   same family as `canary-1b-v2-q8_0-beam4`, so it adds time but no
   independent agreement. That leaves 6 models from 6 families.
3. Tick **Allow models not yet qualified for full sweeps**. Qualification
   is per hardware profile, so every model is "unqualified" until §8 or
   the Linux qualification is run.
4. The preview updates as you choose: segments, audio hours and estimated
   minutes per model. Then click **Queue transcription run**.

Estimated GPU time for 2026-09-26 (4,277 segments, 7.7 h of audio), from
observed speed on this PC. Wall time with model loading and I/O has been
about 1.7× these:

| models | GPU estimate | expect |
| --- | --- | --- |
| parakeet, whisper-turbo, canary-beam4, qwen3 (`review-core`, as for 09-08) | 88 min | ~2.5 h |
| + granite-speech, voxtral-mini (6 families) | 213 min | ~6 h |

A quicker start is the four `review-core` models first. Later, queue a
second run with just granite and voxtral: "only untranscribed" then covers
exactly what those two are missing.

The CLI equivalent:

```powershell
uv run aerochorus sweep create --suite review-core --source home_atc_archive --dir 2026/09/26 --name day-0926 --allow-unqualified
```

### 14.4 Run the worker

```powershell
uv run aerochorus worker transcribe     # works through queued runs, then exits
```

It runs one model at a time on the 5080, through the pinned CrispASR CUDA
image. Progress and ETA show on the Transcribe page. While a model run is
active, the worker keeps Windows from sleeping; the display may still turn
off. Ctrl-C releases the current model run cleanly, and running the same
command again resumes only the missing segments. `uv run aerochorus worker
run` does the same continuously, with heartbeats and scans.

### 14.5 Review

Agreement is computed as results arrive. Useful starting points on the
**Review** page, filtered to the day:

- **2+ exact families, 3+ words**: independent agreement, without the
  "thank you" noise. Listen, then `A` and `S`.
- **Partial agreement (utterances)**: agreed stretches inside otherwise
  disagreeing segments. `U` starts a span prefilled from the next one.
- **Disagreement / needs review**: candidates for adjudication.

### 14.6 ADS-B context and the map

Open a segment, then **ADS-B context & map**. The map (runways, Class B/D
airspace) shows without any fetch. **Fetch ADS-B context** adds traffic.

Until OpenSky grants Trino access, fetches use the REST API client from
`.env`:

- **Coverage**: KBWI arrivals and departures only, with positions
  interpolated from their tracks. Flights are published after a nightly
  batch, so use days before today.
- **Cost**: each REST call costs 30 of the account's daily credits.
  AeroChorus caches each day's flight lists and each flight's track in the
  API process, so browsing segments on the same day gets cheap after the
  first fetch. The provenance line shows the credits left.
- **Limit**: on 2026-09-27 the daily allowance ran out after about 18
  calls. When it does, the panel says when the credits come back. Cached
  snapshots keep working.

Snapshots are cached per segment, so reopening a segment costs nothing.

### 14.7 Optional: adjudicate a few segments (paid)

Put `AEROCHORUS_OPENROUTER_API_KEY=…` in `.env`, then, in a second window:

```powershell
uv run aerochorus adjudicate run --follow
```

On the Review page, select a handful of disagreeing segments, then
**Adjudicate…**. Check the price, tick the confirmation, and send. Results
appear in the inspector ("Use as correction" / "Accept as silver") and on
the **Adjudication** page. Start with 5–10 segments to see quality and
actual cost.

### 14.8 The whole archive, unattended for days

Scale, measured on this PC (wall time per segment, including reading from the
share and recording):

| model | per segment |
| --- | --- |
| parakeet | 0.19 s |
| whisper-turbo | 0.28 s |
| qwen3 | 0.44 s |
| canary-beam4 | 0.50 s |
| granite | 0.75 s |
| voxtral | 0.83 s |

The archive has 447,709 segments (559 h of audio), and a third of them
(147,517) are under 1 second: squelch clicks with 16 h of audio in total.
**Set Min duration (s) to 2.** That keeps 270k segments with 531 h of audio,
which is about 0.1–1.1 s per segment per model at their average length:

| model | ≥ 2 s segments | days, cumulative |
| --- | --- | --- |
| parakeet | ~17 h | 0.7 |
| whisper | ~27 h | 1.8 (2-family agreement everywhere) |
| qwen3 | ~44 h | 3.6 |
| canary-beam4 | ~44 h | 5.4 |
| granite | ~77 h | 8.6 |
| voxtral | ~82 h | 12 |

The Transcribe page queues the selected models **fastest first**, so
agreement coverage arrives early. Queue all six in one run; after about 5
days the first four are done and the rest keep going. The sub-second
segments can be queued later as a separate run with just parakeet and
whisper.

Before leaving it alone:

1. On the Transcribe page, choose **Everything**, min duration `2`, the 6
   voting-family models (not `canary-1b-v2-q8_0`), **only segments these
   models have not transcribed**, and **Allow models not yet qualified**.
   Then click **Queue transcription run**. Creating it takes a few seconds.
2. Start the worker in a loop that restarts it if it ever exits and logs to
   `%USERPROFILE%\aerochorus-data\logs\worker-<date>.log`:

   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\windows\run-worker.ps1
   ```

   Leave that window open. Do not also run `worker transcribe`.
3. **Windows Update:** Settings → Windows Update → *Pause updates* for 1
   week, so the PC does not restart itself.
4. **Docker Desktop** must keep running (it hosts the database, the API and
   the CrispASR container). Don't quit it or let it update.
5. Sleep is handled: the worker keeps Windows awake while a model run is
   active. Don't sign out, and leave the PC on.
6. Check it now and then from the Transcribe page: progress, ETA, and the
   worker's heartbeat. Then check the log file.

**Two models at once.** With `max_concurrent_models = 2` in the worker config
(`[transcription]`), `worker run` starts a second model next to the first. It does
so only when the second one fits in **free** GPU memory minus `vram_headroom_mb`
(1,500 MB by default), and only while the first is fully loaded. Each model gets
its own CrispASR server on `host_port` + 1.

At startup the worker logs `processing queued sweeps with docker, up to 2 model(s)
at once`. If it says 1, the setting is not in the config file the worker read.
Edit `%APPDATA%\aerochorus\worker.toml` in your own editor: edits made from inside
the Claude desktop app land in a private copy the worker never sees.

The footprint comes from qualification measurements when they exist, otherwise
from the model file size plus about 0.7 GB. Anything else on the GPU counts,
including a game, so nothing extra starts while one is running. A fast model
spends much of each segment reading audio and recording results, which leaves
the GPU idle, so a second model fills that gap. Two large models share the GPU
and each one runs slower. The Transcribe page shows both as running. Restart the
worker after changing the setting.

What recovers by itself:

- **Share outage**: the work is handed back and resumes when the share is
  readable.
- **API restart**: the worker waits and continues.
- **A clip that crashes a model**: that segment is recorded as an error and
  CrispASR restarts.
- **A model run that fails outright**: the next model continues. Retry the
  failed one later from the run's ↻ button.

What does not recover by itself: a Windows restart. After one, start Docker
Desktop and the script again, and the run continues where it stopped.

### 14.9 When it looks right: migrate

1. Stop the worker and adjudicator (Ctrl-C), and let no run or batch be
   active.
2. Export the database ([deployment/MIGRATION.md](deployment/MIGRATION.md)
   §1).
3. Build the deploy bundle
   (`powershell -ExecutionPolicy Bypass -File deploy\linux\package.ps1`).
4. Follow
   [deployment/LINUX_DEPLOYMENT_RUNBOOK.md](deployment/LINUX_DEPLOYMENT_RUNBOOK.md):
   copy the bundle, then `sudo bash host-setup.sh`, `bash deploy.sh up`,
   and `import.sh`.
