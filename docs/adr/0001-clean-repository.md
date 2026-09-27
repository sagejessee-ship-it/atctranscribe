# ADR-001: Create a clean AeroChorus repository

Status: Accepted (2026-09-26)

## Context

The v1 ASR ensemble on the RTX 5080 workstation holds a lot of validated
knowledge. It also has duplicated normalizers, family maps, adjudication and
evaluation paths, provider infrastructure, and storage amplification.

## Decision

Do not fork v1 into v2. AeroChorus starts as a clean repository. Validated v1
behaviour is ported selectively and arrives with tests. v1 stays a source of
algorithms, regression cases, evaluation knowledge and compatibility
references, not a deployment architecture.

## Consequences

- Every ported behaviour needs a test that shows why it exists.
- Porting is slower, but v1's structural defects stay out.
