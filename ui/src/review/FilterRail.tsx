import { useEffect, useId, useState, type ReactNode } from "react";

import type { Facets, ReviewFilters } from "../api/types";
import { REVIEW_STATUSES, TRAINING_LABELS } from "./filterMeta";

type Patch = (patch: Partial<ReviewFilters>) => void;

function Group({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <fieldset className="rail__group" aria-labelledby={id}>
      <legend className="rail__title" id={id}>
        {title}
      </legend>
      {children}
    </fieldset>
  );
}

function Checks<T extends string>({
  options,
  value,
  onChange,
  render = (o) => o,
}: {
  options: readonly T[];
  value: T[] | undefined;
  onChange: (next: T[]) => void;
  render?: (option: T) => ReactNode;
}) {
  const selected = new Set(value ?? []);
  if (!options.length) return <div className="muted rail__empty">none</div>;
  return (
    <div className="rail__checks">
      {options.map((option) => (
        <label key={option} className="rail__check">
          <input
            type="checkbox"
            checked={selected.has(option)}
            onChange={(e) => {
              const next = new Set(selected);
              if (e.target.checked) next.add(option);
              else next.delete(option);
              onChange(options.filter((o) => next.has(o)));
            }}
          />
          <span>{render(option)}</span>
        </label>
      ))}
    </div>
  );
}

/** Commits on blur/Enter so typing does not fire a query per keystroke. */
function NumberField({
  label,
  value,
  onCommit,
  step = 1,
  min = 0,
  max,
  placeholder = "any",
}: {
  label: string;
  value: number | null | undefined;
  onCommit: (value: number | null) => void;
  step?: number;
  min?: number;
  max?: number;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState(value == null ? "" : String(value));
  useEffect(() => setDraft(value == null ? "" : String(value)), [value]);
  const commit = () => {
    const parsed = draft.trim() === "" ? null : Number(draft);
    if (parsed !== null && Number.isNaN(parsed)) return;
    if (parsed !== (value ?? null)) onCommit(parsed);
  };
  return (
    <label className="rail__field">
      <span>{label}</span>
      <input
        className="input input--num num"
        type="number"
        inputMode="decimal"
        step={step}
        min={min}
        max={max}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && commit()}
      />
    </label>
  );
}

function TriState({
  label,
  value,
  onChange,
}: {
  label: string;
  value: boolean | null | undefined;
  onChange: (value: boolean | null) => void;
}) {
  return (
    <label className="rail__field">
      <span>{label}</span>
      <select
        className="select"
        value={value == null ? "" : value ? "yes" : "no"}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "yes")}
      >
        <option value="">any</option>
        <option value="yes">yes</option>
        <option value="no">no</option>
      </select>
    </label>
  );
}

const toUtcInput = (iso: string | null | undefined) => (iso ? iso.slice(0, 16) : "");
const fromUtcInput = (value: string) => (value ? `${value}:00Z` : null);

