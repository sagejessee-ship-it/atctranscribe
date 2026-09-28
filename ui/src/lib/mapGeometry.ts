// Small geometry helpers for the simplified airport map (inspector/AirportMap.tsx).

export type Pt = [number, number];

/**
 * True-north equirectangular projection around (lat0, lon0) into a width×height
 * pixel frame, `rangeNm` from the centre to the nearer frame edge.
 */
export function projector(lat0: number, lon0: number, rangeNm: number, width: number, height: number) {
  const k = Math.cos((lat0 * Math.PI) / 180);
  const scale = Math.min(width, height) / 2 / rangeNm; // px per nm
  const xy = (lat: number, lon: number): Pt => [
    width / 2 + (lon - lon0) * 60 * k * scale,
    height / 2 - (lat - lat0) * 60 * scale,
  ];
  return { scale, xy };
}

export function inside([x, y]: Pt, ring: Pt[]): boolean {
  let hit = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

export function edgeDistance(p: Pt, ring: Pt[]): number {
  let best = Infinity;
  for (let i = 0; i + 1 < ring.length; i++) best = Math.min(best, segmentDistance(p, ring[i], ring[i + 1]));
  return best;
}

export function segmentDistance([x, y]: Pt, [x1, y1]: Pt, [x2, y2]: Pt): number {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const t = dx || dy ? Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy))) : 0;
  return Math.hypot(x - (x1 + t * dx), y - (y1 + t * dy));
}

export interface LabelOptions {
  margin?: number;
  minRoom?: number;
  /** Rings whose interior is not this polygon's visible area (e.g. a lower Class B shelf drawn over it). */
  exclude?: Pt[][];
  /** Extra keep-out test (runways, labels already placed). */
  blocked?: (p: Pt) => boolean;
}

/**
 * A point inside the polygon (and outside `exclude`), within the visible
 * frame, as far from the edges as a coarse grid search finds; null when no
 * such point leaves room for a label (off-screen, a sliver, or fully covered).
 */
export function interiorLabelPoint(ring: Pt[], width: number, height: number, options: LabelOptions = {}): Pt | null {
  const { margin = 18, minRoom = 11, exclude = [], blocked } = options;
  const xs = ring.map((p) => p[0]);
  const ys = ring.map((p) => p[1]);
  const x0 = Math.max(Math.min(...xs), margin);
  const x1 = Math.min(Math.max(...xs), width - margin);
  const y0 = Math.max(Math.min(...ys), margin);
  const y1 = Math.min(Math.max(...ys), height - margin);
  if (x0 >= x1 || y0 >= y1) return null;
  let best: Pt | null = null;
  let bestD = minRoom;
  const n = 20;
  for (let i = 0; i <= n; i++) {
    for (let j = 0; j <= n; j++) {
      const p: Pt = [x0 + ((x1 - x0) * i) / n, y0 + ((y1 - y0) * j) / n];
      if (!inside(p, ring) || exclude.some((r) => inside(p, r)) || blocked?.(p)) continue;
      const d = Math.min(edgeDistance(p, ring), ...exclude.map((r) => edgeDistance(p, r)));
      if (d > bestD) {
        bestD = d;
        best = p;
      }
    }
  }
  return best;
}

/** Sectional-chart style altitude: hundreds of feet, SFC for the surface. */
export function hundreds(ft: number | null, ref: string | null): string {
  if (ft == null) return "—";
  if (ft === 0 || ref === "SFC") return "SFC";
  return String(Math.round(ft / 100)).padStart(2, "0");
}

/** ↑/↓ for a climb/descent of at least 300 fpm, else nothing. */
export function trend(fpm: number | null): string {
  if (fpm == null || Math.abs(fpm) < 300) return "";
  return fpm > 0 ? "↑" : "↓";
}
