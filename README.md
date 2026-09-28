# AeroChorus

AeroChorus is a local-first lab for indexing a growing archive of segmented ATC
radio audio. It runs that audio through a suite of open-source ASR models and
keeps every hypothesis with full provenance.

The founding plan is in [docs/plan.md](docs/plan.md), and the decisions in
force are in [docs/adr/](docs/adr/README.md). **Status:** Phases 0–5 are done:

- the corpus index;
- the model registry;
- resumable multi-model sweeps through CrispASR;
- ATCO2 regression evaluation;
- the review workbench (web UI);
- partial-span annotation;
- versioned training datasets;
- utterance-level agreement within a segment (partial agreement);
- on-demand ADS-B context on a simplified airport map (runways, FAA airspace);
- a Transcribe page to queue runs over a chosen slice with chosen models;
- paid model adjudication (Gemini via OpenRouter) of selected segments,
  priced, capped and confirmed (ADR-022).

**Deployment:** a Linux host with a GTX 1070, containers only (ADR-021,
ADR-023). See [docs/deployment/](docs/deployment/LINUX_DEPLOYMENT_RUNBOOK.md).
**Operating it:** [docs/runbook.md](docs/runbook.md).

```
ALIENWARE COLLECTOR ──SMB, read-only──▶ LINUX HOST (GTX 1070 8 GB, 32 GB RAM) ◀──HTTP── LAN browsers
 SDR capture, segmentation             PostgreSQL + API (Docker)                        (Intel Mac, PCs)
 immutable source audio                review edge: web UI + audio (container)
                                       worker + pinned CUDA 12 CrispASR (container, GPU)
                                       models, artifacts, exports, backups (/srv/aerochorus)
```

- **Control plane** (Docker Compose): PostgreSQL 17 and the FastAPI service.
  It is the only writer to the database.
- **Worker** (a container on the Linux host with the GPU passed through;
  native Python on the Windows dev box): scans the mounted archive read-only
  and reports observations to the API. It also runs CrispASR with one model
  at a time: the pinned CUDA 12 binary on the Linux host, or the CUDA image
  on the Windows 5080 box. Results go to the API; raw responses go to a local
  `.json.zst` artifact store.
- **Review edge** (a container on the Linux host): serves the web UI on the
  LAN, proxies the API, and streams source audio read-only.

## Layout

```
src/aerochorus/
  contracts.py      wire models + status enums shared by API and worker (corpus)
  sweep_contracts.py  wire models for the model registry, sweeps and results
  corpus/           pure corpus semantics: paths, filename parsing, UTC resolution
  atc/              ATC domain module: lexicon, the two normalizers, alignment,
                    entities, background quality flags
  datasets/         benchmark adapters (ATCO2), the only gold readers
  eval_contracts.py / eval_client.py  evaluation wire models + client (gold side)
  db/               SQLAlchemy models, Alembic helpers (control plane only)
  migrations/       Alembic migrations (shipped in the package)
  api/              FastAPI app, routes, indexing service
  worker/           read-only filesystem reader, audio probe, scanner, daemon,
                    CrispASR launchers, model store, artifact store, sweep worker
  cli.py            `aerochorus` command (+ cli_transcription.py)
config/models.toml  pinned model catalog (one model per architecture family)
docs/               plan, ADRs, corpus fact sheets
tests/unit          no database needed
tests/integration   PostgreSQL-backed, end-to-end through the HTTP API
```

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
docker compose up -d --build          # postgres, migrations, API on 127.0.0.1:8000
curl http://127.0.0.1:8000/health
```

Register the corpus source once. Its identity and collector semantics live in
PostgreSQL:

```bash
uv run aerochorus source add home_atc_archive --name "Home ATC archive (BWI)" \
    --parser rtlsdr_airband --timezone America/New_York --sentinel 2026
```

Map the source to a path on this machine. Copy
[config/worker.example.toml](config/worker.example.toml) to
`~/.config/aerochorus/worker.toml` or `/etc/aerochorus/worker.toml` (Linux) or
`%APPDATA%\aerochorus\worker.toml` (Windows), or point
`AEROCHORUS_WORKER_CONFIG` at it. Then:

```bash
uv run aerochorus worker health                                   # exit 0 = healthy
uv run aerochorus worker scan home_atc_archive --prefix 2026/09/08 # one day
uv run aerochorus worker scan home_atc_archive                     # everything, incremental
uv run aerochorus source summary home_atc_archive
uv run aerochorus worker run                                       # heartbeats + scheduled scans
```

Transcribe a subset (full walkthrough in the runbook):

```bash
uv run aerochorus models sync && uv run aerochorus worker models pull
uv run aerochorus sweep create --suite smoke --dir 2026/09/08 --limit 100 --allow-unqualified
uv run aerochorus worker transcribe
uv run aerochorus sweep show 1 && uv run aerochorus sweep transcripts 1 --limit 5
```

### On the Linux host

Containers only: no repository, Python or Node on the host. On the Windows
PC, `deploy\linux\package.ps1` builds the images and writes a deploy bundle.
Copy it to the host, then run `sudo bash host-setup.sh` once, followed by
`bash deploy.sh up`:

- `host-setup.sh` checks the driver, installs Docker and the NVIDIA
  Container Toolkit, and mounts the share read-only;
- `deploy.sh up` loads the images, writes the config, starts everything, and
  smoke-tests the API, UI, share, GPU and CrispASR.

The full walkthrough is
[docs/deployment/LINUX_DEPLOYMENT_RUNBOOK.md](docs/deployment/LINUX_DEPLOYMENT_RUNBOOK.md).

## Tests

```bash
uv run pytest                     # unit tests; DB tests are skipped
AEROCHORUS_TEST_DATABASE_URL=postgresql+psycopg://aerochorus:aerochorus@127.0.0.1:5432/aerochorus_test \
    uv run pytest                 # full suite against the Compose Postgres
uv run ruff check . && uv run ruff format --check .
```

The integration suite rebuilds the `aerochorus_test` schema from migrations. It
checks that the ORM models and migrations match, then scans synthetic
collector-style corpora through the real HTTP API. Architecture tests enforce
the read-only source rule, the worker/database boundary, the single
architecture-family registry, and the absence of JSONL storage.

## API

Interactive docs: `http://127.0.0.1:8000/docs`. Main routes:

| route | purpose |
| --- | --- |
| `GET /health` | database reachability + migration state |
| `POST/GET /api/v1/sources[/{key}]` | register and inspect corpus sources |
| `GET /api/v1/sources/{key}/summary` | counts by presence, temporal status, integrity, UTC day |
| `GET /api/v1/sources/{key}/segments?dir=` | browse segments |
| `POST /api/v1/scans`, `…/batches`, `…/finish` | worker scan protocol |
| `POST /api/v1/workers/heartbeat` | worker health reports |
| `POST /api/v1/models/sync`, `GET /api/v1/models`, `PUT /api/v1/suites/{name}` | model registry and suites |
| `POST /api/v1/sweeps`, `GET /api/v1/sweeps/{id}[/report\|/transcripts]` | create, monitor and inspect sweeps |
| `POST /api/v1/evaluation/references`, `GET /api/v1/evaluation/sweeps/{id}[/segments]` | gold import and scoring (the only routes that return gold) |
| `POST /api/v1/sweeps/claim`, `/api/v1/sweep-models/{id}/start\|pending\|results\|finish\|release` | worker sweep protocol |
