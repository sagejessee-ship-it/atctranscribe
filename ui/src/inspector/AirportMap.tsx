import clsx from "clsx";
import { useMemo, useState } from "react";

import type { AdsbAircraft, AdsbSnapshot, AirportProfile, Airspace } from "../api/types";
import { hundreds, interiorLabelPoint, projector, segmentDistance, trend, type Pt } from "../lib/mapGeometry";

/*
 * A deliberately simple airport map: true-north equirectangular projection
 * around the airport reference point, drawn in pixels. Runways from surveyed
 * NASR runway ends, controlled airspace from FAA ADDS (simplified), traffic
 * from a cached ADS-B snapshot. Context for the annotator; not for navigation.
 */

const W = 560;
const H = 380;
const RANGES = [5, 10, 20, 30] as const;
const RINGS: Record<number, number[]> = { 5: [2, 4], 10: [5, 10], 20: [5, 10, 20], 30: [10, 20, 30] };
const CLASS_ORDER = ["B", "C", "D"];

type Trail = NonNullable<AdsbSnapshot["trails"]>[string];

const project = (lat0: number, lon0: number, rangeNm: number) => projector(lat0, lon0, rangeNm, W, H);

function scaleBarNm(range: number): number {
  return [1, 2, 5, 10].filter((n) => n <= range / 3).pop() ?? 1;
}

function AirspaceShape({ airspace, rings, label }: { airspace: Airspace; rings: Pt[][]; label: Pt | null }) {
  const cls = (airspace.airspace_class || "").toUpperCase();
  const title = `${airspace.name}: ${airspace.lower_ft === 0 ? "surface" : `${airspace.lower_ft ?? "?"} ft`} to ${
    airspace.upper_ft ?? "?"
  } ft ${airspace.upper_ref ?? ""}`.trim();
  return (
    <g className={clsx("amap__airspace", `amap__airspace--${cls}`)} data-class={cls}>
      <title>{title}</title>
      {rings.map((ring, i) => (
        <path key={i} d={`M${ring.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join("L")}Z`} />
      ))}
      {label ? (
        <g className="amap__alt" transform={`translate(${label[0].toFixed(1)},${label[1].toFixed(1)})`}>
          <text y={-2} textAnchor="middle">
            {hundreds(airspace.upper_ft, airspace.upper_ref)}
          </text>
          <line x1={-9} x2={9} y1={1} y2={1} />
          <text y={11} textAnchor="middle">
            {hundreds(airspace.lower_ft, airspace.lower_ref)}
          </text>
        </g>
      ) : null}
    </g>
  );
}

function AircraftMark({
  aircraft,
  trail,
  xy,
  highlighted,
  onHighlight,
}: {
  aircraft: AdsbAircraft;
  trail: Trail | undefined;
  xy: (lat: number, lon: number) => Pt;
  highlighted: boolean;
  onHighlight?: (icao: string | null) => void;
}) {
  const [x, y] = xy(aircraft.lat!, aircraft.lon!);
  // Trails meet at the displayed position: solid before it, dashed after.
  const past = [...(trail ?? []).filter((p) => p[0] < aircraft.offset_s).map((p) => xy(p[1], p[2])), [x, y] as Pt];
  const future = [[x, y] as Pt, ...(trail ?? []).filter((p) => p[0] > aircraft.offset_s).map((p) => xy(p[1], p[2]))];
  const line = (pts: Pt[]) => pts.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
  const ground = aircraft.on_ground === true;
  const name = aircraft.callsign ?? aircraft.icao24;
  const alt = aircraft.baro_altitude_ft;
  return (
    <g
      className={clsx("amap__ac", ground && "amap__ac--ground", highlighted && "amap__ac--hi")}
      data-testid={`map-aircraft-${aircraft.icao24}`}
      onMouseEnter={() => onHighlight?.(aircraft.icao24)}
      onMouseLeave={() => onHighlight?.(null)}
    >
      <title>
        {`${name} (${aircraft.icao24}) · ${ground ? "on ground" : `${alt ?? "?"} ft`} · ${aircraft.velocity_kt ?? "?"} kt · hdg ${
          aircraft.heading_deg ?? "?"
        }° · ${aircraft.offset_s > 0 ? "+" : ""}${aircraft.offset_s}s from segment start`}
      </title>
      {past.length > 1 ? <polyline className="amap__trail" points={line(past)} /> : null}
      {future.length > 1 ? <polyline className="amap__trail amap__trail--future" points={line(future)} /> : null}
      {aircraft.heading_deg == null || ground ? (
        <circle className="amap__ac-body" cx={x} cy={y} r={ground ? 2.6 : 3.5} />
      ) : (
        <path
          className="amap__ac-body"
          d="M0,-7 L4.5,5 L0,2.5 L-4.5,5 Z"
          transform={`translate(${x.toFixed(1)},${y.toFixed(1)}) rotate(${aircraft.heading_deg})`}
        />
      )}
      {/* Ramp traffic is dense: name ground aircraft only on hover/highlight. */}
      {!ground || highlighted ? (
        <text className="amap__tag" x={x + 8} y={y - 3}>
          {name}
        </text>
      ) : null}
      {!ground ? (
        <text className="amap__tag amap__tag--sub" x={x + 8} y={y + 8}>
          {alt == null ? "—" : String(Math.round(alt / 100)).padStart(3, "0")}
          {trend(aircraft.vertical_rate_fpm)}
          {aircraft.velocity_kt != null ? ` ${Math.round(aircraft.velocity_kt / 10)}` : ""}
        </text>
      ) : null}
    </g>
  );
}

