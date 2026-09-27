import { useQueries } from "@tanstack/react-query";
import { Link } from "react-router";

import { api } from "../api/client";
import type { ReviewFilters, TrainingLabel } from "../api/types";
import { TrainingBadge } from "../components/badges";
import { Section } from "../components/ui";
import { fmtCount } from "../lib/format";

const LABELS: TrainingLabel[] = ["candidate", "silver", "gold", "rejected"];

/**
 * Training-set overview. Counts come from the review query (server-side);
 * versioned dataset export arrives with Phase 5B (docs/ui/TRAINING_DATASET_LIFECYCLE.md).
 */
export function TrainingPage() {
  const probe = (filters: ReviewFilters) => ({ filters, sort: "utc" as const, descending: true, offset: 0, limit: 1 });
  const counts = useQueries({
    queries: LABELS.flatMap((label) => [
      {
        queryKey: ["review", "count", label, "segment"],
        queryFn: () => api.query(probe({ training_label: [label], min_models: 0 })),
      },
      {
        queryKey: ["review", "count", label, "span"],
        queryFn: () => api.query(probe({ span_labels: [label], min_models: 0 })),
      },
    ]),
  });
  return (
    <div className="page">
      <h1 className="page__title">Training sets</h1>
      <Section title="Labelled data" id="labelled">
        <table className="mini-table training-counts">
          <thead>
            <tr>
              <th scope="col">Label</th>
              <th scope="col">Whole segments</th>
              <th scope="col">Segments with spans</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {LABELS.map((label, i) => {
              const whole = counts[i * 2].data?.total;
              const spans = counts[i * 2 + 1].data?.total;
              const filters = encodeURIComponent(JSON.stringify({ training_label: [label], min_models: 0 }));
              return (
                <tr key={label}>
                  <td>
                    <TrainingBadge label={label} />
                  </td>
                  <td className="num">{whole == null ? "…" : fmtCount(whole)}</td>
                  <td className="num">{spans == null ? "…" : fmtCount(spans)}</td>
                  <td>
                    <Link to={`/review?f=${filters}`}>review</Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Section>
      <p className="muted">
        Versioned dataset export (clip materialization, hashes, grouped splits) is part of Phase 5B.
      </p>
    </div>
  );
}
