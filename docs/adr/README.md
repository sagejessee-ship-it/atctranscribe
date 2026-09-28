# Architecture Decision Records

Each ADR records one decision, why it was made, and what it costs. ADR-001 to
ADR-010 come from the founding plan ([../plan.md](../plan.md)). ADR-011 onward
were made while implementing Phases 0–4.

| ADR | Decision | Enforced by |
| --- | --- | --- |
| [001](0001-clean-repository.md) | Clean repository; port v1 behaviour selectively | — |
| [002](0002-immutable-external-source-audio.md) | Source audio is external, read-only, logically addressed | `ck_corpus_source_read_only`, `test_source_audio_cannot_be_modified`, `ReadOnlyCorpusReader` |
| [003](0003-postgresql-is-authoritative.md) | PostgreSQL is authoritative; JSONL is export-only | `test_no_jsonl_operational_datastore` |
| [004](0004-raw-model-output-preserved-once.md) | Raw model output stored once as `.json.zst` | `ArtifactStore`, `test_artifacts_are_compressed_json` |
| [005](0005-native-crispasr-on-macos.md) | CrispASR native on macOS; Compose is control plane only (amended: CUDA image allowed on NVIDIA hosts). **Superseded for deployment by ADR-021** | `docker-compose.yml`, `worker.crispasr.launcher` |
| [006](0006-persistent-model-process.md) | One persistent CrispASR process per model, restart between models | `SweepWorker`, `test_two_model_sweep_end_to_end` |
| [007](0007-corpus-windows.md) | Sweep runs are corpus windows × model suites | `sweep_run_segment`, `config_sha256` |
| [008](0008-eligible-models.md) | "All models" means all enabled, sweep-eligible models | `ck_model_sweep_requires_sha256` |
| [009](0009-one-architecture-family-registry.md) | Exactly one architecture-family registry | FK `model.architecture_family → architecture_family.key`, `test_exactly_one_architecture_family_registry` |
| [010](0010-absolute-utc.md) | Absolute UTC is mandatory; never fabricated | `ck_segment_utc_matches_temporal_status`, `test_temporal.py` |
| [011](0011-workers-observe-control-plane-interprets.md) | Workers observe files; the control plane interprets and writes | `test_worker_never_loads_the_database_layer` |
| [012](0012-filename-time-and-mtime-corroboration.md) | Filename wall-clock time + source timezone, corroborated by mtime | `aerochorus.corpus.temporal` |
| [013](0013-segment-lifecycle-and-incremental-scans.md) | Segments are never deleted; presence and integrity are tracked; incremental scans use directory mtimes | `test_scanning.py` |
| [014](0014-crispasr-runtime-and-sweep-protocol.md) | CrispASR native/container launchers; claim–start–results–finish protocol; runtime fingerprints; one result per segment | `test_sweeps.py`, `ck_transcription_result_status_semantics` |
| [015](0015-evaluation-gold-isolation-and-atc-domain.md) | One ATC domain module (two normalizers); benchmark clips as a normal source; gold in an isolated schema; derived held-out split; best-single + oracle beside every ensemble; background quality flags; source-aware claims | `test_atc_domain.py`, `test_transcription_path_cannot_see_gold`, `test_evaluation.py` |
| [016](0016-review-edge-server.md) | Review edge server (UI + API proxy + read-only, hash-verified audio); derived, versioned per-segment agreement | `test_edge.py`, `test_worker_never_loads_the_database_layer`, `test_agreement.py` |
| [017](0017-human-annotation-silver-vs-gold.md) | Append-only annotation threads; review status ≠ training label; silver needs 2-family agreement; gold is human-only, confirmed, never batch; benchmark sources never trainable | `ck_annotation_version_gold_is_human`, `test_review.py` |
| [018](0018-partial-span-annotations.md) | Spans are bounded annotation threads on the parent; labels independent of the parent; clips materialized only at export (re-transcribe-selection deferred) | `test_datasets.py`, E2E "partial gold" |
| [019](0019-custom-fine-tuned-model-reregistration.md) | Converted fine-tunes carry a validated `pedigree.lineage` and pass seven qualification gates before becoming sweep-eligible | `test_model_lineage.py` |
| [020](0020-on-demand-cached-adsb-context.md) | ADS-B context is fetched on demand from OpenSky Trino (`state_vectors_data4`), tightly bounded, cached as snapshots; never bulk-ingested; credentials server-side only | `test_opensky.py`, `test_context.py`, `ui/e2e/adsb.spec.ts` |
| [021](0021-linux-pascal-deployment.md) | Primary host is Linux + GTX 1070 8 GB (CUDA 12 pinned; Pascal); hardware profiles; qualification per profile; ensemble eligibility separate from sweep eligibility; explicit memory/offload | `test_hardware_profile.py`, `test_platform_qualification.py`, `deploy/linux/` |
| [022](0022-model-adjudication.md) | Paid model adjudication (Gemini via OpenRouter) only for human-confirmed batches under a hard, worst-case-reserved cost cap; runner on the audio host alone holds the key; result is silver at most (`model_adjudicated`), never over human work, never gold | `test_adjudication.py`, `test_adjudication_prompt.py`, `ui/e2e/adjudication.spec.ts` |
| [023](0023-container-only-linux-deployment.md) | The Linux host runs containers only: images built on the dev PC and shipped as a checksummed bundle; worker with the pinned CUDA 12 CrispASR and GPU via the NVIDIA Container Toolkit; review edge, API, Postgres (loopback), backups and the optional adjudicator as services; config and secrets in `/srv/aerochorus/config` | `deploy/linux/` (`package.ps1`, `host-setup.sh`, `deploy.sh`), container smoke test in the runbook |
