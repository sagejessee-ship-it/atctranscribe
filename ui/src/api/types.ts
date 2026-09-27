// Mirrors src/aerochorus/review_contracts.py. The backend is authoritative:
// agreement, filtering and airport resolution are never computed here.

export type ReviewStatus = "unreviewed" | "reviewed" | "corrected";
export type TrainingLabel = "none" | "candidate" | "silver" | "gold" | "rejected";
export type TextOrigin = "human" | "model_consensus";
export type SearchScope = "any" | "human" | "consensus" | "hypotheses" | "model";
export type SortKey =
  | "utc"
  | "duration"
  | "results"
  | "exact_providers"
  | "exact_families"
  | "near_families"
  | "near_similarity"
  | "channel";
export type BatchAction = "candidate" | "silver" | "clear";

export interface ReviewFilters {
  source_keys?: string[] | null;
  include_benchmark?: boolean;
  airport?: string | null;
  channels?: string[];
  utc_from?: string | null;
  utc_to?: string | null;
  models?: string[];
  families?: string[];
  min_models?: number | null;
  min_success?: number | null;
  min_exact_providers?: number | null;
  min_exact_families?: number | null;
  min_near_families?: number | null;
  min_near_similarity?: number | null;
  max_exact_families?: number | null;
  max_near_families?: number | null;
  review_status?: ReviewStatus[];
  training_label?: TrainingLabel[];
  has_error?: boolean | null;
  has_abstention?: boolean | null;
  has_error_or_abstention?: boolean | null;
  flags?: string[];
  span_labels?: TrainingLabel[];
  has_spans?: boolean | null;
  partial_usable?: boolean | null;
  sample_id?: number | null;
  q?: string | null;
  scope?: SearchScope;
  scope_model?: string | null;
}

export interface ReviewQuery {
  filters: ReviewFilters;
  sort: SortKey;
  descending: boolean;
  offset: number;
  limit: number;
}

export interface ReviewRow {
  segment_id: number;
  source_key: string;
  relative_path: string;
  capture_start_utc: string | null;
  airport: string | null;
  station: string | null;
  channel: string | null;
  frequency_hz: number | null;
  duration_ms: number | null;
  results_count: number;
  error_count: number;
  abstained_count: number;
  best_exact_provider_count: number;
  best_exact_family_count: number;
  best_near_family_count: number;
  best_near_similarity: number | null;
  representative_text: string | null;
  representative_source: string | null;
  review_status: ReviewStatus;
  training_label: TrainingLabel;
  human_text: string | null;
  flags: string[];
  span_count: number;
}

export interface ReviewPage {
  total: number;
  offset: number;
  limit: number;
  rows: ReviewRow[];
  near_threshold: number;
  agreement_version: number;
}

export interface SavedView {
  key: string;
  name: string;
  description: string;
  filters: ReviewFilters;
  sort: SortKey;
  descending: boolean;
}

export interface Facets {
  sources: { key: string; name: string; role: string }[];
  airports: string[];
  channels: string[];
  models: { name: string; family: string; enabled: boolean }[];
  families: string[];
  flags: string[];
  near_threshold: number;
}

export interface ExactGroup {
  normalized: string;
  display_text: string;
  providers: string[];
  families: string[];
  provider_count: number;
  family_count: number;
}

export interface NearGroup {
  anchor_model: string;
  anchor_text: string;
  families: string[];
  providers: string[];
  family_count: number;
  min_similarity: number | null;
  threshold: number;
}

export interface Agreement {
  version: number;
  near_threshold: number;
  results_count: number;
  success_count: number;
  abstained_count: number;
  error_count: number;
  models: string[];
  families: string[];
  best_exact_provider_count: number;
  best_exact_family_count: number;
  best_near_family_count: number;
  best_near_similarity: number | null;
  max_pair_similarity: number | null;
  representative_text: string | null;
  representative_source: string | null;
  exact_groups: ExactGroup[];
  near_group: NearGroup | null;
  flags: Record<string, number>;
  computed_at: string;
}

export interface Hypothesis {
  result_id: string;
  model: string;
  architecture_family: string;
  status: "success" | "abstained" | "error";
  text: string | null;
  language: string | null;
  exact_group: number | null;
  similarity_to_representative: number | null;
  model_confidence: number | null;
  flags: string[];
  sweep_id: number;
  attempt: number;
  created_at: string;
  superseded: boolean;
  provenance: Record<string, unknown>;
}

export interface NeighborView {
  segment_id: number;
  offset_seconds: number | null;
  channel: string | null;
  capture_start_utc: string | null;
  duration_ms: number | null;
  preview: string | null;
  relation: "previous" | "next" | "nearby";
}

export interface AnnotationVersion {
  id: number;
  version: number;
  start_ms: number | null;
  end_ms: number | null;
  text: string | null;
  text_origin: TextOrigin | null;
  review_status: ReviewStatus;
  training_label: TrainingLabel;
  reason_tags: string[];
  notes: string | null;
  annotator: string | null;
  action: string;
  basis: Record<string, unknown>;
  created_at: string;
}

