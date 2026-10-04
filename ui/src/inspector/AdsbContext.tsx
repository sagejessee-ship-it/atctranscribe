import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw, Satellite } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { AdsbDay, AdsbSnapshot, AdsbStatus, AirportProfile } from "../api/types";
import { Button, ErrorBox } from "../components/ui";
import { fmtUtc } from "../lib/format";
import { AirportMap } from "./AirportMap";

const dash = (v: number | string | null | undefined, suffix = "") => (v == null ? "—" : `${v}${suffix}`);
const gb = (bytes: number) => (bytes / 2 ** 30).toFixed(1);
const waiting = (d: AdsbDay) => d.status === "queued" || d.status === "importing";

/**
 * Nearby historical traffic over a simplified airport map (runways, controlled
 * airspace). With the local adsb.lol store (ADR-024) a day is imported once, on
 * request, and its traffic then shows as soon as the panel is opened. With OpenSky
 * nothing is queried until the button is pressed. Snapshots are cached
 * server-side. Context only: it never changes a transcript or label.
 */
export function AdsbContext({
  segmentId,
  hasUtc,
  profile = null,
}: {
  segmentId: number;
  hasUtc: boolean;
  profile?: AirportProfile | null;
}) {
  const client = useQueryClient();
  const [highlight, setHighlight] = useState<string | null>(null);
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  // Status reads the cache only; it never contacts a provider. Polled while importing.
  const status = useQuery({
    queryKey: ["adsb", segmentId],
    queryFn: () => api.adsbStatus(segmentId),
    refetchInterval: (query) => ((query.state.data as AdsbStatus | undefined)?.days.some(waiting) ? 3000 : false),
  });
  const fetcher = useMutation({
    mutationFn: (refresh: boolean) => api.adsbFetch(segmentId, refresh),
    onSuccess: (snapshot) =>
      client.setQueryData(["adsb", segmentId], (old: typeof status.data) =>
        old ? { ...old, snapshot } : old,
      ),
  });
  const importer = useMutation({
    mutationFn: (days: AdsbDay[]) => api.adsbImport(days[0].day, days[days.length - 1].day),
    onSuccess: () => client.invalidateQueries({ queryKey: ["adsb", segmentId] }),
  });
  const snapshot: AdsbSnapshot | null = status.data?.snapshot ?? null;
  const configured = status.data?.configured ?? false;
  const importable = status.data?.importable ?? false;
  const days = status.data?.days ?? [];
  const importing = days.filter(waiting);
  const missing = days.filter((d) => d.status === "not_imported" || d.status === "failed");
  const ready = importable && days.length > 0 && days.every((d) => d.status === "ready");
  // A snapshot from another provider (e.g. an earlier OpenSky fetch) is replaced once the
  // day is in the local store; the old one stays in the history.
  const stale = !!snapshot && importable && snapshot.provider !== status.data?.provider;
  const open = userOpen ?? (!!snapshot || fetcher.isPending || importing.length > 0);

  // A new segment starts collapsed-by-default, without the previous segment's errors.
  const { reset: resetFetch } = fetcher;
  const { reset: resetImport } = importer;
  useEffect(() => {
    setUserOpen(null);
    resetFetch();
    resetImport();
  }, [segmentId, resetFetch, resetImport]);

  // Local and free: once the day is imported, opening the panel shows the traffic.
  const autoFetched = useRef<number | null>(null);
  const { mutate: fetchNow, isPending: fetching } = fetcher;
  useEffect(() => {
    if (open && ready && hasUtc && (!snapshot || stale) && !fetching && autoFetched.current !== segmentId) {
      autoFetched.current = segmentId;
      fetchNow(stale);
    }
  }, [open, ready, hasUtc, snapshot, stale, fetching, segmentId, fetchNow]);

  const label = snapshot
    ? `${snapshot.aircraft.length} aircraft · cached`
    : !configured
      ? "unavailable"
      : !importable
        ? "on demand"
        : importing.length
          ? "importing…"
          : missing.length
            ? "not imported"
            : "imported";

  return (
    <details className="section section--details" open={open} onToggle={(e) => setUserOpen(e.currentTarget.open)}>
      <summary className="section__title">
        ADS-B context &amp; map <span className="muted">{label}</span>
      </summary>
      {status.isError ? <ErrorBox title="ADS-B status unavailable" detail={(status.error as Error).message} /> : null}
      {status.data && !configured ? (
        <p className="note note--neutral" role="status">
          ADS-B context unavailable: {status.data.message ?? "not configured"}. Review is unaffected.
        </p>
      ) : null}
      {importable && hasUtc && (importing.length > 0 || missing.length > 0) ? (
        <ImportPanel
          importing={importing}
          missing={missing}
          busy={importer.isPending}
          onImport={() => importer.mutate(missing)}
        />
      ) : null}
      {importer.error ? <ErrorBox title="Import request failed" detail={(importer.error as Error).message} /> : null}
      {profile?.latitude != null ? (
        <AirportMap
          profile={profile}
          snapshot={snapshot}
          highlight={highlight}
          onHighlight={setHighlight}
          defaultRange={snapshot?.radius_nm ?? status.data?.radius_nm ?? 10}
        />
      ) : null}
      {configured && !snapshot && (!importable || ready) ? (
        <div className="adsb__actions">
          <Button onClick={() => fetcher.mutate(false)} disabled={fetcher.isPending || !hasUtc}>
            <Satellite size={13} aria-hidden />{" "}
            {fetcher.isPending ? (importable ? "Loading traffic…" : "Querying OpenSky…") : "Fetch ADS-B context"}
          </Button>
          <span className="muted">
            {hasUtc
              ? `−${status.data?.window_before_s}/+${status.data?.window_after_s} s, ${status.data?.radius_nm} nm around the airport (${
                  importable ? "every aircraft in the imported day" : "historical state vectors"
                })`
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
                <th scope="col">Type</th>
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
                <tr
                  key={a.icao24}
                  data-hi={a.icao24 === highlight}
                  onMouseEnter={() => setHighlight(a.icao24)}
                  onMouseLeave={() => setHighlight(null)}
                >
                  <td className="num">{a.callsign ?? <span className="muted">—</span>}</td>
                  <td title={a.registration ?? undefined}>{a.type_code ?? <span className="muted">—</span>}</td>
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
            {snapshot.provider_meta.interpolated ? " · positions interpolated from tracks" : ""}
            {typeof snapshot.provider_meta.credits_remaining === "number"
              ? ` · OpenSky credits left ${snapshot.provider_meta.credits_remaining}`
              : ""}
            {typeof snapshot.provider_meta.license === "string" ? ` · data ${snapshot.provider_meta.license}` : ""}
          </p>
          <Button size="sm" onClick={() => fetcher.mutate(true)} disabled={fetcher.isPending}>
            <RefreshCw size={12} aria-hidden /> {fetcher.isPending ? "Refreshing…" : "Refresh context"}
          </Button>
        </>
      ) : null}
    </details>
  );
}

/** The segment's day(s) are not in the local store yet: offer the import, or show its progress. */
function ImportPanel({
  importing,
  missing,
  busy,
  onImport,
}: {
  importing: AdsbDay[];
  missing: AdsbDay[];
  busy: boolean;
  onImport: () => void;
}) {
  if (importing.length) {
    return (
      <div className="adsb__import" role="status">
        {importing.map((d) => {
          const pct = d.bytes_total ? Math.min(100, (100 * d.bytes_read) / d.bytes_total) : 0;
          return (
            <div key={d.day} data-testid="adsb-import-progress">
              <span>{d.status === "queued" ? `Waiting to import ${d.day}…` : `Importing ${d.day} from adsb.lol`}</span>
              <div
                className="bar"
                role="progressbar"
                aria-valuenow={Math.round(pct)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={`Import of ${d.day}`}
              >
                <div className="bar__fill" style={{ width: `${pct}%` }} />
              </div>
              <span className="muted num">
                {d.bytes_total ? `${gb(d.bytes_read)} of ${gb(d.bytes_total)} GB` : "starting…"}
                {d.aircraft ? ` · ${d.aircraft.toLocaleString()} aircraft so far` : ""}
              </span>
            </div>
          );
        })}
        <p className="muted">The traffic shows here when the import finishes. You can keep reviewing meanwhile.</p>
      </div>
    );
  }
  if (!missing.length) return null;
  const failed = missing.filter((d) => d.status === "failed");
  const what = missing.length === 1 ? missing[0].day : `${missing[0].day} and ${missing[missing.length - 1].day}`;
  return (
    <div className="adsb__import">
      <p className="note note--neutral" role="status">
        ADS-B for {what} isn't imported yet. adsb.lol publishes every aircraft its receivers saw each day; the import
        keeps only the positions around the airport.
        {failed.length ? ` The last attempt failed: ${failed[0].error ?? "unknown error"}` : ""}
      </p>
      <div className="adsb__actions">
        <Button onClick={onImport} disabled={busy}>
          <Download size={13} aria-hidden /> {busy ? "Queuing…" : `Import ${what} from adsb.lol`}
        </Button>
        <span className="muted">about 3.5 GB to download per day, a few minutes; nothing is fetched until you click</span>
      </div>
    </div>
  );
}
