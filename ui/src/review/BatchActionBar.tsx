import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { BatchAction, BatchOutcome } from "../api/types";
import { Button, ErrorBox } from "../components/ui";
import { useAnnotator } from "../lib/annotator";

const SKIP_REASONS: Record<string, string> = {
  benchmark_source: "benchmark source",
  human_gold_unchanged: "already human gold",
  rejected_by_human: "rejected by a human",
  no_consensus_text: "no transcript text",
  insufficient_agreement: "no 2-family agreement (silver needs it)",
  not_nominated: "not nominated",
  already_candidate: "already candidate",
  already_silver: "already silver",
  unknown_segment: "unknown",
};

export function describeOutcome(outcome: BatchOutcome): string {
  const skipped = Object.entries(outcome.skipped)
    .map(([reason, n]) => `${n} ${SKIP_REASONS[reason] ?? reason}`)
    .join(", ");
  return `${outcome.applied} updated${skipped ? `; skipped ${skipped}` : ""}`;
}

/**
 * Candidate / silver nomination and clearing for selected rows.
 * Gold is deliberately absent: it is a per-segment human assertion.
 */
export function BatchActionBar({
  selectedIds,
  onClear,
  onDone,
  onAdjudicate,
}: {
  selectedIds: number[];
  onClear: () => void;
  onDone: (message: string) => void;
  onAdjudicate?: (ids: number[]) => void;
}) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const batch = useMutation({
    mutationFn: (action: BatchAction) => api.batch({ action, segment_ids: selectedIds, annotator }),
    onSuccess: (outcome) => {
      onDone(describeOutcome(outcome));
      client.invalidateQueries({ queryKey: ["review"] });
      client.invalidateQueries({ queryKey: ["segment"] });
    },
  });
  if (!selectedIds.length) return null;
  return (
    <div className="batchbar" role="region" aria-label="Batch actions">
      <span className="num">{selectedIds.length} selected</span>
      <Button size="sm" onClick={() => batch.mutate("candidate")} disabled={batch.isPending}>
        Add as candidate
      </Button>
      <Button size="sm" onClick={() => batch.mutate("silver")} disabled={batch.isPending}>
        Mark silver
      </Button>
      <Button size="sm" variant="ghost" onClick={() => batch.mutate("clear")} disabled={batch.isPending}>
        Remove candidate/silver
      </Button>
      {onAdjudicate ? (
        <Button size="sm" onClick={() => onAdjudicate(selectedIds)} title="Price and send to Gemini (asks before spending)">
          Adjudicate…
        </Button>
      ) : null}
      <span className="muted batchbar__note">Gold is set per segment after listening.</span>
      <span className="toolbar__spacer" />
      <Button size="sm" variant="ghost" onClick={onClear}>
        Clear selection
      </Button>
      {batch.error ? <ErrorBox title="Batch action failed" detail={(batch.error as Error).message} /> : null}
    </div>
  );
}
