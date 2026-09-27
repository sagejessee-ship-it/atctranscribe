"""Wire contracts for the review workbench (Phase 5).

Human annotation semantics (ADR-017):

* ``review_status``: what a human did: unreviewed, reviewed (no change),
  corrected (text edited).
* ``training_label``: what the item is for training: none, candidate,
  silver, gold, rejected.
* **Candidate** and **silver** may come from model agreement (batch); **gold**
  is always a human assertion, requires explicit confirmation, and can never
  be set in batch.
* Every save appends a version; nothing is overwritten. Model hypotheses live
  in ``transcription_result`` and are never modified.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"
    CORRECTED = "corrected"


class TrainingLabel(StrEnum):
    NONE = "none"
    CANDIDATE = "candidate"
    SILVER = "silver"
    GOLD = "gold"
    REJECTED = "rejected"


class TextOrigin(StrEnum):
    # Typed or explicitly accepted by a human.
    HUMAN = "human"
    # Representative text of a model-agreement group (batch nomination).
    MODEL_CONSENSUS = "model_consensus"


class SourceRole(StrEnum):
    CORPUS = "corpus"
    BENCHMARK = "benchmark"


class BatchAction(StrEnum):
    CANDIDATE = "candidate"
    SILVER = "silver"
    CLEAR = "clear"


class SearchScope(StrEnum):
    ANY = "any"
    HUMAN = "human"
    CONSENSUS = "consensus"
    HYPOTHESES = "hypotheses"
    MODEL = "model"


class SortKey(StrEnum):
    UTC = "utc"
    DURATION = "duration"
    RESULTS = "results"
    EXACT_PROVIDERS = "exact_providers"
    EXACT_FAMILIES = "exact_families"
    NEAR_FAMILIES = "near_families"
    NEAR_SIMILARITY = "near_similarity"
    CHANNEL = "channel"


# --- query ------------------------------------------------------------------------


class ReviewFilters(BaseModel):
    """Server-side filters. Every field is optional; absent means 'no constraint'."""

    source_keys: list[str] | None = None  # default: all non-benchmark sources
    include_benchmark: bool = False
    airport: str | None = None  # ICAO, resolved through airport aliases
    channels: list[str] = Field(default_factory=list)
    utc_from: datetime | None = None
    utc_to: datetime | None = None
    models: list[str] = Field(default_factory=list)  # a result from any of these exists
    families: list[str] = Field(default_factory=list)
    min_models: int | None = Field(default=1, ge=0)  # 0 includes untranscribed segments
    min_success: int | None = Field(default=None, ge=0)  # models that produced words
    min_exact_providers: int | None = Field(default=None, ge=1)
    min_exact_families: int | None = Field(default=None, ge=1)
    min_near_families: int | None = Field(default=None, ge=1)
    min_near_similarity: float | None = Field(default=None, ge=0, le=1)
    max_exact_families: int | None = Field(default=None, ge=0)  # "disagreement" views
    max_near_families: int | None = Field(default=None, ge=0)
    # Words in the representative transcript (short "thank you"/"roger" segments are
    # often agreed but wrong; single words are low impact).
    min_words: int | None = Field(default=None, ge=0)
    # Utterance agreement within a segment (agreement v2).
    min_utterance_families: int | None = Field(default=None, ge=1)
    min_utterance_tokens: int | None = Field(default=None, ge=1)
    # Agreeing utterances but no whole-segment exact agreement: recoverable partial data.
    partial_agreement: bool | None = None
    review_status: list[ReviewStatus] = Field(default_factory=list)
    training_label: list[TrainingLabel] = Field(default_factory=list)
    has_error: bool | None = None
    has_abstention: bool | None = None
    has_error_or_abstention: bool | None = None
    flags: list[str] = Field(default_factory=list)  # any of these quality flags
    span_labels: list[TrainingLabel] = Field(default_factory=list)  # has a span with label
    has_spans: bool | None = None
    # The whole segment is not gold/silver, but a span of it is: a partially usable recording.
    partial_usable: bool | None = None
    sample_id: int | None = None
    q: str | None = None
    scope: SearchScope = SearchScope.ANY
    scope_model: str | None = None


class ReviewQuery(BaseModel):
    filters: ReviewFilters = Field(default_factory=ReviewFilters)
    sort: SortKey = SortKey.UTC
    descending: bool = True
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class Highlight(BaseModel):
    """Word span [start, end) of a text that belongs to agreed utterance `utterance`."""

    start: int
    end: int
    utterance: int
    family_count: int


class ReviewRow(BaseModel):
    segment_id: int
    source_key: str
    relative_path: str
    capture_start_utc: datetime | None
    airport: str | None
    station: str | None
    channel: str | None
    frequency_hz: int | None
    duration_ms: int | None
    results_count: int
    error_count: int
    abstained_count: int
    best_exact_provider_count: int
    best_exact_family_count: int
    best_near_family_count: int
    best_near_similarity: float | None
    representative_text: str | None
    representative_source: str | None
    review_status: ReviewStatus
    training_label: TrainingLabel
    human_text: str | None
    flags: list[str]
    span_count: int = 0
    representative_tokens: int = 0
    best_utterance_family_count: int = 0
    best_utterance_tokens: int = 0
    representative_highlights: list[Highlight] = Field(default_factory=list)


class ReviewPage(BaseModel):
    total: int
    offset: int
    limit: int
    rows: list[ReviewRow]
    near_threshold: float
    agreement_version: int


class SavedView(BaseModel):
    key: str
    name: str
    description: str
    filters: ReviewFilters
    sort: SortKey = SortKey.UTC
    descending: bool = True


# --- detail -----------------------------------------------------------------------


class HypothesisView(BaseModel):
    result_id: str
    model: str
    architecture_family: str
    status: str
    text: str | None
    language: str | None
    exact_group: int | None  # index into agreement.exact_groups
    similarity_to_representative: float | None
    # Model-specific; never comparable across models (ADR-015).
    model_confidence: float | None
    flags: list[str]
    sweep_id: int
    attempt: int
    created_at: datetime
    superseded: bool  # an older result for the same model
    provenance: dict[str, Any]
    ensemble_eligible: bool = True  # research-only models are shown, never counted
    highlights: list[Highlight] = Field(default_factory=list)
    has_word_times: bool = False


class NeighborView(BaseModel):
    segment_id: int
    offset_seconds: float | None
    channel: str | None
    capture_start_utc: datetime | None
    duration_ms: int | None
    preview: str | None
    relation: str  # previous | next | nearby


class AnnotationVersionView(BaseModel):
    id: int
    version: int
    start_ms: int | None
    end_ms: int | None
    text: str | None
    text_origin: TextOrigin | None
    review_status: ReviewStatus
    training_label: TrainingLabel
    reason_tags: list[str]
    notes: str | None
    annotator: str | None
    action: str
    basis: dict[str, Any]
    created_at: datetime


class AnnotationThreadView(BaseModel):
    thread_id: int
    scope: str  # segment | span
    current: AnnotationVersionView | None
    history: list[AnnotationVersionView]


class AirportRunwayView(BaseModel):
    pair: str
    end_ident: str
    length_ft: int | None
    width_ft: int | None
    true_alignment: float | None
    spoken: list[str]
    latitude: float | None = None
    longitude: float | None = None
    elevation_ft: float | None = None
    displaced_latitude: float | None = None
    displaced_longitude: float | None = None


class AirspaceView(BaseModel):
    name: str
    airspace_class: str
    local_type: str | None = None
    lower_ft: int | None = None
    lower_ref: str | None = None
    upper_ft: int | None = None
    upper_ref: str | None = None
    rings: list[list[list[float]]]
    source_id: str | None = None


class AirspaceIn(BaseModel):
    airspaces: list[AirspaceView]
    provenance: dict[str, Any] = Field(default_factory=dict)


class AirportFrequencyView(BaseModel):
    service: str
    frequency_hz: int
    facility: str | None
    call: str | None
    sectorization: str | None
    spoken: list[str]


class AirportProfileView(BaseModel):
    icao: str
    faa_id: str | None
    iata: str | None
    name: str
    city: str | None
    latitude: float | None
    longitude: float | None
    elevation_ft: float | None
    magnetic_variation: str | None
    timezone: str
    aliases: list[str]
    runways: list[AirportRunwayView]
    frequencies: list[AirportFrequencyView]
    provenance: dict[str, Any]
    airspaces: list[AirspaceView] = Field(default_factory=list)
    airspace_provenance: dict[str, Any] = Field(default_factory=dict)


class SegmentReview(BaseModel):
    segment_id: int
    source_key: str
    source_role: SourceRole
    relative_path: str
    capture_start_utc: datetime | None
    capture_local: str | None
    local_timezone: str | None
    temporal_status: str
    airport: str | None
    station: str | None
    channel: str | None
    frequency_hz: int | None
    channel_service: str | None  # e.g. "Tower (LCL/P)" from the airport profile
    duration_ms: int | None
    sha256: str | None
    agreement: dict[str, Any] | None
    hypotheses: list[HypothesisView]
    segment_annotation: AnnotationThreadView | None
    span_annotations: list[AnnotationThreadView]
    neighbors: list[NeighborView]
    airport_profile: AirportProfileView | None


# --- writes -------------------------------------------------------------------------


class AnnotationSave(BaseModel):
    thread_id: int | None = None  # None: the segment's whole-segment thread / a new span
    scope: str = "segment"
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    text: str | None = None
    review_status: ReviewStatus
    training_label: TrainingLabel
    reason_tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    annotator: str | None = None
    basis: dict[str, Any] = Field(default_factory=dict)
    # Optimistic concurrency: the version the editor started from (0 = new).
    expected_version: int = Field(ge=0)
    # Gold is a deliberate human assertion.
    confirm_gold: bool = False

    @model_validator(mode="after")
    def _span_bounds(self) -> AnnotationSave:
        if self.scope not in ("segment", "span"):
            raise ValueError("scope must be segment or span")
        if self.scope == "span":
            if self.start_ms is None or self.end_ms is None or self.end_ms <= self.start_ms:
                raise ValueError("a span needs 0 <= start_ms < end_ms")
        elif self.start_ms is not None or self.end_ms is not None:
            raise ValueError("whole-segment annotations have no bounds")
        return self


class BatchRequest(BaseModel):
    action: BatchAction
    segment_ids: list[int] = Field(default_factory=list, max_length=5000)
    sample_id: int | None = None
    annotator: str | None = None
    notes: str | None = None


class BatchOutcome(BaseModel):
    applied: int
    skipped: dict[str, int]
    segment_ids_applied: list[int]


class SampleRequest(BaseModel):
    filters: ReviewFilters
    n: int = Field(ge=1, le=5000)
    seed: int = 0
    name: str | None = None


class SampleView(BaseModel):
    id: int
    name: str | None
    n: int
    seed: int
    filters: dict[str, Any]
    filter_sha256: str
    total_matching: int
    segment_ids: list[int]
    created_at: datetime


class AirportAliasIn(BaseModel):
    alias: str
    kind: str  # icao | faa | iata | station | name | spoken


class AirportProfileIn(BaseModel):
    """A complete airport profile; upserting replaces runways/frequencies/aliases."""

    icao: str = Field(pattern=r"^[A-Z0-9]{4}$")
    faa_id: str | None = None
    iata: str | None = None
    name: str
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    elevation_ft: float | None = None
    magnetic_variation: str | None = None
    timezone: str
    runways: list[AirportRunwayView] = Field(default_factory=list)
    frequencies: list[AirportFrequencyView] = Field(default_factory=list)
    aliases: list[AirportAliasIn] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
