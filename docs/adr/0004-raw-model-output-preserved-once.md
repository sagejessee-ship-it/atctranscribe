# ADR-004: Raw model output is preserved once

Status: Accepted (2026-09-26); implementation in Phase 2

## Context

Future parsers may extract information the current parser ignores, so raw
CrispASR output must be recoverable without rerunning inference. v1 duplicated
raw output through every downstream representation.

## Decision

PostgreSQL stores the normalised, queried fields plus the artifact URI,
SHA-256 and size. The raw response is stored exactly once in an artifact store
as `raw/<result_uuid>.json.zst`. Where that store lives on the Mac is
configuration.

## Consequences

The artifact store will be the only place AeroChorus writes files. It has its
own root, which must never be inside a corpus source. The write-guard test
will need an explicit allowance for that module.
