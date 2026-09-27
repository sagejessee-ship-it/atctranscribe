# ADR-008: "All models" means all eligible models

Status: Accepted (2026-09-26)

## Decision

AeroChorus owns its model registry (`model` table). A full sweep means every
enabled, `sweep_eligible` ASR model in the selected suite. It does not mean
every backend CrispASR happens to ship. Per-model capabilities (word
timestamps, token confidence, diarization, Metal) live in `capabilities`.
Pedigree (training data, benchmark contamination, licence) lives in
`pedigree`.

## Consequences

- A model cannot be sweep-eligible without a pinned artifact hash
  (`ck_model_sweep_requires_sha256`).
- Models enter `full-lab-v1` only after passing AeroChorus's own qualification
  benchmark. That benchmark runs after Phase 2.
