# Architecture Decision Records

Each ADR records one decision, why it was made, and what it costs. ADR-001 to
ADR-010 come from the founding plan ([../plan.md](../plan.md)). ADR-011 onward
were made while implementing Phases 0 and 1.

| ADR | Decision | Enforced by |
| --- | --- | --- |
| [001](0001-clean-repository.md) | Clean repository; port v1 behaviour selectively | — |
| [002](0002-immutable-external-source-audio.md) | Source audio is external, read-only, logically addressed | `ck_corpus_source_read_only`, `test_source_audio_cannot_be_modified`, `ReadOnlyCorpusReader` |
| [003](0003-postgresql-is-authoritative.md) | PostgreSQL is authoritative; JSONL is export-only | `test_no_jsonl_operational_datastore` |
| [004](0004-raw-model-output-preserved-once.md) | Raw model output stored once as `.json.zst` | Phase 2 |
| [005](0005-native-crispasr-on-macos.md) | CrispASR native on macOS; containers are control plane only | `docker-compose.yml` |
| [006](0006-persistent-model-process.md) | One persistent CrispASR process per model, restart between models | Phase 2 |
| [007](0007-corpus-windows.md) | Sweep runs are corpus windows × model suites | Phase 3 |
| [008](0008-eligible-models.md) | "All models" means all enabled, sweep-eligible models | `ck_model_sweep_requires_sha256` |
| [009](0009-one-architecture-family-registry.md) | Exactly one architecture-family registry | FK `model.architecture_family → architecture_family.key`, `test_exactly_one_architecture_family_registry` |
| [010](0010-absolute-utc.md) | Absolute UTC is mandatory; never fabricated | `ck_segment_utc_matches_temporal_status`, `test_temporal.py` |
| [011](0011-workers-observe-control-plane-interprets.md) | Workers observe files; the control plane interprets and writes | `test_worker_never_loads_the_database_layer` |
| [012](0012-filename-time-and-mtime-corroboration.md) | Filename wall-clock time + source timezone, corroborated by mtime | `aerochorus.corpus.temporal` |
| [013](0013-segment-lifecycle-and-incremental-scans.md) | Segments are never deleted; presence and integrity are tracked; incremental scans use directory mtimes | `test_scanning.py` |
