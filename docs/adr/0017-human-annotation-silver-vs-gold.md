# ADR-017: Human annotation: versioned threads, silver vs gold

Status: Accepted (2026-09-27)

## Context

Reviewers correct transcripts and decide what can train a model. Three
things must never happen:

- a correction overwrites a model hypothesis or its provenance;
- model agreement quietly becomes "truth";
- benchmark clips (ATCO2) leak into training data.

The existing `reference.gold_segment` is **benchmark** gold, which is
isolated for evaluation (ADR-015). It is not a place for review labels.

## Decision

**Append-only annotation threads.** An `annotation_thread` is one annotated
object: the whole segment (at most one per segment) or one bounded span
(`start_ms`, `end_ms`; Phase 5B). Every save appends an `annotation_version`,
and the thread points at its current version. A save carries the version the
editor started from. If another save landed in between, it is refused with
409. Each version records:

- `text` and `text_origin` (`human` or `model_consensus`);
- `review_status` and `training_label`;
- `reason_tags`, `notes` and `annotator`;
- `action` (edit, batch_silver, …);
- `basis`, which says why: agreement counts, the representative source, and
  the hypothesis the correction started from.

Hypotheses stay in `transcription_result` and are never modified.

**Two separate axes:**

| axis | values | meaning |
| --- | --- | --- |
| `review_status` | unreviewed · reviewed · corrected | What a human did. Accepting a hypothesis verbatim is *reviewed*; any edit is *corrected*. |
| `training_label` | none · candidate · silver · gold · rejected | What the item is for. |

- **Candidate**: possible training use. Batch nomination may set it from any
  representative text.
- **Silver**: high-agreement label, not necessarily human-verified. Batch
  nomination may set it only when 2+ architecture families agree, exactly or
  within the near threshold (`representative_source ∈ {exact, near}`), or
  when a human transcript exists. A lone model's text, or the medoid of
  disagreeing models, is never silver.
- **Gold**: a human assertion that someone listened and the text is exact.
  It needs human text, a reviewed or corrected status, and `confirm_gold`
  from an explicit confirmation dialog. It is **not a batch action**. The
  database enforces it too: `ck_annotation_version_gold_is_human`.
- **Rejected**: unsuitable for training. Batch nomination never overrides a
  human rejection or a human gold.

**Benchmark guard.** `corpus_source.role` is `corpus` or `benchmark`.
Benchmark sources are hidden from review by default and can be reviewed, but
they can never carry candidate, silver or gold. A source cannot switch to
`benchmark` while its segments carry training labels.

## Consequences

- Full audit history for every label, including batch nominations and who
  made them.
- Model consensus text can reach a training set only as candidate or silver,
  with its `basis` recorded, never as gold.
- Two reviewers editing the same segment get a conflict, not a silent
  overwrite. Multi-annotator consensus is out of scope.
