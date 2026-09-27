import type { AirportProfile } from "../api/types";
import { fmtFreq } from "../lib/format";

const SERVICE_ORDER = ["TWR", "GND", "CD", "ATIS", "APP", "DEP", "CLASS_B", "PROC", "EMERG", "OTHER"];

/** Annotator context from the FAA-derived airport profile. Never gold text. */
export function AirportContext({
  profile,
  frequencyHz,
}: {
  profile: AirportProfile | null;
  frequencyHz: number | null;
}) {
  if (!profile) {
    return (
      <details className="section section--details">
        <summary className="section__title">Airport context</summary>
        <p className="muted">
          No airport profile for this station. Bootstrap one with <code>aerochorus airport bootstrap</code>.
        </p>
      </details>
    );
  }
  const prov = profile.provenance as Record<string, string | undefined>;
  const frequencies = [...profile.frequencies].sort(
    (a, b) => SERVICE_ORDER.indexOf(a.service) - SERVICE_ORDER.indexOf(b.service) || a.frequency_hz - b.frequency_hz,
  );
  const pairs = [...new Set(profile.runways.map((r) => r.pair))];
  return (
    <details className="section section--details">
      <summary className="section__title">
        Airport context · {profile.icao} <span className="muted">runways {pairs.join(", ")}</span>
      </summary>
      <dl className="kv kv--compact">
        <dt>name</dt>
        <dd>{profile.name}</dd>
        <dt>ids</dt>
        <dd className="num">
          ICAO {profile.icao} · FAA {profile.faa_id ?? "—"} · IATA {profile.iata ?? "—"}
        </dd>
        <dt>ref. point</dt>
        <dd className="num">
          {profile.latitude?.toFixed(4)}, {profile.longitude?.toFixed(4)} · elev {profile.elevation_ft ?? "—"} ft · var{" "}
          {profile.magnetic_variation ?? "—"}
        </dd>
        <dt>timezone</dt>
        <dd className="num">{profile.timezone}</dd>
        <dt>aliases</dt>
        <dd>{profile.aliases.join(" · ")}</dd>
      </dl>
      <table className="mini-table">
        <caption className="mini-table__caption">Runway ends</caption>
        <thead>
          <tr>
            <th scope="col">End</th>
            <th scope="col">Pair</th>
            <th scope="col">Size ft</th>
            <th scope="col">True °</th>
            <th scope="col">Spoken</th>
          </tr>
        </thead>
        <tbody>
          {profile.runways.map((r) => (
            <tr key={r.end_ident}>
              <td className="num">{r.end_ident}</td>
              <td className="num">{r.pair}</td>
              <td className="num">
                {r.length_ft ?? "—"}×{r.width_ft ?? "—"}
              </td>
              <td className="num">{r.true_alignment ?? "—"}</td>
              <td>{r.spoken.join(" / ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="mini-table">
        <caption className="mini-table__caption">Voice frequencies</caption>
        <thead>
          <tr>
            <th scope="col">Svc</th>
            <th scope="col">MHz</th>
            <th scope="col">Call</th>
            <th scope="col">Use</th>
            <th scope="col">Spoken</th>
          </tr>
        </thead>
        <tbody>
          {frequencies.map((f) => (
            <tr
              key={`${f.service}-${f.frequency_hz}-${f.sectorization}`}
              className={f.frequency_hz === frequencyHz ? "mini-table__hit" : undefined}
              aria-current={f.frequency_hz === frequencyHz ? "true" : undefined}
            >
              <td>{f.service}</td>
              <td className="num">{fmtFreq(f.frequency_hz)}</td>
              <td>{f.call ?? "—"}</td>
              <td className="muted">{f.sectorization ?? ""}</td>
              <td>{f.spoken[0]}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted provenance">
        Source: {prov.source ?? "?"} · cycle {prov.cycle ?? "?"} · NASR effective {prov.nasr_effective_date ?? "?"} ·
        fetched {prov.fetched_at?.slice(0, 10) ?? "?"}
      </p>
    </details>
  );
}
