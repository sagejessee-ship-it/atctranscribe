// Model adjudication (ADR-022): src/aerochorus/adjudication_contracts.py.
import { request } from "./client";
import type { ReviewFilters } from "./types";

export type ReasoningEffort = "low" | "medium" | "high";
export type BatchStatus = "queued" | "running" | "done" | "capped" | "cancelled";
export type ItemStatus = "queued" | "running" | "done" | "failed" | "skipped" | "cancelled";

export interface AdjudicationParams {
  model?: string | null;
  reasoning_effort: ReasoningEffort;
  include_adsb: boolean;
  include_neighbors: boolean;
  redo: boolean;
}

export interface AdjudicationSelection {
  segment_ids?: number[];
  sample_id?: number | null;
  filters?: ReviewFilters | null;
  n?: number | null;
  seed?: number;
}

export interface Pricing {
  model: string;
  source: string;
  prompt: number;
  completion: number;
  audio: number;
  fetched_at: string | null;
}

export interface RunnerSeen {
  runner: string;
  last_seen: string;
}

export interface AdjudicationPreview {
  model: string;
  prompt_version: number;
  reasoning_effort: ReasoningEffort;
  selected: number;
  eligible: number;
  skipped: Record<string, number>;
  audio_seconds: number;
  estimated_cost_usd: number;
  worst_case_cost_usd: number;
  suggested_max_cost_usd: number;
  pricing: Pricing;
  limits: Record<string, number>;
  runners: RunnerSeen[];
  segment_ids: number[];
}

export interface AdjudicationCreate {
  selection: AdjudicationSelection;
  params: AdjudicationParams;
  max_cost_usd: number;
  acknowledged_cost_usd: number;
  confirm: boolean;
  note?: string | null;
  created_by?: string | null;
}

export interface AdjudicationBatch {
  id: number;
  created_at: string;
  created_by: string | null;
  status: BatchStatus;
  model: string;
  prompt_version: number;
  params: Record<string, unknown>;
  selection: Record<string, unknown>;
  item_count: number;
  counts: Partial<Record<ItemStatus, number>>;
  estimated_cost_usd: number;
  max_cost_usd: number;
  spent_usd: number;
  accepted: number;
  note: string | null;
  finished_at: string | null;
  last_activity_at: string | null;
}

export interface AdjudicationResult {
  speech_present?: boolean;
  transcript?: string;
  confidence?: number;
  uncertain_words?: string[];
  callsigns?: string[];
  closest_hypothesis?: string;
  notes?: string;
}

export interface AdjudicationItem {
  id: number;
  batch_id: number;
  segment_id: number;
  status: ItemStatus;
  model: string;
  prompt_version: number;
  transcript: string | null;
  speech_present: boolean | null;
  confidence: number | null;
  result: AdjudicationResult | null;
  best_hypothesis_similarity: number | null;
  best_hypothesis_model: string | null;
  representative_similarity: number | null;
  cost_usd: number | null;
  usage: Record<string, unknown>;
  error: string | null;
  attempts: number;
  created_at: string;
  finished_at: string | null;
  accepted_version_id: number | null;
}

export interface AdjudicationBatchDetail extends AdjudicationBatch {
  items: AdjudicationItem[];
}

export interface AdjudicationStatus {
  default_model: string;
  runners: RunnerSeen[];
  limits: Record<string, number>;
}

export interface AdjudicationAccept {
  item_ids?: number[];
  batch_id?: number | null;
  min_confidence?: number;
  require_model_support?: boolean;
  annotator?: string | null;
  notes?: string | null;
}

export interface AcceptOutcome {
  applied: number;
  skipped: Record<string, number>;
  segment_ids_applied: number[];
}

export const adjudication = {
  status: () => request<AdjudicationStatus>("GET", "/api/v1/adjudication-status"),
  preview: (body: { selection: AdjudicationSelection; params: AdjudicationParams }) =>
    request<AdjudicationPreview>("POST", "/api/v1/adjudications/preview", body),
  create: (body: AdjudicationCreate) => request<AdjudicationBatch>("POST", "/api/v1/adjudications", body),
  list: () => request<AdjudicationBatch[]>("GET", "/api/v1/adjudications"),
  batch: (id: number) => request<AdjudicationBatchDetail>("GET", `/api/v1/adjudications/${id}`),
  cancel: (id: number) => request<AdjudicationBatch>("POST", `/api/v1/adjudications/${id}/cancel`),
  forSegment: (segmentId: number) =>
    request<AdjudicationItem[]>("GET", `/api/v1/segments/${segmentId}/adjudications`),
  accept: (body: AdjudicationAccept) =>
    request<AcceptOutcome>("POST", "/api/v1/adjudication-items/accept", body),
};

const SKIP_REASONS: Record<string, string> = {
  audio_missing: "audio missing",
  too_long: "longer than the audio limit",
  already_queued: "already queued",
  already_adjudicated: "already adjudicated (same model and prompt)",
  unknown_segment: "unknown",
  not_done_failed: "failed",
  not_done_queued: "not finished",
  not_done_running: "not finished",
  not_done_skipped: "skipped",
  not_done_cancelled: "cancelled",
  already_accepted: "already accepted",
  no_speech: "no speech",
  below_min_confidence: "below the minimum confidence",
  no_model_support: "no model hypothesis near it",
  benchmark_source: "benchmark source",
  human_gold_unchanged: "human gold",
  rejected_by_human: "rejected by a human",
  human_text_unchanged: "human text kept",
};

export function describeSkips(skipped: Record<string, number>): string {
  return Object.entries(skipped)
    .map(([reason, n]) => `${n} ${SKIP_REASONS[reason] ?? reason}`)
    .join(", ");
}

export const usd = (value: number | null | undefined, digits = 2) =>
  value == null ? "—" : `$${value < 0.01 && value > 0 ? value.toFixed(4) : value.toFixed(digits)}`;