export function AirportMap({
  profile,
  snapshot,
  highlight = null,
  onHighlight,
  defaultRange = 10,
}: {
  profile: AirportProfile;
  snapshot?: AdsbSnapshot | null;
  highlight?: string | null;
  onHighlight?: (icao: string | null) => void;
  defaultRange?: number;
}) {
  const [range, setRange] = useState<number>(
    RANGES.find((r) => r >= defaultRange) ?? RANGES[RANGES.length - 1],
  );
  const lat0 = profile.latitude;
  const lon0 = profile.longitude;
  const { scale, xy } = useMemo(() => project(lat0 ?? 0, lon0 ?? 0, range), [lat0, lon0, range]);

  const runways = useMemo(() => {
    const pairs = new Map<string, { ident: string; p: Pt; d: Pt | null }[]>();
    for (const r of profile.runways) {
      if (r.latitude == null || r.longitude == null) continue;
      const d = r.displaced_latitude != null && r.displaced_longitude != null ? xy(r.displaced_latitude, r.displaced_longitude) : null;
      pairs.set(r.pair, [...(pairs.get(r.pair) ?? []), { ident: r.end_ident, p: xy(r.latitude, r.longitude), d }]);
    }
    return [...pairs.entries()].filter(([, ends]) => ends.length === 2);
  }, [profile.runways, xy]);

  const airspaces = useMemo(
    () =>
      [...(profile.airspaces ?? [])].sort(
        (a, b) => CLASS_ORDER.indexOf(a.airspace_class) - CLASS_ORDER.indexOf(b.airspace_class) || (b.lower_ft ?? 0) - (a.lower_ft ?? 0),
      ),
    [profile.airspaces],
  );

  // Runway-end idents just beyond each end, pushed further out when two ends
  // are close (e.g. 28 and 33R); omitted when the airport is too small to read.
  const endLabels = useMemo(() => {
    const out = new Map<string, Pt>();
    if (scale < 30) return out;
    const placed: Pt[] = [];
    for (const [, [a, b]] of runways) {
      const len = Math.hypot(b.p[0] - a.p[0], b.p[1] - a.p[1]) || 1;
      const u: Pt = [(b.p[0] - a.p[0]) / len, (b.p[1] - a.p[1]) / len];
      for (const [end, sign] of [
        [a, -1],
        [b, 1],
      ] as const) {
        for (const off of [10, 19, 28, 37]) {
          const at: Pt = [end.p[0] + sign * u[0] * off, end.p[1] + sign * u[1] * off];
          if (!placed.some((q) => Math.abs(q[0] - at[0]) < 20 && Math.abs(q[1] - at[1]) < 11)) {
            placed.push(at);
            out.set(end.ident, at);
            break;
          }
        }
      }
    }
    return out;
  }, [runways, scale]);

  // FAA stacks Class B shelves: each shelf polygon covers everything inside its
  // outer edge, lower floors on top. A label goes where its own floor is the
  // visible one, clear of runways, the map furniture and other labels.
  const shapes = useMemo(() => {
    const projected = airspaces.map((a) => ({ a, rings: a.rings.map((ring) => ring.map(([lon, lat]) => xy(lat, lon))) }));
    const strips = runways.map(([, [a, b]]) => [a.p, b.p] as const);
    const placed: Pt[] = [];
    const blocked = (p: Pt) =>
      Math.hypot(p[0] - W / 2, p[1] - H / 2) < 16 ||
      strips.some(([a, b]) => segmentDistance(p, a, b) < 32) ||
      (p[0] > W - 44 && p[1] < 48) || // north arrow
      (p[0] < 90 && p[1] > H - 34) || // scale bar
      placed.some((q) => Math.abs(q[0] - p[0]) < 34 && Math.abs(q[1] - p[1]) < 30);
    return projected.map(({ a, rings }) => {
      const exclude = projected
        .filter((o) => o.a !== a && o.a.airspace_class === a.airspace_class && (o.a.lower_ft ?? 0) < (a.lower_ft ?? 0))
        .flatMap((o) => o.rings);
      const label = rings.length ? interiorLabelPoint(rings[0], W, H, { exclude, blocked }) : null;
      if (label) placed.push(label);
      return { a, rings, label };
    });
  }, [airspaces, runways, xy]);

  if (lat0 == null || lon0 == null) return null;

  const aircraft = (snapshot?.aircraft ?? []).filter((a) => a.lat != null && a.lon != null);
  const visible = aircraft.filter((a) => {
    const [x, y] = xy(a.lat!, a.lon!);
    return x >= 0 && x <= W && y >= 0 && y <= H;
  });
  // Draw the highlighted aircraft last so it is on top.
  visible.sort((a, b) => Number(a.icao24 === highlight) - Number(b.icao24 === highlight));
  const hidden = aircraft.length - visible.length;
  const bar = scaleBarNm(range);
  const classes = [...new Set(airspaces.map((a) => a.airspace_class))].sort(
    (a, b) => CLASS_ORDER.indexOf(a) - CLASS_ORDER.indexOf(b),
  );
  const nasr = profile.provenance as Record<string, string | undefined>;
  const airspaceSource = (profile.airspace_provenance as Record<string, string | undefined> | undefined)?.source;

  return (
    <figure className="amap" data-testid="airport-map">
      <div className="amap__bar">
        <span className="muted">
          {profile.icao} · {runways.length} runways · {airspaces.length} airspace areas
          {snapshot ? ` · ${visible.length} aircraft` : ""}
          {hidden > 0 ? ` (${hidden} outside view)` : ""}
        </span>
        <div className="segmented" role="group" aria-label="Map range">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              className="segmented__item"
              data-state={r === range ? "on" : "off"}
              aria-pressed={r === range}
              onClick={() => setRange(r)}
            >
              {r} nm
            </button>
          ))}
        </div>
      </div>
      <svg
        className="amap__svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Simplified map of ${profile.icao}: ${runways.length} runways, ${airspaces.length} controlled airspace areas${
          snapshot ? `, ${visible.length} aircraft` : ""
        }, ${range} nautical mile range`}
      >
        <rect className="amap__bg" x={0} y={0} width={W} height={H} />
        {shapes.map(({ a, rings, label }, i) => (
          <AirspaceShape key={`${a.source_id ?? a.name}-${i}`} airspace={a} rings={rings} label={label} />
        ))}
        {RINGS[range].map((r) => (
          <g key={r} className="amap__ring">
            <circle cx={W / 2} cy={H / 2} r={r * scale} />
            <text x={W / 2 + r * scale * Math.SQRT1_2 + 3} y={H / 2 - r * scale * Math.SQRT1_2 - 3}>
              {r} nm
            </text>
          </g>
        ))}
        {runways.map(([pair, [a, b]]) => {
          return (
            <g key={pair} className="amap__rwy" data-testid={`map-runway-${pair}`}>
              <title>{`Runway ${pair}`}</title>
              <line x1={a.p[0]} y1={a.p[1]} x2={b.p[0]} y2={b.p[1]} />
              {[a, b].map((end, k) =>
                end.d ? (
                  <circle key={`d${k}`} className="amap__thr" cx={end.d[0]} cy={end.d[1]} r={1.6}>
                    <title>{`Runway ${end.ident} displaced threshold`}</title>
                  </circle>
                ) : null,
              )}
              {[a, b].map((end) => {
                const at = endLabels.get(end.ident);
                return at ? (
                  <text key={end.ident} x={at[0]} y={at[1] + 3} textAnchor="middle">
                    {end.ident}
                  </text>
                ) : null;
              })}
            </g>
          );
        })}
        {!runways.length ? (
          <g className="amap__rwy">
            <circle cx={W / 2} cy={H / 2} r={4} />
            <text x={W / 2 + 8} y={H / 2 + 4}>
              {profile.icao}
            </text>
          </g>
        ) : null}
        {visible.map((a) => (
          <AircraftMark
            key={a.icao24}
            aircraft={a}
            trail={snapshot?.trails?.[a.icao24]}
            xy={xy}
            highlighted={a.icao24 === highlight}
            onHighlight={onHighlight}
          />
        ))}
        <g className="amap__north" transform={`translate(${W - 20},22)`}>
          <path d="M0,-11 L5,4 L0,1 L-5,4 Z" />
          <text y={16} textAnchor="middle">
            N
          </text>
        </g>
        <g className="amap__scale" transform={`translate(12,${H - 14})`}>
          <path d={`M0,-4 V0 H${bar * scale} V-4`} />
          <text x={bar * scale + 5} y={0}>
            {bar} nm
          </text>
        </g>
      </svg>
      <figcaption className="amap__legend">
        {classes.map((c) => (
          <span key={c} className={clsx("amap__key", `amap__key--${c}`)}>
            Class {c}
          </span>
        ))}
        {snapshot ? (
          <>
            <span className="amap__key amap__key--air">airborne (alt ×100 ft, trend, kt ×10)</span>
            <span className="amap__key amap__key--ground">on ground</span>
            <span className="amap__key amap__key--trail">track (dashed after segment start)</span>
          </>
        ) : null}
        <span className="muted">
          Runways: FAA NASR{nasr.cycle ? ` ${nasr.cycle}` : ""} · Airspace: {airspaceSource ?? "none loaded"}, simplified · true north ·
          not for navigation
        </span>
      </figcaption>
    </figure>
  );
}
