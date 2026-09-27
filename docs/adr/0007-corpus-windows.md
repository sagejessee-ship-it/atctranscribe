# ADR-007: Corpus windows, not infinite runs

Status: Accepted (2026-09-26); implementation in Phase 3

## Decision

A sweep run is `corpus selection × model suite × run configuration`, for
example 2026-08-01..2026-08-07 × `full-lab-v1`. Every model finishes a window
before the sweep advances, so each finished window is an ensemble-ready time
slice even while processing lags months behind collection. Window size is
configurable: start small, and a week is the likely default.

Resume is allowed only when the effective configuration hash matches. A changed
model or configuration means a new run.