export function FilterRail({
  filters,
  facets,
  onChange,
}: {
  filters: ReviewFilters;
  facets: Facets | undefined;
  onChange: Patch;
}) {
  const models = facets?.models.map((m) => m.name) ?? [];
  const familyOf = Object.fromEntries(facets?.models.map((m) => [m.name, m.family]) ?? []);
  return (
    <aside className="rail" aria-label="Filters">
      <Group title="Where">
        <label className="rail__field">
          <span>Airport</span>
          <select
            className="select"
            value={filters.airport ?? ""}
            onChange={(e) => onChange({ airport: e.target.value || null })}
          >
            <option value="">all</option>
            {facets?.airports.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <div className="rail__sub">Channel</div>
        <Checks
          options={facets?.channels ?? []}
          value={filters.channels}
          onChange={(channels) => onChange({ channels })}
        />
        <label className="rail__check">
          <input
            type="checkbox"
            checked={!!filters.include_benchmark}
            onChange={(e) => onChange({ include_benchmark: e.target.checked })}
          />
          <span>include benchmark sources</span>
        </label>
      </Group>

      <Group title="When (UTC)">
        <label className="rail__field">
          <span>From</span>
          <input
            className="input num"
            type="datetime-local"
            value={toUtcInput(filters.utc_from)}
            onChange={(e) => onChange({ utc_from: fromUtcInput(e.target.value) })}
          />
        </label>
        <label className="rail__field">
          <span>Before</span>
          <input
            className="input num"
            type="datetime-local"
            value={toUtcInput(filters.utc_to)}
            onChange={(e) => onChange({ utc_to: fromUtcInput(e.target.value) })}
          />
        </label>
      </Group>

      <Group title="Agreement">
        <NumberField
          label="Exact families ≥"
          value={filters.min_exact_families}
          min={1}
          onCommit={(v) => onChange({ min_exact_families: v })}
        />
        <NumberField
          label="Exact providers ≥"
          value={filters.min_exact_providers}
          min={1}
          onCommit={(v) => onChange({ min_exact_providers: v })}
        />
        <NumberField
          label="Near families ≥"
          value={filters.min_near_families}
          min={1}
          onCommit={(v) => onChange({ min_near_families: v })}
        />
        <NumberField
          label="Near similarity ≥"
          value={filters.min_near_similarity}
          step={0.05}
          max={1}
          onCommit={(v) => onChange({ min_near_similarity: v })}
        />
        <NumberField
          label="Exact families ≤"
          value={filters.max_exact_families}
          onCommit={(v) => onChange({ max_exact_families: v })}
        />
        <NumberField
          label="Words ≥"
          value={filters.min_words}
          onCommit={(v) => onChange({ min_words: v })}
        />
        <NumberField
          label="Utterance families ≥"
          value={filters.min_utterance_families}
          min={1}
          onCommit={(v) => onChange({ min_utterance_families: v })}
        />
        <NumberField
          label="Utterance words ≥"
          value={filters.min_utterance_tokens}
          min={1}
          onCommit={(v) => onChange({ min_utterance_tokens: v })}
        />
        <TriState
          label="Partial agreement"
          value={filters.partial_agreement}
          onChange={(v) => onChange({ partial_agreement: v })}
        />
        <NumberField
          label="Models ≥"
          value={filters.min_models ?? 1}
          placeholder="1"
          onCommit={(v) => onChange({ min_models: v })}
        />
        <p className="rail__note">
          Near threshold: <span className="num">{facets?.near_threshold ?? "—"}</span> (order-aware
          similarity, not a probability)
        </p>
      </Group>

      <Group title="Review">
        <div className="rail__sub">Human review</div>
        <Checks
          options={REVIEW_STATUSES}
          value={filters.review_status}
          onChange={(review_status) => onChange({ review_status })}
        />
        <div className="rail__sub">Training label</div>
        <Checks
          options={TRAINING_LABELS}
          value={filters.training_label}
          onChange={(training_label) => onChange({ training_label })}
        />
        <div className="rail__sub">Has span labelled</div>
        <Checks
          options={TRAINING_LABELS.filter((l) => l !== "none")}
          value={filters.span_labels}
          onChange={(span_labels) => onChange({ span_labels })}
        />
        <TriState label="Has spans" value={filters.has_spans} onChange={(v) => onChange({ has_spans: v })} />
        <TriState
          label="Partial usable"
          value={filters.partial_usable}
          onChange={(v) => onChange({ partial_usable: v })}
        />
      </Group>

      <Group title="Models">
        <Checks
          options={models}
          value={filters.models}
          onChange={(m) => onChange({ models: m })}
          render={(m) => (
            <>
              {m} <span className="muted">{familyOf[m]}</span>
            </>
          )}
        />
        <div className="rail__sub">Architecture family</div>
        <Checks
          options={facets?.families ?? []}
          value={filters.families}
          onChange={(families) => onChange({ families })}
        />
      </Group>

      <Group title="Risk">
        <TriState label="ASR error" value={filters.has_error} onChange={(v) => onChange({ has_error: v })} />
        <TriState
          label="Abstention"
          value={filters.has_abstention}
          onChange={(v) => onChange({ has_abstention: v })}
        />
        <div className="rail__sub">Quality flags</div>
        <Checks options={facets?.flags ?? []} value={filters.flags} onChange={(flags) => onChange({ flags })} />
      </Group>
    </aside>
  );
}
