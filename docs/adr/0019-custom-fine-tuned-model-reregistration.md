# ADR-019: Re-registering custom fine-tuned models into CrispASR sweeps

Status: Accepted (2026-09-27)

## Context

The point of curating gold and silver data is to fine-tune models and bring
them back into the ensemble. CrispASR runs GGUF/ggml artifacts produced by
converters written for one architecture each (Whisper → ggml,
Wav2Vec2ForCTC → GGUF, Parakeet HF → GGUF, then generic re-quantization). A
fine-tuned HF or NeMo checkpoint does not load on its own, and a model that
loads can still be broken, for example by a wrong tokenizer or vocab, or a
silently changed decoder.

## Decision

**Registry representation.** A converted fine-tune is an ordinary `model`
row. Its converted GGUF is the model artifact:

- `model_filename`, `model_sha256`, `artifact_size_bytes`, `quantization`,
  `crisp_backend`;
- `artifact_url`, used when the file is not on Hugging Face.

Its origin is a validated `pedigree.lineage` (`ModelLineage`, extra fields
forbidden):

| field | meaning |
| --- | --- |
| `base_model` | the parent model |
| `fine_tune_dataset` + `fine_tune_dataset_manifest_sha256` | the exact dataset version from `/training` |
| `training_run` | a reference to the training job |
| `native_checkpoint_uri` + `native_checkpoint_sha256` | the checkpoint before conversion |
| `converter` + `converter_version` | the conversion script and its commit |
| `crispasr_version` | the pinned CrispASR that must load it |

Contamination and licence notes stay free-form in `pedigree`. A fine-tune
keeps its base model's **architecture family**. It is never counted as
independent agreement with its parent (ADR-009).

**Re-incorporation gate.** `model.qualification` records one entry per gate,
each with `passed`, time, `by` and evidence:

1. `conversion`: the backend-specific converter succeeded;
2. `artifact_hash`: the artifact hash is recorded (needs `model_sha256`);
3. `crispasr_load`: the pinned CrispASR loads it;
4. `smoke`: smoke audio transcribes;
5. `usability`: output passes AeroChorus checks (quality flags, abstention
   rate);
6. `regression`: the regression suite ran (the ATCO2 benchmark, whose
   report is the evidence);
7. `pedigree`: lineage is complete (validated again when recorded).

A model with a lineage can become `sweep_eligible` only when every gate has
passed (409 otherwise). A later failed gate revokes eligibility. Models
without a lineage (upstream releases) keep the existing rule (ADR-008).

Tools: `aerochorus models qualify <name> <gate> --passed|--failed --evidence
'{...}'` and `POST /api/v1/models/{name}/qualification`.

## Consequences

- Every fine-tune in a sweep can be traced to its dataset manifest, training
  run, checkpoint hash, converter commit and runtime.
- Recording gate evidence is manual today. Automating smoke, usability and
  regression from a qualification sweep is a later step.
- For artifacts that are not on Hugging Face, place the GGUF in the worker's
  `models_dir`, where it is verified by SHA-256, or serve it over HTTP via
  `artifact_url`.
