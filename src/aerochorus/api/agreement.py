"""Persisting per-segment agreement (derived, versioned; ADR-016).

Agreement is computed over the **latest result of each model** for a
segment, regardless of which sweep produced it. It is refreshed inline when a
result is recorded, and in bulk by ``refresh_stale`` (after a version or
threshold change, or for results recorded before Phase 5).
"""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from aerochorus.atc.agreement import AGREEMENT_VERSION, Hypothesis, compute_agreement
from aerochorus.db.models import (
    Model,
    SegmentAgreement,
    SweepRunModel,
    TranscriptionResult,
)
from aerochorus.settings import get_settings


def near_threshold() -> float:
    return get_settings().near_match_threshold


def latest_results(session: Session, segment_ids: list[int]):
    """(segment_id, result, model) for the newest result of each model per segment."""
    if not segment_ids:
        return []
    return session.execute(
        select(TranscriptionResult.segment_id, TranscriptionResult, Model)
        .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
        .join(Model, Model.id == SweepRunModel.model_id)
        .where(
            TranscriptionResult.segment_id.in_(segment_ids),
            # Research-only models are collected and shown, never counted (ADR-021).
            Model.ensemble_eligible.is_(True),
        )
        .ext(distinct_on(TranscriptionResult.segment_id, Model.id))
        .order_by(
            TranscriptionResult.segment_id,
            Model.id,
            TranscriptionResult.created_at.desc(),
        )
    ).all()


def refresh_agreement(session: Session, segment_ids: list[int]) -> int:
    threshold = near_threshold()
    by_segment: dict[int, list[Hypothesis]] = {sid: [] for sid in segment_ids}
    for segment_id, result, model in latest_results(session, segment_ids):
        by_segment[segment_id].append(
            Hypothesis(
                model=model.logical_name,
                family=model.architecture_family,  # the one family registry (ADR-009)
                status=result.status,
                text=result.text,
                flags=tuple((result.quality or {}).get("flags", [])),
            )
        )
    # Database time, so staleness compares against result.created_at on one clock.
    now = func.now()
    rows = [
        {"segment_id": sid, "computed_at": now}
        | compute_agreement(hypotheses, near_threshold=threshold).as_row()
        for sid, hypotheses in by_segment.items()
        if hypotheses
    ]
    if rows:
        statement = pg_insert(SegmentAgreement).values(rows)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["segment_id"],
                set_={c: statement.excluded[c] for c in rows[0] if c != "segment_id"},
            )
        )
    return len(rows)


def refresh_stale(session: Session, batch_size: int = 500, limit: int | None = None) -> int:
    """Recompute agreement where it is missing, outdated, or older than its results."""
    threshold = near_threshold()
    newest = (
        select(
            TranscriptionResult.segment_id,
            func.max(TranscriptionResult.created_at).label("newest"),
        )
        .group_by(TranscriptionResult.segment_id)
        .subquery()
    )
    stale = (
        select(newest.c.segment_id)
        .outerjoin(SegmentAgreement, SegmentAgreement.segment_id == newest.c.segment_id)
        .where(
            or_(
                SegmentAgreement.segment_id.is_(None),
                SegmentAgreement.version != AGREEMENT_VERSION,
                SegmentAgreement.near_threshold != threshold,
                and_(SegmentAgreement.computed_at < newest.c.newest),
            )
        )
        .order_by(newest.c.segment_id)
    )
    total, seen = 0, set()
    while limit is None or total < limit:
        ids = list(session.scalars(stale.limit(batch_size)))
        if not ids or seen.issuperset(ids):  # nothing new: never spin
            break
        seen.update(ids)
        total += refresh_agreement(session, ids)
        session.commit()
    return total
