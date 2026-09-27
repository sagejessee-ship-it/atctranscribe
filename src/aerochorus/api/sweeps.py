"""Model registry, sweep runs and transcription results (control plane).

Invariants:
* a sweep run freezes its segment selection, model list and request
  parameters at creation (``effective_config`` + ``config_sha256``);
* exactly one result per (sweep_run_model, segment); an error result may be
  replaced by a later attempt, a success or abstention never;
* a model run resumes only with the runtime fingerprint it started with
  (same CrispASR build, same model artifact); otherwise create a new sweep;
* a model run completes only when every selected segment has a result.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, and_, case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from aerochorus import __version__
from aerochorus.api.agreement import refresh_agreement
from aerochorus.atc.quality import FLAGS_VERSION, compute_flags
from aerochorus.contracts import PresenceStatus
from aerochorus.db.models import (
    ArchitectureFamily,
    CorpusSource,
    Model,
    ModelPlatformQualification,
    ModelSuite,
    ModelSuiteMember,
    Segment,
    SweepRun,
    SweepRunModel,
    SweepRunSegment,
    TranscriptionResult,
    Worker,
)
from aerochorus.sweep_contracts import (
    PLATFORM_BLOCKING,
    CatalogSync,
    CatalogSyncResult,
    ModelLineage,
    ModelRead,
    ModelRunFinish,
    ModelRunStart,
    ModelRunStatus,
    ModelUpdate,
    PendingBatch,
    PendingSegment,
    PlatformQualificationRead,
    PlatformQualificationWrite,
    QualificationGate,
    QualificationRecord,
    ReflagResult,
    ResultAck,
    ResultPost,
    ResultStatus,
    SuiteRead,
    SuiteWrite,
    SweepCreate,
    SweepModelRead,
    SweepPreview,
    SweepPreviewModel,
    SweepRead,
    SweepReport,
    SweepReportModel,
    SweepStatus,
    TranscriptionRead,
    WordBackfillAck,
    WordBackfillItem,
    WordBackfillPost,
)

# Request fields every model run sends; per-model params and sweep overrides
# are layered on top.
REQUEST_DEFAULTS = {"response_format": "verbose_json", "temperature": "0"}
CLAIMABLE = (ModelRunStatus.QUEUED, ModelRunStatus.RETRYING)
ACTIVE = (ModelRunStatus.LOADING, ModelRunStatus.RUNNING)


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


class Invalid(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical_sha256(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# --- model registry ---------------------------------------------------------------


IDENTITY_FIELDS = (
    "architecture_family",
    "crisp_backend",
    "model_filename",
    "model_sha256",
    "request_params",
)


def model_read(model: Model) -> ModelRead:
    return ModelRead(
        logical_name=model.logical_name,
        architecture_family=model.architecture_family,
        crisp_backend=model.crisp_backend,
        model_filename=model.model_filename,
        model_sha256=model.model_sha256,
        artifact_uri=model.artifact_uri,
        artifact_size_bytes=model.artifact_size_bytes,
        upstream_model=model.upstream_model,
        upstream_revision=model.upstream_revision,
        quantization=model.quantization,
        language=model.language,
        request_params=model.request_params,
        capabilities=model.capabilities,
        pedigree=model.pedigree,
        qualification=model.qualification or {},
        enabled=model.enabled,
        sweep_eligible=model.sweep_eligible,
        experimental=model.experimental,
        ensemble_eligible=model.ensemble_eligible,
    )


def load_model(session: Session, logical_name: str) -> Model:
    model = session.scalar(select(Model).where(Model.logical_name == logical_name))
    if model is None:
        raise NotFound(f"unknown model: {logical_name}")
    return model


def sync_catalog(session: Session, body: CatalogSync) -> CatalogSyncResult:
    families_created, models_created, models_unchanged, suites_written = [], [], [], []

    known_families = set(session.scalars(select(ArchitectureFamily.key)))
    for family in body.families:
        if family.key not in known_families:
            session.add(ArchitectureFamily(**family.model_dump()))
            families_created.append(family.key)
            known_families.add(family.key)
    session.flush()

    for entry in body.models:
        if entry.architecture_family not in known_families:
            raise Invalid(
                f"{entry.logical_name}: unknown architecture family {entry.architecture_family!r}"
            )
        values = entry.model_dump(exclude={"artifact_repo", "artifact_url"}) | {
            "artifact_uri": entry.artifact_uri
        }
        existing = session.scalar(select(Model).where(Model.logical_name == entry.logical_name))
        if existing is None:
            session.add(Model(**values, sweep_eligible=False))
            models_created.append(entry.logical_name)
            continue
        changed = [f for f in IDENTITY_FIELDS if getattr(existing, f) != values[f]]
        if changed:
            raise Conflict(
                f"{entry.logical_name}: {', '.join(changed)} differ from the registered model; "
                "a different artifact needs a new logical_name"
            )
        for field in (
            "upstream_model",
            "upstream_revision",
            "quantization",
            "language",
            "capabilities",
            "pedigree",
            "artifact_uri",
            "artifact_size_bytes",
            "ensemble_eligible",
        ):
            setattr(existing, field, values[field])
        models_unchanged.append(entry.logical_name)
    session.flush()

    for suite in body.suites:
        write_suite(
            session, suite.name, SuiteWrite(description=suite.description, models=suite.models)
        )
        suites_written.append(suite.name)

    return CatalogSyncResult(
        families_created=families_created,
        models_created=models_created,
        models_unchanged=models_unchanged,
        suites_written=suites_written,
    )


def missing_gates(model: Model) -> list[str]:
    """Gates a custom fine-tuned model (one with a lineage) has not passed (ADR-019)."""
    if "lineage" not in (model.pedigree or {}):
        return []
    record = model.qualification or {}
    return [g.value for g in QualificationGate if not record.get(g.value, {}).get("passed")]


def update_model(session: Session, logical_name: str, body: ModelUpdate) -> Model:
    model = load_model(session, logical_name)
    if body.sweep_eligible and (missing := missing_gates(model)):
        raise Conflict(
            f"{logical_name} is a converted fine-tune; it becomes sweep-eligible only after "
            f"every qualification gate passes (missing: {', '.join(missing)})"
        )
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(model, field, value)
    session.flush()
    return model


def record_qualification(session: Session, logical_name: str, body: QualificationRecord) -> Model:
    """Record one gate's outcome; a failed gate revokes sweep eligibility."""
    model = load_model(session, logical_name)
    if body.gate == QualificationGate.ARTIFACT_HASH and body.passed and not model.model_sha256:
        raise Invalid(f"{logical_name} has no model_sha256 to attest")
    if body.gate == QualificationGate.PEDIGREE and body.passed:
        lineage = (model.pedigree or {}).get("lineage")
        if lineage is None:
            raise Invalid(f"{logical_name} has no pedigree.lineage")
        ModelLineage.model_validate(lineage)
    model.qualification = (model.qualification or {}) | {
        body.gate.value: {
            "passed": body.passed,
            "at": utcnow().isoformat(),
            "by": body.by,
            "evidence": body.evidence,
        }
    }
    if not body.passed and missing_gates(model):
        model.sweep_eligible = False
    session.flush()
    return model


