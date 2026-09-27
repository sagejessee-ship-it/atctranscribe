import type { ReviewFilters, ReviewStatus, SearchScope, TrainingLabel } from "../api/types";
import { fmtUtc } from "../lib/format";

export const REVIEW_STATUSES: ReviewStatus[] = ["unreviewed", "reviewed", "corrected"];
export const TRAINING_LABELS: TrainingLabel[] = ["none", "candidate", "silver", "gold", "rejected"];
export const SCOPES: { value: SearchScope; label: string }[] = [
  { value: "any", label: "Any transcript" },
  { value: "human", label: "Human" },
  { value: "consensus", label: "Consensus/best" },
  { value: "hypotheses", label: "Hypotheses" },
  { value: "model", label: "Specific model" },
];

const tri = (value: boolean) => (value ? "yes" : "no");

/** One human-readable chip per active filter; `clear` removes exactly that constraint. */
export function describeFilters(
  f: ReviewFilters,
): { key: string; label: string; clear: (f: ReviewFilters) => ReviewFilters }[] {
  const chips: { key: string; label: string; clear: (f: ReviewFilters) => ReviewFilters }[] = [];
  const drop =
    (...keys: (keyof ReviewFilters)[]) =>
    (x: ReviewFilters) => {
      const copy = { ...x };
      for (const k of keys) delete copy[k];
      return copy;
    };
  const list = (key: keyof ReviewFilters, name: string) => {
    const value = f[key] as string[] | undefined;
    if (value?.length) chips.push({ key, label: `${name}: ${value.join(", ")}`, clear: drop(key) });
  };
  const num = (key: keyof ReviewFilters, label: (v: number) => string) => {
    const value = f[key] as number | null | undefined;
    if (value != null) chips.push({ key, label: label(value), clear: drop(key) });
  };

  if (f.q) {
    const scope = SCOPES.find((s) => s.value === (f.scope ?? "any"))?.label ?? "Any";
    const model = f.scope === "model" && f.scope_model ? ` (${f.scope_model})` : "";
    chips.push({ key: "q", label: `“${f.q}” in ${scope}${model}`, clear: drop("q", "scope", "scope_model") });
  }
  if (f.sample_id != null) chips.push({ key: "sample_id", label: `sample #${f.sample_id}`, clear: drop("sample_id") });
  if (f.airport) chips.push({ key: "airport", label: `airport: ${f.airport}`, clear: drop("airport") });
  list("source_keys", "source");
  if (f.include_benchmark) chips.push({ key: "include_benchmark", label: "incl. benchmark", clear: drop("include_benchmark") });
  list("channels", "channel");
  if (f.utc_from) chips.push({ key: "utc_from", label: `from ${fmtUtc(f.utc_from)}Z`, clear: drop("utc_from") });
  if (f.utc_to) chips.push({ key: "utc_to", label: `before ${fmtUtc(f.utc_to)}Z`, clear: drop("utc_to") });
  list("models", "model");
  list("families", "family");
  if (f.min_models === 0) chips.push({ key: "min_models", label: "incl. untranscribed", clear: drop("min_models") });
  else num("min_models", (v) => `models ≥ ${v}`);
  num("min_success", (v) => `spoken ≥ ${v}`);
  num("min_exact_providers", (v) => `exact providers ≥ ${v}`);
  num("min_exact_families", (v) => `exact families ≥ ${v}`);
  num("max_exact_families", (v) => `exact families ≤ ${v}`);
  num("min_near_families", (v) => `near families ≥ ${v}`);
  num("max_near_families", (v) => `near families ≤ ${v}`);
  num("min_near_similarity", (v) => `near sim ≥ ${v}`);
  list("review_status", "review");
  list("training_label", "training");
  list("span_labels", "span");
  if (f.has_error != null) chips.push({ key: "has_error", label: `error: ${tri(f.has_error)}`, clear: drop("has_error") });
  if (f.has_abstention != null)
    chips.push({ key: "has_abstention", label: `abstention: ${tri(f.has_abstention)}`, clear: drop("has_abstention") });
  if (f.has_error_or_abstention != null)
    chips.push({
      key: "has_error_or_abstention",
      label: `error/abstention: ${tri(f.has_error_or_abstention)}`,
      clear: drop("has_error_or_abstention"),
    });
  list("flags", "flag");
  return chips;
}
