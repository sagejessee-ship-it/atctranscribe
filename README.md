# AeroChorus

AeroChorus is a local-first lab for indexing a growing archive of segmented ATC
radio audio. It runs that audio through a suite of open-source ASR models and
keeps every hypothesis with full provenance.

The founding plan is in [docs/plan.md](docs/plan.md), and the decisions in
force are in [docs/adr/](docs/adr/README.md). **Current scope: Phase 0
(foundation) + Phase 1 (corpus index).** No ASR yet.

```
Linux collector ──SMB (read-only)──▶ native worker ──HTTP──▶ API ──▶ PostgreSQL
 (RTLSDR-Airband)                    observes files          interprets, writes
```

- **Control plane** (Docker Compose): PostgreSQL 17 and the FastAPI service.
  It is the only writer to the database.
- **Worker** (native Python, runs on the Mac): scans the mounted archive
  read-only and reports observations to the API. CrispASR joins it in
  Phase 2.

## Layout

```
src/aerochorus/
  contracts.py      wire models + status enums shared by API and worker
  corpus/           pure corpus semantics: paths, filename parsing, UTC resolution
  db/               SQLAlchemy models, Alembic helpers (control plane only)
  migrations/       Alembic migrations (shipped in the package)
  api/              FastAPI app, routes, indexing service
  worker/           read-only filesystem reader, audio probe, scanner, daemon
  cli.py            `aerochorus` command
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
`~/.config/aerochorus/worker.toml` (macOS/Linux) or
`%APPDATA%\aerochorus\worker.toml` (Windows), or point
`AEROCHORUS_WORKER_CONFIG` at it. Then:

```bash
uv run aerochorus worker health                                   # exit 0 = healthy
uv run aerochorus worker scan home_atc_archive --prefix 2026/09/08 # one day
uv run aerochorus worker scan home_atc_archive                     # everything, incremental
uv run aerochorus source summary home_atc_archive
uv run aerochorus worker run                                       # heartbeats + scheduled scans
```

Scan modes: `incremental` (default; skips directories unchanged since a settled
listing), `full` (lists everything, reads only new or changed files), and
`verify` (re-hashes every file). See
[ADR-013](docs/adr/0013-segment-lifecycle-and-incremental-scans.md).

### On the M1 MacBook Air

Mount the share read-only, for example
`mount_smbfs -o rdonly //user@192.168.68.84/bwi /Volumes/ATC`, ideally with an
SMB account that only has read rights. Then set `root = "/Volumes/ATC"` in
`worker.toml`. While a scan runs, the worker holds a `caffeinate -i`
assertion, so the Mac can still sleep when it is idle.

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
