# Training dataset lifecycle

```text
review & label            freeze (control plane)             export (corpus machine)              train (outside AeroChorus)       re-register
─────────────────  →  training_dataset vN (frozen)  →  clips/*.wav + manifest + hashes  →  fine-tune + convert to GGUF  →  model + lineage + gates
annotation_version     items = exact annotation         read-only parent, sha256 check,     dataset manifest sha256           (ADR-019)
(append-only)          versions + split + source sha    ffmpeg trim, WAV 16 kHz mono         recorded in lineage
```

## 1. Freeze a version

```bash
uv run aerochorus dataset create bwi-atc --labels gold,silver --scopes segment,span \
    --split 0.8,0.1,0.1 --group-by utc_day_channel --seed 0 --created-by sj
uv run aerochorus dataset list
```

or on `/training`: "Freeze new version". The API is `POST /api/v1/datasets`.

| option | meaning |
| --- | --- |
| `labels` | Any of gold, silver, candidate. Rejected and none never. |
| `scopes` | `segment` (whole segments) and/or `span` |
| `filters` | Optional review filters on the parent segments (channels, UTC range, airport, …) |
| `include_spans_of_included_segments` | Off by default, so audio is not duplicated |
| `split.group_by` | `utc_day_channel` (default), `utc_day` or `segment` |
| `split.seed`, `train/validation/test` | Deterministic assignment: `sha256(seed:group)` mapped into the ratios |

A frozen version holds, per item:

- the annotation version id;
- segment, scope and bounds;
- the text, label and text origin (`human` or `model_consensus`);
- the parent's `source_sha256`;
- the split and split group.

`manifest_sha256` is the SHA-256 of the canonical JSON of those fields, and
`definition_sha256` covers the options. Versions are numbered per name.
Freezing the same definition over the same annotations gives the same
manifest hash.

**Frozen means frozen.** Later edits create new annotation versions and
never change an existing dataset. Freeze a new version to pick them up.

**Leakage control.** Grouping by UTC day × channel keeps a controller
instruction and its readback, a few seconds apart on the same frequency, in
the same split.

**Benchmark exclusion.** Items come only from `role = corpus` sources, so
ATCO2 can never enter a training set.

## 2. Export (materialize clips)

This runs where the corpus is mounted (the Mac in production) and needs
ffmpeg (`brew install ffmpeg` / `winget install Gyan.FFmpeg`):

```bash
uv run aerochorus dataset export 3 --out ~/aerochorus-data/datasets
```

For each item, the exporter:

1. reads the parent with `ReadOnlyCorpusReader` and checks its SHA-256
   against the frozen `source_sha256`. If the source changed since freezing,
   the export aborts and records nothing;
2. decodes with ffmpeg from **stdin**, so ffmpeg never sees a source path,
   and for spans trims to `[start_ms, end_ms)` after decoding;
3. writes 16 kHz mono PCM16 WAV with exact headers, and hashes it.

Output: `<out>/<name>-v<version>/`

| file | contents |
| --- | --- |
| `clips/<split>/<ordinal>_seg<id>[_<start>-<end>].wav` | derived clip audio |
| `manifest.jsonl` | one line per clip: audio path, text, split, duration, clip SHA-256, label, text origin, scope, bounds, source + source SHA-256, annotation version, split group |
| `dataset.json` | the frozen definition and hashes, plus export metadata |

The exporter then reports every clip's hash back (`POST
/datasets/{id}/export`) and the version becomes `exported`. A re-export with
identical clips is accepted. One that produces different clips is refused
(409): freeze a new version instead.

The exporter refuses an output directory inside any source root. Source
audio is never written (`test_export_materializes_clips_and_never_touches_sources`).

## 3. Train, convert, re-register

Training is outside AeroChorus in Phase 5. To bring a fine-tune back:

1. Convert it with the architecture's CrispASR converter, then quantize.
2. Add it to `config/models.toml` with `artifact_url`, and a
   `pedigree.lineage` naming the dataset (`name vN` plus its
   `manifest_sha256`), the training run, the checkpoint hash, the converter
   commit and the CrispASR version. Then run `aerochorus models sync`.
3. Run a qualification sweep and record each gate with `aerochorus models
   qualify <name> <gate> --passed --evidence '{...}'`.
4. `aerochorus models set <name> --eligible` succeeds only once all seven
   gates have passed (ADR-019).

## /training page

The page shows:

- current label counts: candidate, silver, gold and rejected, split into
  whole segments and spans, with minutes of audio;
- channel and UTC-day distributions;
- the freeze form;
- dataset versions, with items, minutes, split counts, the manifest hash,
  and the export status or the export command to run.

It does not orchestrate training.
