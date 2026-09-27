# ADR-006: Persistent model process, sequential models

Status: Accepted (2026-09-26); implementation in Phase 2

## Decision

A model is never reloaded for each audio segment. For each model in a sweep:
start the CrispASR server, load the model, process the whole requested corpus
window, then stop the server. Only then start the next model. CrispASR is
restarted between models even though it supports loading models at runtime.

## Consequences

- The lifecycle is deterministic, memory is reclaimed cleanly, fault recovery
  is simple, and provenance is clean (one process = one model identity).
- Throughput is secondary to reliability on the passively cooled M1 Air.