def suite_read(session: Session, suite: ModelSuite) -> SuiteRead:
    names = session.scalars(
        select(Model.logical_name)
        .join(ModelSuiteMember, ModelSuiteMember.model_id == Model.id)
        .where(ModelSuiteMember.suite_id == suite.id)
        .order_by(ModelSuiteMember.execution_order)
    )
    return SuiteRead(name=suite.name, description=suite.description, models=list(names))


def write_suite(session: Session, name: str, body: SuiteWrite) -> ModelSuite:
    if len(set(body.models)) != len(body.models):
        raise Invalid("a suite lists each model once")
    models = [load_model(session, n) for n in body.models]
    suite = session.scalar(select(ModelSuite).where(ModelSuite.name == name))
    if suite is None:
        suite = ModelSuite(name=name, description=body.description)
        session.add(suite)
        session.flush()
    elif body.description is not None:
        suite.description = body.description
    session.execute(delete(ModelSuiteMember).where(ModelSuiteMember.suite_id == suite.id))
    session.add_all(
        ModelSuiteMember(suite_id=suite.id, model_id=m.id, execution_order=i)
        for i, m in enumerate(models)
    )
    session.flush()
    return suite


# --- sweep creation ---------------------------------------------------------------


def _resolve_models(session: Session, body: SweepCreate) -> tuple[ModelSuite, list[Model]]:
    """The suite (named, or an ad-hoc suite recorded for an explicit list) and its models."""
    if body.models:
        by_name = {
            m.logical_name: m
            for m in session.scalars(select(Model).where(Model.logical_name.in_(body.models)))
        }
        missing = [n for n in body.models if n not in by_name]
        if missing:
            raise NotFound(f"unknown models: {', '.join(missing)}")
        name = "adhoc-" + canonical_sha256(body.models)[:10]
        suite = session.scalar(select(ModelSuite).where(ModelSuite.name == name))
        if suite is None:
            suite = write_suite(
                session,
                name,
                SuiteWrite(description="Ad-hoc model list (Transcribe page)", models=body.models),
            )
        members = [by_name[n] for n in body.models]
    else:
        suite = session.scalar(select(ModelSuite).where(ModelSuite.name == body.suite))
        if suite is None:
            raise NotFound(f"unknown suite: {body.suite}")
        members = list(
            session.scalars(
                select(Model)
                .join(ModelSuiteMember, ModelSuiteMember.model_id == Model.id)
                .where(ModelSuiteMember.suite_id == suite.id)
                .order_by(ModelSuiteMember.execution_order)
            )
        )
    models = [m for m in members if m.enabled]
    if not models:
        raise Invalid(f"suite {suite.name} has no enabled models")
    return suite, models


