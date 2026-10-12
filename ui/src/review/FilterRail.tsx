import { useEffect, useId, useState, type ReactNode } from "react";

import type { Facets, ReviewFilters } from "../api/types";
import { HelpTip } from "../components/HelpTip";
import { fmtCount } from "../lib/format";
import { REVIEW_STATUSES, TRAINING_LABELS } from "./filterMeta";
import { FAMILY_TEXT, VOTING_TEXT, columnHelp } from "./glossary";

type Patch = (patch: Partial<ReviewFilters>) => void;

function Group({ title, help, children }: { title: string; help?: ReactNode; children: ReactNode }) {
  const id = useId();
  return (
    <fieldset className="rail__group" aria-labelledby={id}>
      <legend className="rail__title">
        <span id={id}>{title}</span>
        {help ? (
          <HelpTip label={title} wide>
            {help}
          </HelpTip>
        ) : null}
      </legend>
      {children}
    </fieldset>
  );
}

function AgreementHelp({ nearThreshold }: { nearThreshold: number | undefined }) {
  const terms = columnHelp(nearThreshold).filter((c) =>
    ["exact_providers", "exact_families", "near_families", "near_similarity"].includes(c.id),
  );
  return (
    <>
      <p>
        Agreement compares the voting models' transcripts of a segment. Independent agreement (different families)
        is the strongest sign a transcript is right.
      </p>
      <dl className="glossary">
        {terms.map((t) => (
          <div key={t.id}>
            <dt>{t.name}</dt>
            <dd>{t.text}</dd>
          </div>
        ))}
        <div>
          <dt>Utterance families / words</dt>
          <dd>
            Partial agreement: a stretch of words inside the segment that several families agree on, even when the
            whole transcripts differ.
          </dd>
        </div>
      </dl>
    </>
  );
}

function ModelsHelp() {
  return (
    <>
      <p>
        <b>Has a result</b>: segments any ticked model (or family) transcribed, whether or not it voted.
      </p>
      <p>
        <b>Agrees</b>: segments where a ticked model or family is in the agreeing group (the largest identical group
        or the near-agreement group, each with 2+ families). Use it to find where, say, granite-speech agrees with
        the others. Only voting models can agree.
      </p>
      <p>{FAMILY_TEXT}</p>
      <p>{VOTING_TEXT}</p>
      <p className="muted">Ticking several means any of them. A model and a family together must both match.</p>
    </>
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
  // Models with results (and anything already ticked), voting ones first.
  const selected = new Set([...(filters.models ?? []), ...(filters.families ?? [])]);
  const listed = (facets?.models ?? [])
    .filter((m) => m.has_results || selected.has(m.name))
    .sort((a, b) => Number(b.voting) - Number(a.voting) || a.name.localeCompare(b.name));
  const byName = Object.fromEntries(listed.map((m) => [m.name, m]));
  const families = [...new Set(listed.map((m) => m.family))]
    .concat((filters.families ?? []).filter((f) => !listed.some((m) => m.family === f)))
    .sort();
  const votingFamilies = new Set(listed.filter((m) => m.voting).map((m) => m.family));
  const match = filters.model_match ?? "result";
  const tickedNonVoting =
    match === "agrees" &&
    ((filters.models ?? []).some((m) => byName[m] && !byName[m].voting) ||
      (filters.families ?? []).some((f) => !votingFamilies.has(f)));
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

      <Group title="Agreement" help={<AgreementHelp nearThreshold={facets?.near_threshold} />}>
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

      <Group title="Models" help={<ModelsHelp />}>
        <div className="rail__mode" role="radiogroup" aria-label="Show segments where the ticked models or families">
          <label>
            <input
              type="radio"
              name="model-match"
              checked={match === "result"}
              onChange={() => onChange({ model_match: "result" })}
            />
            has a result
          </label>
          <label>
            <input
              type="radio"
              name="model-match"
              checked={match === "agrees"}
              onChange={() => onChange({ model_match: "agrees" })}
            />
            agrees
          </label>
        </div>
        <Checks
          options={listed.map((m) => m.name)}
          value={filters.models}
          onChange={(m) => onChange({ models: m })}
          render={(name) => {
            const m = byName[name];
            return (
              <span className="rail__model">
                <span className="rail__model-name">{name}</span>
                {m ? (
                  <span className="rail__model-meta">
                    {m.family} · {m.voting ? "voting" : "not voting"}
                    {m.results ? ` · ${fmtCount(m.results)}` : ""}
                  </span>
                ) : null}
              </span>
            );
          }}
        />
        <div className="rail__sub">Architecture family</div>
        <Checks
          options={families}
          value={filters.families}
          onChange={(f) => onChange({ families: f })}
          render={(f) => (
            <>
              {f}
              {votingFamilies.has(f) ? null : <span className="muted"> · not voting</span>}
            </>
          )}
        />
        {tickedNonVoting ? (
          <p className="rail__note" role="status">
            A ticked model or family doesn't vote, so it is never in an agreeing group. Switch to "has a result" to
            see what it transcribed.
          </p>
        ) : null}
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
