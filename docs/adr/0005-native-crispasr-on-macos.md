# ADR-005: CrispASR runs natively on macOS

Status: Accepted (2026-09-26). **Superseded for deployment by
[ADR-021](0021-linux-pascal-deployment.md) (2026-09-27):** the Mac is an
Intel client, not an inference host. The native-binary launcher described
here is kept; it now runs the pinned CUDA 12 build on the Linux host.

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

## Amendment (2026-09-27, ADR-014)

On the Mac this decision stands: Docker on macOS cannot reach Metal, so
CrispASR runs natively there. On NVIDIA hosts (the RTX 5080 box, future Linux
GPU workers) the worker may instead run CrispASR's CUDA server image,
`launcher = "docker"`, pinned by digest. It is still one model per server,
started and stopped by the worker, with the same HTTP contract. The Compose
stack itself stays control-plane only.