def _selected_segment_ids(session: Session, selection, models: list[Model]) -> list[int]:
    source = session.scalar(
        select(CorpusSource).where(CorpusSource.logical_key == selection.source_key)
    )
    if source is None:
        raise NotFound(f"unknown corpus source: {selection.source_key}")
    query = select(Segment.id).where(
        Segment.source_id == source.id, Segment.presence_status == PresenceStatus.PRESENT
    )
    if selection.utc_from is not None:
        query = query.where(Segment.capture_start_utc >= selection.utc_from)
    if selection.utc_to is not None:
        query = query.where(Segment.capture_start_utc < selection.utc_to)
    if selection.relative_dir is not None:
        query = query.where(Segment.relative_dir == selection.relative_dir)
    if selection.channels:
        query = query.where(Segment.channel.in_(selection.channels))
    if selection.min_duration_ms is not None:
        query = query.where(Segment.duration_ms >= selection.min_duration_ms)
    if selection.max_duration_ms is not None:
        query = query.where(Segment.duration_ms <= selection.max_duration_ms)
    if selection.untranscribed_only:
        done = (
            select(TranscriptionResult.segment_id)
            .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
            .where(SweepRunModel.model_id.in_([m.id for m in models]))
            .group_by(TranscriptionResult.segment_id)
            .having(func.count(func.distinct(SweepRunModel.model_id)) >= len(models))
        )
        query = query.where(Segment.id.not_in(done))
    if selection.limit is not None:
        sample_key = func.md5(func.concat(str(selection.seed), ":", Segment.id))
        query = query.order_by(sample_key).limit(selection.limit)
    chosen = select(Segment.id).where(Segment.id.in_(query.scalar_subquery()))
    return list(
        session.scalars(
            chosen.order_by(Segment.capture_start_utc.asc().nulls_last(), Segment.relative_path)
        )
    )


def preview_sweep(session: Session, body: SweepCreate) -> SweepPreview:
    """What a sweep would cover and roughly how long it takes; creates nothing."""
    if body.models:
        models = list(session.scalars(select(Model).where(Model.logical_name.in_(body.models))))
        order = {n: i for i, n in enumerate(body.models)}
        models.sort(key=lambda m: order[m.logical_name])
        missing = set(body.models) - {m.logical_name for m in models}
        if missing:
            raise NotFound(f"unknown models: {', '.join(sorted(missing))}")
    else:
        _, models = _resolve_models(session, body)
    ids = _selected_segment_ids(session, body.selection, models) if models else []
    audio_ms = 0
    if ids:
        audio_ms = int(
            session.scalar(
                select(func.coalesce(func.sum(Segment.duration_ms), 0)).where(Segment.id.in_(ids))
            )
        )
    rows = session.execute(
        select(
            SweepRunModel.model_id,
            func.sum(SweepRunModel.inference_ms_total),
            func.sum(SweepRunModel.audio_ms_total),
        )
        .where(SweepRunModel.audio_ms_total > 0)
        .group_by(SweepRunModel.model_id)
    )
    rtf = {mid: float(inf) / float(aud) for mid, inf, aud in rows if aud}
    # Loading a model takes ~10-60 s per run; one model resident at a time.
    load_minutes = 1.0
    out, total = [], 0.0
    for m in models:
        observed = rtf.get(m.id)
        minutes = (audio_ms / 60000 * observed + load_minutes) if observed is not None else None
        if minutes is not None:
            total += minutes
        out.append(
            SweepPreviewModel(
                logical_name=m.logical_name,
                architecture_family=m.architecture_family,
                enabled=m.enabled,
                sweep_eligible=m.sweep_eligible,
                ensemble_eligible=m.ensemble_eligible,
                observed_rtf=round(observed, 4) if observed is not None else None,
                estimated_minutes=round(minutes, 1) if minutes is not None else None,
            )
        )
    warnings = []
    if not ids:
        warnings.append("the selection matches no segments")
    if any(not m.enabled for m in models):
        warnings.append("disabled models are skipped")
    unknown = [m.logical_name for m in models if m.id not in rtf]
    if unknown:
        warnings.append(f"no speed history yet (estimate excludes): {', '.join(unknown)}")
    families = {m.architecture_family for m in models}
    if len(families) < len(models):
        warnings.append(
            "some models share an architecture family: they never count as independent agreement"
        )
    return SweepPreview(
        segments=len(ids),
        audio_minutes=round(audio_ms / 60000, 1),
        models=out,
        estimated_minutes=round(total, 1) if ids and len(unknown) < len(models) else None,
        needs_unqualified=[m.logical_name for m in models if m.enabled and not m.sweep_eligible],
        warnings=warnings,
    )


