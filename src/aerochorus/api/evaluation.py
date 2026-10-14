"""Evaluation against gold references (Phase 4, ADR-015).

This is the only control-plane module that reads the ``reference`` schema. It
scores sweeps (Phase 4) and adjudication batches (ADR-025).

Scoring rules:
* one symmetric normalizer (``normalize_for_scoring``) for gold and hypothesis;
* abstentions, errors and missing results are scored as empty hypotheses
  (all deletions), so silence is never free;
* every report shows best single model and the per-segment oracle, with the
  ensemble slot reserved for Phase 6; contaminated models are excluded from
  both claims;
* entity recall uses the human word-class tags in the gold.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aerochorus.adjudication_contracts import GateConfig, ItemStatus
from aerochorus.api.sweeps import Conflict, NotFound, load_run
from aerochorus.atc.align import contains_run, edit_counts, sequence_similarity
from aerochorus.atc.entities import ENTITY_CLASSES, gold_entity_spans
from aerochorus.atc.normalize import evidence_tokens, normalize_for_scoring
from aerochorus.atc.quality import compute_flags
from aerochorus.db.models import (
    AdjudicationBatch,
    AdjudicationItem,
    CorpusSource,
    GoldSegment,
    Model,
    Segment,
    SegmentAgreement,
    SweepRun,
    SweepRunModel,
    SweepRunSegment,
    TranscriptionResult,
)
from aerochorus.eval_contracts import (
    AdjudicationEvalBatch,
    AdjudicationEvalReport,
    AgreementBin,
    CalibrationBin,
    EntityScore,
    EvalModelMetrics,
    EvalReport,
    EvalSegmentRow,
    ReferenceImport,
    ReferenceImportResult,
)
from aerochorus.sweep_contracts import ResultStatus

AGREEMENT_BINS = ((0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01))


def import_references(session: Session, body: ReferenceImport) -> ReferenceImportResult:
    source = session.scalar(select(CorpusSource).where(CorpusSource.logical_key == body.source_key))
    if source is None:
        raise NotFound(f"unknown corpus source: {body.source_key}")
    existing = {
        g.relative_path: g
        for g in session.scalars(select(GoldSegment).where(GoldSegment.source_id == source.id))
    }
    inserted = unchanged = 0
    conflicts: list[str] = []
    for ref in body.references:
        current = existing.get(ref.relative_path)
        if current is None:
            session.add(GoldSegment(source_id=source.id, **ref.model_dump()))
            existing[ref.relative_path] = current
            inserted += 1
        elif (current.text_raw, current.start_ms, current.end_ms) == (
            ref.text_raw,
            ref.start_ms,
            ref.end_ms,
        ):
            unchanged += 1
        else:
            conflicts.append(ref.relative_path)
    if conflicts:
        raise Conflict(
            f"{len(conflicts)} references differ from already-imported gold (gold is immutable): "
            + ", ".join(conflicts[:5])
        )
    session.flush()
    return ReferenceImportResult(inserted=inserted, unchanged=unchanged, conflicts=[])


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _load(session: Session, run: SweepRun, english_only: bool, split: str | None):
    gold_rows = session.execute(
        select(Segment.id, Segment.relative_path, GoldSegment)
        .join(SweepRunSegment, SweepRunSegment.segment_id == Segment.id)
        .outerjoin(
            GoldSegment,
            (GoldSegment.source_id == Segment.source_id)
            & (GoldSegment.relative_path == Segment.relative_path),
        )
        .where(SweepRunSegment.run_id == run.id)
        .order_by(SweepRunSegment.ordinal)
    ).all()
    without_gold = sum(1 for _, _, g in gold_rows if g is None)
    gold = [
        (segment_id, path, g)
        for segment_id, path, g in gold_rows
        if g is not None
        and not (english_only and g.non_english)
        and (split is None or g.split == split)
    ]
    srms = list(
        session.scalars(
            select(SweepRunModel)
            .where(SweepRunModel.run_id == run.id)
            .order_by(SweepRunModel.execution_order)
        )
    )
    results: dict[tuple[int, int], TranscriptionResult] = {
        (r.sweep_run_model_id, r.segment_id): r
        for r in session.scalars(
            select(TranscriptionResult).where(
                TranscriptionResult.sweep_run_model_id.in_([s.id for s in srms])
            )
        )
    }
    return gold, without_gold, srms, results


def _hypothesis(result: TranscriptionResult | None) -> str:
    if result is None or result.status != ResultStatus.SUCCESS:
        return ""
    return result.text or ""


def _contaminated(srm: SweepRunModel, dataset: str) -> bool:
    benchmarks = srm.model.pedigree.get("contaminated_benchmarks") or []
    return any(str(b).lower() in dataset.lower() for b in benchmarks)


def evaluate_sweep(
    session: Session,
    run_id: int,
    *,
    canonical_numbers: bool = False,
    english_only: bool = False,
    split: str | None = None,
) -> EvalReport:
    run = load_run(session, run_id)
    gold, without_gold, srms, results = _load(session, run, english_only, split)
    if not gold:
        raise NotFound(f"sweep {run_id} has no segments with gold references")
    dataset = gold[0][2].dataset

    per_model = {s.id: Counter() for s in srms}
    entity_counts = {s.id: {c: Counter() for c in ENTITY_CLASSES} for s in srms}
    flag_counts = {s.id: Counter() for s in srms}
    errors_by_segment: dict[int, dict[int, int]] = defaultdict(dict)
    ref_tokens_by_segment: dict[int, int] = {}
    split_errors: dict[str, Counter] = defaultdict(Counter)
    split_tokens: Counter = Counter()
    agreement: dict[int, float] = {}

    gold_flags: Counter = Counter()
    for segment_id, _, g in gold:
        gold_flags.update(
            compute_flags(normalize_for_scoring(g.text_raw), audio_ms=g.end_ms - g.start_ms).flags
        )
        ref = normalize_for_scoring(g.text_raw, canonical_numbers=canonical_numbers)
        ref_tokens = ref.split()
        ref_tokens_by_segment[segment_id] = len(ref_tokens)
        split_tokens[g.split] += len(ref_tokens)
        spans = gold_entity_spans(g.text_raw, canonical_numbers=canonical_numbers)
        spoken = []
        for srm in srms:
            result = results.get((srm.id, segment_id))
            stats = per_model[srm.id]
            status = result.status if result else "missing"
            stats[status] += 1
            hyp = normalize_for_scoring(_hypothesis(result), canonical_numbers=canonical_numbers)
            hyp_tokens = hyp.split()
            counts = edit_counts(ref_tokens, hyp_tokens)
            stats["errors"] += counts.errors
            stats["S"] += counts.substitutions
            stats["D"] += counts.deletions
            stats["I"] += counts.insertions
            stats["ref_tokens"] += len(ref_tokens)
            stats["char_errors"] += _levenshtein(ref, hyp)
            stats["ref_chars"] += len(ref)
            stats["exact"] += int(ref_tokens == hyp_tokens)
            errors_by_segment[segment_id][srm.id] = counts.errors
            split_errors[g.split][srm.id] += counts.errors
            for span in spans:
                entity_counts[srm.id][span.entity_class]["spans"] += 1
                entity_counts[srm.id][span.entity_class]["recovered"] += contains_run(
                    hyp_tokens, span.tokens
                )
            if result is not None:
                for flag in (result.quality or {}).get("flags", []):
                    flag_counts[srm.id][flag] += 1
                if result.status == ResultStatus.SUCCESS and result.text:
                    spoken.append(evidence_tokens(result.text))
        if len(spoken) >= 2:
            pairs = list(combinations(spoken, 2))
            agreement[segment_id] = sum(sequence_similarity(a, b) for a, b in pairs) / len(pairs)

    total_ref = sum(ref_tokens_by_segment.values())
    fair = [s for s in srms if not _contaminated(s, dataset)]
    models = []
    for srm in srms:
        stats = per_model[srm.id]
        models.append(
            EvalModelMetrics(
                logical_name=srm.model.logical_name,
                architecture_family=srm.model.architecture_family,
                status=srm.status,
                contaminated=_contaminated(srm, dataset),
                segments=len(gold),
                success=stats[ResultStatus.SUCCESS],
                abstained=stats[ResultStatus.ABSTAINED],
                error=stats[ResultStatus.ERROR] + stats["missing"],
                reference_tokens=stats["ref_tokens"],
                token_errors=stats["errors"],
                substitutions=stats["S"],
                deletions=stats["D"],
                insertions=stats["I"],
                token_error_rate=_rate(stats["errors"], stats["ref_tokens"]),
                char_error_rate=_rate(stats["char_errors"], stats["ref_chars"]),
                exact_match_rate=_rate(stats["exact"], len(gold)),
                entities={
                    cls: EntityScore(
                        spans=c["spans"],
                        recovered=c["recovered"],
                        recall=_rate(c["recovered"], c["spans"]),
                    )
                    for cls, c in entity_counts[srm.id].items()
                },
                flags=dict(flag_counts[srm.id].most_common()),
                real_time_factor=(
                    round(srm.inference_ms_total / srm.audio_ms_total, 4)
                    if srm.audio_ms_total
                    else None
                ),
            )
        )

    notes = [
        "Scoring: normalize_for_scoring on gold and hypotheses alike"
        + (" with canonical numbers." if canonical_numbers else "; number format as produced."),
        "Abstentions, errors and missing results score as empty hypotheses (all deletions).",
        "Ensemble: not implemented yet (Phase 6); best-single and oracle are the bars "
        "it must beat.",
    ]
    if without_gold:
        notes.append(f"{without_gold} sweep segments have no gold reference and are not scored.")
    contaminated = [s.model.logical_name for s in srms if s not in fair]
    if contaminated:
        notes.append(f"Excluded from best-single/oracle (trained on {dataset}): {contaminated}")

    best_single = oracle = None
    if fair:
        by_errors = sorted(fair, key=lambda s: (per_model[s.id]["errors"], s.execution_order))
        winner = by_errors[0]
        best_single = {
            "model": winner.model.logical_name,
            "token_error_rate": _rate(per_model[winner.id]["errors"], total_ref),
        }
        wins: Counter = Counter()
        oracle_errors = 0
        fair_ids = {s.id for s in fair}
        for errors in errors_by_segment.values():
            fair_errors = {sid: e for sid, e in errors.items() if sid in fair_ids}
            low = min(fair_errors.values())
            oracle_errors += low
            for sid, e in fair_errors.items():
                if e == low:
                    wins[sid] += 1
        oracle = {
            "token_error_rate": _rate(oracle_errors, total_ref),
            "segment_wins_incl_ties": {s.model.logical_name: wins[s.id] for s in fair},
        }

    by_split = {
        name: {s.model.logical_name: _rate(errs[s.id], split_tokens[name]) for s in srms}
        | {"_segments": sum(1 for _, _, g in gold if g.split == name)}
        for name, errs in sorted(split_errors.items())
    }

    bins = []
    winner_id = by_errors[0].id if fair else None
    for low, high in AGREEMENT_BINS:
        ids = [sid for sid, a in agreement.items() if low <= a < high]
        tokens = sum(ref_tokens_by_segment[i] for i in ids)
        fair_ids = {s.id for s in fair}
        bins.append(
            AgreementBin(
                agreement_range=f"{low:.2f}-{min(high, 1.0):.2f}",
                segments=len(ids),
                oracle_ter=_rate(
                    sum(
                        min(e for sid, e in errors_by_segment[i].items() if sid in fair_ids)
                        for i in ids
                    ),
                    tokens,
                )
                if ids and fair_ids
                else None,
                best_single_ter=_rate(sum(errors_by_segment[i][winner_id] for i in ids), tokens)
                if ids and winner_id
                else None,
            )
        )
    no_agreement = len(gold) - len(agreement)
    if no_agreement:
        notes.append(
            f"{no_agreement} segments had fewer than two spoken hypotheses (no agreement)."
        )

    return EvalReport(
        sweep_id=run.id,
        suite=run.suite.name,
        source_key=run.selection_definition["source_key"],
        canonical_numbers=canonical_numbers,
        english_only=english_only,
        split=split,
        segments_in_sweep=run.segments_total,
        segments_scored=len(gold),
        segments_without_gold=without_gold,
        reference_tokens=total_ref,
        models=models,
        best_single=best_single,
        oracle=oracle,
        ensemble=None,
        by_split=by_split,
        agreement_bins=bins,
        gold_flag_counts=dict(gold_flags.most_common()),
        notes=notes,
    )


def evaluation_segments(
    session: Session,
    run_id: int,
    *,
    model: str | None,
    limit: int,
    offset: int,
    canonical_numbers: bool = False,
) -> list[EvalSegmentRow]:
    """Gold vs every hypothesis per segment, worst first (by `model`, else by oracle)."""
    run = load_run(session, run_id)
    gold, _, srms, results = _load(session, run, False, None)
    names = {s.id: s.model.logical_name for s in srms}
    if model is not None and model not in names.values():
        raise NotFound(f"{model} is not part of sweep {run_id}")
    rows = []
    for segment_id, path, g in gold:
        ref = normalize_for_scoring(g.text_raw, canonical_numbers=canonical_numbers)
        per_model: dict[str, dict[str, Any]] = {}
        for srm in srms:
            result = results.get((srm.id, segment_id))
            hyp = normalize_for_scoring(_hypothesis(result), canonical_numbers=canonical_numbers)
            errors = edit_counts(ref.split(), hyp.split()).errors
            per_model[names[srm.id]] = {
                "status": result.status if result else "missing",
                "text": result.text if result else None,
                "errors": errors,
                "flags": (result.quality or {}).get("flags", []) if result else [],
            }
        rows.append(
            EvalSegmentRow(
                segment_id=segment_id,
                relative_path=path,
                split=g.split,
                reference=g.text_raw,
                reference_scoring=ref,
                results=per_model,
            )
        )

    def key(row: EvalSegmentRow) -> int:
        if model is not None:
            return row.results[model]["errors"]
        return min(r["errors"] for r in row.results.values())

    rows.sort(key=key, reverse=True)
    return rows[offset : offset + limit]


# --- adjudicator evaluation (ADR-025) -----------------------------------------------------------

CONFIDENCE_BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.01))


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _calibration(
    pairs: list[tuple[float, int]],
) -> tuple[list[CalibrationBin], float | None, float | None]:
    """Bins of stated confidence against exact match, expected calibration error, Brier."""
    if not pairs:
        return [], None, None
    bins, gap = [], 0.0
    for low, high in CONFIDENCE_BINS:
        inside = [(c, e) for c, e in pairs if low <= c < high]
        hits = sum(e for _, e in inside)
        stated = sum(c for c, _ in inside)
        gap += abs(hits - stated)
        bins.append(
            CalibrationBin(
                confidence_range=f"{low:.1f}-{min(high, 1.0):.1f}",
                items=len(inside),
                mean_confidence=round(stated / len(inside), 4) if inside else None,
                exact_match_rate=_rate(hits, len(inside)),
            )
        )
    brier = sum((c - e) ** 2 for c, e in pairs) / len(pairs)
    return bins, round(gap / len(pairs), 4), round(brier, 4)


def _contaminated_models(session: Session, dataset: str | None) -> set[str]:
    if not dataset:
        return set()
    out = set()
    for name, pedigree in session.execute(select(Model.logical_name, Model.pedigree)):
        benchmarks = (pedigree or {}).get("contaminated_benchmarks") or []
        if any(str(b).lower() in dataset.lower() for b in benchmarks):
            out.add(name)
    return out


def _score_batch(
    session: Session,
    batch: AdjudicationBatch,
    gate: GateConfig,
    split: str | None,
    english_only: bool,
) -> AdjudicationEvalBatch:
    from aerochorus.api.adjudication import gate_item, votes_by_segment

    rows = session.execute(
        select(AdjudicationItem, GoldSegment)
        .join(Segment, Segment.id == AdjudicationItem.segment_id)
        .outerjoin(
            GoldSegment,
            (GoldSegment.source_id == Segment.source_id)
            & (GoldSegment.relative_path == Segment.relative_path),
        )
        .where(AdjudicationItem.batch_id == batch.id)
        .order_by(AdjudicationItem.id)
    ).all()
    segment_ids = [item.segment_id for item, _ in rows]
    votes = votes_by_segment(session, segment_ids)
    representative = dict(
        session.execute(
            select(SegmentAgreement.segment_id, SegmentAgreement.representative_text).where(
                SegmentAgreement.segment_id.in_(segment_ids)
            )
        ).all()
    )
    totals: Counter = Counter()
    by_decision: dict[str, Counter] = defaultdict(Counter)
    per_model: dict[str, Counter] = defaultdict(Counter)
    entities = {c: Counter() for c in ENTITY_CLASSES}
    pairs: list[tuple[float, int]] = []  # (stated confidence, exact match)
    latencies: list[float] = []
    dataset = None
    for item, gold in rows:
        if gold is None:
            totals["without_gold"] += 1
            continue
        if (english_only and gold.non_english) or (split and gold.split != split):
            continue
        if item.status in (ItemStatus.QUEUED, ItemStatus.RUNNING):
            totals["pending"] += 1
            continue
        dataset = dataset or gold.dataset
        done = item.status == ItemStatus.DONE
        answer = (item.transcript or "") if done else ""  # failed: an empty answer
        totals["scored"] += 1
        totals["failed"] += not done
        ref_raw = normalize_for_scoring(gold.text_raw).split()
        totals["tokens_raw"] += len(ref_raw)
        totals["errors_raw"] += edit_counts(ref_raw, normalize_for_scoring(answer).split()).errors
        ref = normalize_for_scoring(gold.text_raw, canonical_numbers=True).split()
        hyp = normalize_for_scoring(answer, canonical_numbers=True).split()
        errors = edit_counts(ref, hyp).errors
        exact = int(ref == hyp)
        totals["tokens"] += len(ref)
        totals["errors"] += errors
        totals["exact"] += exact
        for span in gold_entity_spans(gold.text_raw, canonical_numbers=True):
            entities[span.entity_class]["spans"] += 1
            entities[span.entity_class]["recovered"] += contains_run(hyp, span.tokens)
        if done and item.confidence is not None:
            pairs.append((float(item.confidence), exact))
        latency = (item.usage or {}).get("latency_ms")
        if done and isinstance(latency, int | float):
            latencies.append(float(latency))
        decision = gate_item(item, votes.get(item.segment_id, []), gate).decision
        bucket = by_decision[decision]
        bucket["items"] += 1
        bucket["errors"] += errors
        bucket["tokens"] += len(ref)
        bucket["exact"] += exact
        for vote in votes.get(item.segment_id, []):
            text = vote.text if vote.status == ResultStatus.SUCCESS else ""
            words = normalize_for_scoring(text, canonical_numbers=True).split()
            per_model[vote.model]["errors"] += edit_counts(ref, words).errors
            per_model[vote.model]["tokens"] += len(ref)
            per_model[vote.model]["segments"] += 1
        rep = representative.get(item.segment_id)
        rep_words = normalize_for_scoring(rep, canonical_numbers=True).split()
        totals["rep_errors"] += edit_counts(ref, rep_words).errors

    calibration, ece, brier = _calibration(pairs)
    accepted, escalated = by_decision["accept"], by_decision["escalate"]
    decided = accepted["items"] + escalated["items"]
    contaminated = _contaminated_models(session, dataset)
    fair = {
        m: c
        for m, c in per_model.items()
        if m not in contaminated and c["segments"] == totals["scored"]
    }
    best = min(fair, key=lambda m: (fair[m]["errors"], m)) if fair else None
    return AdjudicationEvalBatch(
        batch_id=batch.id,
        model=batch.model,
        prompt_version=batch.prompt_version,
        items=len(rows),
        scored=totals["scored"],
        failed=totals["failed"],
        pending=totals["pending"],
        without_gold=totals["without_gold"],
        reference_tokens=totals["tokens"],
        token_error_rate=_rate(totals["errors_raw"], totals["tokens_raw"]),
        token_error_rate_canonical=_rate(totals["errors"], totals["tokens"]),
        exact_match_rate=_rate(totals["exact"], totals["scored"]),
        entities={
            cls: EntityScore(
                spans=c["spans"], recovered=c["recovered"], recall=_rate(c["recovered"], c["spans"])
            )
            for cls, c in entities.items()
        },
        calibration=calibration,
        expected_calibration_error=ece,
        brier_score=brier,
        gate={
            "accepted": accepted["items"],
            "escalated": escalated["items"],
            "accept_rate": _rate(accepted["items"], decided),
            "escalate_rate": _rate(escalated["items"], decided),
            "token_error_rate_accepted": _rate(accepted["errors"], accepted["tokens"]),
            "token_error_rate_escalated": _rate(escalated["errors"], escalated["tokens"]),
            "exact_match_rate_accepted": _rate(accepted["exact"], accepted["items"]),
            "exact_match_rate_escalated": _rate(escalated["exact"], escalated["items"]),
        },
        baseline={
            "best_single": {
                "model": best,
                "token_error_rate_canonical": _rate(fair[best]["errors"], fair[best]["tokens"]),
            }
            if best
            else None,
            "representative_token_error_rate_canonical": _rate(
                totals["rep_errors"], totals["tokens"]
            ),
            "models": {m: _rate(c["errors"], c["tokens"]) for m, c in sorted(per_model.items())},
            "contaminated": sorted(contaminated & set(per_model)),
        },
        latency_ms_p50=_percentile(latencies, 0.5),
        latency_ms_p95=_percentile(latencies, 0.95),
    )


def evaluate_adjudications(
    session: Session,
    batch_ids: list[int],
    *,
    gate: GateConfig,
    split: str | None = None,
    english_only: bool = False,
) -> AdjudicationEvalReport:
    """Adjudicated transcripts against gold, side by side per batch (one batch per profile).

    Same symmetric normaliser as the sweep report; failed items score as empty answers.
    Calibration compares stated confidence with exact match (canonical numbers). The gate
    is the one ``adjudicate gate`` applies, and the baseline is every ASR model on the same
    segments, so the adjudicator is judged against what it was given.
    """
    if not batch_ids:
        raise Conflict("name at least one adjudication batch")
    reports = []
    for batch_id in batch_ids:
        batch = session.get(AdjudicationBatch, batch_id)
        if batch is None:
            raise NotFound(f"unknown adjudication batch: {batch_id}")
        reports.append(_score_batch(session, batch, gate, split, english_only))
    return AdjudicationEvalReport(
        gate=gate.model_dump(),
        split=split,
        english_only=english_only,
        batches=reports,
        notes=[
            "Token error rates: normalize_for_scoring on gold and answers alike; 'canonical' "
            "also folds number format (niner/nine, digits/words), matching the adjudication "
            "convention of numbers in words.",
            "Failed items score as empty answers (all deletions); pending items are not scored.",
            "Calibration: stated confidence against exact match (canonical). ECE over the bins "
            "shown; Brier over every scored item with a confidence.",
            "Baseline: each ASR model's current hypothesis on the same segments. Best single "
            "excludes models trained on this benchmark and models missing segments.",
        ],
    )
