import type { ReviewRow } from "../api/types";

type Source = Pick<
  ReviewRow,
  | "human_text"
  | "representative_source"
  | "representative_models"
  | "representative_families"
  | "representative_highlights"
>;

/**
 * Where the Transcript column's text comes from, in plain words. It is never a blend:
 * a reviewer's saved text, or one model's own words picked by agreement (ADR-016).
 */
export function transcriptSource(row: Source): string {
  if (row.human_text) return "Saved by a reviewer: this replaces the models' text here.";
  const models = row.representative_models ?? [];
  const families = row.representative_families ?? [];
  const who = models.length ? models.join(", ") : "one model";
  switch (row.representative_source) {
    case "exact":
      return `${families.length} model families wrote exactly this (ignoring case and punctuation): ${who}.`;
    case "near":
      return `${who}'s words. ${families.length} families nearly agree with it (${families.join(", ")}).`;
    case "medoid":
      return `No two model families agree. These are ${who}'s words: the hypothesis closest to all the others.`;
    case "single":
      return `Only ${who} produced words.`;
    default:
      return "";
  }
}

export function highlightNote(row: Source): string | null {
  return !row.human_text && row.representative_highlights?.length
    ? "Marked: stretches that at least two model families agree on."
    : null;
}