def create_sweep(session: Session, body: SweepCreate, *, allow_ineligible: bool) -> SweepRun:
    suite, models = _resolve_models(session, body)
    ineligible = [m.logical_name for m in models if not m.sweep_eligible]
    if ineligible and not allow_ineligible:
        raise Conflict(
            f"not sweep-eligible (qualify them first, or allow unqualified models "
            f"for a smoke/qualification sweep): {', '.join(ineligible)}"
        )
    selection = body.selection

    ordered = _selected_segment_ids(session, selection, models)
    if not ordered:
        raise Invalid("the selection matches no segments")

    model_configs = [
        {
            "logical_name": m.logical_name,
            "architecture_family": m.architecture_family,
            "crisp_backend": m.crisp_backend,
            "model_filename": m.model_filename,
            "model_sha256": m.model_sha256,
            "request_params": REQUEST_DEFAULTS | m.request_params | body.request_overrides,
        }
        for m in models
    ]
    effective_config = {
        "aerochorus_version": __version__,
        "suite": suite.name,
        "selection": selection.model_dump(mode="json"),
        "segment_ids_sha256": canonical_sha256(ordered),
        "models": model_configs,
    }
    run = SweepRun(
        name=body.name,
        suite_id=suite.id,
        status=SweepStatus.QUEUED,
        selection_definition=selection.model_dump(mode="json"),
        effective_config=effective_config,
        config_sha256=canonical_sha256(effective_config),
        segments_total=len(ordered),
    )
    session.add(run)
    session.flush()
    session.execute(
        pg_insert(SweepRunSegment),
        [{"run_id": run.id, "segment_id": sid, "ordinal": i} for i, sid in enumerate(ordered)],
    )
    session.add_all(
        SweepRunModel(
            run_id=run.id,
            model_id=m.id,
            execution_order=i,
            status=ModelRunStatus.QUEUED,
            attempts=0,
            segments_total=len(ordered),
            segments_completed=0,
            segments_abstained=0,
            segments_error=0,
            audio_ms_total=0,
            inference_ms_total=0,
            runtime={},
        )
        for i, m in enumerate(models)
    )
    session.flush()
    return run


# --- reads -------------------------------------------------------------------------------


def load_run(session: Session, run_id: int) -> SweepRun:
    run = session.get(SweepRun, run_id)
    if run is None:
        raise NotFound(f"unknown sweep: {run_id}")
    return run


def load_model_run(session: Session, sweep_model_id: int, *, lock: bool = False) -> SweepRunModel:
    srm = session.get(SweepRunModel, sweep_model_id, with_for_update=lock)
    if srm is None:
        raise NotFound(f"unknown sweep model run: {sweep_model_id}")
    return srm


def _rtf(srm: SweepRunModel) -> float | None:
    if not srm.audio_ms_total:
        return None
    return round(srm.inference_ms_total / srm.audio_ms_total, 4)


def sweep_read(session: Session, run: SweepRun) -> SweepRead:
    srms = session.scalars(
        select(SweepRunModel)
        .where(SweepRunModel.run_id == run.id)
        .order_by(SweepRunModel.execution_order)
    )
    return SweepRead(
        id=run.id,
        name=run.name,
        status=run.status,
        suite=run.suite.name,
        selection=run.selection_definition,
        config_sha256=run.config_sha256,
        segments_total=run.segments_total,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        models=[
            SweepModelRead(
                id=s.id,
                logical_name=s.model.logical_name,
                architecture_family=s.model.architecture_family,
                execution_order=s.execution_order,
                status=s.status,
                attempts=s.attempts,
                claimed_by=s.worker.name if s.worker else None,
                segments_total=s.segments_total,
                segments_completed=s.segments_completed,
                segments_abstained=s.segments_abstained,
                segments_error=s.segments_error,
                audio_ms_total=s.audio_ms_total,
                inference_ms_total=s.inference_ms_total,
                real_time_factor=_rtf(s),
                runtime=s.runtime,
                last_error=s.last_error,
                started_at=s.started_at,
                completed_at=s.completed_at,
            )
            for s in srms
        ],
    )


def request_params(run: SweepRun, model: Model) -> dict[str, str]:
    for entry in run.effective_config["models"]:
        if entry["logical_name"] == model.logical_name:
            return dict(entry["request_params"])
    raise Conflict(f"{model.logical_name} is not part of sweep {run.id}")


# --- worker protocol --------------------------------------------------------------------


def claim(
    session: Session,
    worker: Worker,
    lease_seconds: int,
    source_keys: list[str] | None = None,
    hardware_profile: str | None = None,
) -> SweepRunModel | None:
    now = utcnow()
    query = select(SweepRunModel).join(SweepRun, SweepRun.id == SweepRunModel.run_id)
    if source_keys is not None:
        query = query.where(SweepRun.selection_definition["source_key"].astext.in_(source_keys))
    if hardware_profile:
        # A model recorded as OOM/unsupported/failing on this profile is left for other
        # workers; it never blocks the rest of the sweep (ADR-021).
        blocked = select(ModelPlatformQualification.model_id).where(
            ModelPlatformQualification.hardware_profile == hardware_profile,
            ModelPlatformQualification.state.in_([s.value for s in PLATFORM_BLOCKING]),
        )
        query = query.where(SweepRunModel.model_id.not_in(blocked))
    candidate = session.scalar(
        query.where(
            SweepRun.status.in_([SweepStatus.QUEUED, SweepStatus.RUNNING]),
            or_(
                SweepRunModel.status.in_(CLAIMABLE),
                and_(
                    SweepRunModel.status.in_(ACTIVE),
                    or_(
                        SweepRunModel.lease_expires_at < now,
                        SweepRunModel.claimed_by == worker.id,
                    ),
                ),
            ),
        )
        .order_by(SweepRun.id, SweepRunModel.execution_order)
        .limit(1)
        .with_for_update(of=SweepRunModel, skip_locked=True)
    )
    if candidate is None:
        return None
    candidate.status = ModelRunStatus.LOADING
    candidate.claimed_by = worker.id
    candidate.lease_expires_at = now + timedelta(seconds=lease_seconds)
    candidate.attempts += 1
    candidate.last_error = None
    run = candidate.run
    if run.status == SweepStatus.QUEUED:
        run.status = SweepStatus.RUNNING
        run.started_at = run.started_at or now
    session.flush()
    return candidate


