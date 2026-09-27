import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";

import {
  adjudication,
  describeSkips,
  usd,
  type AdjudicationBatch,
  type AdjudicationParams,
  type AdjudicationSelection,
  type ReasoningEffort,
} from "../api/adjudication";
import type { ReviewFilters } from "../api/types";
import { Button, Dialog, ErrorBox } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import { describeFilters } from "./filterMeta";

const EFFORTS: { value: ReasoningEffort; title: string }[] = [
  { value: "low", title: "cheapest; transcription rarely needs more" },
  { value: "medium", title: "more reasoning tokens (billed as output)" },
  { value: "high", title: "most reasoning tokens; several times the cost" },
];

function useDebounced<T>(value: T, ms = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

/**
 * Send a bundle (audio + every hypothesis + agreement + airport/ADS-B context)
 * to the adjudicating model. Costs money: the selection is priced first, a hard
 * cost cap is set, and nothing is queued without an explicit confirmation.
 */
export function AdjudicateDialog({
  open,
  onOpenChange,
  segmentIds,
  filters,
  total,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Explicit segments (selected rows, or the open segment); else a sample of `filters`. */
  segmentIds?: number[];
  filters?: ReviewFilters;
  total?: number;
  onCreated: (batch: AdjudicationBatch) => void;
}) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const [effort, setEffort] = useState<ReasoningEffort>("low");
  const [includeAdsb, setIncludeAdsb] = useState(true);
  const [includeNeighbors, setIncludeNeighbors] = useState(true);
  const [redo, setRedo] = useState(false);
  const [n, setN] = useState(25);
  const [seed, setSeed] = useState(() => Math.floor(Math.random() * 1_000_000));
  const [cap, setCap] = useState<string>("");
  const [note, setNote] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  const explicit = !!segmentIds?.length;
  const selection: AdjudicationSelection = explicit
    ? { segment_ids: segmentIds }
    : { filters: { ...(filters ?? {}), sample_id: null }, n, seed };
  const params: AdjudicationParams = {
    reasoning_effort: effort,
    include_adsb: includeAdsb,
    include_neighbors: includeNeighbors,
    redo,
  };
  const body = useDebounced({ selection, params });
  const preview = useQuery({
    queryKey: ["adjudication-preview", body],
    queryFn: () => adjudication.preview(body),
    enabled: open,
    staleTime: 30_000,
  });
  const plan = preview.data;
  // A new estimate needs a new look: reset the cap suggestion and the confirmation.
  useEffect(() => {
    if (plan) setCap(plan.suggested_max_cost_usd.toFixed(2));
    setConfirmed(false);
  }, [plan]);
  useEffect(() => {
    if (!open) setConfirmed(false);
  }, [open]);

  const capValue = Number(cap);
  const maxCap = plan?.limits.max_batch_usd ?? 25;
  const capOk = Number.isFinite(capValue) && capValue > 0 && capValue <= maxCap;
  const create = useMutation({
    mutationFn: () =>
      adjudication.create({
        selection: explicit ? selection : { segment_ids: plan!.segment_ids },
        params,
        max_cost_usd: capValue,
        acknowledged_cost_usd: plan!.estimated_cost_usd,
        confirm: true,
        note: note.trim() || null,
        created_by: annotator,
      }),
    onSuccess: (batch) => {
      client.invalidateQueries({ queryKey: ["adjudications"] });
      client.invalidateQueries({ queryKey: ["adjudication-batches"] });
      onCreated(batch);
      onOpenChange(false);
    },
  });
  const chips = describeFilters({ ...(filters ?? {}), sample_id: null });
  const minutes = plan ? plan.audio_seconds / 60 : 0;
  const ready = !!plan && plan.eligible > 0 && capOk && !preview.isFetching;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      width={560}
      title="Adjudicate with Gemini"
      description="Sends each clip's audio, every model hypothesis, the agreement analysis and the airport / cached ADS-B context to OpenRouter. The answer is model output: silver at most, never gold."
    >
      <form
        className="form adjudicate"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready && confirmed) create.mutate();
        }}
      >
        {explicit ? (
          <p>
            <span className="num">{segmentIds!.length}</span> selected segment{segmentIds!.length === 1 ? "" : "s"}.
          </p>
        ) : (
          <>
            <div className="form__filters">
              {chips.length ? chips.map((c) => <span key={c.key} className="chip">{c.label}</span>) : "No filters: all transcribed segments."}
            </div>
            <div className="form__row">
              <label className="field">
                <span>Random sample N</span>
                <input className="input input--num num" type="number" min={1} max={plan?.limits.max_items ?? 500} value={n} onChange={(e) => setN(Number(e.target.value))} />
              </label>
              <label className="field">
                <span>Seed</span>
                <input className="input input--num num" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
              </label>
              <span className="muted adjudicate__of">of {total ?? "?"} matching</span>
            </div>
          </>
        )}
        <div className="form__row">
          <div className="field">
            <span>Reasoning</span>
            <div className="segmented" role="group" aria-label="Reasoning effort">
              {EFFORTS.map((e) => (
                <button
                  key={e.value}
                  type="button"
                  className="segmented__item"
                  data-state={effort === e.value ? "on" : "off"}
                  aria-pressed={effort === e.value}
                  title={e.title}
                  onClick={() => setEffort(e.value)}
                >
                  {e.value}
                </button>
              ))}
            </div>
          </div>
          <div className="adjudicate__checks">
            <label className="rail__check">
              <input type="checkbox" checked={includeAdsb} onChange={(e) => setIncludeAdsb(e.target.checked)} />
              cached ADS-B traffic (never queries OpenSky)
            </label>
            <label className="rail__check">
              <input type="checkbox" checked={includeNeighbors} onChange={(e) => setIncludeNeighbors(e.target.checked)} />
              nearby transmissions as context
            </label>
            <label className="rail__check">
              <input type="checkbox" checked={redo} onChange={(e) => setRedo(e.target.checked)} />
              re-send segments already adjudicated
            </label>
          </div>
        </div>

        {preview.error ? <ErrorBox title="Could not price the selection" detail={(preview.error as Error).message} /> : null}
        <dl className={clsx("kv kv--compact adjudicate__plan", preview.isFetching && "is-stale")} aria-live="polite" data-testid="adjudication-plan">
          <dt>model</dt>
          <dd className="num">
            {plan?.model ?? "…"} · prompt v{plan?.prompt_version ?? "?"}
          </dd>
          <dt>sends</dt>
          <dd>
            <span className="num">{plan?.eligible ?? "…"}</span> of <span className="num">{plan?.selected ?? "…"}</span> segments
            {plan && Object.keys(plan.skipped).length ? <span className="muted"> (skipped: {describeSkips(plan.skipped)})</span> : null}
          </dd>
          <dt>audio</dt>
          <dd className="num">{plan ? `${minutes < 1 ? `${plan.audio_seconds.toFixed(0)} s` : `${minutes.toFixed(1)} min`}` : "…"}</dd>
          <dt>estimate</dt>
          <dd>
            <strong className="num">{plan ? usd(plan.estimated_cost_usd) : "…"}</strong>{" "}
            <span className="muted">
              typical · worst case <span className="num">{plan ? usd(plan.worst_case_cost_usd) : "…"}</span>
            </span>
          </dd>
          <dt>prices</dt>
          <dd className="muted">
            {plan
              ? `$${plan.pricing.prompt}/M input, $${plan.pricing.audio}/M audio, $${plan.pricing.completion}/M output (${plan.pricing.source})`
              : "…"}
          </dd>
        </dl>
        {plan && !plan.runners.length ? (
          <p className="note note--warn" role="status">
            No adjudication runner has checked in. The batch will wait until{" "}
            <code>aerochorus adjudicate run</code> runs on the host with the audio (it holds the OpenRouter key).
          </p>
        ) : null}
        <div className="form__row">
          <label className="field">
            <span>Cost cap (USD)</span>
            <input
              className="input input--num num"
              type="number"
              min={0.01}
              max={maxCap}
              step={0.01}
              value={cap}
              onChange={(e) => {
                setCap(e.target.value);
                setConfirmed(false);
              }}
              aria-describedby="cap-help"
            />
          </label>
          <label className="field field--grow">
            <span>Note (optional)</span>
            <input className="input" value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
        </div>
        <p id="cap-help" className="muted">
          Hard ceiling: an item is only sent if its worst case still fits. Batch limit {usd(maxCap)}.
        </p>
        <label className="rail__check adjudicate__confirm">
          <input
            type="checkbox"
            checked={confirmed}
            disabled={!ready}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          <span>
            Send <span className="num">{plan?.eligible ?? 0}</span> clips and their transcripts to OpenRouter (Google), spending at most{" "}
            <span className="num">{capOk ? usd(capValue) : "—"}</span>.
          </span>
        </label>
        {create.error ? <ErrorBox title="Batch not created" detail={(create.error as Error).message} /> : null}
        <div className="dialog__actions">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!ready || !confirmed || create.isPending}>
            {create.isPending ? "Queuing…" : `Send ${plan?.eligible ?? 0} to Gemini`}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
