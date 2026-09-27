// Transcription runs (sweeps), models and workers: src/aerochorus/sweep_contracts.py.
import { ApiError } from "./client";

export interface ModelRead {
  logical_name: string;
  architecture_family: string;
  crisp_backend: string;
  quantization: string | null;
  artifact_size_bytes: number | null;
  enabled: boolean;
  sweep_eligible: boolean;
  experimental: boolean;
  ensemble_eligible: boolean;
}

export interface SuiteRead {
  name: string;
  description: string | null;
  models: string[];
}

export interface SweepSelection {
  source_key: string;
  utc_from?: string | null;
  utc_to?: string | null;
  relative_dir?: string | null;
  channels?: string[];
  min_duration_ms?: number | null;
  max_duration_ms?: number | null;
  limit?: number | null;
  seed?: number;
  untranscribed_only?: boolean;
}

export interface SweepCreate {
  models: string[];
  selection: SweepSelection;
  name?: string | null;
}

export interface SweepPreview {
  segments: number;
  audio_minutes: number;
  models: {
    logical_name: string;
    architecture_family: string;
    enabled: boolean;
    sweep_eligible: boolean;
    ensemble_eligible: boolean;
    observed_rtf: number | null;
    estimated_minutes: number | null;
  }[];
  estimated_minutes: number | null;
  needs_unqualified: string[];
  warnings: string[];
}

export type RunStatus = "queued" | "loading" | "running" | "completed" | "failed" | "retrying";
export type SweepStatus = "queued" | "running" | "paused" | "completed" | "partial" | "cancelled";

export interface SweepModelRead {
  id: number;
  logical_name: string;
  architecture_family: string;
  execution_order: number;
  status: RunStatus;
  attempts: number;
  claimed_by: string | null;
  segments_total: number;
  segments_completed: number;
  segments_abstained: number;
  segments_error: number;
  audio_ms_total: number;
  inference_ms_total: number;
  real_time_factor: number | null;
  last_error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface SweepRead {
  id: number;
  name: string | null;
  status: SweepStatus;
  suite: string;
  selection: SweepSelection;
  segments_total: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  models: SweepModelRead[];
}

export interface WorkerRead {
  name: string;
  hostname: string;
  platform: string;
  version: string;
  health: Record<string, unknown>;
  last_heartbeat_at: string;
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    throw new ApiError(0, `AeroChorus server unreachable (${(error as Error).message})`);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const runs = {
  models: () => call<ModelRead[]>("GET", "/api/v1/models"),
  suites: () => call<SuiteRead[]>("GET", "/api/v1/suites"),
  workers: () => call<WorkerRead[]>("GET", "/api/v1/workers"),
  list: (limit = 25) => call<SweepRead[]>("GET", `/api/v1/sweeps?limit=${limit}`),
  preview: (body: SweepCreate) => call<SweepPreview>("POST", "/api/v1/sweeps/preview", body),
  create: (body: SweepCreate, allowUnqualified: boolean) =>
    call<SweepRead>("POST", `/api/v1/sweeps?allow_unqualified=${allowUnqualified}`, body),
  control: (id: number, action: "pause" | "resume" | "cancel" | "retry") =>
    call<SweepRead>("POST", `/api/v1/sweeps/${id}/${action}`),
};