def _require_holder(srm: SweepRunModel, worker: Worker) -> None:
    if srm.claimed_by != worker.id:
        raise Conflict(f"model run {srm.id} is not claimed by {worker.name}")


def results_recorded(session: Session, srm: SweepRunModel) -> int:
    return session.scalar(
        select(func.count())
        .select_from(TranscriptionResult)
        .where(TranscriptionResult.sweep_run_model_id == srm.id)
    )


def start_model_run(
    session: Session, srm: SweepRunModel, worker: Worker, body: ModelRunStart
) -> SweepRunModel:
    _require_holder(srm, worker)
    if srm.status not in ACTIVE:
        raise Conflict(f"model run {srm.id} is {srm.status}")
    runtime_changed = (
        srm.runtime_fingerprint is not None and srm.runtime_fingerprint != body.runtime_fingerprint
    )
    if runtime_changed and results_recorded(session, srm):
        raise Conflict(
            "runtime differs from the one this model run started with "
            f"(recorded {srm.runtime.get('crispasr_version')!r}); results would not be "
            "comparable, so create a new sweep instead of resuming this one"
        )
    now = utcnow()
    srm.runtime_fingerprint = body.runtime_fingerprint
    srm.runtime = body.runtime
    srm.status = ModelRunStatus.RUNNING
    srm.started_at = srm.started_at or now
    srm.lease_expires_at = now + timedelta(seconds=body.lease_seconds)
    session.flush()
    return srm


def pending(session: Session, srm: SweepRunModel, limit: int) -> PendingBatch:
    run = srm.run
    batch = PendingBatch(sweep_status=run.status, model_status=srm.status, segments=[])
    if run.status != SweepStatus.RUNNING or srm.status != ModelRunStatus.RUNNING:
        return batch
    result = aliased(TranscriptionResult)
    rows = session.execute(
        select(
            Segment.id,
            CorpusSource.logical_key,
            Segment.relative_path,
            Segment.sha256,
            Segment.duration_ms,
        )
        .select_from(SweepRunSegment)
        .join(Segment, Segment.id == SweepRunSegment.segment_id)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .outerjoin(
            result,
            and_(result.sweep_run_model_id == srm.id, result.segment_id == Segment.id),
        )
        .where(
            SweepRunSegment.run_id == run.id,
            or_(
                result.id.is_(None),
                and_(result.status == ResultStatus.ERROR, result.attempt < srm.attempts),
            ),
        )
        .order_by(SweepRunSegment.ordinal)
        .limit(limit)
    )
    batch.segments = [
        PendingSegment(
            segment_id=r[0], source_key=r[1], relative_path=r[2], sha256=r[3], duration_ms=r[4]
        )
        for r in rows
    ]
    return batch


def record_result(
    session: Session, srm: SweepRunModel, worker: Worker, body: ResultPost
) -> ResultAck:
    _require_holder(srm, worker)
    if srm.status != ModelRunStatus.RUNNING:
        raise Conflict(f"model run {srm.id} is {srm.status}")
    in_selection = session.scalar(
        select(func.count())
        .select_from(SweepRunSegment)
        .where(SweepRunSegment.run_id == srm.run_id, SweepRunSegment.segment_id == body.segment_id)
    )
    if not in_selection:
        raise Invalid(f"segment {body.segment_id} is not part of sweep {srm.run_id}")

    existing = session.scalar(
        select(TranscriptionResult)
        .where(
            TranscriptionResult.sweep_run_model_id == srm.id,
            TranscriptionResult.segment_id == body.segment_id,
        )
        .with_for_update()
    )
    replaced_error = False
    if existing is not None:
        if existing.status != ResultStatus.ERROR or existing.attempt >= srm.attempts:
            raise Conflict(
                f"segment {body.segment_id} already has a {existing.status} result for this "
                "model run"
            )
        srm.segments_error -= 1
        srm.audio_ms_total -= existing.audio_ms or 0
        srm.inference_ms_total -= existing.inference_ms or 0
        session.delete(existing)
        session.flush()
        replaced_error = True

    values = body.model_dump(exclude={"lease_seconds"})
    if body.status != ResultStatus.SUCCESS:
        values["text"] = values["text"] or ("" if body.status == ResultStatus.ABSTAINED else None)
    quality = compute_flags(
        values["text"],
        audio_ms=body.audio_ms,
        reported_language=body.language,
        expected_language=request_params(srm.run, srm.model).get("language"),
    ).as_dict()
    session.add(
        TranscriptionResult(
            **values, quality=quality, sweep_run_model_id=srm.id, attempt=srm.attempts
        )
    )
    counter = {
        ResultStatus.SUCCESS: "segments_completed",
        ResultStatus.ABSTAINED: "segments_abstained",
        ResultStatus.ERROR: "segments_error",
    }[body.status]
    setattr(srm, counter, getattr(srm, counter) + 1)
    srm.audio_ms_total += body.audio_ms or 0
    srm.inference_ms_total += body.inference_ms or 0
    srm.lease_expires_at = utcnow() + timedelta(seconds=body.lease_seconds)
    session.flush()
    # Keep the review workbench's derived agreement current (ADR-016).
    refresh_agreement(session, [body.segment_id])
    return ResultAck(
        replaced_error=replaced_error,
        segments_recorded=srm.segments_completed + srm.segments_abstained + srm.segments_error,
        segments_total=srm.segments_total,
    )


