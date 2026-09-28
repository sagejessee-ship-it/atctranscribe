import { useEffect, useRef, useState } from "react";
import { Columns3, Dices, PanelLeft, PanelRight, RefreshCw, Search, Sparkles, X } from "lucide-react";

import type { Facets, ReviewFilters, SavedView, SearchScope } from "../api/types";
import { Button, IconButton, Menu } from "../components/ui";
import { fmtCount } from "../lib/format";
import { SCOPES, describeFilters } from "./filterMeta";

export function SavedViewSelector({
  views,
  active,
  onSelect,
}: {
  views: SavedView[] | undefined;
  active: string | null;
  onSelect: (view: SavedView | null) => void;
}) {
  return (
    <label className="toolbar__field">
      <span className="sr-only">Saved view</span>
      <select
        className="select select--view"
        aria-label="Saved view"
        value={active ?? ""}
        onChange={(e) => onSelect(views?.find((v) => v.key === e.target.value) ?? null)}
      >
        <option value="">All transcribed segments</option>
        {views?.map((v) => (
          <option key={v.key} value={v.key} title={v.description}>
            {v.name}
          </option>
        ))}
      </select>
    </label>
  );
}

export function SearchBox({
  filters,
  facets,
  onApply,
}: {
  filters: ReviewFilters;
  facets: Facets | undefined;
  onApply: (patch: Partial<ReviewFilters>) => void;
}) {
  const [draft, setDraft] = useState(filters.q ?? "");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => setDraft(filters.q ?? ""), [filters.q]);
  // "/" focuses search, like most workbenches.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const scope = filters.scope ?? "any";
  return (
    <form
      className="search"
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        onApply({ q: draft.trim() || null });
      }}
    >
      <Search size={14} className="search__icon" aria-hidden />
      <input
        ref={input}
        className="input search__input"
        type="search"
        aria-label="Search transcripts"
        placeholder="Search transcripts  ( / )"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
      {filters.q ? (
        <IconButton label="Clear search" onClick={() => onApply({ q: null })}>
          <X size={13} />
        </IconButton>
      ) : null}
      <select
        className="select"
        aria-label="Search scope"
        value={scope}
        onChange={(e) =>
          onApply({
            scope: e.target.value as SearchScope,
            scope_model: e.target.value === "model" ? (filters.scope_model ?? facets?.models[0]?.name) : null,
          })
        }
      >
        {SCOPES.map((s) => (
          <option key={s.value} value={s.value}>
            {s.label}
          </option>
        ))}
      </select>
      {scope === "model" ? (
        <select
          className="select"
          aria-label="Model to search"
          value={filters.scope_model ?? ""}
          onChange={(e) => onApply({ scope_model: e.target.value })}
        >
          {facets?.models.map((m) => (
            <option key={m.name} value={m.name}>
              {m.name}
            </option>
          ))}
        </select>
      ) : null}
    </form>
  );
}

export function ActiveFilterChips({
  filters,
  onChange,
}: {
  filters: ReviewFilters;
  onChange: (filters: ReviewFilters) => void;
}) {
  const chips = describeFilters(filters);
  if (!chips.length) return null;
  return (
    <div className="chips" aria-label="Active filters">
      {chips.map((chip) => (
        <span key={chip.key} className="chip">
          {chip.label}
          <button
            type="button"
            className="chip__remove"
            aria-label={`Remove filter ${chip.label}`}
            onClick={() => onChange(chip.clear(filters))}
          >
            <X size={11} aria-hidden />
          </button>
        </span>
      ))}
      <Button variant="ghost" size="sm" onClick={() => onChange({})}>
        Clear all
      </Button>
    </div>
  );
}

export interface ColumnToggle {
  id: string;
  label: string;
  visible: boolean;
}

export function CorpusToolbar({
  filters,
  facets,
  views,
  activeView,
  total,
  fetching,
  columns,
  railOpen,
  inspectorOpen,
  onFilters,
  onView,
  onSample,
  onAdjudicate,
  onColumn,
  onRefresh,
  onToggleRail,
  onToggleInspector,
}: {
  filters: ReviewFilters;
  facets: Facets | undefined;
  views: SavedView[] | undefined;
  activeView: string | null;
  total: number | undefined;
  fetching: boolean;
  columns: ColumnToggle[];
  railOpen: boolean;
  inspectorOpen: boolean;
  onFilters: (patch: Partial<ReviewFilters>) => void;
  onView: (view: SavedView | null) => void;
  onSample: () => void;
  onAdjudicate?: () => void;
  onColumn: (id: string, visible: boolean) => void;
  onRefresh: () => void;
  onToggleRail: () => void;
  onToggleInspector: () => void;
}) {
  return (
    <div className="toolbar" role="toolbar" aria-label="Corpus toolbar">
      <IconButton label={railOpen ? "Hide filters" : "Show filters"} onClick={onToggleRail} aria-pressed={railOpen}>
        <PanelLeft size={15} />
      </IconButton>
      <SavedViewSelector views={views} active={activeView} onSelect={onView} />
      <SearchBox filters={filters} facets={facets} onApply={onFilters} />
      <span className="toolbar__spacer" />
      <span className="count num" aria-live="polite">
        {total == null ? "…" : `${fmtCount(total)} ${total === 1 ? "segment" : "segments"}`}
        {fetching ? <span className="muted"> · loading</span> : null}
      </span>
      <Button size="sm" onClick={onSample} title="Deterministic sample from the current filters">
        <Dices size={13} aria-hidden /> Sample N
      </Button>
      {onAdjudicate ? (
        <Button size="sm" onClick={onAdjudicate} title="Send a random sample of the current filters to Gemini (priced and capped first)">
          <Sparkles size={13} aria-hidden /> Adjudicate…
        </Button>
      ) : null}
      <Menu.Root>
        <Menu.Trigger asChild>
          <IconButton label="Columns">
            <Columns3 size={15} />
          </IconButton>
        </Menu.Trigger>
        <Menu.Content align="end">
          <Menu.Label>Columns</Menu.Label>
          {columns.map((c) => (
            <Menu.Check key={c.id} checked={c.visible} onChange={(v) => onColumn(c.id, v)}>
              {c.label}
            </Menu.Check>
          ))}
        </Menu.Content>
      </Menu.Root>
      <IconButton label="Refresh" onClick={onRefresh}>
        <RefreshCw size={14} />
      </IconButton>
      <IconButton
        label={inspectorOpen ? "Hide inspector" : "Show inspector"}
        onClick={onToggleInspector}
        aria-pressed={inspectorOpen}
      >
        <PanelRight size={15} />
      </IconButton>
    </div>
  );
}
