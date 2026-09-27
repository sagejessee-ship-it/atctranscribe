import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import type { RowSelectionState, VisibilityState } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { api } from "../api/client";
import type { ReviewFilters, SavedView } from "../api/types";
import { Button, ErrorBox, IconButton } from "../components/ui";
import { SegmentInspector, type InspectorCommands } from "../inspector/SegmentInspector";
import { readPref, writePref } from "../lib/annotator";
import { fmtCount } from "../lib/format";
import { BatchActionBar } from "./BatchActionBar";
import { ActiveFilterChips, CorpusToolbar } from "./CorpusToolbar";
import { FilterRail } from "./FilterRail";
import { SampleDialog } from "./SampleDialog";
import { COLUMN_LABELS, SegmentDataGrid } from "./SegmentDataGrid";
import { PAGE_SIZES, useReviewState } from "./useReviewState";

const TEXT_INPUT = /^(text|search|number|email|url|tel|password|date|datetime-local|time)$/;

function isEditing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  if (el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
  return el.tagName === "INPUT" && TEXT_INPUT.test((el as HTMLInputElement).type);
}

export function ReviewPage({ onHelp }: { onHelp: () => void }) {
  const { state, update, query } = useReviewState();
  const client = useQueryClient();
  const page = useQuery({
    queryKey: ["review", query],
    queryFn: () => api.query(query),
    placeholderData: keepPreviousData,
  });
  const facets = useQuery({ queryKey: ["facets"], queryFn: api.facets, staleTime: 60_000 });
  const views = useQuery({ queryKey: ["views"], queryFn: api.views, staleTime: Infinity });
  const sample = useQuery({
    queryKey: ["sample", state.filters.sample_id],
    queryFn: () => api.sample(state.filters.sample_id!),
    enabled: state.filters.sample_id != null,
  });

  const rows = useMemo(() => page.data?.rows ?? [], [page.data]);
  const [selection, setSelection] = useState<RowSelectionState>({});
  const [visibility, setVisibility] = useState<VisibilityState>(() => readPref("columns", {}));
  const [railOpen, setRailOpen] = useState<boolean>(() => readPref("rail", true));
  const [inspectorOpen, setInspectorOpen] = useState<boolean>(() => readPref("inspector", true));
  const [inspectorWidth, setInspectorWidth] = useState<number>(() =>
    readPref("inspectorWidth", Math.round(Math.min(560, Math.max(380, window.innerWidth * 0.38)))),
  );
  const [sampling, setSampling] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const commands = useRef<InspectorCommands | null>(null);
  const pendingSelect = useRef<"first" | "last" | null>(null);

  useEffect(() => writePref("columns", visibility), [visibility]);
  useEffect(() => writePref("rail", railOpen), [railOpen]);
  useEffect(() => writePref("inspector", inspectorOpen), [inspectorOpen]);
  useEffect(() => writePref("inspectorWidth", inspectorWidth), [inspectorWidth]);
  // A new filter set starts a new selection.
  const filterKey = JSON.stringify(state.filters);
  useEffect(() => setSelection({}), [filterKey]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 6000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  // The selection is also tracked synchronously: URL-driven state lags behind rapid keypresses.
  const selectedRef = useRef(state.selected);
  useEffect(() => {
    selectedRef.current = state.selected;
  }, [state.selected]);
  const select = useCallback(
    (id: number | null) => {
      selectedRef.current = id;
      update({ selected: id }, { replace: true });
    },
    [update],
  );

  // After paging via J/K, land on the first/last row of the new page.
  useEffect(() => {
    if (!pendingSelect.current || page.isPlaceholderData || !rows.length) return;
    select(pendingSelect.current === "first" ? rows[0].segment_id : rows[rows.length - 1].segment_id);
    pendingSelect.current = null;
  }, [rows, page.isPlaceholderData, select]);

  const total = page.data?.total ?? 0;
  const lastPage = Math.max(0, Math.ceil(total / state.size) - 1);
  const index = rows.findIndex((r) => r.segment_id === state.selected);

  const move = useCallback(
    (delta: 1 | -1) => {
      if (!rows.length) return;
      const index = rows.findIndex((r) => r.segment_id === selectedRef.current);
      if (index < 0) return select(rows[delta > 0 ? 0 : rows.length - 1].segment_id);
      const next = index + delta;
      if (next >= 0 && next < rows.length) return select(rows[next].segment_id);
      if (delta > 0 && state.page < lastPage) {
        pendingSelect.current = "first";
        update({ page: state.page + 1 });
      } else if (delta < 0 && state.page > 0) {
        pendingSelect.current = "last";
        update({ page: state.page - 1 });
      }
    },
    [rows, select, state.page, lastPage, update],
  );
  const canPrev = index > 0 || state.page > 0;
  const canNext = (index >= 0 && index < rows.length - 1) || state.page < lastPage;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || isEditing(e.target)) return;
      if (document.querySelector("[role=dialog]")) return;
      const c = commands.current;
      const handled = (() => {
        switch (e.key) {
          case " ":
            c?.togglePlay();
            return true;
          case "j":
          case "J":
          case "ArrowUp":
            move(-1);
            return true;
          case "k":
          case "K":
          case "ArrowDown":
            move(1);
            return true;
          case "r":
          case "R":
            c?.replay();
            return true;
          case "l":
          case "L":
            c?.toggleLoop();
            return true;
          case "u":
          case "U":
            c?.nextUtterance();
            return true;
          case "c":
          case "C":
            c?.focusCorrection();
            return true;
          case "a":
          case "A":
            c?.useSelectedHypothesis();
            return true;
          case "s":
          case "S":
            c?.markSilver();
            return true;
          case "g":
          case "G":
            c?.markGold();
            return true;
          case "x":
          case "X":
            c?.reject();
            return true;
          case "?":
            onHelp();
            return true;
          default:
            return false;
        }
      })();
      if (handled) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [move, onHelp]);

  const setFilters = (filters: ReviewFilters) => update({ filters });
  const patchFilters = (patch: Partial<ReviewFilters>) => update({ filters: { ...state.filters, ...patch } });
  const applyView = (view: SavedView | null) =>
    view
      ? update({ filters: view.filters, view: view.key, sort: view.sort, descending: view.descending, page: 0 })
      : update({ filters: {}, view: null, page: 0 });

  const selectedIds = Object.keys(selection)
    .filter((k) => selection[k])
    .map(Number);
  const columns = Object.entries(COLUMN_LABELS).map(([id, label]) => ({
    id,
    label,
    visible: visibility[id] !== false,
  }));

  const startDrag = (event: React.PointerEvent) => {
    const startX = event.clientX;
    const startWidth = inspectorWidth;
    const onMove = (e: PointerEvent) =>
      setInspectorWidth(Math.min(900, Math.max(360, startWidth + (startX - e.clientX))));
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  return (
    <div className="review">
      <CorpusToolbar
        filters={state.filters}
        facets={facets.data}
        views={views.data}
        activeView={state.view}
        total={page.data?.total}
        fetching={page.isFetching}
        columns={columns}
        railOpen={railOpen}
        inspectorOpen={inspectorOpen}
        onFilters={patchFilters}
        onView={applyView}
        onSample={() => setSampling(true)}
        onColumn={(id, visible) => setVisibility({ ...visibility, [id]: visible })}
        onRefresh={() => client.invalidateQueries()}
        onToggleRail={() => setRailOpen((v) => !v)}
        onToggleInspector={() => setInspectorOpen((v) => !v)}
      />
      <ActiveFilterChips filters={state.filters} onChange={setFilters} />
      {sample.data ? (
        <div className="banner" role="status">
          Sample #{sample.data.id}
          {sample.data.name ? ` “${sample.data.name}”` : ""}: {sample.data.segment_ids.length} of{" "}
          {fmtCount(sample.data.total_matching)} matching, seed <span className="num">{sample.data.seed}</span>, filter{" "}
          <span className="num" title={sample.data.filter_sha256}>
            {sample.data.filter_sha256.slice(0, 12)}
          </span>
        </div>
      ) : null}
      <BatchActionBar selectedIds={selectedIds} onClear={() => setSelection({})} onDone={setToast} />
      <div className="review__body">
        {railOpen ? <FilterRail filters={state.filters} facets={facets.data} onChange={patchFilters} /> : null}
        <div className="review__grid">
          {page.isError ? (
            <ErrorBox
              title="Segments could not be loaded"
              detail={(page.error as Error).message}
              action={
                <Button size="sm" onClick={() => page.refetch()}>
                  Retry
                </Button>
              }
            />
          ) : null}
          <SegmentDataGrid
            rows={rows}
            loading={page.isPending}
            sort={state.sort}
            descending={state.descending}
            activeId={state.selected}
            selection={selection}
            visibility={visibility}
            onSort={(sort, descending) => update({ sort, descending, page: 0 })}
            onActivate={(id) => select(id)}
            onSelection={setSelection}
          />
          <div className="grid__footer">
            <span className="num">
              {total ? `${fmtCount(state.page * state.size + 1)}–${fmtCount(Math.min(total, (state.page + 1) * state.size))}` : "0"} of{" "}
              {fmtCount(total)}
            </span>
            <IconButton label="Previous page" disabled={state.page === 0} onClick={() => update({ page: state.page - 1 })}>
              <ChevronLeft size={14} />
            </IconButton>
            <IconButton label="Next page" disabled={state.page >= lastPage} onClick={() => update({ page: state.page + 1 })}>
              <ChevronRight size={14} />
            </IconButton>
            <label className="grid__size">
              <span className="muted">rows</span>
              <select
                className="select"
                value={state.size}
                onChange={(e) => update({ size: Number(e.target.value), page: 0 })}
                aria-label="Rows per page"
              >
                {PAGE_SIZES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            {page.data ? (
              <span className="muted grid__meta">
                agreement v{page.data.agreement_version} · near ≥ {page.data.near_threshold}
              </span>
            ) : null}
          </div>
        </div>
        {inspectorOpen ? (
          <>
            <div
              className="splitter"
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize inspector"
              aria-valuenow={inspectorWidth}
              aria-valuemin={360}
              aria-valuemax={900}
              tabIndex={0}
              onPointerDown={startDrag}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") setInspectorWidth((w) => Math.min(900, w + 24));
                if (e.key === "ArrowRight") setInspectorWidth((w) => Math.max(360, w - 24));
              }}
            />
            <div className="review__inspector" style={{ width: inspectorWidth }}>
              <SegmentInspector
                segmentId={state.selected}
                commands={commands}
                onPrev={canPrev ? () => move(-1) : undefined}
                onNext={canNext ? () => move(1) : undefined}
                onOpen={(id) => select(id)}
                onToast={setToast}
              />
            </div>
          </>
        ) : null}
      </div>
      <div className="toast" role="status" aria-live="polite">
        {toast}
      </div>
      <SampleDialog
        open={sampling}
        onOpenChange={setSampling}
        filters={state.filters}
        total={page.data?.total}
        onCreated={(s) => {
          update({ filters: { sample_id: s.id, min_models: 0 }, page: 0 });
          setToast(`Sample #${s.id}: ${s.segment_ids.length} segments (seed ${s.seed})`);
        }}
      />
    </div>
  );
}
