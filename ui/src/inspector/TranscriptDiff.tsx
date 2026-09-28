import { Fragment } from "react";

import { diffWords } from "../lib/format";

/** Word diff of a hypothesis (before) against the correction (after). Display only. */
export function TranscriptDiff({ before, after, label }: { before: string; after: string; label: string }) {
  if (!before || !after) return null;
  const ops = diffWords(before, after);
  const changed = ops.some((o) => o.kind !== "eq");
  return (
    <div className="diff" aria-label={`Differences from ${label}`}>
      <span className="muted diff__label">vs {label}:</span>{" "}
      {changed ? (
        ops.map((op, i) => (
          <Fragment key={i}>
            {op.kind === "eq" ? (
              op.text
            ) : op.kind === "del" ? (
              <del className="diff__del">{op.text}</del>
            ) : (
              <ins className="diff__ins">{op.text}</ins>
            )}{" "}
          </Fragment>
        ))
      ) : (
        <span className="muted">identical</span>
      )}
    </div>
  );
}
