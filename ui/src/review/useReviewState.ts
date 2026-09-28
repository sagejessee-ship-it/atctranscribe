import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";

import type { ReviewFilters, ReviewQuery, SortKey } from "../api/types";

export const PAGE_SIZES = [50, 100, 200, 500, 1000] as const;
const DEFAULT_SIZE = 100;

export interface ReviewState {
  filters: ReviewFilters;
  view: string | null;
  sort: SortKey;
  descending: boolean;
  page: number;
  size: number;
  selected: number | null;
}

function parseFilters(raw: string | null): ReviewFilters {
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? (value as ReviewFilters) : {};
  } catch {
    return {};
  }
}

/** Drop empty values so URLs stay short and chips only show real constraints. */
export function compactFilters(filters: ReviewFilters): ReviewFilters {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(filters)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value) && value.length === 0) continue;
    if (key === "scope" && value === "any") continue;
    if (key === "include_benchmark" && value === false) continue;
    if (key === "min_models" && value === 1) continue; // the server default
    out[key] = value;
  }
  return out as ReviewFilters;
}

/**
 * Review state lives in the URL: filters, sort, page and the selected segment
 * survive reloads, and back/forward walks through filter changes. Selection
 * changes replace the history entry so J/K does not flood it.
 */
export function useReviewState() {
  const [params, setParams] = useSearchParams();

  const state: ReviewState = useMemo(
    () => ({
      filters: parseFilters(params.get("f")),
      view: params.get("view"),
      sort: (params.get("sort") as SortKey) || "utc",
      descending: params.get("desc") !== "0",
      page: Math.max(0, Number(params.get("page") ?? 0) || 0),
      size: Number(params.get("size")) || DEFAULT_SIZE,
      selected: params.get("seg") ? Number(params.get("seg")) : null,
    }),
    [params],
  );

  const update = useCallback(
    (patch: Partial<ReviewState>, options: { replace?: boolean } = {}) => {
      setParams(
        () => {
          // Router updates are transitions: derive from the live URL, not a possibly stale
          // render, so rapid J/K presses and filter edits never undo each other.
          const next = new URLSearchParams(window.location.search);
          const set = (key: string, value: string | null) =>
            value === null ? next.delete(key) : next.set(key, value);
          if ("filters" in patch) {
            const compact = compactFilters(patch.filters ?? {});
            set("f", Object.keys(compact).length ? JSON.stringify(compact) : null);
            if (!("page" in patch)) next.delete("page");
            if (!("view" in patch)) next.delete("view");
          }
          if ("view" in patch) set("view", patch.view ?? null);
          if ("sort" in patch) set("sort", patch.sort === "utc" ? null : (patch.sort ?? null));
          if ("descending" in patch) set("desc", patch.descending ? null : "0");
          if ("page" in patch) set("page", patch.page ? String(patch.page) : null);
          if ("size" in patch) set("size", patch.size === DEFAULT_SIZE ? null : String(patch.size));
          if ("selected" in patch) set("seg", patch.selected == null ? null : String(patch.selected));
          return next;
        },
        { replace: options.replace },
      );
    },
    [setParams],
  );

  const query: ReviewQuery = useMemo(
    () => ({
      filters: state.filters,
      sort: state.sort,
      descending: state.descending,
      offset: state.page * state.size,
      limit: state.size,
    }),
    [state.filters, state.sort, state.descending, state.page, state.size],
  );

  return { state, update, query };
}
