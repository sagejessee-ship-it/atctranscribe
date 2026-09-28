import { useEffect, useMemo, useRef } from "react";
import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type RowSelectionState,
  type VisibilityState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import clsx from "clsx";
import { ArrowDown, ArrowUp } from "lucide-react";

import type { ReviewRow, SortKey } from "../api/types";
import { AgreementBadge, Badge, StatusBadge, TrainingBadge } from "../components/badges";
import { HighlightedText } from "../components/HighlightedText";
import { fmtDuration, fmtSim, fmtUtc } from "../lib/format";

interface Meta {
  sort?: SortKey;
  width: string;
  align?: "right" | "center";
  label: string;
}

const meta = (m: Meta) => ({ meta: m });

export const COLUMN_LABELS: Record<string, string> = {
  utc: "UTC",
  airport: "Airport",
  channel: "Channel",
  duration: "Duration",
  results: "Models",
  exact_providers: "Exact providers",
  exact_families: "Exact families",
  near_families: "Near families",
  near_similarity: "Near similarity",
  text: "Transcript",
  review: "Review",
  training: "Training",
  risk: "Risk",
};

function RiskCell({ row }: { row: ReviewRow }) {
  const parts = [];
  if (row.error_count) parts.push(<Badge key="e" tone="danger" title={`${row.error_count} model error(s)`}>E{row.error_count}</Badge>);
  if (row.abstained_count)
    parts.push(<Badge key="a" tone="neutral" title={`${row.abstained_count} model(s) produced no words`}>A{row.abstained_count}</Badge>);
  if (row.flags.length) parts.push(<Badge key="f" tone="warn" title={row.flags.join(", ")}>F{row.flags.length}</Badge>);
  if (row.span_count) parts.push(<Badge key="s" tone="info" title={`${row.span_count} span annotation(s)`}>S{row.span_count}</Badge>);
  if (row.best_utterance_family_count >= 2 && row.best_exact_family_count < 2)
    parts.push(
      <Badge key="u" tone="derived" title={`partial agreement: an utterance of ${row.best_utterance_tokens} words agreed by ${row.best_utterance_family_count} families`}>
        U{row.best_utterance_family_count}
      </Badge>,
    );
  return parts.length ? <span className="cell-badges">{parts}</span> : <span className="muted">—</span>;
}

function TextCell({ row }: { row: ReviewRow }) {
  if (row.human_text)
    return (
      <span className="cell-text">
        <span className="origin origin--human" title="human transcript">H</span>
        {row.human_text}
      </span>
    );
  if (row.representative_text)
    return (
      <span className="cell-text">
        <span className="origin origin--model" title={`model consensus (${row.representative_source ?? "?"})`}>M</span>
        <HighlightedText text={row.representative_text} highlights={row.representative_highlights ?? []} />
      </span>
    );
  return <span className="muted">{row.results_count ? "(no words)" : "(untranscribed)"}</span>;
}

const columns: ColumnDef<ReviewRow>[] = [
  {
    id: "select",
    ...meta({ width: "28px", align: "center", label: "Select" }),
    header: ({ table }) => (
      <input
        type="checkbox"
        aria-label="Select all rows on this page"
        checked={table.getIsAllRowsSelected()}
        ref={(el) => {
          if (el) el.indeterminate = table.getIsSomeRowsSelected();
        }}
        onChange={table.getToggleAllRowsSelectedHandler()}
      />
    ),
    cell: ({ row }) => (
      <input
        type="checkbox"
        aria-label={`Select segment ${row.original.segment_id}`}
        checked={row.getIsSelected()}
        onClick={(e) => e.stopPropagation()}
        onChange={row.getToggleSelectedHandler()}
      />
    ),
  },
  { id: "utc", ...meta({ sort: "utc", width: "142px", label: "UTC" }), cell: ({ row }) => <span className="num">{fmtUtc(row.original.capture_start_utc)}</span> },
  { id: "airport", ...meta({ width: "52px", label: "Apt" }), cell: ({ row }) => <span className="num">{row.original.airport ?? "—"}</span> },
  { id: "channel", ...meta({ sort: "channel", width: "64px", label: "Chan" }), cell: ({ row }) => row.original.channel ?? "—" },
  { id: "duration", ...meta({ sort: "duration", width: "58px", align: "right", label: "Dur" }), cell: ({ row }) => <span className="num">{fmtDuration(row.original.duration_ms)}</span> },
  { id: "results", ...meta({ sort: "results", width: "44px", align: "right", label: "Mdl" }), cell: ({ row }) => <span className="num">{row.original.results_count}</span> },
  {
    id: "exact_providers",
    ...meta({ sort: "exact_providers", width: "50px", align: "right", label: "ExP" }),
    cell: ({ row }) => <span className="num">{row.original.best_exact_provider_count || "—"}</span>,
  },
  {
    id: "exact_families",
    ...meta({ sort: "exact_families", width: "54px", align: "center", label: "ExF" }),
    cell: ({ row }) =>
      row.original.best_exact_family_count ? (
        <AgreementBadge families={row.original.best_exact_family_count} />
      ) : (
        <span className="muted">—</span>
      ),
  },
  {
    id: "near_families",
    ...meta({ sort: "near_families", width: "54px", align: "center", label: "NrF" }),
    cell: ({ row }) =>
      row.original.best_near_family_count ? (
        <AgreementBadge families={row.original.best_near_family_count} kind="near" />
      ) : (
        <span className="muted">—</span>
      ),
  },
  {
    id: "near_similarity",
    ...meta({ sort: "near_similarity", width: "58px", align: "right", label: "NrSim" }),
    cell: ({ row }) => <span className="num">{fmtSim(row.original.best_near_similarity)}</span>,
  },
  { id: "text", ...meta({ width: "minmax(220px, 1fr)", label: "Transcript" }), cell: ({ row }) => <TextCell row={row.original} /> },
  { id: "review", ...meta({ width: "88px", label: "Review" }), cell: ({ row }) => <StatusBadge status={row.original.review_status} /> },
  { id: "training", ...meta({ width: "84px", label: "Training" }), cell: ({ row }) => <TrainingBadge label={row.original.training_label} /> },
  { id: "risk", ...meta({ width: "96px", label: "Risk" }), cell: ({ row }) => <RiskCell row={row.original} /> },
];

