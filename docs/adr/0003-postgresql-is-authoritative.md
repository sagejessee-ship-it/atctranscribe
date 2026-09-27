# ADR-003: PostgreSQL is authoritative

Status: Accepted (2026-09-26)

## Context

v1 generated about 580 MB of JSONL for one hour of source audio, because
every downstream representation re-serialised everything upstream of it.

## Decision

PostgreSQL owns corpus indexing, processing state, model registry snapshots,
run state, transcription metadata, derived results and human annotations.
JSONL is only an export or interchange format. Schema changes go through
Alembic migrations, which ship inside the package (`aerochorus:migrations`).

## Consequences

- `test_no_jsonl_operational_datastore` guards against JSONL creeping back in
  as storage. When an export module arrives, allow it explicitly in that test.
- `test_models_match_migrations` fails if the ORM models and migrations drift
  apart.
