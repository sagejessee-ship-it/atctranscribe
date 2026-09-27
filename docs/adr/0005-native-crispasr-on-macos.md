# ADR-005: CrispASR runs natively on macOS

Status: Accepted (2026-09-26)

## Decision

- Docker Compose runs the control plane only: PostgreSQL, the API, and later
  the web UI.
- The macOS host runs the AeroChorus worker and CrispASR natively (Metal
  build).
- CrispASR is never containerised on the Mac.

## Consequences

The worker is a plain Python process installed with `uv`. It reaches the
control plane over HTTP on `127.0.0.1:8000`. Compose publishes ports on
loopback only.
