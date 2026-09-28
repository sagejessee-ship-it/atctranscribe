import { Fragment } from "react";
import clsx from "clsx";

import type { Highlight } from "../api/types";

/**
 * Renders `text` word by word, marking the word spans the backend reported as
 * belonging to agreed utterances. Spans are computed server-side (agreement v2);
 * this component only displays them.
 */
export function HighlightedText({ text, highlights }: { text: string; highlights: Highlight[] }) {
  if (!highlights.length) return <>{text}</>;
  const words = text.split(/\s+/).filter(Boolean);
  const mark: (Highlight | null)[] = words.map(() => null);
  for (const h of [...highlights].sort((a, b) => a.family_count - b.family_count)) {
    for (let i = h.start; i < Math.min(h.end, words.length); i++) mark[i] = h;
  }
  const out: { h: Highlight | null; words: string[] }[] = [];
  words.forEach((w, i) => {
    const last = out[out.length - 1];
    if (last && last.h === mark[i]) last.words.push(w);
    else out.push({ h: mark[i], words: [w] });
  });
  return (
    <>
      {out.map((run, i) => (
        <Fragment key={i}>
          {i > 0 ? " " : ""}
          {run.h ? (
            <mark
              className={clsx("agree-mark", run.h.family_count >= 3 && "agree-mark--strong")}
              title={`agreed utterance U${run.h.utterance + 1}: ${run.h.family_count} families`}
            >
              {run.words.join(" ")}
            </mark>
          ) : (
            run.words.join(" ")
          )}
        </Fragment>
      ))}
    </>
  );
}
