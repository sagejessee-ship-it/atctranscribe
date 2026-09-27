"""Versioned training datasets: freeze in the control plane, export on a corpus machine.

Freezing snapshots the *current* annotation versions that match a definition.
Later edits create new annotation versions and never change a frozen dataset;
a new dataset version picks them up.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aerochorus.api.review import filtered_ids
from aerochorus.api.sweeps import Conflict, Invalid, NotFound
from aerochorus.dataset_contracts import (
    DatasetCreate,
    DatasetItem,
    DatasetStatus,
    DatasetView,
    ExportReport,
    SplitGrouping,
    SplitPolicy,
    TrainingSummary,
)
from aerochorus.db.models import (
    AnnotationThread,
    AnnotationVersion,
    CorpusSource,
    Segment,
    TrainingDataset,
    TrainingDatasetItem,
)
from aerochorus.review_contracts import SourceRole, TrainingLabel

# The frozen fields that define an example; their canonical JSON is the manifest hash.
MANIFEST_FIELDS = (
    "ordinal",
    "annotation_version_id",
    "segment_id",
    "scope",
    "start_ms",
    "end_ms",
    "text",
    "training_label",
    "text_origin",
    "source_sha256",
    "split",
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def split_group(segment: Segment, grouping: SplitGrouping) -> str:
    day = segment.capture_start_utc.date().isoformat() if segment.capture_start_utc else None
    if grouping == SplitGrouping.SEGMENT:
        return f"segment:{segment.id}"
    if day is None:  # no UTC: group by directory, still keeping neighbours together
        return f"dir:{segment.source_id}:{segment.relative_dir}"
    if grouping == SplitGrouping.UTC_DAY:
        return f"{segment.source_id}:{day}"
    return f"{segment.source_id}:{day}:{segment.channel or '-'}"


def assign_split(group: str, policy: SplitPolicy) -> str:
    """Deterministic: the same group and seed always land in the same split."""
    digest = hashlib.sha256(f"{policy.seed}:{group}".encode()).hexdigest()
    position = int(digest[:12], 16) / float(16**12)
    if position < policy.train:
        return "train"
    if position < policy.train + policy.validation:
        return "validation"
    return "test"


def _candidates(session: Session, body: DatasetCreate):
    query = (
        select(AnnotationVersion, AnnotationThread, Segment, CorpusSource)
        .join(AnnotationThread, AnnotationThread.current_version_id == AnnotationVersion.id)
        .join(Segment, Segment.id == AnnotationThread.segment_id)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .where(
            CorpusSource.role == SourceRole.CORPUS,  # benchmark audio never trains (ADR-017)
            AnnotationVersion.training_label.in_([label.value for label in body.labels]),
            AnnotationThread.scope.in_(body.scopes),
            func.coalesce(func.btrim(AnnotationVersion.text), "") != "",
        )
        .order_by(
            Segment.capture_start_utc.nulls_last(),
            Segment.id,
            AnnotationThread.scope.desc(),  # 'segment' before 'span'
            AnnotationVersion.start_ms.nulls_first(),
            AnnotationVersion.id,
        )
    )
    if body.filters is not None:
        query = query.where(Segment.id.in_(filtered_ids(session, body.filters)))
    return session.execute(query).all()


def create_dataset(session: Session, body: DatasetCreate) -> DatasetView:
    rows = _candidates(session, body)
    whole = {seg.id for version, thread, seg, _ in rows if thread.scope == "segment"}
    items: list[dict[str, Any]] = []
    for version, thread, segment, _source in rows:
        if (
            thread.scope == "span"
            and segment.id in whole
            and not body.include_spans_of_included_segments
        ):
            continue
        group = split_group(segment, body.split.group_by)
        if thread.scope == "span":
            duration = version.end_ms - version.start_ms
        else:
            duration = segment.duration_ms
        items.append(
            {
                "ordinal": len(items),
                "annotation_version_id": version.id,
                "segment_id": segment.id,
                "scope": thread.scope,
                "start_ms": version.start_ms,
                "end_ms": version.end_ms,
                "text": version.text.strip(),
                "training_label": version.training_label,
                "text_origin": version.text_origin,
                "source_sha256": segment.sha256,
                "split": assign_split(group, body.split),
                "split_group": group,
                "duration_ms": duration,
            }
        )
    if not items:
        raise Invalid("no current annotations match this definition; nothing to freeze")

    definition = body.model_dump(mode="json", exclude={"created_by", "description", "name"})
    current = session.scalar(
        select(func.max(TrainingDataset.version)).where(TrainingDataset.name == body.name)
    )
    counts = {
        "by_split": dict(Counter(i["split"] for i in items)),
        "by_label": dict(Counter(i["training_label"] for i in items)),
        "by_scope": dict(Counter(i["scope"] for i in items)),
        "by_origin": dict(Counter(i["text_origin"] or "none" for i in items)),
    }
    dataset = TrainingDataset(
        name=body.name,
        version=(current or 0) + 1,
        description=body.description,
        definition=definition,
        definition_sha256=canonical_sha256(definition),
        manifest_sha256=canonical_sha256([{k: item[k] for k in MANIFEST_FIELDS} for item in items]),
        item_count=len(items),
        audio_ms_total=sum(i["duration_ms"] or 0 for i in items),
        counts=counts,
        created_by=body.created_by,
    )
    session.add(dataset)
    session.flush()
    session.add_all(TrainingDatasetItem(dataset_id=dataset.id, **item) for item in items)
    session.flush()
    return dataset_view(dataset)


def dataset_view(dataset: TrainingDataset) -> DatasetView:
    return DatasetView(
        id=dataset.id,
        name=dataset.name,
        version=dataset.version,
        description=dataset.description,
        status=DatasetStatus(dataset.status),
        created_at=dataset.created_at,
        created_by=dataset.created_by,
        definition=dataset.definition,
        definition_sha256=dataset.definition_sha256,
        manifest_sha256=dataset.manifest_sha256,
        item_count=dataset.item_count,
        audio_ms_total=dataset.audio_ms_total,
        counts=dataset.counts or {},
        export=dataset.export,
    )


def load_dataset(session: Session, dataset_id: int, *, lock: bool = False) -> TrainingDataset:
    dataset = session.get(TrainingDataset, dataset_id, with_for_update=lock)
    if dataset is None:
        raise NotFound(f"unknown dataset: {dataset_id}")
    return dataset


def dataset_items(
    session: Session, dataset_id: int, offset: int = 0, limit: int = 1000
) -> list[DatasetItem]:
    load_dataset(session, dataset_id)
    rows = session.execute(
        select(TrainingDatasetItem, Segment.relative_path, CorpusSource.logical_key)
        .join(Segment, Segment.id == TrainingDatasetItem.segment_id)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .where(TrainingDatasetItem.dataset_id == dataset_id)
        .order_by(TrainingDatasetItem.ordinal)
        .offset(offset)
        .limit(limit)
    )
    return [
        DatasetItem(
            ordinal=item.ordinal,
            annotation_version_id=item.annotation_version_id,
            segment_id=item.segment_id,
            scope=item.scope,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            text=item.text,
            training_label=TrainingLabel(item.training_label),
            text_origin=item.text_origin,
            source_key=source_key,
            relative_path=relative_path,
            source_sha256=item.source_sha256,
            split=item.split,
            split_group=item.split_group,
            duration_ms=item.duration_ms,
            clip_path=item.clip_path,
            clip_sha256=item.clip_sha256,
            clip_bytes=item.clip_bytes,
        )
        for item, relative_path, source_key in rows
    ]


def record_export(session: Session, dataset_id: int, body: ExportReport) -> DatasetView:
    dataset = load_dataset(session, dataset_id, lock=True)
    items = {
        item.ordinal: item
        for item in session.scalars(
            select(TrainingDatasetItem).where(TrainingDatasetItem.dataset_id == dataset_id)
        )
    }
    reported = {clip.ordinal: clip for clip in body.clips}
    if set(reported) != set(items):
        missing = sorted(set(items) - set(reported))[:5]
        extra = sorted(set(reported) - set(items))[:5]
        raise Invalid(f"export must cover every item exactly (missing {missing}, unknown {extra})")
    clips_sha256 = canonical_sha256([reported[o].clip_sha256 for o in sorted(reported)])
    if dataset.status == DatasetStatus.EXPORTED:
        if (dataset.export or {}).get("clips_sha256") != clips_sha256:
            raise Conflict(
                "dataset was already exported with different clip audio; "
                "create a new dataset version instead"
            )
        return dataset_view(dataset)  # idempotent re-export: identical clips
    for ordinal, clip in reported.items():
        item = items[ordinal]
        item.clip_path = clip.clip_path
        item.clip_sha256 = clip.clip_sha256
        item.clip_bytes = clip.clip_bytes
    dataset.export = {
        "exported_at": datetime.now(UTC).isoformat(),
        "exported_by": body.exported_by,
        "out_uri": body.out_uri,
        "audio_format": body.audio_format,
        "tool": body.tool,
        "clips_sha256": clips_sha256,
        "manifest_file_sha256": body.manifest_file_sha256,
        "clip_audio_ms": sum(c.duration_ms for c in body.clips),
    }
    dataset.status = DatasetStatus.EXPORTED
    session.flush()
    return dataset_view(dataset)


def training_summary(session: Session) -> TrainingSummary:
    """Current labels over reviewable (corpus-role) sources."""
    base = (
        select(
            AnnotationVersion.training_label,
            AnnotationVersion.start_ms,
            AnnotationVersion.end_ms,
            AnnotationThread.scope,
            Segment.duration_ms,
            Segment.channel,
            Segment.capture_start_utc,
        )
        .select_from(AnnotationVersion)
        .join(AnnotationThread, AnnotationThread.current_version_id == AnnotationVersion.id)
        .join(Segment, Segment.id == AnnotationThread.segment_id)
        .join(CorpusSource, CorpusSource.id == Segment.source_id)
        .where(
            CorpusSource.role == SourceRole.CORPUS,
            AnnotationVersion.training_label != TrainingLabel.NONE.value,
        )
        .subquery()
    )
    label, scope = base.c.training_label, base.c.scope
    duration = func.coalesce(base.c.end_ms - base.c.start_ms, base.c.duration_ms, 0)
    by_label = session.execute(
        select(label, scope, func.count(), func.sum(duration)).group_by(label, scope)
    )
    by_channel = session.execute(
        select(base.c.channel, label, func.count()).group_by(base.c.channel, label)
    )
    day = func.date(func.timezone("UTC", base.c.capture_start_utc))
    by_day = session.execute(select(day, label, func.count()).group_by(day, label).order_by(day))
    return TrainingSummary(
        by_label=[
            {"label": lb, "scope": sc, "items": n, "audio_ms": int(ms or 0)}
            for lb, sc, n, ms in by_label
        ],
        by_channel=[{"channel": ch, "label": lb, "items": n} for ch, lb, n in by_channel],
        by_day=[
            {"day": d.isoformat() if d else None, "label": lb, "items": n} for d, lb, n in by_day
        ],
    )
