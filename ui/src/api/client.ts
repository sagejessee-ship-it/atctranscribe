import type {
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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
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
};

export const audioUrl = (segmentId: number) => `/audio/${segmentId}`;

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