const HEADER_TITLES: Record<string, string> = {
  results: "model results recorded (latest per model)",
  exact_providers: "largest exact-match group: number of models",
  exact_families: "largest exact-match group: independent architecture families",
  near_families: "near-match group: architecture families within the threshold",
  near_similarity: "weakest similarity inside the near-match group (not a probability)",
};

export function SegmentDataGrid({
  rows,
  loading,
  sort,
  descending,
  activeId,
  selection,
  visibility,
  onSort,
  onActivate,
  onSelection,
}: {
  rows: ReviewRow[];
  loading: boolean;
  sort: SortKey;
  descending: boolean;
  activeId: number | null;
  selection: RowSelectionState;
  visibility: VisibilityState;
  onSort: (sort: SortKey, descending: boolean) => void;
  onActivate: (id: number) => void;
  onSelection: (selection: RowSelectionState) => void;
}) {
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => String(row.segment_id),
    manualSorting: true,
    enableRowSelection: true,
    state: { rowSelection: selection, columnVisibility: visibility },
    onRowSelectionChange: (updater) =>
      onSelection(typeof updater === "function" ? updater(selection) : updater),
  });
  const visible = table.getVisibleLeafColumns();
  const template = visible.map((c) => (c.columnDef.meta as Meta).width).join(" ");

  const scroller = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const tableRows = table.getRowModel().rows;
  const virtualizer = useVirtualizer({
    count: tableRows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => 32,
    overscan: 12,
  });
  const activeIndex = useMemo(() => rows.findIndex((r) => r.segment_id === activeId), [rows, activeId]);
  useEffect(() => {
    if (activeIndex >= 0) virtualizer.scrollToIndex(activeIndex, { align: "auto" });
  }, [activeIndex, virtualizer]);

  return (
    <div
      ref={gridRef}
      className="grid"
      role="grid"
      tabIndex={0}
      aria-label="Segments (J/K to move)"
      aria-rowcount={rows.length + 1}
      aria-busy={loading}
    >
      <div className="grid__scroll" ref={scroller}>
        <div className="grid__head" role="row" style={{ gridTemplateColumns: template }}>
          {table.getHeaderGroups()[0].headers.map((header) => {
            const m = header.column.columnDef.meta as Meta;
            const sortable = !!m.sort;
            const active = sortable && m.sort === sort;
            const content =
              header.id === "select" ? flexRender(header.column.columnDef.header, header.getContext()) : m.label;
            return (
              <div
                key={header.id}
                role="columnheader"
                aria-sort={active ? (descending ? "descending" : "ascending") : undefined}
                className={clsx("grid__hcell", m.align && `grid__cell--${m.align}`)}
                title={HEADER_TITLES[header.id] ?? COLUMN_LABELS[header.id]}
              >
                {sortable ? (
                  <button
                    type="button"
                    className={clsx("grid__sort", active && "grid__sort--active")}
                    onClick={() => onSort(m.sort!, active ? !descending : true)}
                  >
                    {content}
                    {active ? descending ? <ArrowDown size={11} aria-hidden /> : <ArrowUp size={11} aria-hidden /> : null}
                  </button>
                ) : (
                  content
                )}
              </div>
            );
          })}
        </div>
        {!loading && rows.length === 0 ? (
          <div className="grid__empty">No segments match these filters.</div>
        ) : null}
        <div className="grid__body" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((item) => {
            const row = tableRows[item.index];
            const id = row.original.segment_id;
            return (
              <div
                key={row.id}
                role="row"
                aria-selected={id === activeId}
                data-segment-id={id}
                className={clsx(
                  "grid__row",
                  row.getIsSelected() && "grid__row--selected",
                  id === activeId && "grid__row--active",
                )}
                style={{ gridTemplateColumns: template, transform: `translateY(${item.start}px)` }}
                onClick={() => {
                  // Take keyboard focus from e.g. the search box so J/K/Space work next.
                  gridRef.current?.focus({ preventScroll: true });
                  onActivate(id);
                }}
              >
                {row.getVisibleCells().map((cell) => {
                  const m = cell.column.columnDef.meta as Meta;
                  return (
                    <div key={cell.id} role="gridcell" className={clsx("grid__cell", m.align && `grid__cell--${m.align}`)}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
