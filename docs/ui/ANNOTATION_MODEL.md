# Annotation model

The decision is recorded in [ADR-017](../adr/0017-human-annotation-silver-vs-gold.md).
This page is the working reference.

## Tables

```text
segment 1 ── * annotation_thread (scope: segment | span)
                     │ current_version_id
                     ▼
               annotation_version (append-only; UNIQUE(thread_id, version))
```

| column | notes |
| --- | --- |
| `start_ms`, `end_ms` | Null for whole-segment threads. For spans, `0 ≤ start < end ≤ duration` (+50 ms tolerance). See ADR-018. |
| `text` | The transcript this version asserts. It may be null (for example "reviewed, rejected"). |
| `text_origin` | `human` (typed, or a hypothesis accepted by a human) or `model_consensus` (batch nomination). |
| `review_status` | `unreviewed` · `reviewed` · `corrected` |
| `training_label` | `none` · `candidate` · `silver` · `gold` · `rejected` |
| `reason_tags` | Free list. The UI offers: unclear audio, clipped boundary, overlapping speech, controller clear / pilot poor, noise, non-ATC, bad segmentation, multiple utterances, partial usable span, uncertain callsign, uncertain number/runway/frequency. |
| `notes`, `annotator` | The annotator is a local label (the nav bar "user" button), not authentication. |
| `action` | `edit`, `batch_candidate`, `batch_silver`, `batch_clear` |
| `basis` | Why. For batch: agreement version, exact/near family and provider counts, representative source and sample id. For edits: the hypothesis the text started from. |

Database guarantees:

- CHECK `gold_is_human`: gold needs `text_origin = 'human'`, non-empty text,
  and a status of reviewed or corrected;
- paired and ordered span bounds;
- enumerations for every state column.

## Rules enforced by the API

| rule | where |
| --- | --- |
| A save names `expected_version`, and a mismatch returns 409 | `review.save_annotation` |
| Gold needs `confirm_gold: true`, text, and a reviewed/corrected status | `review.save_annotation` + DB CHECK |
| Benchmark sources never get candidate/silver/gold | `review.save_annotation`, `review.batch_nominate` |
| Batch actions: candidate · silver · clear. Gold is not in the enum. | `BatchAction` |
| Batch silver needs 2-family agreement (`representative_source ∈ {exact, near}`) or existing human text | `review.batch_nominate` |
| Batch never changes current gold or human-rejected items | `review.batch_nominate` |
| Clear only removes candidate or silver | `review.batch_nominate` |
| A source cannot become `benchmark` while it has training labels | `PATCH /sources/{key}/role` |

## How the UI maps actions

| action | request |
| --- | --- |
| Save (Ctrl+Enter) | `POST /review/segments/{id}/annotations` with the editor's text. Status is `reviewed` if the text equals a hypothesis verbatim, else `corrected`. |
| `S` (silver) | With typed text: save with `training_label=silver`. Without: batch silver for this one segment, so the model consensus is used with its basis. |
| `G` (gold) | Opens the confirmation dialog showing the exact text. Confirm saves with `confirm_gold: true`. |
| `X` (reject) | Save with `training_label=rejected`. |
| Batch bar | `POST /review/batch` with the selected ids |

Nothing is shown as saved until the API answers. A 409 offers "Reload
annotation".

## Search

"Human" search scope matches the **current** version of any thread: the
whole segment or a span. Older versions stay in the history but do not match.

## Spans (Phase 5B)

- A span is a thread with `scope = 'span'`. Each save is a version with its
  own bounds, text and label. Moving the region on the waveform and saving
  records new bounds, and history keeps the old ones.
- A span's label is independent of the parent's. The usual partial-gold case
  is: the parent is `rejected` or unreviewed, and a span is `gold`.
- Filters: `span_labels` (a span has the label), `has_spans`, and
  `partial_usable`, meaning a gold/silver span on a parent that is not
  itself gold/silver. There is a built-in "Partial usable" view.
- Datasets include spans as `scope = span` items. The export trims the
  parent audio to the bounds (see TRAINING_DATASET_LIFECYCLE.md).
