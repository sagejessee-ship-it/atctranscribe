# ADR-010: Absolute UTC is mandatory

Status: Accepted (2026-09-26)

## Context

The v1 audit found that missing absolute UTC was the main obstacle to future
contextual fusion (ADS-B, airport and procedure context).

## Decision

Every segment carries `capture_start_utc`/`capture_end_utc` when the evidence
supports them, plus a `temporal_status`:

| status | meaning | UTC stored |
| --- | --- | --- |
| `resolved` | filename time converted with the source timezone and corroborated by mtime | yes |
| `unverified` | filename time converted with the source timezone; mtime disagrees or duration unknown | yes (flagged) |
| `ambiguous` | DST fall-back fold that the evidence cannot disambiguate | no |
| `unresolved` | no usable time in the name, timezone not configured, or wall-clock time inside a DST gap | no |

A timestamp is never fabricated from mtime alone. The database enforces that
UTC is present **if and only if** the status is `resolved` or `unverified`
(`ck_segment_utc_matches_temporal_status`). Future temporal fusion should
consume only `resolved` segments unless it deliberately opts into
`unverified` ones.

See ADR-012 for how the status is derived.
