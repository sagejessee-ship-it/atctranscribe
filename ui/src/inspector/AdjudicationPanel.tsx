import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";

import { adjudication, describeSkips, usd, type AdjudicationItem, type ItemStatus } from "../api/adjudication";
import type { SegmentReview } from "../api/types";
import { Badge } from "../components/badges";
import { Button, ErrorBox, Section } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import { fmtSim, fmtUtc } from "../lib/format";
import { TranscriptDiff } from "./TranscriptDiff";

const TONE: Record<ItemStatus, "ok" | "info" | "warn" | "danger" | "neutral"> = {
  queued: "neutral",
  running: "info",
  done: "ok",
  failed: "danger",
  skipped: "warn",
  cancelled: "neutral",
};

function ItemCard({
  item,
  representative,
  onUse,
  onAccept,
  accepting,
}: {
  item: AdjudicationItem;
  representative: string | null;
  onUse: (text: string) => void;
  onAccept: (id: number) => void;
  accepting: boolean;
}) {
  const r = item.result ?? {};
  const usable = item.status === "done" && item.speech_present && !!item.transcript;
  return (
    <article className="adj-item" data-testid={`adjudication-${item.id}`}>
      <header className="adj-item__head">
        <Badge tone={TONE[item.status]}>{item.status}</Badge>
        <span className="num">{item.model}</span>
        {item.confidence != null ? (
          <span className="muted" title="the model's own probability that the transcript is exact">
            conf <span className="num">{item.confidence.toFixed(2)}</span>
          </span>
        ) : null}
        <span className="muted num" title="what OpenRouter charged for this clip">
          {usd(item.cost_usd, 4)}
        </span>
        <span className="muted">
          batch #{item.batch_id} · {item.finished_at ? fmtUtc(item.finished_at) : fmtUtc(item.created_at)}
        </span>
        {item.accepted_version_id ? <Badge tone="derived">accepted as silver</Badge> : null}
      </header>
      {item.status === "done" && !item.speech_present ? (
        <p className="muted">No intelligible speech, according to the model.</p>
      ) : null}
      {item.transcript ? (
        <>
          <p className="adj-item__text">{item.transcript}</p>
          {representative ? <TranscriptDiff before={representative} after={item.transcript} label="representative" /> : null}
        </>
      ) : null}
      {item.status === "done" ? (
        <p className="muted adj-item__meta">
          nearest hypothesis <span className="num">{item.best_hypothesis_model ?? "—"}</span> (similarity{" "}
          <span className="num">{fmtSim(item.best_hypothesis_similarity)}</span>)
          {r.uncertain_words?.length ? <> · unsure of “{r.uncertain_words.join("”, “")}”</> : null}
          {r.callsigns?.length ? <> · callsigns {r.callsigns.join(", ")}</> : null}
          {r.notes ? <> · {r.notes}</> : null}
        </p>
      ) : null}
      {item.error && item.status !== "done" ? <p className="note note--danger">{item.error}</p> : null}
      {usable ? (
        <div className="adj-item__actions">
          <Button size="sm" onClick={() => onUse(item.transcript!)} title="Put this text in the correction editor to check and save yourself">
            Use as correction
          </Button>
          {!item.accepted_version_id ? (
            <Button size="sm" onClick={() => onAccept(item.id)} disabled={accepting} title="Silver (model_adjudicated); never over human text, gold or a rejection">
              Accept as silver
            </Button>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

/**
 * Adjudications of this segment by the external model (ADR-022). Model output:
 * a human can take it into the editor, or accept it as silver. Never gold.
 */
export function AdjudicationPanel({
  segment,
  onUse,
  onAdjudicate,
  onToast,
}: {
  segment: SegmentReview;
  onUse: (text: string) => void;
  onAdjudicate?: () => void;
  onToast: (message: string) => void;
}) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const items = useQuery({
    queryKey: ["adjudications", segment.segment_id],
    queryFn: () => adjudication.forSegment(segment.segment_id),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((i) => i.status === "queued" || i.status === "running") ? 3000 : false,
  });
  const accept = useMutation({
    mutationFn: (id: number) => adjudication.accept({ item_ids: [id], annotator }),
    onSuccess: (outcome) => {
      onToast(outcome.applied ? "Accepted as silver" : `Not applied: ${describeSkips(outcome.skipped)}`);
      client.invalidateQueries({ queryKey: ["adjudications", segment.segment_id] });
      client.invalidateQueries({ queryKey: ["segment", segment.segment_id] });
      client.invalidateQueries({ queryKey: ["review"] });
    },
  });
  const list = items.data ?? [];
  const representative = (segment.agreement?.representative_text as string | null | undefined) ?? null;
  return (
    <Section
      title="Model adjudication"
      id="adjudication"
      aside={
        onAdjudicate ? (
          <Button size="sm" onClick={onAdjudicate} title="Price and send this segment to Gemini (asks before spending)">
            <Sparkles size={12} aria-hidden /> Adjudicate…
          </Button>
        ) : null
      }
    >
      {items.error ? <ErrorBox title="Adjudications unavailable" detail={(items.error as Error).message} /> : null}
      {items.isSuccess && !list.length ? (
        <p className="muted">Not adjudicated. It costs money per clip, so it runs only on segments you send.</p>
      ) : null}
      {list.slice(0, 3).map((item) => (
        <ItemCard
          key={item.id}
          item={item}
          representative={representative}
          onUse={onUse}
          onAccept={(id) => accept.mutate(id)}
          accepting={accept.isPending}
        />
      ))}
      {list.length > 3 ? <p className="muted">{list.length - 3} older adjudications on the Adjudication page.</p> : null}
      {accept.error ? <ErrorBox title="Not accepted" detail={(accept.error as Error).message} /> : null}
    </Section>
  );
}
