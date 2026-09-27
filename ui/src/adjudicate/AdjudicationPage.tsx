import { useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";

import {
  adjudication,
  describeSkips,
  usd,
  type AdjudicationBatch,
  type BatchStatus,
} from "../api/adjudication";
import { Badge } from "../components/badges";
import { Button, Dialog, ErrorBox, Section } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import { fmtSim, fmtUtc } from "../lib/format";

const TONE: Record<BatchStatus, "ok" | "info" | "warn" | "neutral"> = {
  queued: "neutral",
  running: "info",
  done: "ok",
  capped: "warn",
  cancelled: "neutral",
};
const OPEN: BatchStatus[] = ["queued", "running"];

function finished(batch: AdjudicationBatch) {
  const c = batch.counts;
  return (c.done ?? 0) + (c.failed ?? 0) + (c.skipped ?? 0) + (c.cancelled ?? 0);
}

/** Batch accept: adjudicated text -> silver where nothing human would be overridden. */
function AcceptDialog({
  batch,
  open,
  onOpenChange,
  onDone,
}: {
  batch: AdjudicationBatch;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string) => void;
}) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const [minConfidence, setMinConfidence] = useState(0.8);
  const [support, setSupport] = useState(true);
  const accept = useMutation({
    mutationFn: () =>
      adjudication.accept({
        batch_id: batch.id,
        min_confidence: minConfidence,
        require_model_support: support,
        annotator,
      }),
    onSuccess: (outcome) => {
      const skipped = describeSkips(outcome.skipped);
      onDone(`${outcome.applied} accepted as silver${skipped ? `; skipped ${skipped}` : ""}`);
      client.invalidateQueries();
      onOpenChange(false);
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Accept batch #${batch.id} as silver`}
      description="Silver with text origin model_adjudicated. Never over human text, human gold or a rejection; never on benchmark sources; never gold."
    >
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          accept.mutate();
        }}
      >
        <div className="form__row">
          <label className="field">
            <span>Minimum model confidence</span>
            <input
              className="input input--num num"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value))}
            />
          </label>
          <label className="rail__check">
            <input type="checkbox" checked={support} onChange={(e) => setSupport(e.target.checked)} />
            only when an ASR hypothesis nearly matches (independent support)
          </label>
        </div>
        {accept.error ? <ErrorBox title="Not accepted" detail={(accept.error as Error).message} /> : null}
        <div className="dialog__actions">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={accept.isPending}>
            {accept.isPending ? "Accepting…" : "Accept as silver"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function BatchDetail({ batchId, onToast }: { batchId: number; onToast: (m: string) => void }) {
  const client = useQueryClient();
  const [accepting, setAccepting] = useState(false);
  const detail = useQuery({
    queryKey: ["adjudication-batch", batchId],
    queryFn: () => adjudication.batch(batchId),
    refetchInterval: (q) => (q.state.data && OPEN.includes(q.state.data.status) ? 3000 : false),
  });
  const cancel = useMutation({
    mutationFn: () => adjudication.cancel(batchId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["adjudication-batch", batchId] });
      client.invalidateQueries({ queryKey: ["adjudication-batches"] });
    },
  });
  if (detail.error) return <ErrorBox title={`Batch ${batchId} unavailable`} detail={(detail.error as Error).message} />;
  const batch = detail.data;
  if (!batch) return <div className="skeleton" />;
  const done = batch.items.filter((i) => i.status === "done");
  return (
    <Section
      title={`Batch #${batch.id}`}
      id="adjudication-batch"
      aside={
        <span className="adj-batch__actions">
          {OPEN.includes(batch.status) ? (
            <Button size="sm" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
              Cancel queued
            </Button>
          ) : null}
          <Button size="sm" onClick={() => setAccepting(true)} disabled={!done.length}>
            Accept as silver…
          </Button>
        </span>
      }
    >
      <dl className="kv kv--compact">
        <dt>model</dt>
        <dd className="num">
          {batch.model} · prompt v{batch.prompt_version} · reasoning {String(batch.params.reasoning_effort ?? "low")}
        </dd>
        <dt>spent</dt>
        <dd>
          <strong className="num">{usd(batch.spent_usd, 4)}</strong> of cap <span className="num">{usd(batch.max_cost_usd)}</span>{" "}
          <span className="muted">(estimate {usd(batch.estimated_cost_usd)})</span>
        </dd>
        <dt>created</dt>
        <dd>
          {fmtUtc(batch.created_at)} by {batch.created_by ?? "—"}
          {batch.note ? ` · ${batch.note}` : ""}
        </dd>
      </dl>
      <table className="mini-table adj-items">
        <caption className="sr-only">Items of batch {batch.id}</caption>
        <thead>
          <tr>
            <th scope="col">Segment</th>
            <th scope="col">Status</th>
            <th scope="col">Transcript</th>
            <th scope="col" title="the model's own confidence">Conf</th>
            <th scope="col" title="similarity to the nearest ASR hypothesis">Near hyp</th>
            <th scope="col">Cost</th>
          </tr>
        </thead>
        <tbody>
          {batch.items.map((item) => (
            <tr key={item.id}>
              <td className="num">
                <Link to={`/review?seg=${item.segment_id}`}>{item.segment_id}</Link>
              </td>
              <td>
                <Badge tone={item.status === "done" ? "ok" : item.status === "failed" ? "danger" : item.status === "running" ? "info" : "neutral"}>
                  {item.status}
                </Badge>
                {item.accepted_version_id ? <Badge tone="derived">silver</Badge> : null}
              </td>
              <td className="mini-table__text">
                {item.transcript ?? (item.status === "done" ? <span className="muted">no speech</span> : <span className="muted">{item.error ?? ""}</span>)}
              </td>
              <td className="num">{item.confidence?.toFixed(2) ?? "—"}</td>
              <td className="num">{fmtSim(item.best_hypothesis_similarity)}</td>
              <td className="num">{usd(item.cost_usd, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {cancel.error ? <ErrorBox title="Not cancelled" detail={(cancel.error as Error).message} /> : null}
      <AcceptDialog batch={batch} open={accepting} onOpenChange={setAccepting} onDone={onToast} />
    </Section>
  );
}

/** Adjudication batches: progress, spend against the cap, results, silver acceptance. */
export function AdjudicationPage() {
  const [selected, setSelected] = useState<number | null>(null);
  const [toast, setToast] = useState("");
  const status = useQuery({ queryKey: ["adjudication-status"], queryFn: adjudication.status, refetchInterval: 15_000 });
  const batches = useQuery({
    queryKey: ["adjudication-batches"],
    queryFn: adjudication.list,
    refetchInterval: (q) => ((q.state.data ?? []).some((b) => OPEN.includes(b.status)) ? 3000 : false),
  });
  const list = batches.data ?? [];
  const current = selected ?? list[0]?.id ?? null;
  const runners = status.data?.runners ?? [];
  return (
    <div className="page page--wide">
      <h1 className="page__title">Adjudication</h1>
      <p className="muted adj-page__intro">
        Batches sent to {status.data?.default_model ?? "the adjudicating model"} through OpenRouter. Create them from the review
        workbench (select rows, or “Adjudicate…” for a sample of the current filters); every batch is priced and capped
        before anything is sent.
      </p>
      {status.data && !runners.length ? (
        <p className="note note--warn" role="status">
          No runner is polling. Queued batches wait until <code>aerochorus adjudicate run --follow</code> runs on the host
          with the audio and <code>AEROCHORUS_OPENROUTER_API_KEY</code>.
        </p>
      ) : runners.length ? (
        <p className="muted">
          Runner{runners.length > 1 ? "s" : ""}: {runners.map((r) => `${r.runner} (seen ${fmtUtc(r.last_seen).slice(11)}Z)`).join(", ")}
        </p>
      ) : null}
      <div className="adj-page">
        <Section title="Batches" id="adjudication-batches">
          {batches.error ? <ErrorBox title="Batches unavailable" detail={(batches.error as Error).message} /> : null}
          <table className="mini-table adj-batches">
            <caption className="sr-only">Adjudication batches</caption>
            <thead>
              <tr>
                <th scope="col">Batch</th>
                <th scope="col">Status</th>
                <th scope="col">Progress</th>
                <th scope="col">Spent / cap</th>
                <th scope="col">Silver</th>
              </tr>
            </thead>
            <tbody>
              {list.map((b) => {
                const pct = b.item_count ? (100 * finished(b)) / b.item_count : 0;
                return (
                  <tr
                    key={b.id}
                    className={clsx("adj-batches__row", b.id === current && "is-selected")}
                    onClick={() => setSelected(b.id)}
                    aria-selected={b.id === current}
                  >
                    <td className="num">
                      <button type="button" className="linklike" onClick={() => setSelected(b.id)}>
                        #{b.id}
                      </button>{" "}
                      <span className="muted">{fmtUtc(b.created_at).slice(0, 16)}</span>
                    </td>
                    <td>
                      <Badge tone={TONE[b.status]}>{b.status}</Badge>
                    </td>
                    <td>
                      <div className="bar" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={`Batch ${b.id} progress`}>
                        <div className="bar__fill" style={{ width: `${pct}%` }} />
                      </div>
                      <span className="muted num">
                        {finished(b)}/{b.item_count}
                        {b.counts.failed ? ` · ${b.counts.failed} failed` : ""}
                      </span>
                    </td>
                    <td className="num">
                      {usd(b.spent_usd, 4)} / {usd(b.max_cost_usd)}
                    </td>
                    <td className="num">{b.accepted}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {batches.isSuccess && !list.length ? <p className="muted">No batches yet.</p> : null}
        </Section>
        <div>{current != null ? <BatchDetail batchId={current} onToast={setToast} /> : null}</div>
      </div>
      <div className="toast" role="status" aria-live="polite">
        {toast}
      </div>
    </div>
  );
}
