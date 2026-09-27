import type { Agreement } from "../api/types";
import { AgreementBadge, Badge } from "../components/badges";
import { Section } from "../components/ui";
import { fmtSim } from "../lib/format";

export const GROUP_LABEL = (index: number) => `G${index + 1}`;

/** Exact groups, near group, abstentions/errors and flags, exactly as the backend computed them. */
export function AgreementSummary({ agreement }: { agreement: Agreement | null }) {
  if (!agreement) {
    return (
      <Section title="Agreement" id="agreement">
        <p className="muted">No model results yet.</p>
      </Section>
    );
  }
  const near = agreement.near_group;
  const flags = Object.entries(agreement.flags ?? {});
  return (
    <Section
      title="Agreement"
      id="agreement"
      aside={
        <span className="muted num" title="agreement algorithm version and near-match threshold">
          v{agreement.version} · near ≥ {agreement.near_threshold}
        </span>
      }
    >
      <div className="agree__counts">
        <span>
          <span className="muted">results</span> <span className="num">{agreement.results_count}</span>
        </span>
        <span>
          <span className="muted">spoken</span> <span className="num">{agreement.success_count}</span>
        </span>
        <span>
          <span className="muted">abstained</span>{" "}
          {agreement.abstained_count ? <Badge tone="neutral">{agreement.abstained_count}</Badge> : <span className="num">0</span>}
        </span>
        <span>
          <span className="muted">errors</span>{" "}
          {agreement.error_count ? <Badge tone="danger">{agreement.error_count}</Badge> : <span className="num">0</span>}
        </span>
        <span>
          <span className="muted">families</span> <span className="num">{agreement.families.length}</span>
        </span>
      </div>
      {agreement.exact_groups.length ? (
        <table className="mini-table">
          <caption className="sr-only">Exact agreement groups</caption>
          <thead>
            <tr>
              <th scope="col">Group</th>
              <th scope="col">Agree</th>
              <th scope="col">Text</th>
              <th scope="col">Providers</th>
            </tr>
          </thead>
          <tbody>
            {agreement.exact_groups.map((g, i) => (
              <tr key={g.normalized}>
                <td className="num">{GROUP_LABEL(i)}</td>
                <td>
                  <AgreementBadge families={g.family_count} providers={g.provider_count} />
                </td>
                <td className="mini-table__text">{g.display_text}</td>
                <td className="muted mini-table__providers" title={g.families.join(", ")}>
                  {g.providers.join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No model produced words.</p>
      )}
      {near ? (
        <p className="agree__near">
          <span className="muted">Near group</span>{" "}
          <AgreementBadge families={near.family_count} kind="near" /> anchored on{" "}
          <span className="num">{near.anchor_model}</span>, weakest similarity{" "}
          <span className="num">{fmtSim(near.min_similarity)}</span>{" "}
          <span className="muted">({near.families.join(", ")})</span>
        </p>
      ) : null}
      {flags.length ? (
        <div className="agree__flags">
          <span className="muted">Risk flags</span>
          {flags.map(([flag, count]) => (
            <Badge key={flag} tone="warn" title={`${count} hypothesis(es) flagged`}>
              {flag} ×{count}
            </Badge>
          ))}
        </div>
      ) : null}
      <p className="muted agree__rep">
        Representative text source: <span className="num">{agreement.representative_source ?? "—"}</span>
      </p>
    </Section>
  );
}
