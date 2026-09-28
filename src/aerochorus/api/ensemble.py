"""Which models vote in agreement: evidence on this corpus, and deliberate decisions.

A model votes (``ensemble_eligible``) only after someone has looked at how it behaves.
Downloading or running a model never changes that. The evidence here compares
each model's latest transcripts with the other families' exact consensus, and
counts how often it produces words where the voters heard nothing (the "thank you"
hallucination). A decision records who, when and why, refreshes agreement for every
segment the model transcribed, and is kept by `models sync` from then on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from aerochorus.api.agreement import near_threshold, refresh_agreement
from aerochorus.api.sweeps import Invalid, load_model, model_read
from aerochorus.atc.align import sequence_similarity
from aerochorus.atc.normalize import evidence_tokens
from aerochorus.db.models import (
    Model,
    SegmentAgreement,
    SweepRunModel,
    TranscriptionResult,
)
from aerochorus.sweep_contracts import EnsembleDecision, EnsembleDecisionResult, ModelEvidence

EVIDENCE_SAMPLE = 3000  # most recent segments per model
REFRESH_BATCH = 500


def _latest(session: Session, model: Model, limit: int):
    """(result, agreement) for the model's newest result on its most recent segments."""
    newest = (
        select(TranscriptionResult.id)
        .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
        .where(SweepRunModel.model_id == model.id)
        .ext(distinct_on(TranscriptionResult.segment_id))
        .order_by(TranscriptionResult.segment_id.desc(), TranscriptionResult.created_at.desc())
        .limit(limit)
        .subquery()
    )
    return session.execute(
        select(TranscriptionResult, SegmentAgreement)
        .join(newest, newest.c.id == TranscriptionResult.id)
        .outerjoin(SegmentAgreement, SegmentAgreement.segment_id == TranscriptionResult.segment_id)
    ).all()


def _reference(groups: list[dict[str, Any]], family: str) -> list[str] | None:
    """Tokens of the largest exact group that 2+ *other* families agree on."""
    best, best_n = None, 1
    for group in groups or []:
        others = len(set(group.get("families") or []) - {family})
        if others >= 2 and others > best_n:
            best, best_n = group, others
    return evidence_tokens(best.get("normalized") or "") if best else None


def evidence(session: Session) -> list[ModelEvidence]:
    threshold = near_threshold()
    out = []
    models = session.scalars(select(Model).order_by(Model.logical_name))
    for model in models:
        rows = _latest(session, model, EVIDENCE_SAMPLE)
        spoken = abstained = errors = 0
        sims: list[float] = []
        silent = spoke_over_silence = 0
        for result, agreement in rows:
            own_spoke = result.status == "success" and bool((result.text or "").strip())
            if result.status == "error":
                errors += 1
            elif own_spoke:
                spoken += 1
            else:
                abstained += 1
            if agreement is None or result.status == "error":
                continue
            voter = model.ensemble_eligible
            # Voters' consensus without this model's own vote.
            reference = _reference(agreement.exact_groups, model.architecture_family)
            if reference and own_spoke:
                sims.append(sequence_similarity(evidence_tokens(result.text), reference))
            others_spoke = agreement.success_count - (1 if voter and own_spoke else 0)
            others_quiet = agreement.abstained_count - (1 if voter and not own_spoke else 0)
            if others_spoke == 0 and others_quiet >= 2:
                silent += 1
                spoke_over_silence += own_spoke
        out.append(
            ModelEvidence(
                logical_name=model.logical_name,
                architecture_family=model.architecture_family,
                ensemble_eligible=model.ensemble_eligible,
                results=len(rows),
                spoken=spoken,
                abstained=abstained,
                errors=errors,
                compared=len(sims),
                mean_similarity=round(sum(sims) / len(sims), 3) if sims else None,
                near_rate=round(sum(s >= threshold for s in sims) / len(sims), 3) if sims else None,
                exact_rate=round(sum(s >= 0.9999 for s in sims) / len(sims), 3) if sims else None,
                silent_segments=silent,
                speaks_over_silence=round(spoke_over_silence / silent, 3) if silent else None,
            )
        )
    return out


def decide(session: Session, logical_name: str, body: EnsembleDecision) -> EnsembleDecisionResult:
    model = load_model(session, logical_name)
    if model.ensemble_eligible == body.eligible:
        raise Invalid(f"{logical_name} already {'votes' if body.eligible else 'is research-only'}")
    model.ensemble_decision = {
        "eligible": body.eligible,
        "previous": model.ensemble_eligible,
        "reason": body.reason.strip(),
        "by": body.by,
        "at": datetime.now(UTC).isoformat(),
    }
    model.ensemble_eligible = body.eligible
    session.flush()
    # The voter set changed: recompute agreement wherever this model has a result.
    segment_ids = sorted(
        set(
            session.scalars(
                select(TranscriptionResult.segment_id)
                .join(SweepRunModel, SweepRunModel.id == TranscriptionResult.sweep_run_model_id)
                .where(SweepRunModel.model_id == model.id)
            )
        )
    )
    refreshed = 0
    for start in range(0, len(segment_ids), REFRESH_BATCH):
        chunk = segment_ids[start : start + REFRESH_BATCH]
        refreshed += refresh_agreement(session, chunk)
        # Rows the refresh rewrote carry this transaction's now(). One it did not rewrite
        # belongs to a segment with no voter left (its only voter was this model): drop it.
        session.execute(
            delete(SegmentAgreement).where(
                SegmentAgreement.segment_id.in_(chunk),
                SegmentAgreement.computed_at < func.now(),
            )
        )
    session.flush()
    return EnsembleDecisionResult(model=model_read(model), segments_refreshed=refreshed)
