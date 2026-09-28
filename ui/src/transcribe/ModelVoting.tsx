import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { runs, type ModelEvidence, type ModelRead } from "../api/transcribe";
import { Badge } from "../components/badges";
import { Button, Dialog, ErrorBox, Section } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import { fmtUtc } from "../lib/format";

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);

function Evidence({ e }: { e: ModelEvidence }) {
  return (
    <dl className="kv kv--compact">
      <dt>results</dt>
      <dd className="num">
        {e.results} ({e.spoken} spoken, {e.abstained} empty, {e.errors} errors)
      </dd>
      <dt>vs consensus</dt>
      <dd>
        {e.compared ? (
          <>
            <span className="num">{pct(e.exact_rate)}</span> exact,{" "}
            <span className="num">{pct(e.near_rate)}</span> near-match{" "}
            <span className="muted">(over {e.compared} segments where 2+ other families agree)</span>
          </>
        ) : (
          <span className="muted">no segments yet where 2+ other families agree</span>
        )}
      </dd>
      <dt>over silence</dt>
      <dd>
        {e.silent_segments ? (
          <>
            speaks on <span className="num">{pct(e.speaks_over_silence)}</span> of{" "}
            <span className="num">{e.silent_segments}</span> segments where the voters heard nothing
          </>
        ) : (
          <span className="muted">no segments where the voters all heard nothing</span>
        )}
      </dd>
    </dl>
  );
}

function DecisionDialog({
  model,
  evidence,
  onClose,
  onDone,
}: {
  model: ModelRead;
  evidence: ModelEvidence | undefined;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const [reason, setReason] = useState("");
  const promote = !model.ensemble_eligible;
  const decide = useMutation({
    mutationFn: () => runs.decideEnsemble(model.logical_name, { eligible: promote, reason: reason.trim(), by: annotator }),
    onSuccess: (out) => {
      client.invalidateQueries({ queryKey: ["models"] });
      client.invalidateQueries({ queryKey: ["model-evidence"] });
      client.invalidateQueries({ queryKey: ["review"] });
      client.invalidateQueries({ queryKey: ["segment"] });
      onDone(
        `${model.logical_name} ${promote ? "now votes" : "is now research-only"}; agreement recomputed for ${out.segments_refreshed} segments`,
      );
      onClose();
    },
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && onClose()}
      width={560}
      title={promote ? `Let ${model.logical_name} vote` : `Make ${model.logical_name} research-only`}
      description={
        promote
          ? "Its transcripts will count toward agreement (exact/near groups, utterances, silver eligibility) like the other voting models. Models of the same family never count as independent agreement."
          : "Its transcripts stay recorded and visible, but no longer count toward agreement."
      }
    >
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          if (reason.trim().length >= 3) decide.mutate();
        }}
      >
        {evidence ? <Evidence e={evidence} /> : null}
        <label className="field">
          <span>Reason (recorded with the decision)</span>
          <textarea
            className="input"
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={promote ? "e.g. reviewed 50 segments: matches consensus, no hallucinations on noise" : "e.g. says 'thank you' on noise"}
          />
        </label>
        <p className="muted">
          Agreement is recomputed for every segment this model transcribed ({evidence?.results ?? "?"} in the sample shown).
          Existing silver and gold labels are not changed.
        </p>
        {decide.error ? <ErrorBox title="Not changed" detail={(decide.error as Error).message} /> : null}
        <div className="dialog__actions">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={reason.trim().length < 3 || decide.isPending}>
            {decide.isPending ? "Recomputing agreement…" : promote ? "Let it vote" : "Make research-only"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/**
 * Which models vote in agreement. A model votes only after a deliberate decision:
 * downloading or running it never changes that. Evidence from this corpus is shown
 * next to each switch.
 */
export function ModelVoting({ models, onMessage }: { models: ModelRead[]; onMessage: (m: string) => void }) {
  const evidence = useQuery({ queryKey: ["model-evidence"], queryFn: runs.evidence, staleTime: 60_000 });
  const [deciding, setDeciding] = useState<ModelRead | null>(null);
  const byName = new Map((evidence.data ?? []).map((e) => [e.logical_name, e]));
  const shown = models
    .filter((m) => m.enabled || (byName.get(m.logical_name)?.results ?? 0) > 0)
    .sort((a, b) => Number(b.ensemble_eligible) - Number(a.ensemble_eligible) || a.logical_name.localeCompare(b.logical_name));
  return (
    <Section title="Model voting" id="model-voting" aside={<span className="muted">evidence from your latest transcripts</span>}>
      {evidence.error ? <ErrorBox title="Evidence unavailable" detail={(evidence.error as Error).message} /> : null}
      <table className="mini-table voting">
        <caption className="sr-only">Which models vote in agreement, with evidence</caption>
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Status</th>
            <th scope="col" title="latest result per segment, up to 3000 segments">Results</th>
            <th scope="col" title="exact match with the consensus of 2+ other families">Exact</th>
            <th scope="col" title="order-aware similarity at or above the near-match threshold">Near</th>
            <th scope="col" title="produces words where the voters all heard nothing">Over silence</th>
            <th scope="col">
              <span className="sr-only">Action</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((m) => {
            const e = byName.get(m.logical_name);
            const d = m.ensemble_decision;
            return (
              <tr key={m.logical_name}>
                <td>
                  <span className="num">{m.logical_name}</span> <span className="muted">{m.architecture_family}</span>
                </td>
                <td>
                  <Badge
                    tone={m.ensemble_eligible ? "ok" : "derived"}
                    title={d ? `decided ${fmtUtc(d.at)} by ${d.by ?? "?"}: ${d.reason}` : "catalog default (config/models.toml)"}
                  >
                    {m.ensemble_eligible ? "voting" : "research"}
                  </Badge>
                  {d ? <span className="muted"> decided</span> : null}
                </td>
                <td className="num">{e ? `${e.results}` : "…"}</td>
                <td className="num">{e?.compared ? pct(e.exact_rate) : "—"}</td>
                <td className="num">{e?.compared ? pct(e.near_rate) : "—"}</td>
                <td className="num">{e?.silent_segments ? pct(e.speaks_over_silence) : "—"}</td>
                <td>
                  <Button size="sm" variant="ghost" onClick={() => setDeciding(m)} aria-label={m.ensemble_eligible ? `Make ${m.logical_name} research-only` : `Let ${m.logical_name} vote`}>
                    {m.ensemble_eligible ? "Make research…" : "Let vote…"}
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted voting__note">
        A model votes only after a deliberate decision; downloading or running it never changes this. Look at its
        transcripts in the review workbench first (filter by model). Decisions are recorded and kept by{" "}
        <code>models sync</code>.
      </p>
      {deciding ? (
        <DecisionDialog model={deciding} evidence={byName.get(deciding.logical_name)} onClose={() => setDeciding(null)} onDone={onMessage} />
      ) : null}
    </Section>
  );
}
