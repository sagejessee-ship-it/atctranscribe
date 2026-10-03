"""Model adjudication batches (ADR-022): plan, price, queue, hand out, record, accept.

The control plane never calls the paid model. It decides *what* may be sent
(a human-confirmed batch under a cost cap), builds the text context, hands items
to a runner on the host with the audio, and records what came back. Claims
reserve each item's worst-case cost, so a batch never spends past its cap.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aerochorus.adjudication.openrouter import fetch_pricing
from aerochorus.adjudication.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    estimate_item_cost,
    render_chat_prompt,
    render_context,
)
from aerochorus.adjudication_contracts import (
    AdjudicationAccept,
    AdjudicationAcceptOutcome,
    AdjudicationBatchDetail,
    AdjudicationBatchView,
    AdjudicationClaimRequest,
    AdjudicationContext,
    AdjudicationCreate,
    AdjudicationItemView,
    AdjudicationPreview,
    AdjudicationPreviewRequest,
    AdjudicationResultPost,
    AdjudicationSelection,
    BatchStatus,
    ClaimedAdjudication,
    ItemStatus,
    Pricing,
    RunnerSeen,
)
from aerochorus.api import context as context_svc
from aerochorus.api.review import (
    _append_version,
    _segment_thread,
    filtered_ids,
    segment_review,
)
from aerochorus.api.sweeps import Conflict, Invalid, NotFound
from aerochorus.atc.align import sequence_similarity
from aerochorus.atc.normalize import evidence_tokens
from aerochorus.atc.telephony import telephony_hint
from aerochorus.contracts import PresenceStatus
from aerochorus.db.models import (
    AdjudicationBatch,
    AdjudicationItem,
    AnnotationVersion,
    CorpusSource,
    ReviewSample,
    Segment,
    TranscriptionResult,
)
from aerochorus.review_contracts import ReviewStatus, SourceRole, TextOrigin, TrainingLabel
from aerochorus.settings import ControlPlaneSettings

STALE_CLAIM = timedelta(minutes=20)
MAX_ATTEMPTS = 3
OPEN_BATCH = (BatchStatus.QUEUED.value, BatchStatus.RUNNING.value)


# --- pricing --------------------------------------------------------------------------------


class PricingBook:
    """OpenRouter's public prices, cached for six hours; configured defaults when offline."""

    TTL = timedelta(hours=6)

    def __init__(self, settings: ControlPlaneSettings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self._http = http
        self._cache: dict[str, tuple[Pricing, datetime]] = {}
        self._lock = threading.Lock()

    def defaults(self, model: str) -> Pricing:
        s = self.settings
        return Pricing(
            model=model,
            source="configured defaults (OpenRouter price list unavailable)",
            prompt=s.adjudication_price_prompt,
            completion=s.adjudication_price_completion,
            audio=s.adjudication_price_audio,
        )

    def get(self, model: str) -> Pricing:
        now = datetime.now(UTC)
        with self._lock:
            hit = self._cache.get(model)
            if hit and now - hit[1] < self.TTL:
                return hit[0]
        try:
            http = self._http or httpx.Client()
            try:
                pricing = fetch_pricing(model, http, self.settings.openrouter_models_url)
            finally:
                if self._http is None:
                    http.close()
        except (httpx.HTTPError, ValueError):
            pricing = None
        pricing = pricing or self.defaults(model)
        with self._lock:
            self._cache[model] = (pricing, now)
        return pricing


class RunnerRegistry:
    """Runners seen polling for work (in memory: they poll every few seconds)."""

    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def mark_seen(self, runner: str, now: datetime) -> None:
        with self._lock:
            self._seen[runner] = now

    def recent(self, now: datetime, within: timedelta = timedelta(minutes=5)) -> list[RunnerSeen]:
        with self._lock:
            return [
                RunnerSeen(runner=name, last_seen=seen)
                for name, seen in sorted(self._seen.items())
                if now - seen <= within
            ]


# --- planning ---------------------------------------------------------------------------------


def _selected_ids(session: Session, selection: AdjudicationSelection) -> list[int]:
    ids = set(selection.segment_ids)
    if selection.sample_id is not None:
        sample = session.get(ReviewSample, selection.sample_id)
        if sample is None:
            raise NotFound(f"unknown sample: {selection.sample_id}")
        ids |= set(sample.segment_ids)
    if selection.filters is not None:
        key = func.md5(func.concat(str(selection.seed), ":", Segment.id))
        query = filtered_ids(session, selection.filters).order_by(key).limit(selection.n)
        ids |= set(session.scalars(query))
    return sorted(ids)


def _plan(
    session: Session,
    selection: AdjudicationSelection,
    params: dict[str, Any],
    model: str,
    pricing: Pricing,
    settings: ControlPlaneSettings,
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """Eligible items with their cost estimates, skip reasons, and how many were selected."""
    ids = _selected_ids(session, selection)
    skipped: dict[str, int] = {}

    def skip(reason: str, n: int = 1) -> None:
        skipped[reason] = skipped.get(reason, 0) + n

    rows = {
        row.id: row
        for row in session.execute(
            select(Segment.id, Segment.duration_ms, Segment.presence_status).where(
                Segment.id.in_(ids)
            )
        )
    }
    if unknown := set(ids) - rows.keys():
        skip("unknown_segment", len(unknown))
    hypotheses = dict(
        session.execute(
            select(TranscriptionResult.segment_id, func.count(TranscriptionResult.id))
            .where(TranscriptionResult.segment_id.in_(ids))
            .group_by(TranscriptionResult.segment_id)
        ).all()
    )
    done_before = set(
        session.scalars(
            select(AdjudicationItem.segment_id)
            .join(AdjudicationBatch, AdjudicationBatch.id == AdjudicationItem.batch_id)
            .where(
                AdjudicationItem.segment_id.in_(ids),
                AdjudicationBatch.model == model,
                AdjudicationBatch.prompt_version == PROMPT_VERSION,
                AdjudicationItem.status == ItemStatus.DONE,
            )
        )
    )
    in_flight = set(
        session.scalars(
            select(AdjudicationItem.segment_id).where(
                AdjudicationItem.segment_id.in_(ids),
                AdjudicationItem.status.in_((ItemStatus.QUEUED, ItemStatus.RUNNING)),
            )
        )
    )
    price = {"prompt": pricing.prompt, "completion": pricing.completion, "audio": pricing.audio}
    eligible = []
    for segment_id in ids:
        row = rows.get(segment_id)
        if row is None:
            continue
        if row.presence_status != PresenceStatus.PRESENT:
            skip("audio_missing")
            continue
        duration_s = (row.duration_ms or 0) / 1000
        if duration_s > settings.adjudication_max_audio_s:
            skip("too_long")
            continue
        if segment_id in in_flight:
            skip("already_queued")
            continue
        if segment_id in done_before and not params.get("redo"):
            skip("already_adjudicated")
            continue
        typical, worst = estimate_item_cost(
            duration_s, hypotheses.get(segment_id, 0), params["reasoning_effort"], price
        )
        eligible.append(
            {"segment_id": segment_id, "duration_s": duration_s, "typical": typical, "worst": worst}
        )
    return eligible, skipped, len(ids)


def preview(
    session: Session,
    body: AdjudicationPreviewRequest,
    settings: ControlPlaneSettings,
    pricing_book: PricingBook,
    runners: RunnerRegistry,
    now: datetime | None = None,
) -> AdjudicationPreview:
    now = now or datetime.now(UTC)
    model = body.params.model or settings.adjudication_model
    pricing = pricing_book.get(model)
    params = body.params.model_dump() | {"model": model}
    eligible, skipped, selected = _plan(session, body.selection, params, model, pricing, settings)
    typical = sum(e["typical"] for e in eligible)
    worst = sum(e["worst"] for e in eligible)
    return AdjudicationPreview(
        model=model,
        prompt_version=PROMPT_VERSION,
        reasoning_effort=body.params.reasoning_effort,
        selected=selected,
        eligible=len(eligible),
        skipped=skipped,
        audio_seconds=round(sum(e["duration_s"] for e in eligible), 1),
        estimated_cost_usd=round(typical, 4),
        worst_case_cost_usd=round(worst, 4),
        suggested_max_cost_usd=min(
            max(round(worst + 0.005, 2), 0.01), settings.adjudication_max_batch_usd
        ),
        pricing=pricing,
        limits={
            "max_items": settings.adjudication_max_items,
            "max_batch_usd": settings.adjudication_max_batch_usd,
            "max_audio_s": settings.adjudication_max_audio_s,
        },
        runners=runners.recent(now),
        segment_ids=[e["segment_id"] for e in eligible],
    )


def create(
    session: Session,
    body: AdjudicationCreate,
    settings: ControlPlaneSettings,
    pricing_book: PricingBook,
) -> AdjudicationBatchView:
    if not body.confirm:
        raise Invalid(
            "confirm must be true: adjudication sends audio and transcripts to an external "
            "paid service (OpenRouter)"
        )
    if body.max_cost_usd > settings.adjudication_max_batch_usd:
        raise Invalid(
            f"cost cap ${body.max_cost_usd:.2f} exceeds the per-batch limit "
            f"${settings.adjudication_max_batch_usd:.2f} (AEROCHORUS_ADJUDICATION_MAX_BATCH_USD)"
        )
    model = body.params.model or settings.adjudication_model
    pricing = pricing_book.get(model)
    params = body.params.model_dump() | {"model": model}
    eligible, skipped, selected = _plan(session, body.selection, params, model, pricing, settings)
    if not eligible:
        raise Invalid(f"nothing to adjudicate: {skipped or 'empty selection'}")
    if len(eligible) > settings.adjudication_max_items:
        raise Invalid(
            f"{len(eligible)} items exceed the per-batch limit of "
            f"{settings.adjudication_max_items}; select fewer"
        )
    estimate = round(sum(e["typical"] for e in eligible), 4)
    if abs(estimate - body.acknowledged_cost_usd) > max(0.01, 0.05 * body.acknowledged_cost_usd):
        raise Conflict(
            f"the estimate changed (now ${estimate:.4f}, you confirmed "
            f"${body.acknowledged_cost_usd:.4f}); review the preview again"
        )
    batch = AdjudicationBatch(
        created_by=body.created_by,
        status=BatchStatus.QUEUED,
        model=model,
        prompt_version=PROMPT_VERSION,
        params=params,
        selection=body.selection.model_dump(mode="json")
        | {"selected": selected, "skipped": skipped},
        pricing=pricing.model_dump(mode="json"),
        item_count=len(eligible),
        estimated_cost_usd=estimate,
        max_cost_usd=body.max_cost_usd,
        note=body.note,
    )
    session.add(batch)
    session.flush()
    session.add_all(
        AdjudicationItem(
            batch_id=batch.id,
            segment_id=e["segment_id"],
            estimated_cost_usd=e["typical"],
            reserve_usd=e["worst"],
        )
        for e in eligible
    )
    session.flush()
    return batch_view(session, batch)


# --- views ------------------------------------------------------------------------------------


def _counts(session: Session, batch_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {b: {} for b in batch_ids}
    for batch_id, status, n in session.execute(
        select(AdjudicationItem.batch_id, AdjudicationItem.status, func.count())
        .where(AdjudicationItem.batch_id.in_(batch_ids))
        .group_by(AdjudicationItem.batch_id, AdjudicationItem.status)
    ):
        out[batch_id][status] = n
    return out


def batch_view(
    session: Session, batch: AdjudicationBatch, counts: dict[str, int] | None = None
) -> AdjudicationBatchView:
    counts = counts if counts is not None else _counts(session, [batch.id])[batch.id]
    stats = session.execute(
        select(
            func.count(AdjudicationItem.accepted_version_id),
            func.max(func.greatest(AdjudicationItem.claimed_at, AdjudicationItem.finished_at)),
        ).where(AdjudicationItem.batch_id == batch.id)
    ).one()
    return AdjudicationBatchView(
        id=batch.id,
        created_at=batch.created_at,
        created_by=batch.created_by,
        status=BatchStatus(batch.status),
        model=batch.model,
        prompt_version=batch.prompt_version,
        params=batch.params,
        selection=batch.selection,
        item_count=batch.item_count,
        counts=counts,
        estimated_cost_usd=batch.estimated_cost_usd,
        max_cost_usd=batch.max_cost_usd,
        spent_usd=round(batch.spent_usd, 6),
        accepted=stats[0],
        note=batch.note,
        finished_at=batch.finished_at,
        last_activity_at=stats[1],
    )


def item_view(item: AdjudicationItem, batch: AdjudicationBatch) -> AdjudicationItemView:
    return AdjudicationItemView(
        id=item.id,
        batch_id=item.batch_id,
        segment_id=item.segment_id,
        status=ItemStatus(item.status),
        model=batch.model,
        prompt_version=batch.prompt_version,
        transcript=item.transcript,
        speech_present=item.speech_present,
        confidence=item.confidence,
        result=item.result,
        best_hypothesis_similarity=item.best_hypothesis_similarity,
        best_hypothesis_model=item.best_hypothesis_model,
        representative_similarity=item.representative_similarity,
        cost_usd=item.cost_usd,
        usage=item.usage,
        error=item.error,
        attempts=item.attempts,
        created_at=item.created_at,
        finished_at=item.finished_at,
        accepted_version_id=item.accepted_version_id,
    )


def list_batches(session: Session, limit: int = 50) -> list[AdjudicationBatchView]:
    batches = list(
        session.scalars(
            select(AdjudicationBatch).order_by(AdjudicationBatch.id.desc()).limit(limit)
        )
    )
    counts = _counts(session, [b.id for b in batches])
    return [batch_view(session, b, counts[b.id]) for b in batches]


def batch_detail(session: Session, batch_id: int) -> AdjudicationBatchDetail:
    batch = session.get(AdjudicationBatch, batch_id)
    if batch is None:
        raise NotFound(f"unknown adjudication batch: {batch_id}")
    items = session.scalars(
        select(AdjudicationItem)
        .where(AdjudicationItem.batch_id == batch_id)
        .order_by(AdjudicationItem.id)
    )
    return AdjudicationBatchDetail(
        **batch_view(session, batch).model_dump(), items=[item_view(i, batch) for i in items]
    )


def segment_items(session: Session, segment_id: int) -> list[AdjudicationItemView]:
    rows = session.execute(
        select(AdjudicationItem, AdjudicationBatch)
        .join(AdjudicationBatch, AdjudicationBatch.id == AdjudicationItem.batch_id)
        .where(AdjudicationItem.segment_id == segment_id)
        .order_by(AdjudicationItem.id.desc())
    ).all()
    return [item_view(item, batch) for item, batch in rows]


def cancel(session: Session, batch_id: int, now: datetime | None = None) -> AdjudicationBatchView:
    batch = session.get(AdjudicationBatch, batch_id, with_for_update=True)
    if batch is None:
        raise NotFound(f"unknown adjudication batch: {batch_id}")
    if batch.status in OPEN_BATCH:
        for item in session.scalars(
            select(AdjudicationItem).where(
                AdjudicationItem.batch_id == batch_id,
                AdjudicationItem.status == ItemStatus.QUEUED,
            )
        ):
            item.status = ItemStatus.CANCELLED
        batch.status = BatchStatus.CANCELLED
        batch.finished_at = now or datetime.now(UTC)
        session.flush()
    return batch_view(session, batch)


# --- the context bundle ---------------------------------------------------------------------


def build_bundle(session: Session, segment_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """Everything the model sees except the audio: plain data, rendered by the runner."""
    review = segment_review(session, segment_id)
    local = review.capture_local[11:19] if review.capture_local else None
    bundle: dict[str, Any] = {
        "segment": {
            "id": review.segment_id,
            "airport": review.airport,
            "station": review.station,
            "channel": review.channel,
            "service": review.channel_service,
            "frequency_mhz": review.frequency_hz / 1e6 if review.frequency_hz else None,
            "utc": review.capture_start_utc.strftime("%Y-%m-%d %H:%M:%SZ")
            if review.capture_start_utc
            else None,
            "local": local,
            "duration_s": round((review.duration_ms or 0) / 1000, 1),
        },
        "airport": None,
        "hypotheses": [],
        "abstained": [],
        "errors": [],
        "agreement": None,
        "neighbors": [],
        "adsb": None,
    }
    profile = review.airport_profile
    if profile:
        bundle["airport"] = {
            "icao": profile.icao,
            "name": profile.name,
            "runways": [
                {"end": r.end_ident, "pair": r.pair, "spoken": r.spoken} for r in profile.runways
            ],
            "frequencies": [
                {"service": f.service, "mhz": f.frequency_hz / 1e6, "call": f.call}
                for f in profile.frequencies
            ],
        }
    for h in review.hypotheses:
        if h.superseded:
            continue
        if h.status == "success" and (h.text or "").strip():
            bundle["hypotheses"].append(
                {
                    "model": h.model,
                    "family": h.architecture_family,
                    "text": h.text,
                    "research_only": not h.ensemble_eligible,
                }
            )
        elif h.status in ("abstained", "success"):
            bundle["abstained"].append(h.model)
        else:
            bundle["errors"].append(h.model)
    a = review.agreement
    if a:
        near = a.get("near_group") or None
        bundle["agreement"] = {
            "exact_groups": [
                {"text": g.get("display_text"), "families": g.get("family_count", 0)}
                for g in a.get("exact_groups") or []
            ],
            "near": {
                "families": near.get("family_count", 0),
                "min_similarity": near.get("min_similarity"),
                "anchor": near.get("anchor_model"),
            }
            if near
            else None,
            "utterances": [
                {
                    "text": u.get("text"),
                    "families": u.get("family_count"),
                    "start_s": u["start_ms"] / 1000 if u.get("start_ms") is not None else None,
                    "end_s": u["end_ms"] / 1000 if u.get("end_ms") is not None else None,
                }
                for u in a.get("utterances") or []
            ],
            "representative": a.get("representative_text"),
        }
    if params.get("include_neighbors", True):
        bundle["neighbors"] = [
            {"offset_s": n.offset_seconds, "text": n.preview}
            for n in review.neighbors
            if n.preview and n.relation in ("previous", "next", "nearby")
        ][:4]
    if params.get("include_adsb", True):
        snapshot = context_svc.latest(session, segment_id)
        if snapshot is not None:
            bundle["adsb"] = {
                "snapshot_id": snapshot.id,
                "aircraft": [
                    {
                        "callsign": ac.get("callsign"),
                        "icao24": ac.get("icao24"),
                        "telephony": telephony_hint(ac.get("callsign")),
                        "alt_ft": ac.get("baro_altitude_ft"),
                        "vs_fpm": ac.get("vertical_rate_fpm"),
                        "on_ground": ac.get("on_ground"),
                        "distance_nm": ac.get("distance_nm"),
                    }
                    for ac in snapshot.summary
                ],
            }
    return bundle


def context_for(session: Session, segment_id: int) -> AdjudicationContext:
    """One segment's adjudication input as text, for manual tests (no batch, no spend)."""
    segment = session.get(Segment, segment_id)
    if segment is None:
        raise NotFound(f"unknown segment: {segment_id}")
    bundle = build_bundle(session, segment_id, {"include_adsb": True, "include_neighbors": True})
    return AdjudicationContext(
        segment_id=segment_id,
        relative_path=segment.relative_path,
        sha256=segment.sha256,
        prompt_version=PROMPT_VERSION,
        chat_prompt=render_chat_prompt(bundle),
        system_prompt=SYSTEM_PROMPT,
        context_text=render_context(bundle),
    )


# --- runner protocol --------------------------------------------------------------------------


def _requeue_stale(session: Session, now: datetime) -> None:
    for item in session.scalars(
        select(AdjudicationItem)
        .where(
            AdjudicationItem.status == ItemStatus.RUNNING,
            AdjudicationItem.claimed_at < now - STALE_CLAIM,
        )
        .with_for_update(skip_locked=True)
    ):
        if item.attempts >= MAX_ATTEMPTS:
            item.status = ItemStatus.FAILED
            item.error = f"abandoned by {item.claimed_by} after {item.attempts} attempts"
            item.finished_at = now
        else:
            item.status = ItemStatus.QUEUED
            item.error = f"claim by {item.claimed_by} went stale; requeued"


def _finish_if_drained(session: Session, batch: AdjudicationBatch, now: datetime) -> None:
    open_items = session.scalar(
        select(func.count()).where(
            AdjudicationItem.batch_id == batch.id,
            AdjudicationItem.status.in_((ItemStatus.QUEUED, ItemStatus.RUNNING)),
        )
    )
    if not open_items and batch.status in OPEN_BATCH:
        batch.status = BatchStatus.DONE
        batch.finished_at = now


def claim(
    session: Session,
    body: AdjudicationClaimRequest,
    runners: RunnerRegistry,
    now: datetime | None = None,
) -> list[ClaimedAdjudication]:
    now = now or datetime.now(UTC)
    runners.mark_seen(body.runner, now)
    _requeue_stale(session, now)
    claimed: list[ClaimedAdjudication] = []
    blocked: set[int] = set()  # batches whose cap has no room right now
    while len(claimed) < body.limit:
        query = (
            select(AdjudicationItem)
            .join(AdjudicationBatch, AdjudicationBatch.id == AdjudicationItem.batch_id)
            .where(
                AdjudicationItem.status == ItemStatus.QUEUED,
                AdjudicationBatch.status.in_(OPEN_BATCH),
            )
            .order_by(AdjudicationItem.batch_id, AdjudicationItem.id)
            .limit(1)
            .with_for_update(of=AdjudicationItem, skip_locked=True)
        )
        if blocked:
            query = query.where(AdjudicationItem.batch_id.not_in(blocked))
        item = session.scalar(query)
        if item is None:
            break
        batch = session.get(AdjudicationBatch, item.batch_id, with_for_update=True)
        reserved = session.scalar(
            select(func.coalesce(func.sum(AdjudicationItem.reserve_usd), 0.0)).where(
                AdjudicationItem.batch_id == batch.id,
                AdjudicationItem.status == ItemStatus.RUNNING,
            )
        )
        if batch.spent_usd + reserved + item.reserve_usd > batch.max_cost_usd:
            blocked.add(batch.id)
            if not reserved:  # nothing in flight will free room: the cap is reached
                for rest in session.scalars(
                    select(AdjudicationItem).where(
                        AdjudicationItem.batch_id == batch.id,
                        AdjudicationItem.status == ItemStatus.QUEUED,
                    )
                ):
                    rest.status = ItemStatus.SKIPPED
                    rest.error = (
                        f"cost cap ${batch.max_cost_usd:.2f} reached (spent ${batch.spent_usd:.4f})"
                    )
                    rest.finished_at = now
                batch.status = BatchStatus.CAPPED
                batch.finished_at = now
            continue
        item.status = ItemStatus.RUNNING
        item.claimed_by = body.runner
        item.claimed_at = now
        item.attempts += 1
        if batch.status == BatchStatus.QUEUED:
            batch.status = BatchStatus.RUNNING
        segment = session.get(Segment, item.segment_id)
        source = session.get(CorpusSource, segment.source_id)
        claimed.append(
            ClaimedAdjudication(
                item_id=item.id,
                batch_id=batch.id,
                segment_id=segment.id,
                source_key=source.logical_key,
                relative_path=segment.relative_path,
                sha256=segment.sha256,
                duration_ms=segment.duration_ms,
                model=batch.model,
                prompt_version=batch.prompt_version,
                params=batch.params,
                pricing=batch.pricing,
                bundle=build_bundle(session, segment.id, batch.params),
            )
        )
        session.flush()
    return claimed


def _similarities(session: Session, segment_id: int, transcript: str) -> dict[str, Any]:
    review = segment_review(session, segment_id)
    tokens = evidence_tokens(transcript)
    best, best_model = None, None
    for h in review.hypotheses:
        if h.superseded or h.status != "success" or not h.text:
            continue
        sim = sequence_similarity(tokens, evidence_tokens(h.text))
        if best is None or sim > best:
            best, best_model = sim, h.model
    rep = (review.agreement or {}).get("representative_text")
    return {
        "best_hypothesis_similarity": None if best is None else round(best, 4),
        "best_hypothesis_model": best_model,
        "representative_similarity": round(sequence_similarity(tokens, evidence_tokens(rep)), 4)
        if rep
        else None,
    }


def post_result(
    session: Session, item_id: int, body: AdjudicationResultPost, now: datetime | None = None
) -> AdjudicationItemView:
    now = now or datetime.now(UTC)
    item = session.get(AdjudicationItem, item_id, with_for_update=True)
    if item is None:
        raise NotFound(f"unknown adjudication item: {item_id}")
    batch = session.get(AdjudicationBatch, item.batch_id, with_for_update=True)
    if body.cost_usd:  # money spent is recorded whatever happens to the answer
        batch.spent_usd += body.cost_usd
        item.cost_usd = (item.cost_usd or 0.0) + body.cost_usd
    if item.status != ItemStatus.RUNNING or item.claimed_by != body.runner:
        # Late answer after a stale requeue or a cancel: keep the spend, not the result.
        item.error = (
            f"late result from {body.runner} ignored (item was {item.status}, "
            f"claimed by {item.claimed_by})"
        )
        session.flush()
        return item_view(item, batch)
    item.usage = body.usage
    item.request = body.request
    item.response = body.response
    item.error = body.error
    if body.status == "done":
        result = body.result or {}
        transcript = (body.transcript or "").strip()
        item.status = ItemStatus.DONE
        item.result = result
        item.transcript = transcript or None
        item.speech_present = bool(result.get("speech_present")) and bool(transcript)
        item.confidence = result.get("confidence")
        if transcript:
            for key, value in _similarities(session, item.segment_id, transcript).items():
                setattr(item, key, value)
        item.finished_at = now
    elif body.retryable and item.attempts < MAX_ATTEMPTS:
        item.status = ItemStatus.QUEUED
    else:
        item.status = ItemStatus.FAILED
        item.finished_at = now
    session.flush()
    _finish_if_drained(session, batch, now)
    session.flush()
    return item_view(item, batch)


# --- accepting as silver ------------------------------------------------------------------------


def accept(
    session: Session, body: AdjudicationAccept, settings: ControlPlaneSettings
) -> AdjudicationAcceptOutcome:
    """Adjudicated text -> silver, where no human text or decision would be overridden."""
    query = select(AdjudicationItem, AdjudicationBatch).join(
        AdjudicationBatch, AdjudicationBatch.id == AdjudicationItem.batch_id
    )
    if body.item_ids:
        query = query.where(AdjudicationItem.id.in_(body.item_ids))
    if body.batch_id is not None:
        query = query.where(AdjudicationItem.batch_id == body.batch_id)
    rows = session.execute(query.order_by(AdjudicationItem.id)).all()
    if body.item_ids and len(rows) < len(set(body.item_ids)) and body.batch_id is None:
        raise NotFound("unknown adjudication item(s)")
    skipped: dict[str, int] = {}
    applied: list[int] = []

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for item, batch in rows:
        if item.status != ItemStatus.DONE:
            skip(f"not_done_{item.status}")
            continue
        if item.accepted_version_id is not None:
            skip("already_accepted")
            continue
        if not item.transcript or not item.speech_present:
            skip("no_speech")
            continue
        if (item.confidence or 0.0) < body.min_confidence:
            skip("below_min_confidence")
            continue
        if body.require_model_support and (
            item.best_hypothesis_similarity is None
            or item.best_hypothesis_similarity < settings.near_match_threshold
        ):
            skip("no_model_support")
            continue
        segment = session.get(Segment, item.segment_id)
        if session.get(CorpusSource, segment.source_id).role == SourceRole.BENCHMARK:
            skip("benchmark_source")
            continue
        thread = _segment_thread(session, item.segment_id, create=True)
        current = (
            session.get(AnnotationVersion, thread.current_version_id)
            if thread.current_version_id
            else None
        )
        label = TrainingLabel(current.training_label) if current else TrainingLabel.NONE
        if label == TrainingLabel.GOLD:
            skip("human_gold_unchanged")
            continue
        if label == TrainingLabel.REJECTED:
            skip("rejected_by_human")
            continue
        if current and current.text and current.text_origin == TextOrigin.HUMAN:
            skip("human_text_unchanged")
            continue
        version = _append_version(
            session,
            thread,
            action="adjudication_silver",
            text=item.transcript,
            text_origin=TextOrigin.MODEL_ADJUDICATED,
            review_status=ReviewStatus(current.review_status)
            if current
            else ReviewStatus.UNREVIEWED,
            training_label=TrainingLabel.SILVER,
            reason_tags=current.reason_tags if current else [],
            notes=body.notes or (current.notes if current else None),
            annotator=body.annotator,
            basis={
                "adjudication_item_id": item.id,
                "adjudication_batch_id": batch.id,
                "model": batch.model,
                "prompt_version": batch.prompt_version,
                "confidence": item.confidence,
                "best_hypothesis_similarity": item.best_hypothesis_similarity,
                "best_hypothesis_model": item.best_hypothesis_model,
                "min_confidence": body.min_confidence,
                "require_model_support": body.require_model_support,
            },
        )
        item.accepted_version_id = version.id
        applied.append(item.segment_id)
    session.flush()
    return AdjudicationAcceptOutcome(
        applied=len(applied), skipped=skipped, segment_ids_applied=applied
    )