export interface AnnotationThread {
  thread_id: number;
  scope: "segment" | "span";
  current: AnnotationVersion | null;
  history: AnnotationVersion[];
}

export interface AirportRunway {
  pair: string;
  end_ident: string;
  length_ft: number | null;
  width_ft: number | null;
  true_alignment: number | null;
  spoken: string[];
}

export interface AirportFrequency {
  service: string;
  frequency_hz: number;
  facility: string | null;
  call: string | null;
  sectorization: string | null;
  spoken: string[];
}

export interface AirportProfile {
  icao: string;
  faa_id: string | null;
  iata: string | null;
  name: string;
  city: string | null;
  latitude: number | null;
  longitude: number | null;
  elevation_ft: number | null;
  magnetic_variation: string | null;
  timezone: string;
  aliases: string[];
  runways: AirportRunway[];
  frequencies: AirportFrequency[];
  provenance: Record<string, unknown>;
}

export interface SegmentReview {
  segment_id: number;
  source_key: string;
  source_role: "corpus" | "benchmark";
  relative_path: string;
  capture_start_utc: string | null;
  capture_local: string | null;
  local_timezone: string | null;
  temporal_status: string;
  airport: string | null;
  station: string | null;
  channel: string | null;
  frequency_hz: number | null;
  channel_service: string | null;
  duration_ms: number | null;
  sha256: string | null;
  agreement: Agreement | null;
  hypotheses: Hypothesis[];
  segment_annotation: AnnotationThread | null;
  span_annotations: AnnotationThread[];
  neighbors: NeighborView[];
  airport_profile: AirportProfile | null;
}

export interface AnnotationSave {
  thread_id?: number | null;
  scope?: "segment" | "span";
  start_ms?: number | null;
  end_ms?: number | null;
  text?: string | null;
  review_status: ReviewStatus;
  training_label: TrainingLabel;
  reason_tags?: string[];
  notes?: string | null;
  annotator?: string | null;
  basis?: Record<string, unknown>;
  expected_version: number;
  confirm_gold?: boolean;
}

export interface BatchRequest {
  action: BatchAction;
  segment_ids: number[];
  sample_id?: number | null;
  annotator?: string | null;
  notes?: string | null;
}

export interface BatchOutcome {
  applied: number;
  skipped: Record<string, number>;
  segment_ids_applied: number[];
}

export interface SampleView {
  id: number;
  name: string | null;
  n: number;
  seed: number;
  filters: ReviewFilters;
  filter_sha256: string;
  total_matching: number;
  segment_ids: number[];
  created_at: string;
}

// --- training datasets (src/aerochorus/dataset_contracts.py) ---------------------------

export type SplitGrouping = "utc_day_channel" | "utc_day" | "segment";

export interface DatasetCreate {
  name: string;
  description?: string | null;
  labels: TrainingLabel[];
  scopes: ("segment" | "span")[];
  filters?: ReviewFilters | null;
  split: { group_by: SplitGrouping; seed: number; train: number; validation: number; test: number };
  include_spans_of_included_segments?: boolean;
  created_by?: string | null;
}

export interface DatasetView {
  id: number;
  name: string;
  version: number;
  description: string | null;
  status: "frozen" | "exported";
  created_at: string;
  created_by: string | null;
  definition: Record<string, unknown>;
  definition_sha256: string;
  manifest_sha256: string;
  item_count: number;
  audio_ms_total: number;
  counts: Record<string, Record<string, number>>;
  export: Record<string, unknown> | null;
}

export interface TrainingSummary {
  by_label: { label: TrainingLabel; scope: "segment" | "span"; items: number; audio_ms: number }[];
  by_channel: { channel: string | null; label: TrainingLabel; items: number }[];
  by_day: { day: string | null; label: TrainingLabel; items: number }[];
}

// --- on-demand ADS-B context (src/aerochorus/api/context.py) ---------------------------

export interface AdsbAircraft {
  icao24: string;
  callsign: string | null;
  time: number;
  offset_s: number;
  lat: number | null;
  lon: number | null;
  distance_nm: number | null;
  baro_altitude_ft: number | null;
  geo_altitude_ft: number | null;
  heading_deg: number | null;
  velocity_kt: number | null;
  vertical_rate_fpm: number | null;
  on_ground: boolean | null;
  squawk: string | null;
}

export interface AdsbSnapshot {
  id: number;
  segment_id: number;
  provider: string;
  source: string;
  fetched_at: string;
  t_start: string;
  t_end: string;
  radius_nm: number;
  row_count: number;
  response_sha256: string;
  query: Record<string, unknown>;
  provider_meta: Record<string, unknown>;
  aircraft: AdsbAircraft[];
  cached: boolean;
}

export interface AdsbStatus {
  configured: boolean;
  provider: string | null;
  message: string | null;
  window_before_s: number;
  window_after_s: number;
  radius_nm: number;
  snapshot: AdsbSnapshot | null;
}
