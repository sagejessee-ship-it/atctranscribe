# ADR-011: Workers observe; the control plane interprets and writes

Status: Accepted (2026-09-26)

## Context

The corpus must be indexed by a process that can see the mounted share, which
means a native host process (the SMB mount is on the host). The plan also
requires workers to reach PostgreSQL only through the API, so that an M1, a
Linux GPU box, or the 5080 can all be workers without control-plane changes.
Bind-mounting a network share into Docker Desktop is brittle, especially
across share reconnects.

## Decision

- The corpus indexer is a **worker capability**. `aerochorus worker scan`
  lists, stats, reads and hashes files, and probes audio headers. It sends raw
  **observations** (relative path, size, mtime in ns, SHA-256, audio probe) to
  the API in batches.
- The **API interprets** observations with the pure functions in
  `aerochorus.corpus`: filename parsing, UTC resolution, change detection. It
  is the only writer to PostgreSQL.
- Worker code must not import `sqlalchemy`, `alembic`, `psycopg`,
  `aerochorus.db` or `aerochorus.api`.
- Every batch commits on arrival, so a scan's progress survives interruption.

## Consequences

- Derived fields (station, channel, frequency, UTC, temporal status) can be
  recomputed from stored observations after a parser fix, without rescanning.
- Mount paths exist only in `worker.toml`, per machine.
- `test_worker_never_loads_the_database_layer` enforces the import boundary by
  importing the worker in a clean interpreter.
