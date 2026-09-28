import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Pause, Play, RotateCw, Square } from "lucide-react";

import { api } from "../api/client";
import { runs, type ModelRead, type SweepCreate, type SweepRead, type WorkerRead } from "../api/transcribe";
import { Badge } from "../components/badges";
import { ModelVoting } from "./ModelVoting";
import { Button, ErrorBox, IconButton, Section } from "../components/ui";
import { fmtCount, fmtUtc } from "../lib/format";

type Scope = "day" | "range" | "all";

const STATUS_TONE: Record<string, "ok" | "info" | "warn" | "danger" | "neutral" | "derived"> = {
  queued: "neutral",
  loading: "info",
  running: "info",
  retrying: "warn",
  paused: "warn",
  completed: "ok",
  partial: "warn",
  failed: "danger",
  cancelled: "neutral",
};

function useDebounced<T>(value: T, ms = 400): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

const minutes = (m: number | null | undefined) =>
  m == null ? "—" : m < 90 ? `${m.toFixed(0)} min` : `${(m / 60).toFixed(1)} h`;

/** Which models to run: default = every enabled model that votes in agreement. */
function ModelPicker({
  models,
  suites,
  selected,
  onChange,
  preview,
}: {
  models: ModelRead[];
  suites: { name: string; models: string[] }[];
  selected: string[];
  onChange: (names: string[]) => void;
  preview: Map<string, { observed_rtf: number | null; estimated_minutes: number | null }>;
}) {
  const enabled = models.filter((m) => m.enabled);
  const set = new Set(selected);
  const toggle = (name: string, on: boolean) =>
    onChange(enabled.map((m) => m.logical_name).filter((n) => (n === name ? on : set.has(n))));
  return (
    <fieldset className="field fieldset" aria-label="Models">
      <legend>
        Models <span className="muted">({selected.length} selected; run one at a time, fastest first)</span>
      </legend>
      <div className="transcribe__model-actions">
        <Button size="sm" onClick={() => onChange(enabled.filter((m) => m.ensemble_eligible).map((m) => m.logical_name))}>
          Voting models
        </Button>
        <Button size="sm" onClick={() => onChange(enabled.map((m) => m.logical_name))}>
          All
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChange([])}>
          None
        </Button>
        <select
          className="select"
          aria-label="Select a suite"
          value=""
          onChange={(e) => {
            const suite = suites.find((s) => s.name === e.target.value);
            if (suite) onChange(suite.models.filter((n) => enabled.some((m) => m.logical_name === n)));
          }}
        >
          <option value="">From suite…</option>
          {suites
            .filter((s) => !s.name.startsWith("adhoc-"))
            .map((s) => (
              <option key={s.name} value={s.name}>
                {s.name} ({s.models.length})
              </option>
            ))}
        </select>
      </div>
      <table className="mini-table transcribe__models">
        <thead>
          <tr>
            <th scope="col">
              <span className="sr-only">Run</span>
            </th>
            <th scope="col">Model</th>
            <th scope="col">Family</th>
            <th scope="col">Size</th>
            <th scope="col">Status</th>
            <th scope="col" title="measured inference time / audio time on past runs">RTF</th>
            <th scope="col">Est.</th>
          </tr>
        </thead>
        <tbody>
          {enabled.map((m) => {
            const p = preview.get(m.logical_name);
            return (
              <tr key={m.logical_name} className={clsx(set.has(m.logical_name) && "transcribe__model--on")}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Run ${m.logical_name}`}
                    checked={set.has(m.logical_name)}
                    onChange={(e) => toggle(m.logical_name, e.target.checked)}
                  />
                </td>
                <td className="num">{m.logical_name}</td>
                <td className="muted">{m.architecture_family}</td>
                <td className="num">{m.artifact_size_bytes ? `${(m.artifact_size_bytes / 1e9).toFixed(1)} GB` : "—"}</td>
                <td>
                  {m.ensemble_eligible ? (
                    <Badge tone="info" title="counts in ensemble agreement">
                      voter
                    </Badge>
                  ) : (
                    <Badge tone="derived" title="research-only: recorded, never counted in agreement">
                      research
                    </Badge>
                  )}{" "}
                  {!m.sweep_eligible ? (
                    <Badge tone="warn" title="not yet qualified for full sweeps">
                      unqualified
                    </Badge>
                  ) : null}
                </td>
                <td className="num">{p?.observed_rtf != null ? p.observed_rtf.toFixed(3) : "—"}</td>
                <td className="num">{set.has(m.logical_name) ? minutes(p?.estimated_minutes) : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </fieldset>
  );
}

function NewRun() {
  const client = useQueryClient();
  const facets = useQuery({ queryKey: ["facets"], queryFn: api.facets, staleTime: 60_000 });
  const models = useQuery({ queryKey: ["models"], queryFn: runs.models });
  const suites = useQuery({ queryKey: ["suites"], queryFn: runs.suites });

  const [source, setSource] = useState("home_atc_archive");
  const [scope, setScope] = useState<Scope>("day");
  const [day, setDay] = useState(() => new Date(Date.now() - 86_400_000).toISOString().slice(0, 10));
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [channels, setChannels] = useState<string[]>([]);
  const [minSec, setMinSec] = useState("");
  const [limit, setLimit] = useState("");
  const [seed, setSeed] = useState(0);
  const [untranscribed, setUntranscribed] = useState(true);
  const [selected, setSelected] = useState<string[] | null>(null);
  const [allowUnqualified, setAllowUnqualified] = useState(false);
  const [name, setName] = useState("");

  // Default model selection: every enabled voting model.
  useEffect(() => {
    if (selected === null && models.data) {
      setSelected(models.data.filter((m) => m.enabled && m.ensemble_eligible).map((m) => m.logical_name));
    }
  }, [models.data, selected]);

  const body: SweepCreate | null = useMemo(() => {
    if (!selected?.length) return null;
    return {
      models: selected,
      name: name.trim() || null,
      selection: {
        source_key: source,
        relative_dir: scope === "day" && day ? day.replaceAll("-", "/") : null,
        utc_from: scope === "range" && from ? `${from}:00Z` : null,
        utc_to: scope === "range" && to ? `${to}:00Z` : null,
        channels,
        min_duration_ms: minSec ? Math.round(Number(minSec) * 1000) : null,
        limit: limit ? Number(limit) : null,
        seed,
        untranscribed_only: untranscribed,
      },
    };
  }, [selected, name, source, scope, day, from, to, channels, minSec, limit, seed, untranscribed]);
  const debounced = useDebounced(body);
  const preview = useQuery({
    queryKey: ["sweep-preview", debounced],
    queryFn: () => runs.preview(debounced!),
    enabled: debounced !== null,
  });
  const previewModels = useMemo(
    () => new Map((preview.data?.models ?? []).map((m) => [m.logical_name, m])),
    [preview.data],
  );
  const needsUnqualified = preview.data?.needs_unqualified ?? [];
  // Models run one at a time over the whole selection: fastest first, so every segment
  // gets 2-family agreement as early as possible (unmeasured models last).
  const fastestFirst = (names: string[]) =>
    [...names].sort(
      (a, b) =>
        (previewModels.get(a)?.observed_rtf ?? Number.POSITIVE_INFINITY) -
        (previewModels.get(b)?.observed_rtf ?? Number.POSITIVE_INFINITY),
    );
  const create = useMutation({
    mutationFn: () => runs.create({ ...body!, models: fastestFirst(body!.models ?? []) }, allowUnqualified),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["sweeps"] });
      client.invalidateQueries({ queryKey: ["sweep-preview"] });
    },
  });

  const sources = facets.data?.sources ?? [];
  return (
    <Section title="New transcription run" id="new-run">
      <form
        className="form transcribe__form"
        aria-label="New transcription run"
        onSubmit={(e) => {
          e.preventDefault();
          if (body) create.mutate();
        }}
      >
        <div className="form__row">
          <label className="field">
            <span>Source</span>
            <select className="select" value={source} onChange={(e) => setSource(e.target.value)}>
              {sources.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.key}
                  {s.role === "benchmark" ? " (benchmark)" : ""}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="field fieldset">
            <legend>Portion</legend>
            <div className="transcribe__scope">
              {(["day", "range", "all"] as const).map((s) => (
                <label key={s} className="rail__check">
                  <input type="radio" name="scope" checked={scope === s} onChange={() => setScope(s)} />
                  {s === "day" ? "One day" : s === "range" ? "UTC range" : "Everything"}
                </label>
              ))}
            </div>
          </fieldset>
          {scope === "day" ? (
            <label className="field">
              <span>Day (archive folder)</span>
              <input className="input num" type="date" aria-label="Day" value={day} onChange={(e) => setDay(e.target.value)} />
            </label>
          ) : null}
          {scope === "range" ? (
            <>
              <label className="field">
                <span>From (UTC)</span>
                <input className="input num" type="datetime-local" aria-label="From (UTC)" value={from} onChange={(e) => setFrom(e.target.value)} />
              </label>
              <label className="field">
                <span>Before (UTC)</span>
                <input className="input num" type="datetime-local" aria-label="Before (UTC)" value={to} onChange={(e) => setTo(e.target.value)} />
              </label>
            </>
          ) : null}
        </div>
        <div className="form__row">
          <fieldset className="field fieldset">
            <legend>Channels (none checked = all)</legend>
            <div className="transcribe__channels">
              {(facets.data?.channels ?? []).map((c) => (
                <label key={c} className="rail__check">
                  <input
                    type="checkbox"
                    checked={channels.includes(c)}
                    onChange={(e) => setChannels(e.target.checked ? [...channels, c] : channels.filter((x) => x !== c))}
                  />
                  {c}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="field">
            <span>Min duration (s)</span>
            <input className="input input--num num" type="number" min={0} step={0.5} value={minSec} onChange={(e) => setMinSec(e.target.value)} placeholder="any" />
          </label>
          <label className="field">
            <span>Sample N</span>
            <input className="input input--num num" type="number" min={1} value={limit} onChange={(e) => setLimit(e.target.value)} placeholder="all" />
          </label>
          <label className="field">
            <span>Seed</span>
            <input className="input input--num num" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
          </label>
          <label className="rail__check transcribe__untranscribed">
            <input type="checkbox" checked={untranscribed} onChange={(e) => setUntranscribed(e.target.checked)} />
            only segments these models have not transcribed
          </label>
        </div>

        {models.data && selected ? (
          <ModelPicker
            models={models.data}
            suites={suites.data ?? []}
            selected={selected}
            onChange={setSelected}
            preview={previewModels}
          />
        ) : null}

        <div className="transcribe__preview" role="status" aria-live="polite">
          {!body ? (
            <span className="muted">Select at least one model.</span>
          ) : preview.isFetching && !preview.data ? (
            <span className="muted">Counting…</span>
          ) : preview.error ? (
            <ErrorBox title="Preview failed" detail={(preview.error as Error).message} />
          ) : preview.data ? (
            <>
              <strong className="num">{fmtCount(preview.data.segments)}</strong> segments ·{" "}
              <strong className="num">{minutes(preview.data.audio_minutes)}</strong> of audio ·{" "}
              {selected?.length} models · estimated <strong className="num">{minutes(preview.data.estimated_minutes)}</strong>
              {preview.data.warnings.map((w) => (
                <div key={w} className="muted transcribe__warning">
                  {w}
                </div>
              ))}
            </>
          ) : null}
        </div>
        {needsUnqualified.length ? (
          <label className="rail__check note note--warn">
            <input type="checkbox" checked={allowUnqualified} onChange={(e) => setAllowUnqualified(e.target.checked)} />
            Allow models not yet qualified for full sweeps ({needsUnqualified.join(", ")}): fine for smoke and
            qualification runs.
          </label>
        ) : null}
        <div className="form__row">
          <label className="field field--grow">
            <span>Run name (optional)</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. BWI 2026-09-26 TWR" />
          </label>
        </div>
        {create.error ? <ErrorBox title="Run not created" detail={(create.error as Error).message} /> : null}
        {create.data ? (
          <p className="note note--derived" role="status">
            Queued run #{create.data.id}: {fmtCount(create.data.segments_total)} segments × {create.data.models.length}{" "}
            models. A worker picks it up on its next claim.
          </p>
        ) : null}
        <div>
          <Button
            type="submit"
            variant="primary"
            disabled={!body || create.isPending || !preview.data?.segments || (needsUnqualified.length > 0 && !allowUnqualified)}
          >
            {create.isPending ? "Queuing…" : "Queue transcription run"}
          </Button>
        </div>
      </form>
    </Section>
  );
}

function progressOf(run: SweepRead) {
  const total = run.models.reduce((n, m) => n + m.segments_total, 0);
  const done = run.models.reduce((n, m) => n + m.segments_completed, 0);
  // Remaining time from the observed rate of this run (segments per ms of wall time).
  const active = run.models.filter((m) => m.started_at);
  const elapsed = active.reduce(
    (n, m) => n + ((m.completed_at ? Date.parse(m.completed_at) : Date.now()) - Date.parse(m.started_at!)),
    0,
  );
  const eta = done > 0 && elapsed > 0 && done < total ? ((total - done) * elapsed) / done / 60000 : null;
  return { total, done, pct: total ? (100 * done) / total : 0, eta };
}

function RunRow({ run }: { run: SweepRead }) {
  const client = useQueryClient();
  const control = useMutation({
    mutationFn: (action: "pause" | "resume" | "cancel" | "retry") => runs.control(run.id, action),
    onSuccess: () => client.invalidateQueries({ queryKey: ["sweeps"] }),
  });
  const { total, done, pct, eta } = progressOf(run);
  const live = ["queued", "running", "paused"].includes(run.status);
  const failed = run.models.some((m) => m.status === "failed");
  const sel = run.selection;
  const scope = sel.relative_dir ?? (sel.utc_from ? `${fmtUtc(sel.utc_from)}→` : "all");
  return (
    <tr className="runs__row" data-testid={`run-${run.id}`}>
      <td className="num">#{run.id}</td>
      <td>
        <div>{run.name ?? <span className="muted">(unnamed)</span>}</div>
        <div className="muted num runs__scope">
          {sel.source_key} · {scope}
          {sel.channels?.length ? ` · ${sel.channels.join(",")}` : ""}
          {sel.limit ? ` · n=${sel.limit}` : ""}
        </div>
      </td>
      <td>
        <Badge tone={STATUS_TONE[run.status] ?? "neutral"}>{run.status}</Badge>
      </td>
      <td className="runs__progress">
        <div className="bar" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={`Run ${run.id} progress`}>
          <div className="bar__fill" style={{ width: `${pct}%` }} />
        </div>
        <span className="num muted">
          {fmtCount(done)} / {fmtCount(total)} ({pct.toFixed(0)}%){eta != null && live ? ` · ~${minutes(eta)} left` : ""}
        </span>
        <ul className="runs__models">
          {run.models.map((m) => (
            <li key={m.id} title={m.last_error ?? undefined}>
              <span className="num">{m.logical_name}</span>{" "}
              <Badge tone={STATUS_TONE[m.status] ?? "neutral"}>{m.status}</Badge>{" "}
              <span className="num muted">
                {m.segments_completed}/{m.segments_total}
                {m.segments_error ? ` · ${m.segments_error} err` : ""}
                {m.segments_abstained ? ` · ${m.segments_abstained} empty` : ""}
                {m.real_time_factor != null ? ` · RTF ${m.real_time_factor.toFixed(3)}` : ""}
                {m.claimed_by && ["loading", "running"].includes(m.status) ? ` · on ${m.claimed_by}` : ""}
              </span>
            </li>
          ))}
        </ul>
      </td>
      <td className="num muted">{fmtUtc(run.created_at)}Z</td>
      <td className="runs__actions">
        {run.status === "paused" ? (
          <IconButton label="Resume" onClick={() => control.mutate("resume")}>
            <Play size={13} />
          </IconButton>
        ) : live ? (
          <IconButton label="Pause" onClick={() => control.mutate("pause")}>
            <Pause size={13} />
          </IconButton>
        ) : null}
        {live ? (
          <IconButton label="Cancel" onClick={() => window.confirm(`Cancel run #${run.id}? Recorded results are kept.`) && control.mutate("cancel")}>
            <Square size={13} />
          </IconButton>
        ) : null}
        {failed ? (
          <IconButton label="Retry failed models" onClick={() => control.mutate("retry")}>
            <RotateCw size={13} />
          </IconButton>
        ) : null}
        {control.error ? <span className="note note--danger">{(control.error as Error).message}</span> : null}
      </td>
    </tr>
  );
}

function Workers({ workers }: { workers: WorkerRead[] }) {
  return (
    <table className="mini-table">
      <caption className="mini-table__caption">Workers</caption>
      <thead>
        <tr>
          <th scope="col">Worker</th>
          <th scope="col">Profile</th>
          <th scope="col">GPU</th>
          <th scope="col">Last heartbeat</th>
          <th scope="col">Health</th>
        </tr>
      </thead>
      <tbody>
        {workers.map((w) => {
          const hw = (w.health.hardware ?? {}) as { profile?: string; gpus?: { name: string }[] };
          const age = (Date.now() - Date.parse(w.last_heartbeat_at)) / 1000;
          const stale = age > 180;
          return (
            <tr key={w.name}>
              <td className="num">{w.name}</td>
              <td className="num">{hw.profile ?? "—"}</td>
              <td>{hw.gpus?.[0]?.name ?? <span className="muted">—</span>}</td>
              <td className="num">
                {age < 90 ? `${Math.round(age)} s ago` : age < 5400 ? `${Math.round(age / 60)} min ago` : `${Math.round(age / 3600)} h ago`}
              </td>
              <td>
                <Badge tone={stale ? "neutral" : w.health.status === "ok" ? "ok" : "warn"}>
                  {stale ? "offline" : String(w.health.status ?? "?")}
                </Badge>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Select a portion of the archive, choose models, queue a run, watch progress. */
export function TranscribePage() {
  const sweeps = useQuery({
    queryKey: ["sweeps"],
    queryFn: () => runs.list(25),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((r) => ["queued", "running"].includes(r.status)) ? 4000 : 15000,
  });
  const workers = useQuery({ queryKey: ["workers"], queryFn: runs.workers, refetchInterval: 15000 });
  const models = useQuery({ queryKey: ["models"], queryFn: runs.models });
  const [message, setMessage] = useState("");
  return (
    <div className="page page--wide">
      <h1 className="page__title">Transcribe</h1>
      <div className="transcribe">
        <NewRun />
        <div>
          <Section title="Runs" id="runs" aside={<span className="muted">refreshes automatically</span>}>
            {sweeps.error ? <ErrorBox title="Runs unavailable" detail={(sweeps.error as Error).message} /> : null}
            <table className="mini-table runs">
              <caption className="sr-only">Transcription runs</caption>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Name / scope</th>
                  <th scope="col">Status</th>
                  <th scope="col">Progress</th>
                  <th scope="col">Created</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(sweeps.data ?? []).map((run) => (
                  <RunRow key={run.id} run={run} />
                ))}
              </tbody>
            </table>
            {sweeps.data && !sweeps.data.length ? <p className="muted">No runs yet.</p> : null}
          </Section>
          <Section title="Workers" id="workers">
            {workers.data ? <Workers workers={workers.data} /> : null}
          </Section>
          {models.data ? <ModelVoting models={models.data} onMessage={setMessage} /> : null}
        </div>
      </div>
      <div className="toast" role="status" aria-live="polite">
        {message}
      </div>
    </div>
  );
}
