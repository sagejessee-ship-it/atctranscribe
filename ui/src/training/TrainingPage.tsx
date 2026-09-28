import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";

import { api } from "../api/client";
import type { DatasetView, SplitGrouping, TrainingLabel, TrainingSummary } from "../api/types";
import { TrainingBadge } from "../components/badges";
import { Button, ErrorBox, Section } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import { fmtCount, fmtUtc } from "../lib/format";

const LABELS: TrainingLabel[] = ["gold", "silver", "candidate", "rejected"];
const minutes = (ms: number) => (ms / 60000).toFixed(1);

function reviewLink(filters: Record<string, unknown>) {
  return `/review?f=${encodeURIComponent(JSON.stringify({ min_models: 0, ...filters }))}`;
}

function Pivot({
  rows,
  keyName,
  title,
}: {
  rows: { key: string; label: TrainingLabel; items: number }[];
  keyName: string;
  title: string;
}) {
  const keys = [...new Set(rows.map((r) => r.key))];
  const cell = (key: string, label: TrainingLabel) =>
    rows.filter((r) => r.key === key && r.label === label).reduce((n, r) => n + r.items, 0);
  return (
    <table className="mini-table training-pivot">
      <caption className="mini-table__caption">{title}</caption>
      <thead>
        <tr>
          <th scope="col">{keyName}</th>
          {LABELS.map((l) => (
            <th scope="col" key={l}>
              {l}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {keys.map((key) => (
          <tr key={key}>
            <td className="num">{key}</td>
            {LABELS.map((l) => (
              <td key={l} className="num">
                {cell(key, l) || <span className="muted">·</span>}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Summary({ summary }: { summary: TrainingSummary }) {
  const total = (label: TrainingLabel, scope: "segment" | "span") =>
    summary.by_label.find((r) => r.label === label && r.scope === scope);
  return (
    <Section title="Labelled data (current versions, corpus sources)" id="labelled">
      <table className="mini-table training-counts">
        <thead>
          <tr>
            <th scope="col">Label</th>
            <th scope="col">Whole segments</th>
            <th scope="col">Spans</th>
            <th scope="col">Minutes</th>
            <th scope="col">
              <span className="sr-only">Links</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {LABELS.map((label) => {
            const seg = total(label, "segment");
            const span = total(label, "span");
            return (
              <tr key={label}>
                <td>
                  <TrainingBadge label={label} />
                </td>
                <td className="num">{fmtCount(seg?.items ?? 0)}</td>
                <td className="num">{fmtCount(span?.items ?? 0)}</td>
                <td className="num">{minutes((seg?.audio_ms ?? 0) + (span?.audio_ms ?? 0))}</td>
                <td>
                  <Link to={reviewLink({ training_label: [label] })}>segments</Link>
                  {" · "}
                  <Link to={reviewLink({ span_labels: [label] })}>spans</Link>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="training-pivots">
        <Pivot
          title="By channel"
          keyName="Channel"
          rows={summary.by_channel.map((r) => ({ key: r.channel ?? "—", label: r.label, items: r.items }))}
        />
        <Pivot
          title="By UTC day"
          keyName="Day"
          rows={summary.by_day.map((r) => ({ key: r.day ?? "—", label: r.label, items: r.items }))}
        />
      </div>
    </Section>
  );
}

function CreateDataset({ onCreated }: { onCreated: (d: DatasetView) => void }) {
  const [annotator] = useAnnotator();
  const [name, setName] = useState("bwi-atc");
  const [labels, setLabels] = useState<TrainingLabel[]>(["gold"]);
  const [scopes, setScopes] = useState<("segment" | "span")[]>(["segment", "span"]);
  const [groupBy, setGroupBy] = useState<SplitGrouping>("utc_day_channel");
  const [ratios, setRatios] = useState("0.8,0.1,0.1");
  const [seed, setSeed] = useState(0);
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: () => {
      const [train, validation, test] = ratios.split(",").map(Number);
      return api.createDataset({
        name,
        description: description || null,
        labels,
        scopes,
        split: { group_by: groupBy, seed, train, validation, test },
        created_by: annotator,
      });
    },
    onSuccess: onCreated,
  });
  const toggle = <T,>(list: T[], value: T, on: boolean) => (on ? [...list, value] : list.filter((v) => v !== value));
  return (
    <form
      className="form dataset-form"
      aria-label="Create dataset version"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <div className="form__row">
        <label className="field">
          <span>Name</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} pattern="[a-z0-9][a-z0-9_.\-]*" />
        </label>
        <fieldset className="field fieldset">
          <legend>Labels</legend>
          {(["gold", "silver", "candidate"] as TrainingLabel[]).map((l) => (
            <label key={l} className="rail__check">
              <input type="checkbox" checked={labels.includes(l)} onChange={(e) => setLabels(toggle(labels, l, e.target.checked))} />
              {l}
            </label>
          ))}
        </fieldset>
        <fieldset className="field fieldset">
          <legend>Scopes</legend>
          {(["segment", "span"] as const).map((s) => (
            <label key={s} className="rail__check">
              <input type="checkbox" checked={scopes.includes(s)} onChange={(e) => setScopes(toggle(scopes, s, e.target.checked))} />
              {s === "segment" ? "whole segments" : "spans"}
            </label>
          ))}
        </fieldset>
        <label className="field">
          <span>Split groups</span>
          <select className="select" value={groupBy} onChange={(e) => setGroupBy(e.target.value as SplitGrouping)}>
            <option value="utc_day_channel">UTC day × channel</option>
            <option value="utc_day">UTC day</option>
            <option value="segment">segment (random)</option>
          </select>
        </label>
        <label className="field">
          <span>train,val,test</span>
          <input className="input num" value={ratios} onChange={(e) => setRatios(e.target.value)} />
        </label>
        <label className="field">
          <span>Seed</span>
          <input className="input input--num num" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
        </label>
      </div>
      <label className="field">
        <span>Description</span>
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      {create.error ? <ErrorBox title="Dataset not created" detail={(create.error as Error).message} /> : null}
      <div>
        <Button type="submit" variant="primary" disabled={create.isPending || !labels.length || !scopes.length}>
          {create.isPending ? "Freezing…" : "Freeze new version"}
        </Button>
      </div>
    </form>
  );
}

function DatasetRow({ d }: { d: DatasetView }) {
  const splits = d.counts.by_split ?? {};
  const exp = d.export as { exported_at?: string; out_uri?: string; clips_sha256?: string } | null;
  return (
    <tr>
      <td className="num">{d.id}</td>
      <td>
        <strong>{d.name}</strong> <span className="num muted">v{d.version}</span>
        {d.description ? <div className="muted">{d.description}</div> : null}
      </td>
      <td className="num">{fmtCount(d.item_count)}</td>
      <td className="num">{minutes(d.audio_ms_total)}</td>
      <td className="num">
        {["train", "validation", "test"].map((s) => `${s[0]}${splits[s] ?? 0}`).join(" ")}
      </td>
      <td className="num" title={d.manifest_sha256}>
        {d.manifest_sha256.slice(0, 12)}
      </td>
      <td>
        {d.status === "exported" ? (
          <span title={exp?.out_uri}>
            exported <span className="num muted">{fmtUtc(exp?.exported_at)}Z</span>
          </span>
        ) : (
          <code>aerochorus dataset export {d.id} --out &lt;dir&gt;</code>
        )}
      </td>
    </tr>
  );
}

/** Label overview and versioned dataset lifecycle. Not a training orchestrator. */
export function TrainingPage() {
  const client = useQueryClient();
  const summary = useQuery({ queryKey: ["training-summary"], queryFn: api.trainingSummary });
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  const [created, setCreated] = useState<DatasetView | null>(null);
  const sorted = useMemo(() => datasets.data ?? [], [datasets.data]);
  return (
    <div className="page">
      <h1 className="page__title">Training sets</h1>
      {summary.data ? <Summary summary={summary.data} /> : summary.isError ? <ErrorBox title="Summary unavailable" detail={(summary.error as Error).message} /> : <div className="skeleton" />}
      <Section title="Dataset versions" id="datasets">
        <p className="muted">
          Freezing snapshots the current annotation versions (text, bounds, labels, source hashes) with a grouped,
          seeded split. Export materializes clips from the immutable parent audio on a machine with the corpus mounted.
        </p>
        <CreateDataset
          onCreated={(d) => {
            setCreated(d);
            client.invalidateQueries({ queryKey: ["datasets"] });
          }}
        />
        {created ? (
          <p className="note note--derived" role="status">
            Froze {created.name} v{created.version}: {created.item_count} items, manifest{" "}
            <span className="num">{created.manifest_sha256.slice(0, 16)}</span>
          </p>
        ) : null}
        <table className="mini-table datasets">
          <caption className="sr-only">Dataset versions</caption>
          <thead>
            <tr>
              <th scope="col">Id</th>
              <th scope="col">Dataset</th>
              <th scope="col">Items</th>
              <th scope="col">Min</th>
              <th scope="col">Splits</th>
              <th scope="col">Manifest</th>
              <th scope="col">Export</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((d) => (
              <DatasetRow key={d.id} d={d} />
            ))}
          </tbody>
        </table>
        {!sorted.length && datasets.isSuccess ? <p className="muted">No dataset versions yet.</p> : null}
      </Section>
    </div>
  );
}
