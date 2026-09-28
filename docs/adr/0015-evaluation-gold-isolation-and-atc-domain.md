# ADR-015: Evaluation, gold isolation, and the ATC domain module

Status: Accepted (2026-09-27)

## Context

Phase 4 turns ATCO2 into a regression benchmark. The v1 audit
(`atc-asr-corpus-studio/docs/v1-audit`) found four problems:

- v1 had four text normalizers. Scoring used a weaker one for hypotheses
  than for gold (**DEFECT-2**), which inverted its provider ranking.
- The metrics that mattered (oracle, entity accuracy, agreement vs
  correctness) lived in an unused code path.
- There was no held-out split, so every threshold was fitted in-sample.
- Gold isolation worked, but only by file-layout convention.

Re-scoring v1's stored hypotheses with a symmetric scorer (2026-09-27)
confirmed the first problem in practice. v1's published ranking (Canary
0.581 best) becomes Qwen3-ASR-1.7B 0.501, Parakeet-TDT-v3 0.535, …, with
Canary only sixth (0.586).

## Decision

**One ATC domain module** (`aerochorus.atc`). It is pure code: a versioned
lexicon (NATO alphabet, ICAO numbers, ATC vocabulary, filler words,
off-domain and conversational phrases) and exactly two normalizers:

| function | job | strength |
| --- | --- | --- |
| `normalize_for_scoring` | accuracy vs gold, applied to **both** sides | strips markup, case, Unicode punctuation/symbols; `canonical_numbers` opt-in |
| `normalize_for_evidence` | agreement between models | case + edge punctuation only: `16L ≠ 16R`, `"two seven zero" ≠ "270"` |

Punctuation is removed by Unicode category, not `[^\w\s]`. That regex
(v1) strips combining marks and shreds non-Latin words, so language drift
must stay intact to stay visible.

**Benchmark corpus = an ordinary read-only source.** `eval atco2 prepare`
clips each gold segment at its exact XML boundaries (no VAD, no padding,
end-clamped like v1) into a derived directory. The clips are named
`<recording_id>/<index>_<start_ms>_<end_ms>.wav`, and the directory contains
**no gold text**. It is registered as the source `atco2_fixed` (parser
`atco2_clip`, UTC from the ATCO2 recording name, `mtime_corroboration =
false`, so segments are honestly `unverified`) and transcribed by normal
sweeps. Nothing on the transcription path knows it is a benchmark.

**Gold lives in its own schema.** `reference.gold_segment` holds the raw
gold, speaker, tags and split, keyed by `(source, relative_path)`. Gold is
immutable: re-importing different text is refused (409). Only
`aerochorus.api.evaluation` reads it, and only `/api/v1/evaluation/*`
returns it. Architecture tests enforce this: the worker, scanner and
CrispASR path never import `aerochorus.datasets`, `eval_*` or the API
package, and only the evaluation service uses the gold model. Integration
tests check every payload the worker sees for gold phrases. A dedicated
database role with no grant on `reference` is the next hardening step, and
is not needed while one API process owns the database.

**Held-out split.** ATCO2 v1_beta ships none, so the split is derived
deterministically by recording (SHA-256 of the recording id →
`calibration`/`test`). That gives 456 and 421 segments; whole recordings
stay on one side. Phase 6 fits thresholds on `calibration` only and reports
on `test`.

**Scoring rules** (`eval report`):

- Micro token error rate with S/D/I, CER, exact match, abstain and error
  rates. Abstentions, errors and missing results score as empty
  hypotheses, so silence is never free.
- Entity recall per human word-class tag (`[#callsign]`, `[#value]`,
  `[#command]`). This is the metric that matches the product's promise.
- Every report shows the **best single model** and the **per-segment
  oracle** beside the ensemble slot. The ensemble slot is empty until
  Phase 6, and a future ensemble must beat both.
- A model whose pedigree lists the benchmark in `contaminated_benchmarks`
  is excluded from both claims.
- Family agreement (mean pairwise LCS similarity of spoken hypotheses) is
  binned against error, which is the evidence Phase 6 will build on.

**Quality flags run in the background.** Every result gets deterministic,
versioned flags on ingest: `empty`, `filler_only`, `non_latin_script`,
`diacritics`, `language_mismatch`, `off_domain_phrase`,
`conversational_phrase`, `repetition_loop`, `low_atc_content`,
`implausible_rate`. They flag and never edit text. `results reflag`
recomputes them after a lexicon change. Every threshold is named and
**unfitted** until Phase 6 fits it on the calibration split.

**Sweeps are claimed only by workers that mount their source.**
`ClaimRequest.source_keys` means the ATCO2 benchmark (which exists only on
the 5080 box) is never handed to the M1, and vice versa for sources only
the Mac mounts.

## Consequences

- Benchmark numbers are comparable across models and releases because
  there is one scorer. They are **not** comparable to v1's published
  numbers; use the re-scored v1 table in the runbook.
- Evaluation requires the benchmark clips to be mounted by some worker.
  When the control plane moves to the M1, the 5080 box stays the
  benchmark worker, pointed at the M1's API.
- The quality-flag thresholds are placeholders by design, and none of them
  gates anything yet.
