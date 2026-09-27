"""Review workbench service (Phase 5A; ADR-016, ADR-017).

Everything the UI shows is computed here, never in React: filtering,
sorting, pagination, search, agreement, airport resolution, neighbors.

Human annotations are append-only versions on threads (a whole segment, or a
bounded span). Model hypotheses are read-only. Gold is a deliberate human
assertion and is never produced in batch; benchmark sources never receive
training labels.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, Text, and_, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, aliased

from aerochorus.api.agreement import near_threshold
from aerochorus.api.airports import (
    channel_service,
    profile_view,
    resolve_airport,
    station_map,
    stations_for,
)
from aerochorus.api.sweeps import Conflict, Invalid, NotFound
from aerochorus.atc.agreement import AGREEMENT_VERSION
from aerochorus.atc.align import sequence_similarity
from aerochorus.atc.normalize import evidence_tokens, normalize_for_evidence
from aerochorus.db.models import (
    AnnotationThread,
    AnnotationVersion,
    CorpusSource,
    Model,
    ReviewSample,
    Segment,
    SegmentAgreement,
    SweepRunModel,
    TranscriptionResult,
)
from aerochorus.review_contracts import (
    AnnotationSave,
    AnnotationThreadView,
    AnnotationVersionView,
    BatchAction,
    BatchOutcome,
    BatchRequest,
    HypothesisView,
    NeighborView,
    ReviewFilters,
    ReviewPage,
    ReviewQuery,
    ReviewRow,
    ReviewStatus,
    SampleRequest,
    SampleView,
    SavedView,
    SearchScope,
    SegmentReview,
    SortKey,
    SourceRole,
    TextOrigin,
    TrainingLabel,
)

# Representative-text sources that count as independent agreement (atc/agreement.py).
SILVER_SOURCES = ("exact", "near")

TRAINING_LABELS_FORBIDDEN_ON_BENCHMARK = {
    TrainingLabel.CANDIDATE,
    TrainingLabel.SILVER,
    TrainingLabel.GOLD,
}

SAVED_VIEWS = [
    SavedView(
        key="exact-2-families",
        name="2+ exact families",
        description="At least two independent architecture families produced the same text",
        filters=ReviewFilters(min_exact_families=2),
    ),
    SavedView(
        key="exact-3-providers",
        name="3+ exact providers",
        description="At least three models produced the same text (families may repeat)",
        filters=ReviewFilters(min_exact_providers=3),
    ),
    SavedView(
        key="near-3-families",
        name="3+ near families",
        description="Three or more families within the near-match similarity threshold",
        filters=ReviewFilters(min_near_families=3),
    ),
    SavedView(
        key="high-agreement-unreviewed",
        name="High agreement / unreviewed",
        description="2+ exact families, not yet looked at by a human",
        filters=ReviewFilters(min_exact_families=2, review_status=[ReviewStatus.UNREVIEWED]),
    ),
    SavedView(
        key="human-corrected",
        name="Human corrected",
        description="A human edited the transcript",
        filters=ReviewFilters(review_status=[ReviewStatus.CORRECTED]),
    ),
    SavedView(
        key="silver-candidates",
        name="Silver candidates",
        description="Nominated as training candidate or silver",
        filters=ReviewFilters(training_label=[TrainingLabel.CANDIDATE, TrainingLabel.SILVER]),
    ),
    SavedView(
        key="gold",
        name="Gold verified",
        description="Human-verified gold",
        filters=ReviewFilters(training_label=[TrainingLabel.GOLD]),
    ),
    SavedView(
        key="disagreement",
        name="Disagreement / needs review",
        description="2+ models produced words but no two families agree, exactly or nearly",
        filters=ReviewFilters(min_success=2, max_exact_families=1, max_near_families=1),
    ),
    SavedView(
        key="errors-abstentions",
        name="ASR errors or abstentions",
        description="At least one model failed or produced nothing",
        filters=ReviewFilters(has_error_or_abstention=True),
    ),
]


# --- query building ------------------------------------------------------------------


def _text_array(values: list[str]):
    return cast(values, ARRAY(Text))


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _annotation_ids(pattern: str, scope: str | None = None) -> Select:
    """Segments whose *current* human annotation text matches (trigram-indexed)."""
    query = (
        select(AnnotationThread.segment_id)
        .join(AnnotationVersion, AnnotationVersion.id == AnnotationThread.current_version_id)
        .where(AnnotationVersion.text.ilike(pattern))
    )
    return query.where(AnnotationThread.scope == scope) if scope else query


def _hypothesis_ids(pattern: str | None, model: str | None = None) -> Select:
    """Segments with a model hypothesis matching (any result, any sweep)."""
    query = select(TranscriptionResult.segment_id)
    if pattern:
        query = query.where(TranscriptionResult.text.ilike(pattern))
    if model:
        query = (
            query.join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
            .join(Model, Model.id == SweepRunModel.model_id)
            .where(Model.logical_name == model)
        )
    return query


def _consensus_ids(pattern: str) -> Select:
    return select(SegmentAgreement.segment_id).where(
        SegmentAgreement.representative_text.ilike(pattern)
    )


class _Aliases:
    def __init__(self) -> None:
        self.thread = aliased(AnnotationThread, name="seg_thread")
        self.version = aliased(AnnotationVersion, name="seg_version")


def _base(session: Session, filters: ReviewFilters) -> tuple[Select, _Aliases]:
    a = _Aliases()
    span_thread = aliased(AnnotationThread)
    span_count = (
        select(func.count())
        .select_from(span_thread)
        .where(span_thread.segment_id == Segment.id, span_thread.scope == "span")
        .scalar_subquery()
    )
    query = (
        select(
            Segment,
            CorpusSource.logical_key,
            SegmentAgreement,
            a.version,
            span_count.label("span_count"),
        )
        .select_from(Segment)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .outerjoin(SegmentAgreement, SegmentAgreement.segment_id == Segment.id)
        .outerjoin(a.thread, and_(a.thread.segment_id == Segment.id, a.thread.scope == "segment"))
        .outerjoin(a.version, a.version.id == a.thread.current_version_id)
    )
    return _apply_filters(session, query, filters, a), a


def _apply_filters(session: Session, query: Select, f: ReviewFilters, a: _Aliases) -> Select:
    sa = SegmentAgreement
    if f.source_keys:
        query = query.where(CorpusSource.logical_key.in_(f.source_keys))
    elif not f.include_benchmark:
        query = query.where(CorpusSource.role == SourceRole.CORPUS)
    if f.airport:
        query = query.where(Segment.station.in_(stations_for(session, f.airport)))
    if f.channels:
        query = query.where(Segment.channel.in_(f.channels))
    if f.utc_from:
        query = query.where(Segment.capture_start_utc >= f.utc_from)
    if f.utc_to:
        query = query.where(Segment.capture_start_utc < f.utc_to)
    if f.models:
        query = query.where(sa.models.has_any(_text_array(f.models)))
    if f.families:
        query = query.where(sa.families.has_any(_text_array(f.families)))
    if f.min_models:
        query = query.where(sa.results_count >= f.min_models)
    if f.min_success is not None:
        query = query.where(sa.success_count >= f.min_success)
    for value, column, op in (
        (f.min_exact_providers, sa.best_exact_provider_count, "ge"),
        (f.min_exact_families, sa.best_exact_family_count, "ge"),
        (f.min_near_families, sa.best_near_family_count, "ge"),
        (f.min_near_similarity, sa.best_near_similarity, "ge"),
        (f.max_exact_families, sa.best_exact_family_count, "le"),
        (f.max_near_families, sa.best_near_family_count, "le"),
    ):
        if value is not None:
            query = query.where(column >= value if op == "ge" else column <= value)
    if f.review_status:
        query = query.where(
            func.coalesce(a.version.review_status, ReviewStatus.UNREVIEWED.value).in_(
                [s.value for s in f.review_status]
            )
        )
    if f.training_label:
        query = query.where(
            func.coalesce(a.version.training_label, TrainingLabel.NONE.value).in_(
                [label.value for label in f.training_label]
            )
        )
    if f.has_error is not None:
        query = query.where(sa.error_count > 0 if f.has_error else sa.error_count == 0)
    if f.has_abstention is not None:
        query = query.where(sa.abstained_count > 0 if f.has_abstention else sa.abstained_count == 0)
    if f.has_error_or_abstention is not None:
        problem = or_(sa.error_count > 0, sa.abstained_count > 0)
        query = query.where(problem if f.has_error_or_abstention else ~problem)
    if f.flags:
        query = query.where(sa.flags.has_any(_text_array(f.flags)))
    if f.span_labels:
        thread = aliased(AnnotationThread)
        version = aliased(AnnotationVersion)
        query = query.where(
            exists().where(
                thread.segment_id == Segment.id,
                thread.scope == "span",
                version.id == thread.current_version_id,
                version.training_label.in_([label.value for label in f.span_labels]),
            )
        )
    if f.sample_id is not None:
        sample = session.get(ReviewSample, f.sample_id)
        if sample is None:
            raise NotFound(f"unknown sample: {f.sample_id}")
        query = query.where(Segment.id.in_(sample.segment_ids or [-1]))
    q = (f.q or "").strip()
    if q or (f.scope == SearchScope.MODEL and f.scope_model):
        # IN (union of id sets) lets each branch use its own trigram index.
        pattern = _like(q) if q else None
        if f.scope == SearchScope.ANY:
            ids = _hypothesis_ids(pattern).union(_consensus_ids(pattern), _annotation_ids(pattern))
        elif f.scope == SearchScope.HUMAN:
            ids = _annotation_ids(pattern)
        elif f.scope == SearchScope.CONSENSUS:
            ids = _consensus_ids(pattern)
        elif f.scope == SearchScope.HYPOTHESES:
            ids = _hypothesis_ids(pattern)
        else:
            if not f.scope_model:
                raise Invalid("scope 'model' needs scope_model")
            ids = _hypothesis_ids(pattern, f.scope_model)
        query = query.where(Segment.id.in_(ids))
    return query


SORT_COLUMNS = {
    SortKey.UTC: Segment.capture_start_utc,
    SortKey.DURATION: Segment.duration_ms,
    SortKey.RESULTS: SegmentAgreement.results_count,
    SortKey.EXACT_PROVIDERS: SegmentAgreement.best_exact_provider_count,
    SortKey.EXACT_FAMILIES: SegmentAgreement.best_exact_family_count,
    SortKey.NEAR_FAMILIES: SegmentAgreement.best_near_family_count,
    SortKey.NEAR_SIMILARITY: SegmentAgreement.best_near_similarity,
    SortKey.CHANNEL: Segment.channel,
}


def query_segments(session: Session, body: ReviewQuery) -> ReviewPage:
    query, _ = _base(session, body.filters)
    ids = query.with_only_columns(Segment.id).order_by(None)
    total = session.scalar(select(func.count()).select_from(ids.subquery()))
    column = SORT_COLUMNS[body.sort]
    order = column.desc().nulls_last() if body.descending else column.asc().nulls_last()
    tiebreak = Segment.id.desc() if body.descending else Segment.id.asc()
    rows = session.execute(query.order_by(order, tiebreak).offset(body.offset).limit(body.limit))
    stations = station_map(session)
    return ReviewPage(
        total=total,
        offset=body.offset,
        limit=body.limit,
        rows=[
            _row(seg, key, sa, version, spans, stations) for seg, key, sa, version, spans in rows
        ],
        near_threshold=near_threshold(),
        agreement_version=AGREEMENT_VERSION,
    )


def _row(seg, source_key, sa, version, span_count, stations) -> ReviewRow:
    return ReviewRow(
        segment_id=seg.id,
        source_key=source_key,
        relative_path=seg.relative_path,
        capture_start_utc=seg.capture_start_utc,
        airport=resolve_airport(seg.station, stations),
        station=seg.station,
        channel=seg.channel,
        frequency_hz=seg.frequency_hz,
        duration_ms=seg.duration_ms,
        results_count=sa.results_count if sa else 0,
        error_count=sa.error_count if sa else 0,
        abstained_count=sa.abstained_count if sa else 0,
        best_exact_provider_count=sa.best_exact_provider_count if sa else 0,
        best_exact_family_count=sa.best_exact_family_count if sa else 0,
        best_near_family_count=sa.best_near_family_count if sa else 0,
        best_near_similarity=sa.best_near_similarity if sa else None,
        representative_text=sa.representative_text if sa else None,
        representative_source=sa.representative_source if sa else None,
        review_status=version.review_status if version else ReviewStatus.UNREVIEWED,
        training_label=version.training_label if version else TrainingLabel.NONE,
        human_text=version.text if version and version.text_origin == "human" else None,
        flags=sorted((sa.flags or {}).keys()) if sa else [],
        span_count=span_count or 0,
    )


def filtered_ids(session: Session, filters: ReviewFilters) -> Select:
    query, _ = _base(session, filters)
    return query.with_only_columns(Segment.id).order_by(None)


# --- detail -------------------------------------------------------------------------------


def _version_view(v: AnnotationVersion) -> AnnotationVersionView:
    return AnnotationVersionView(
        id=v.id,
        version=v.version,
        start_ms=v.start_ms,
        end_ms=v.end_ms,
        text=v.text,
        text_origin=v.text_origin,
        review_status=v.review_status,
        training_label=v.training_label,
        reason_tags=v.reason_tags or [],
        notes=v.notes,
        annotator=v.annotator,
        action=v.action,
        basis=v.basis or {},
        created_at=v.created_at,
    )


def thread_views(session: Session, segment_id: int) -> list[AnnotationThreadView]:
    threads = list(
        session.scalars(
            select(AnnotationThread)
            .where(AnnotationThread.segment_id == segment_id)
            .order_by(AnnotationThread.id)
        )
    )
    views = []
    for thread in threads:
        versions = list(
            session.scalars(
                select(AnnotationVersion)
                .where(AnnotationVersion.thread_id == thread.id)
                .order_by(AnnotationVersion.version.desc())
            )
        )
        current = next((v for v in versions if v.id == thread.current_version_id), None)
        views.append(
            AnnotationThreadView(
                thread_id=thread.id,
                scope=thread.scope,
                current=_version_view(current) if current else None,
                history=[_version_view(v) for v in versions],
            )
        )
    return views


def _hypotheses(session: Session, segment: Segment, sa: SegmentAgreement | None):
    rows = session.execute(
        select(TranscriptionResult, SweepRunModel, Model)
        .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
        .join(Model, Model.id == SweepRunModel.model_id)
        .where(TranscriptionResult.segment_id == segment.id)
        .order_by(Model.logical_name, TranscriptionResult.created_at.desc())
    ).all()
    groups = {g["normalized"]: i for i, g in enumerate(sa.exact_groups)} if sa else {}
    representative = (
        evidence_tokens(sa.representative_text) if sa and sa.representative_text else None
    )
    seen: set[int] = set()
    out = []
    for result, srm, model in rows:
        superseded = model.id in seen
        seen.add(model.id)
        normalized = normalize_for_evidence(result.text)
        similarity = None
        if representative is not None and result.status == "success" and normalized:
            similarity = round(sequence_similarity(evidence_tokens(result.text), representative), 4)
        out.append(
            HypothesisView(
                result_id=str(result.id),
                model=model.logical_name,
                architecture_family=model.architecture_family,
                status=result.status,
                text=result.text,
                language=result.language,
                exact_group=None if superseded else groups.get(normalized),
                similarity_to_representative=similarity,
                model_confidence=result.mean_token_confidence,
                flags=(result.quality or {}).get("flags", []),
                sweep_id=srm.run_id,
                attempt=result.attempt,
                created_at=result.created_at,
                superseded=superseded,
                provenance={
                    "model_sha256": model.model_sha256,
                    "crisp_backend": model.crisp_backend,
                    "quantization": model.quantization,
                    "crispasr_version": (srm.runtime or {}).get("crispasr_version"),
                    "runtime_artifact": (srm.runtime or {}).get("artifact"),
                    "artifact_uri": result.artifact_uri,
                    "inference_ms": result.inference_ms,
                    "audio_sha256": result.audio_sha256,
                    "error_type": result.error_type,
                    "error_message": result.error_message,
                },
            )
        )
    return out


def _neighbors(session: Session, segment: Segment, window_seconds: int = 60) -> list[NeighborView]:
    base = select(Segment, SegmentAgreement.representative_text).outerjoin(
        SegmentAgreement, SegmentAgreement.segment_id == Segment.id
    )
    same_channel = base.where(
        Segment.source_id == segment.source_id,
        Segment.channel == segment.channel,
        Segment.id != segment.id,
    )
    out: list[NeighborView] = []

    def view(seg: Segment, text: str | None, relation: str) -> NeighborView:
        offset = None
        if seg.capture_start_utc and segment.capture_start_utc:
            offset = round((seg.capture_start_utc - segment.capture_start_utc).total_seconds(), 1)
        return NeighborView(
            segment_id=seg.id,
            offset_seconds=offset,
            channel=seg.channel,
            capture_start_utc=seg.capture_start_utc,
            duration_ms=seg.duration_ms,
            preview=text,
            relation=relation,
        )

    if segment.capture_start_utc is not None:
        t = segment.capture_start_utc
        previous = session.execute(
            same_channel.where(Segment.capture_start_utc < t)
            .order_by(Segment.capture_start_utc.desc())
            .limit(1)
        ).first()
        following = session.execute(
            same_channel.where(Segment.capture_start_utc > t)
            .order_by(Segment.capture_start_utc.asc())
            .limit(1)
        ).first()
        if previous:
            out.append(view(previous[0], previous[1], "previous"))
        if following:
            out.append(view(following[0], following[1], "next"))
        taken = {n.segment_id for n in out} | {segment.id}
        nearby = session.execute(
            base.where(
                Segment.source_id == segment.source_id,
                Segment.capture_start_utc.between(
                    t - timedelta(seconds=window_seconds), t + timedelta(seconds=window_seconds)
                ),
                Segment.id.not_in(taken),
            )
            .order_by(func.abs(func.extract("epoch", Segment.capture_start_utc - literal(t))))
            .limit(6)
        ).all()
        out += [view(s, text, "nearby") for s, text in nearby]
        out.sort(key=lambda n: (n.offset_seconds is None, n.offset_seconds or 0))
    else:  # no UTC: fall back to path order within the channel
        for relation, cmp, order in (
            (
                "previous",
                Segment.relative_path < segment.relative_path,
                Segment.relative_path.desc(),
            ),
            ("next", Segment.relative_path > segment.relative_path, Segment.relative_path.asc()),
        ):
            row = session.execute(same_channel.where(cmp).order_by(order).limit(1)).first()
            if row:
                out.append(view(row[0], row[1], relation))
    return out


def segment_review(session: Session, segment_id: int) -> SegmentReview:
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise NotFound(f"unknown segment: {segment_id}")
    source = session.get(CorpusSource, segment.source_id)
    sa = session.get(SegmentAgreement, segment_id)
    icao = resolve_airport(segment.station, station_map(session))
    profile = profile_view(session, icao) if icao else None
    zone_name = (
        profile.timezone if profile else (source.adapter_config or {}).get("filename_timezone")
    )
    local = None
    if segment.capture_start_utc and zone_name:
        local = segment.capture_start_utc.astimezone(ZoneInfo(zone_name)).isoformat()
    threads = thread_views(session, segment_id)
    return SegmentReview(
        segment_id=segment.id,
        source_key=source.logical_key,
        source_role=source.role,
        relative_path=segment.relative_path,
        capture_start_utc=segment.capture_start_utc,
        capture_local=local,
        local_timezone=zone_name,
        temporal_status=segment.temporal_status,
        airport=icao,
        station=segment.station,
        channel=segment.channel,
        frequency_hz=segment.frequency_hz,
        channel_service=channel_service(session, icao, segment.frequency_hz),
        duration_ms=segment.duration_ms,
        sha256=segment.sha256,
        agreement=(
            {
                c.name: getattr(sa, c.key)
                for c in SegmentAgreement.__table__.columns
                if c.name not in ("segment_id",)
            }
            if sa
            else None
        ),
        hypotheses=_hypotheses(session, segment, sa),
        segment_annotation=next((t for t in threads if t.scope == "segment"), None),
        span_annotations=[t for t in threads if t.scope == "span"],
        neighbors=_neighbors(session, segment),
        airport_profile=profile,
    )


# --- annotation writes ----------------------------------------------------------------------


def _append_version(
    session: Session,
    thread: AnnotationThread,
    *,
    action: str,
    text: str | None,
    text_origin: TextOrigin | None,
    review_status: ReviewStatus,
    training_label: TrainingLabel,
    start_ms: int | None = None,
    end_ms: int | None = None,
    reason_tags: list[str] | None = None,
    notes: str | None = None,
    annotator: str | None = None,
    basis: dict[str, Any] | None = None,
) -> AnnotationVersion:
    current = (
        session.get(AnnotationVersion, thread.current_version_id)
        if (thread.current_version_id)
        else None
    )
    version = AnnotationVersion(
        thread_id=thread.id,
        version=(current.version if current else 0) + 1,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        text_origin=text_origin.value if text_origin else None,
        review_status=review_status.value,
        training_label=training_label.value,
        reason_tags=reason_tags or [],
        notes=notes,
        annotator=annotator,
        action=action,
        basis=basis or {},
    )
    session.add(version)
    session.flush()
    thread.current_version_id = version.id
    session.flush()
    return version


def _segment_thread(session: Session, segment_id: int, *, create: bool) -> AnnotationThread | None:
    thread = session.scalar(
        select(AnnotationThread)
        .where(AnnotationThread.segment_id == segment_id, AnnotationThread.scope == "segment")
        .with_for_update()
    )
    if thread is None and create:
        thread = AnnotationThread(segment_id=segment_id, scope="segment")
        session.add(thread)
        session.flush()
    return thread


def save_annotation(
    session: Session, segment_id: int, body: AnnotationSave
) -> AnnotationThreadView:
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise NotFound(f"unknown segment: {segment_id}")
    source = session.get(CorpusSource, segment.source_id)
    label = TrainingLabel(body.training_label)
    if source.role == SourceRole.BENCHMARK and label in TRAINING_LABELS_FORBIDDEN_ON_BENCHMARK:
        raise Invalid(
            f"{source.logical_key} is a benchmark source: its segments can never become training "
            "data (it would contaminate evaluation)"
        )
    text = (body.text or "").strip() or None
    if label == TrainingLabel.GOLD:
        if not body.confirm_gold:
            raise Invalid("gold is a human assertion: confirm_gold must be true")
        if not text:
            raise Invalid("gold needs transcript text")
        if body.review_status == ReviewStatus.UNREVIEWED:
            raise Invalid("gold must be reviewed")
    if body.scope == "span" and segment.duration_ms and body.end_ms > segment.duration_ms + 50:
        raise Invalid(f"span ends after the segment ({segment.duration_ms} ms)")

    if body.thread_id is not None:
        thread = session.get(AnnotationThread, body.thread_id, with_for_update=True)
        if thread is None or thread.segment_id != segment_id or thread.scope != body.scope:
            raise NotFound(f"annotation thread {body.thread_id} does not belong to this segment")
    elif body.scope == "segment":
        thread = _segment_thread(session, segment_id, create=True)
    else:
        thread = AnnotationThread(segment_id=segment_id, scope="span")
        session.add(thread)
        session.flush()

    current = (
        session.get(AnnotationVersion, thread.current_version_id)
        if (thread.current_version_id)
        else None
    )
    current_number = current.version if current else 0
    if body.expected_version != current_number:
        raise Conflict(
            f"annotation changed since it was loaded (now version {current_number}, "
            f"editor had {body.expected_version}); reload before saving"
        )
    _append_version(
        session,
        thread,
        action="edit",
        text=text,
        text_origin=TextOrigin.HUMAN if text else None,
        review_status=ReviewStatus(body.review_status),
        training_label=label,
        start_ms=body.start_ms,
        end_ms=body.end_ms,
        reason_tags=body.reason_tags,
        notes=body.notes,
        annotator=body.annotator,
        basis=body.basis,
    )
    return next(t for t in thread_views(session, segment_id) if t.thread_id == thread.id)


def batch_nominate(session: Session, body: BatchRequest) -> BatchOutcome:
    ids = list(body.segment_ids)
    if body.sample_id is not None:
        sample = session.get(ReviewSample, body.sample_id)
        if sample is None:
            raise NotFound(f"unknown sample: {body.sample_id}")
        ids += sample.segment_ids
    ids = sorted(set(ids))
    if not ids:
        raise Invalid("no segments selected")

    rows = session.execute(
        select(Segment.id, CorpusSource.role, SegmentAgreement)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .outerjoin(SegmentAgreement, SegmentAgreement.segment_id == Segment.id)
        .where(Segment.id.in_(ids))
    ).all()
    found = {row[0] for row in rows}
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for _ in set(ids) - found:
        skip("unknown_segment")
    applied: list[int] = []
    for segment_id, role, sa in rows:
        if role == SourceRole.BENCHMARK:
            skip("benchmark_source")
            continue
        thread = _segment_thread(session, segment_id, create=body.action != BatchAction.CLEAR)
        current = (
            session.get(AnnotationVersion, thread.current_version_id)
            if thread and thread.current_version_id
            else None
        )
        current_label = TrainingLabel(current.training_label) if current else TrainingLabel.NONE
        if current_label == TrainingLabel.GOLD:
            skip("human_gold_unchanged")
            continue
        if current_label == TrainingLabel.REJECTED and body.action != BatchAction.CLEAR:
            skip("rejected_by_human")
            continue
        review_status = ReviewStatus(current.review_status) if current else ReviewStatus.UNREVIEWED
        if body.action == BatchAction.CLEAR:
            if current_label not in (TrainingLabel.CANDIDATE, TrainingLabel.SILVER):
                skip("not_nominated")
                continue
            _append_version(
                session,
                thread,
                action="batch_clear",
                text=current.text,
                text_origin=TextOrigin(current.text_origin) if current.text_origin else None,
                review_status=review_status,
                training_label=TrainingLabel.NONE,
                reason_tags=current.reason_tags,
                notes=body.notes or current.notes,
                annotator=body.annotator,
                basis={"previous_label": current_label.value},
            )
            applied.append(segment_id)
            continue
        # Prefer an existing human transcript; otherwise the agreement's representative text.
        if current and current.text and current.text_origin == TextOrigin.HUMAN:
            text, origin = current.text, TextOrigin.HUMAN
        elif sa and sa.representative_text:
            text, origin = sa.representative_text, TextOrigin.MODEL_CONSENSUS
        else:
            skip("no_consensus_text")
            continue
        target = (
            TrainingLabel.SILVER if body.action == BatchAction.SILVER else TrainingLabel.CANDIDATE
        )
        # Silver means agreement: 2+ families exactly or nearly. A lone model's text or a
        # medoid of disagreeing models can be a candidate, never silver.
        if (
            target == TrainingLabel.SILVER
            and origin == TextOrigin.MODEL_CONSENSUS
            and sa.representative_source not in SILVER_SOURCES
        ):
            skip("insufficient_agreement")
            continue
        if current_label == target:
            skip("already_" + target.value)
            continue
        _append_version(
            session,
            thread,
            action=f"batch_{target.value}",
            text=text,
            text_origin=origin,
            review_status=review_status,
            training_label=target,
            reason_tags=current.reason_tags if current else [],
            notes=body.notes or (current.notes if current else None),
            annotator=body.annotator,
            basis={
                "agreement_version": sa.version if sa else None,
                "best_exact_family_count": sa.best_exact_family_count if sa else None,
                "best_exact_provider_count": sa.best_exact_provider_count if sa else None,
                "best_near_family_count": sa.best_near_family_count if sa else None,
                "best_near_similarity": sa.best_near_similarity if sa else None,
                "representative_source": sa.representative_source if sa else None,
                "sample_id": body.sample_id,
            },
        )
        applied.append(segment_id)
    return BatchOutcome(applied=len(applied), skipped=skipped, segment_ids_applied=applied)


def create_sample(session: Session, body: SampleRequest) -> SampleView:
    """Deterministic sample: order the filtered set by md5(seed:id) and take n."""
    ids_query = filtered_ids(session, body.filters)
    total = session.scalar(select(func.count()).select_from(ids_query.subquery()))
    key = func.md5(func.concat(str(body.seed), ":", Segment.id))
    chosen = list(session.scalars(ids_query.order_by(key).limit(body.n)))
    filters = body.filters.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    sample = ReviewSample(
        name=body.name,
        filters=filters,
        filter_sha256=digest,
        seed=body.seed,
        n=body.n,
        total_matching=total,
        segment_ids=sorted(chosen),
    )
    session.add(sample)
    session.flush()
    return sample_view(sample)


def sample_view(sample: ReviewSample) -> SampleView:
    return SampleView(
        id=sample.id,
        name=sample.name,
        n=sample.n,
        seed=sample.seed,
        filters=sample.filters,
        filter_sha256=sample.filter_sha256,
        total_matching=sample.total_matching,
        segment_ids=sample.segment_ids,
        created_at=sample.created_at,
    )
