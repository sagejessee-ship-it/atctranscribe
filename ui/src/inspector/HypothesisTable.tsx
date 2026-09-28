import { Fragment, useState } from "react";
import clsx from "clsx";
import { ClipboardCopy, CornerDownLeft, Info } from "lucide-react";

import type { Hypothesis } from "../api/types";
import { Badge } from "../components/badges";
import { Button, IconButton, Section } from "../components/ui";
import { HighlightedText } from "../components/HighlightedText";
import { fmtSim } from "../lib/format";
import { GROUP_LABEL } from "./AgreementSummary";

const STATUS_TONE = { success: "ok", abstained: "neutral", error: "danger" } as const;

/** Every model result, immutable. Selecting a row makes it the "A" source and the diff base. */
export function HypothesisTable({
  hypotheses,
  selectedId,
  onSelect,
  onUse,
}: {
  hypotheses: Hypothesis[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onUse: (text: string) => void;
}) {
  const [showOlder, setShowOlder] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const older = hypotheses.filter((h) => h.superseded).length;
  const rows = hypotheses.filter((h) => showOlder || !h.superseded);
  return (
    <Section
      title="Hypotheses"
      id="hypotheses"
      aside={
        older ? (
          <Button size="sm" variant="ghost" onClick={() => setShowOlder((v) => !v)}>
            {showOlder ? "Hide" : "Show"} {older} older
          </Button>
        ) : null
      }
    >
      {rows.length === 0 ? <p className="muted">No model results yet.</p> : null}
      <table className="hyp-table">
        <caption className="sr-only">Model hypotheses (read-only)</caption>
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Status</th>
            <th scope="col">Transcript</th>
            <th scope="col" title="exact group / similarity to the representative text">Grp · sim</th>
            <th scope="col" title="model-specific score; not comparable across models">Model conf.</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((h) => (
            <Fragment key={h.result_id}>
              <tr
                className={clsx("hyp-row", selectedId === h.result_id && "hyp-row--selected", h.superseded && "hyp-row--old")}
                onClick={() => onSelect(h.result_id)}
              >
                <td className="hyp-row__model">
                  <span className="num">{h.model}</span>
                  {selectedId === h.result_id ? <span className="sr-only">(selected)</span> : null}
                  <span className="muted hyp-row__family">
                    {h.architecture_family}
                    {h.ensemble_eligible === false ? " · research" : ""}
                    {h.has_word_times ? " · timed" : ""}
                  </span>
                </td>
                <td>
                  <Badge tone={STATUS_TONE[h.status]}>{h.status}</Badge>
                </td>
                <td className="hyp-row__text">
                  {h.text ? (
                    <HighlightedText text={h.text} highlights={h.highlights ?? []} />
                  ) : (
                    <span className="muted">{h.status === "error" ? String(h.provenance.error_type ?? "error") : "(no words)"}</span>
                  )}
                  {h.flags.length ? (
                    <span className="hyp-row__flags">
                      {h.flags.map((f) => (
                        <Badge key={f} tone="warn">
                          {f}
                        </Badge>
                      ))}
                    </span>
                  ) : null}
                </td>
                <td className="num hyp-row__grp">
                  {h.exact_group != null ? GROUP_LABEL(h.exact_group) : "—"} · {fmtSim(h.similarity_to_representative)}
                </td>
                <td className="num">{h.model_confidence == null ? "—" : h.model_confidence.toFixed(2)}</td>
                <td className="hyp-row__actions">
                  <IconButton
                    label="Use as correction starting text (A)"
                    disabled={!h.text}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (h.text) onUse(h.text);
                    }}
                  >
                    <CornerDownLeft size={13} />
                  </IconButton>
                  <IconButton
                    label={copied === h.result_id ? "Copied" : "Copy transcript"}
                    disabled={!h.text}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (!h.text) return;
                      navigator.clipboard?.writeText(h.text).then(
                        () => setCopied(h.result_id),
                        () => setCopied(null),
                      );
                    }}
                  >
                    <ClipboardCopy size={13} />
                  </IconButton>
                  <IconButton
                    label="Provenance"
                    aria-expanded={open === h.result_id}
                    onClick={(e) => {
                      e.stopPropagation();
                      setOpen(open === h.result_id ? null : h.result_id);
                    }}
                  >
                    <Info size={13} />
                  </IconButton>
                </td>
              </tr>
              {open === h.result_id ? (
                <tr className="hyp-prov">
                  <td colSpan={6}>
                    <dl className="kv kv--compact">
                      <dt>result</dt>
                      <dd className="num">{h.result_id}</dd>
                      <dt>sweep / attempt</dt>
                      <dd className="num">
                        {h.sweep_id} / {h.attempt}
                      </dd>
                      <dt>recorded</dt>
                      <dd className="num">{h.created_at}</dd>
                      {Object.entries(h.provenance)
                        .filter(([, v]) => v != null && v !== "")
                        .map(([k, v]) => (
                          <Fragment key={k}>
                            <dt>{k.replaceAll("_", " ")}</dt>
                            <dd className="num">{String(v)}</dd>
                          </Fragment>
                        ))}
                    </dl>
                  </td>
                </tr>
              ) : null}
            </Fragment>
          ))}
        </tbody>
      </table>
    </Section>
  );
}