def _settle_run(session: Session, run: SweepRun) -> None:
    statuses = list(
        session.scalars(select(SweepRunModel.status).where(SweepRunModel.run_id == run.id))
    )
    terminal = (ModelRunStatus.COMPLETED, ModelRunStatus.FAILED)
    if run.status in (SweepStatus.QUEUED, SweepStatus.RUNNING) and all(
        s in terminal for s in statuses
    ):
        all_done = all(s == ModelRunStatus.COMPLETED for s in statuses)
        run.status = SweepStatus.COMPLETED if all_done else SweepStatus.PARTIAL
        run.completed_at = utcnow()


def finish_model_run(
    session: Session, srm: SweepRunModel, worker: Worker, body: ModelRunFinish
) -> SweepRunModel:
    _require_holder(srm, worker)
    if srm.status not in ACTIVE:
        raise Conflict(f"model run {srm.id} is {srm.status}")
    if body.status == ModelRunStatus.COMPLETED:
        missing = len(pending_all_ids(session, srm))
        if missing:
            raise Conflict(f"coverage incomplete: {missing} selected segments have no result")
    now = utcnow()
    srm.status = body.status
    srm.last_error = body.error_message
    srm.completed_at = now
    srm.lease_expires_at = None
    session.flush()
    _settle_run(session, srm.run)
    session.flush()
    return srm


def pending_all_ids(session: Session, srm: SweepRunModel) -> list[int]:
    result = aliased(TranscriptionResult)
    return list(
        session.scalars(
            select(SweepRunSegment.segment_id)
            .outerjoin(
                result,
                and_(
                    result.sweep_run_model_id == srm.id,
                    result.segment_id == SweepRunSegment.segment_id,
                ),
            )
            .where(SweepRunSegment.run_id == srm.run_id, result.id.is_(None))
        )
    )


def release_model_run(session: Session, srm: SweepRunModel, worker: Worker, reason: str) -> None:
    """Hand an active model run back to the queue (pause, shutdown, crash recovery)."""
    _require_holder(srm, worker)
    if srm.status in ACTIVE:
        srm.status = ModelRunStatus.QUEUED
        srm.claimed_by = None
        srm.lease_expires_at = None
        srm.last_error = reason
        session.flush()


# --- operator controls ---------------------------------------------------------------------


def set_sweep_status(session: Session, run: SweepRun, action: str) -> SweepRun:
    transitions = {
        "pause": ({SweepStatus.QUEUED, SweepStatus.RUNNING}, SweepStatus.PAUSED),
        "resume": ({SweepStatus.PAUSED}, SweepStatus.QUEUED),
        "cancel": (
            {SweepStatus.QUEUED, SweepStatus.RUNNING, SweepStatus.PAUSED},
            SweepStatus.CANCELLED,
        ),
    }
    allowed, target = transitions[action]
    if run.status not in allowed:
        raise Conflict(f"cannot {action} a sweep that is {run.status}")
    run.status = target
    if target == SweepStatus.CANCELLED:
        run.completed_at = utcnow()
    session.flush()
    return run


def retry_model(session: Session, run: SweepRun, logical_name: str | None) -> list[str]:
    """Re-queue failed model runs (or one named run) so their errors are retried."""
    srms = session.scalars(select(SweepRunModel).where(SweepRunModel.run_id == run.id))
    retried = []
    for srm in srms:
        if logical_name and srm.model.logical_name != logical_name:
            continue
        if srm.status == ModelRunStatus.FAILED or (
            logical_name and srm.status == ModelRunStatus.COMPLETED
        ):
            srm.status = ModelRunStatus.RETRYING
            srm.completed_at = None
            srm.claimed_by = None
            retried.append(srm.model.logical_name)
    if not retried:
        raise Conflict("nothing to retry")
    if run.status in (SweepStatus.COMPLETED, SweepStatus.PARTIAL):
        run.status = SweepStatus.QUEUED
        run.completed_at = None
    session.flush()
    return retried


