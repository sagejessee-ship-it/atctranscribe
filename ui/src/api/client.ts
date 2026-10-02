import type {
  AdsbSnapshot,
  AdsbStatus,
  AnnotationSave,
  DatasetCreate,
  DatasetView,
  TrainingSummary,
  AnnotationThread,
  BatchOutcome,
  BatchRequest,
  Facets,
  ReviewFilters,
  ReviewPage,
  ReviewQuery,
  SampleView,
  SavedView,
  SegmentReview,
} from "./types";

/** An API failure with the server's own explanation (FastAPI `detail`). */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // Pydantic validation errors
    return detail
      .map((d) => (typeof d === "object" && d && "msg" in d ? String(d.msg) : String(d)))
      .join("; ");
  }
  return JSON.stringify(detail);
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
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
    let detail: string = response.statusText;
    try {
      detail = describe((await response.json()).detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  query: (body: ReviewQuery) => request<ReviewPage>("POST", "/api/v1/review/query", body),
  views: () => request<SavedView[]>("GET", "/api/v1/review/views"),
  facets: () => request<Facets>("GET", "/api/v1/review/facets"),
  segment: (id: number) => request<SegmentReview>("GET", `/api/v1/review/segments/${id}`),
  saveAnnotation: (id: number, body: AnnotationSave) =>
    request<AnnotationThread>("POST", `/api/v1/review/segments/${id}/annotations`, body),
  batch: (body: BatchRequest) => request<BatchOutcome>("POST", "/api/v1/review/batch", body),
  createSample: (body: { filters: ReviewFilters; n: number; seed: number; name?: string }) =>
    request<SampleView>("POST", "/api/v1/review/samples", body),
  sample: (id: number) => request<SampleView>("GET", `/api/v1/review/samples/${id}`),
  trainingSummary: () => request<TrainingSummary>("GET", "/api/v1/training/summary"),
  datasets: () => request<DatasetView[]>("GET", "/api/v1/datasets"),
  createDataset: (body: DatasetCreate) => request<DatasetView>("POST", "/api/v1/datasets", body),
  adsbStatus: (id: number) => request<AdsbStatus>("GET", `/api/v1/context/adsb/${id}`),
  adsbFetch: (id: number, refresh = false) =>
    request<AdsbSnapshot>("POST", `/api/v1/context/adsb/${id}${refresh ? "?refresh=true" : ""}`),
};

export const audioUrl = (segmentId: number) => `/audio/${segmentId}`;

export const TEST_PACK_MAX = 200;

/**
 * Selected segments' audio + chat-ready adjudication prompts as one zip, built by the
 * edge server (which reads the audio), saved through the browser's normal download.
 */
export async function downloadTestPack(segmentIds: number[]): Promise<{ filename: string; segments: number; skipped: number }> {
  let response: Response;
  try {
    response = await fetch("/edge/test-pack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segment_ids: segmentIds }),
    });
  } catch (error) {
    throw new ApiError(0, `AeroChorus server unreachable (${(error as Error).message})`);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = describe((await response.json()).detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  const blob = await response.blob();
  const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "");
  const filename = match?.[1] ?? "aerochorus-test-pack.zip";
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  return {
    filename,
    segments: Number(response.headers.get("X-Pack-Segments") ?? segmentIds.length),
    skipped: Number(response.headers.get("X-Pack-Skipped") ?? 0),
  };
}

/** Fetch the edge server's explanation when an <audio> element fails to load. */
export async function audioProblem(segmentId: number): Promise<string> {
  try {
    const response = await fetch(audioUrl(segmentId), { headers: { Range: "bytes=0-0" } });
    if (response.ok) return "the browser could not decode this audio";
    try {
      return describe((await response.json()).detail);
    } catch {
      return `${response.status} ${response.statusText}`;
    }
  } catch (error) {
    return `review edge server unreachable (${(error as Error).message})`;
  }
}
