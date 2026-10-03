"""Wire contracts for model adjudication (ADR-022).

An adjudication sends one segment's audio together with every model hypothesis,
the agreement analysis and the airport / ADS-B context to a large audio-capable
model (default: Gemini Pro Latest through OpenRouter) and stores the transcript
it returns.

* It costs money, so it only runs on a batch a human created explicitly, after
  seeing the estimate, with a hard cost cap (reserved at worst-case per item).
* The runner lives where the audio is (the worker/edge host): it reads source
  audio read-only, verifies its sha256, and holds the OpenRouter key. The
  control plane never needs the key; browsers never see it.
* The result is model output: it may become **silver** (``text_origin =
  model_adjudicated``) or seed a human correction. It is never gold by itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from aerochorus.review_contracts import ReviewFilters


class BatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CAPPED = "capped"  # stopped: the next item could have exceeded the cost cap
    CANCELLED = "cancelled"


class ItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


ReasoningEffort = Literal["low", "medium", "high"]


class AdjudicationParams(BaseModel):
    model: str | None = None  # None: the configured default
    reasoning_effort: ReasoningEffort = "low"
    include_adsb: bool = True  # cached snapshots only; never triggers an OpenSky query
    include_neighbors: bool = True
    redo: bool = False  # also send segments already adjudicated with this model and prompt


class AdjudicationSelection(BaseModel):
    """Which segments: explicit ids, a stored sample, and/or a random sample of a filter."""

    segment_ids: list[int] = Field(default_factory=list)
    sample_id: int | None = None
    filters: ReviewFilters | None = None
    n: int | None = Field(default=None, ge=1, le=5000)  # required with filters
    seed: int = 0

    @model_validator(mode="after")
    def _something(self) -> AdjudicationSelection:
        if not self.segment_ids and self.sample_id is None and self.filters is None:
            raise ValueError("select segment_ids, a sample_id or filters")
        if self.filters is not None and self.n is None:
            raise ValueError("a filter selection needs n (how many to sample)")
        return self


class AdjudicationPreviewRequest(BaseModel):
    selection: AdjudicationSelection
    params: AdjudicationParams = Field(default_factory=AdjudicationParams)


class Pricing(BaseModel):
    """USD per million tokens, and where the numbers came from."""

    model: str
    source: str
    prompt: float
    completion: float
    audio: float
    fetched_at: datetime | None = None


class RunnerSeen(BaseModel):
    runner: str
    last_seen: datetime


class AdjudicationStatus(BaseModel):
    default_model: str
    runners: list[RunnerSeen]  # seen polling in the last five minutes
    limits: dict[str, float]


class AdjudicationPreview(BaseModel):
    model: str
    prompt_version: int
    reasoning_effort: ReasoningEffort
    selected: int
    eligible: int
    skipped: dict[str, int]
    audio_seconds: float
    estimated_cost_usd: float  # typical
    worst_case_cost_usd: float  # every item hits max_tokens
    suggested_max_cost_usd: float
    pricing: Pricing
    limits: dict[str, float]
    runners: list[RunnerSeen]
    segment_ids: list[int]


class AdjudicationCreate(BaseModel):
    selection: AdjudicationSelection
    params: AdjudicationParams = Field(default_factory=AdjudicationParams)
    max_cost_usd: float = Field(gt=0)
    # The estimate the human saw; the server recomputes it and refuses if it moved.
    acknowledged_cost_usd: float = Field(ge=0)
    confirm: bool = False
    note: str | None = None
    created_by: str | None = None


class AdjudicationBatchView(BaseModel):
    id: int
    created_at: datetime
    created_by: str | None
    status: BatchStatus
    model: str
    prompt_version: int
    params: dict[str, Any]
    selection: dict[str, Any]
    item_count: int
    counts: dict[str, int]
    estimated_cost_usd: float
    max_cost_usd: float
    spent_usd: float
    accepted: int
    note: str | None
    finished_at: datetime | None
    last_activity_at: datetime | None


class AdjudicationItemView(BaseModel):
    id: int
    batch_id: int
    segment_id: int
    status: ItemStatus
    model: str
    prompt_version: int
    transcript: str | None
    speech_present: bool | None
    confidence: float | None
    result: dict[str, Any] | None
    best_hypothesis_similarity: float | None
    best_hypothesis_model: str | None
    representative_similarity: float | None
    cost_usd: float | None
    usage: dict[str, Any]
    error: str | None
    attempts: int
    created_at: datetime
    finished_at: datetime | None
    accepted_version_id: int | None


class AdjudicationBatchDetail(AdjudicationBatchView):
    items: list[AdjudicationItemView]


class AdjudicationContext(BaseModel):
    """A segment's adjudication input as text: the chat-ready prompt for manual tests,
    and the instructions + context exactly as the automated adjudicator sends them."""

    segment_id: int
    relative_path: str
    sha256: str | None
    prompt_version: int
    chat_prompt: str
    system_prompt: str
    context_text: str


# --- runner protocol ------------------------------------------------------------------------


class AdjudicationClaimRequest(BaseModel):
    runner: str
    limit: int = Field(default=1, ge=1, le=8)


class ClaimedAdjudication(BaseModel):
    item_id: int
    batch_id: int
    segment_id: int
    source_key: str
    relative_path: str
    sha256: str | None
    duration_ms: int | None
    model: str
    prompt_version: int
    params: dict[str, Any]
    pricing: dict[str, Any]
    bundle: dict[str, Any]


class AdjudicationResultPost(BaseModel):
    runner: str
    status: Literal["done", "failed"]
    transcript: str | None = None
    result: dict[str, Any] | None = None
    request: dict[str, Any] = Field(default_factory=dict)  # never the audio itself
    response: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = Field(default=None, ge=0)
    error: str | None = None
    retryable: bool = False  # failed but worth another attempt (rate limit, outage)


# --- accepting results as silver ----------------------------------------------------------------


class AdjudicationAccept(BaseModel):
    item_ids: list[int] = Field(default_factory=list)
    batch_id: int | None = None
    min_confidence: float = Field(default=0.0, ge=0, le=1)
    # Only when the adjudicated text near-matches at least one model hypothesis.
    require_model_support: bool = False
    annotator: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _something(self) -> AdjudicationAccept:
        if not self.item_ids and self.batch_id is None:
            raise ValueError("pass item_ids or a batch_id")
        return self


class AdjudicationAcceptOutcome(BaseModel):
    applied: int
    skipped: dict[str, int]
    segment_ids_applied: list[int]
