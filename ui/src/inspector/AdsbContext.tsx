import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Satellite } from "lucide-react";

import { api } from "../api/client";
import type { AdsbSnapshot } from "../api/types";
import { Button, ErrorBox } from "../components/ui";
import { fmtUtc } from "../lib/format";

const dash = (v: number | string | null | undefined, suffix = "") => (v == null ? "—" : `${v}${suffix}`);

/**
 * Nearby historical traffic from OpenSky, on demand. Nothing is queried until
 * the button is pressed; the snapshot is then cached server-side. Context
 * only: it never changes a transcript or label.
 */
export function AdsbContext({ segmentId, hasUtc }: { segmentId: number; hasUtc: boolean }) {
  const client = useQueryClient();
  // Status reads the cache only; it never contacts the provider.
  const status = useQuery({ queryKey: ["adsb", segmentId], queryFn: () => api.adsbStatus(segmentId) });
  const fetcher = useMutation({
    mutationFn: (refresh: boolean) => api.adsbFetch(segmentId, refresh),
    onSuccess: (snapshot) =>
      client.setQueryData(["adsb", segmentId], (old: typeof status.data) =>
        old ? { ...old, snapshot } : old,
      ),
  });
  const snapshot: AdsbSnapshot | null = status.data?.snapshot ?? null;
  const configured = status.data?.configured ?? false;

  return (
    <details className="section section--details" open={!!snapshot || fetcher.isPending}>
      <summary className="section__title">
        ADS-B context{" "}
        <span className="muted">
          {snapshot ? `${snapshot.aircraft.length} aircraft · cached` : configured ? "on demand" : "unavailable"}
        </span>
      </summary>
      {status.isError ? <ErrorBox title="ADS-B status unavailable" detail={(status.error as Error).message} /> : null}
      {status.data && !configured ? (
        <p className="note note--neutral" role="status">
          ADS-B context unavailable: {status.data.message ?? "not configured"}. Review is unaffected.
        </p>
      ) : null}
      {configured && !snapshot ? (
        <div className="adsb__actions">
          <Button onClick={() => fetcher.mutate(false)} disabled={fetcher.isPending || !hasUtc}>
            <Satellite size={13} aria-hidden /> {fetcher.isPending ? "Querying OpenSky…" : "Fetch ADS-B context"}
          </Button>
          <span className="muted">
            {hasUtc
              ? `−${status.data?.window_before_s}/+${status.data?.window_after_s} s, ${status.data?.radius_nm} nm around the airport (historical state vectors)`
              : "segment has no absolute UTC"}
          </span>
        </div>
      ) : null}
      {fetcher.error ? (
        <ErrorBox
          title="ADS-B fetch failed (annotations are unaffected)"
          detail={(fetcher.error as Error).message}
          action={
            <Button size="sm" onClick={() => fetcher.mutate(!!snapshot)}>
              Retry
            </Button>
          }
        />
      ) : null}
      {snapshot ? (
        <>
          <table className="mini-table adsb">
            <caption className="sr-only">Nearby aircraft (nearest state vector to the segment start)</caption>
            <thead>
              <tr>
                <th scope="col">Callsign</th>
                <th scope="col">ICAO24</th>
                <th scope="col" title="state vector time minus segment start">Δt</th>
                <th scope="col">Dist nm</th>
                <th scope="col">Lat, lon</th>
                <th scope="col">Alt ft</th>
                <th scope="col">Hdg°</th>
                <th scope="col">Kt</th>
                <th scope="col">V/S fpm</th>
                <th scope="col">Gnd</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.aircraft.map((a) => (
                <tr key={a.icao24}>
                  <td className="num">{a.callsign ?? <span className="muted">—</span>}</td>
                  <td className="num">{a.icao24}</td>
                  <td className="num">
                    {a.offset_s > 0 ? "+" : ""}
                    {a.offset_s}s
                  </td>
                  <td className="num">{dash(a.distance_nm)}</td>
                  <td className="num">
                    {dash(a.lat)}, {dash(a.lon)}
                  </td>
                  <td className="num">{dash(a.baro_altitude_ft)}</td>
                  <td className="num">{dash(a.heading_deg)}</td>
                  <td className="num">{dash(a.velocity_kt)}</td>
                  <td className="num">{dash(a.vertical_rate_fpm)}</td>
                  <td>{a.on_ground == null ? "—" : a.on_ground ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!snapshot.aircraft.length ? <p className="muted">No aircraft in the queried window.</p> : null}
          <p className="muted provenance" data-testid="adsb-provenance">
            {snapshot.provider} · {snapshot.source} · {fmtUtc(snapshot.t_start)}–{fmtUtc(snapshot.t_end).slice(11)}Z ·{" "}
            {snapshot.radius_nm} nm of {String(snapshot.query.airport ?? "?")} · hours{" "}
            {((snapshot.query.hours as number[]) ?? []).join(", ")} · {snapshot.row_count} vectors · response{" "}
            <span className="num" title={snapshot.response_sha256}>
              {snapshot.response_sha256.slice(0, 12)}
            </span>{" "}
            · snapshot #{snapshot.id} fetched {fmtUtc(snapshot.fetched_at)}Z
          </p>
          <Button size="sm" onClick={() => fetcher.mutate(true)} disabled={fetcher.isPending}>
            <RefreshCw size={12} aria-hidden /> {fetcher.isPending ? "Refreshing…" : "Refresh context"}
          </Button>
        </>
      ) : null}
    </details>
  );
}