# --- inspection --------------------------------------------------------------------------


def transcription_read(tr: TranscriptionResult) -> TranscriptionRead:
    srm = tr.sweep_run_model
    return TranscriptionRead(
        id=tr.id,
        sweep_id=srm.run_id,
        model=srm.model.logical_name,
        architecture_family=srm.model.architecture_family,
        segment_id=tr.segment_id,
        relative_path=tr.segment.relative_path,
        status=tr.status,
        text=tr.text,
        language=tr.language,
        audio_ms=tr.audio_ms,
        inference_ms=tr.inference_ms,
        has_word_timestamps=tr.has_word_timestamps,
        has_token_confidence=tr.has_token_confidence,
        mean_token_confidence=tr.mean_token_confidence,
        artifact_uri=tr.artifact_uri,
        error_type=tr.error_type,
        error_message=tr.error_message,
        attempt=tr.attempt,
        created_at=tr.created_at,
    )


def segment_results(session: Session, segment_id: int) -> list[TranscriptionRead]:
    rows = session.scalars(
        select(TranscriptionResult)
        .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
        .where(TranscriptionResult.segment_id == segment_id)
        .order_by(SweepRunModel.run_id, SweepRunModel.execution_order)
    )
    return [transcription_read(r) for r in rows]


def sweep_transcripts(
    session: Session, run: SweepRun, limit: int, offset: int
) -> list[dict[str, Any]]:
    """Segments of a sweep with every model's hypothesis, in selection order."""
    segments = list(
        session.execute(
            select(
                Segment.id,
                Segment.relative_path,
                Segment.capture_start_utc,
                Segment.duration_ms,
                Segment.channel,
            )
            .join(SweepRunSegment, SweepRunSegment.segment_id == Segment.id)
            .where(SweepRunSegment.run_id == run.id)
            .order_by(SweepRunSegment.ordinal)
            .limit(limit)
            .offset(offset)
        )
    )
    ids = [s[0] for s in segments]
    by_segment: dict[int, dict[str, Any]] = {sid: {} for sid in ids}
    if ids:
        rows = session.execute(
            select(
                TranscriptionResult.segment_id,
                Model.logical_name,
                TranscriptionResult.status,
                TranscriptionResult.text,
            )
            .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
            .join(Model, Model.id == SweepRunModel.model_id)
            .where(SweepRunModel.run_id == run.id, TranscriptionResult.segment_id.in_(ids))
        )
        for segment_id, model_name, status, text_value in rows:
            by_segment[segment_id][model_name] = {"status": status, "text": text_value}
    return [
        {
            "segment_id": s[0],
            "relative_path": s[1],
            "capture_start_utc": s[2],
            "duration_ms": s[3],
            "channel": s[4],
            "results": by_segment[s[0]],
        }
        for s in segments
    ]


def sweep_report(session: Session, run: SweepRun) -> SweepReport:
    tr = TranscriptionResult

    def count_of(status: ResultStatus):
        return func.count().filter(tr.status == status)

    language_by_model = {
        m["logical_name"]: m["request_params"].get("language")
        for m in run.effective_config["models"]
    }
    rows = session.execute(
        select(
            SweepRunModel,
            func.count(tr.id),
            count_of(ResultStatus.SUCCESS),
            count_of(ResultStatus.ABSTAINED),
            count_of(ResultStatus.ERROR),
            func.avg(tr.inference_ms),
            func.avg(case((tr.has_word_timestamps, 1.0), else_=0.0)).filter(
                tr.status == ResultStatus.SUCCESS
            ),
            func.avg(tr.mean_token_confidence),
            func.avg(func.length(tr.text)).filter(tr.status == ResultStatus.SUCCESS),
        )
        .outerjoin(tr, tr.sweep_run_model_id == SweepRunModel.id)
        .where(SweepRunModel.run_id == run.id)
        .group_by(SweepRunModel.id)
        .order_by(SweepRunModel.execution_order)
    )
    flag_rows = session.execute(
        select(
            tr.sweep_run_model_id,
            func.jsonb_array_elements_text(tr.quality["flags"]).label("flag"),
        )
        .join(SweepRunModel, SweepRunModel.id == tr.sweep_run_model_id)
        .where(SweepRunModel.run_id == run.id)
    ).all()
    flags_by_model: dict[int, dict[str, int]] = {}
    for srm_id, flag in flag_rows:
        counts = flags_by_model.setdefault(srm_id, {})
        counts[flag] = counts.get(flag, 0) + 1
    models = []
    for srm, n, ok, abstained, err, mean_ms, wts, conf, chars in rows:
        expected = language_by_model.get(srm.model.logical_name)
        drift = 0
        if expected:
            drift = session.scalar(
                select(func.count()).where(
                    tr.sweep_run_model_id == srm.id,
                    tr.status == ResultStatus.SUCCESS,
                    tr.language.is_not(None),
                    func.lower(tr.language) != expected.lower(),
                )
            )
        models.append(
            SweepReportModel(
                logical_name=srm.model.logical_name,
                architecture_family=srm.model.architecture_family,
                status=srm.status,
                results=n,
                success=ok,
                abstained=abstained,
                error=err,
                abstain_rate=round(abstained / n, 4) if n else None,
                error_rate=round(err / n, 4) if n else None,
                real_time_factor=_rtf(srm),
                mean_inference_ms=round(float(mean_ms), 1) if mean_ms is not None else None,
                word_timestamp_rate=round(float(wts), 4) if wts is not None else None,
                mean_token_confidence=round(float(conf), 4) if conf is not None else None,
                language_drift=drift,
                mean_chars=round(float(chars), 1) if chars is not None else None,
                flags=dict(sorted(flags_by_model.get(srm.id, {}).items(), key=lambda kv: -kv[1])),
            )
        )
    return SweepReport(sweep_id=run.id, segments_total=run.segments_total, models=models)


