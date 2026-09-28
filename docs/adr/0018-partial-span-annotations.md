# ADR-018: Partial-span annotations

Status: Accepted (2026-09-27)

## Context

ATC recordings are often mixed. A segment can hold one clean controller
instruction and a garbled pilot readback, or start with squelch noise.
Throwing away the whole segment wastes the clean phrase, and cutting
source audio would break ADR-002.

## Decision

A **span** is an annotation thread with `scope = 'span'` and bounds
`[start_ms, end_ms)` relative to its parent segment. It uses the same
append-only versions and labels as whole-segment annotations (ADR-017):

- **Bounds are data.** Source audio is never cut or copied at annotation
  time. Moving a region's edges saves a new version with new bounds.
- **Independent labels.** A segment can be `rejected` while one of its spans
  is `gold`. The "Partial usable" view and filter (`partial_usable`) find
  exactly that case.
- **Gold is still human.** A gold span needs typed text and confirmation of
  exactly that span. The DB CHECK applies to spans too.
- **Search and filters.** "Human" search matches the current span texts.
  `span_labels`, `has_spans` and `partial_usable` filter on them.
- **Export** (see TRAINING_DATASET_LIFECYCLE.md) is where clip audio is
  materialized. The exporter reads the parent through
  `ReadOnlyCorpusReader`, checks its SHA-256 against the dataset snapshot,
  and trims after decoding. The clip is hashed and stored outside every
  source root.
- **Nesting.** A span inside a segment that is itself in a dataset is left
  out by default, so the same audio is not trained on twice. The dataset
  option `include_spans_of_included_segments` turns that off.

UI: WaveSurfer.js regions (drag to select, drag edges to adjust, click to
loop), exact start/end fields in milliseconds, a list of saved spans, and a
span editor with the same label control and gold confirmation.

**Deferred:** "Re-transcribe selection", which would run a model on the span
and store it as a child result with parent and offset provenance, is not
built yet. When it is, it will be a sweep over `(segment, start_ms, end_ms)`
units using CrispASR's request offsets, with results stored beside, never
over, the parent's hypotheses.

## Consequences

- Nothing about the parent changes: its hypotheses, hash and audio stay as
  they were.
- Span bounds are sample-accurate at 16 kHz after decoding. MP3
  frame/encoder padding means that whole-segment clip lengths can differ
  from the probe's duration by about 100–150 ms. Spans are cut exactly.