def reflag_results(session: Session, batch_size: int = 2000) -> ReflagResult:
    """Recompute quality flags for results computed with an older flags version."""
    version = TranscriptionResult.quality["version"].astext.cast(Integer)
    total = 0
    while True:
        rows = list(
            session.scalars(
                select(TranscriptionResult)
                .where(version.is_distinct_from(FLAGS_VERSION))
                .limit(batch_size)
            )
        )
        if not rows:
            break
        for result in rows:
            srm = result.sweep_run_model
            result.quality = compute_flags(
                result.text,
                audio_ms=result.audio_ms,
                reported_language=result.language,
                expected_language=request_params(srm.run, srm.model).get("language"),
            ).as_dict()
        session.flush()
        total += len(rows)
    return ReflagResult(flags_version=FLAGS_VERSION, reflagged=total)


# --- hardware-profile qualification (ADR-021) ---------------------------------------------


def platform_qualification_read(
    row: ModelPlatformQualification, model: Model
) -> PlatformQualificationRead:
    return PlatformQualificationRead(
        model=model.logical_name,
        hardware_profile=row.hardware_profile,
        architecture_family=model.architecture_family,
        crisp_backend=model.crisp_backend,
        state=row.state,
        metrics=row.metrics or {},
        notes=row.notes,
        recorded_by=row.recorded_by,
        updated_at=row.updated_at,
    )


def record_platform_qualification(
    session: Session, logical_name: str, profile: str, body: PlatformQualificationWrite
) -> PlatformQualificationRead:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", profile):
        raise Invalid(f"invalid hardware profile id: {profile!r}")
    model = load_model(session, logical_name)
    row = session.get(ModelPlatformQualification, (model.id, profile))
    if row is None:
        row = ModelPlatformQualification(model_id=model.id, hardware_profile=profile)
        session.add(row)
    row.state = body.state.value
    row.metrics = body.metrics
    row.notes = body.notes
    row.recorded_by = body.recorded_by
    row.updated_at = utcnow()
    session.flush()
    return platform_qualification_read(row, model)


def list_platform_qualifications(
    session: Session, profile: str | None = None
) -> list[PlatformQualificationRead]:
    query = select(ModelPlatformQualification, Model).join(
        Model, Model.id == ModelPlatformQualification.model_id
    )
    if profile:
        query = query.where(ModelPlatformQualification.hardware_profile == profile)
    rows = session.execute(
        query.order_by(ModelPlatformQualification.hardware_profile, Model.logical_name)
    )
    return [platform_qualification_read(row, model) for row, model in rows]


# --- word timings backfill (agreement v2) ----------------------------------------------------


def word_backfill_candidates(
    session: Session, limit: int, after: str | None = None
) -> list[WordBackfillItem]:
    """Results whose model reported word timestamps but whose timings are not stored yet."""
    query = select(TranscriptionResult.id, TranscriptionResult.artifact_uri).where(
        TranscriptionResult.has_word_timestamps.is_(True),
        TranscriptionResult.words.is_(None),
        TranscriptionResult.artifact_uri.is_not(None),
    )
    if after:
        query = query.where(TranscriptionResult.id > after)
    rows = session.execute(query.order_by(TranscriptionResult.id).limit(limit))
    return [WordBackfillItem(id=rid, artifact_uri=uri) for rid, uri in rows]


def store_word_backfill(session: Session, body: list[WordBackfillPost]) -> WordBackfillAck:
    """Attach word timings read from raw artifacts; recompute agreement for those segments."""
    by_id = {item.id: item for item in body}
    results = list(
        session.scalars(select(TranscriptionResult).where(TranscriptionResult.id.in_(by_id)))
    )
    segments = set()
    for result in results:
        words = by_id[result.id].words
        # [] marks "looked, none usable" so the result is not offered again.
        result.words = [list(w) for w in words] if words else []
        segments.add(result.segment_id)
    session.flush()
    refreshed = refresh_agreement(session, sorted(segments)) if segments else 0
    return WordBackfillAck(updated=len(results), segments_refreshed=refreshed)
