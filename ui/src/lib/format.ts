// Display formatting only. Monospace/tabular numerics are applied by the `num` class.

export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").slice(0, 19);
}

/** Local wall-clock time as the server computed it (already zone-shifted ISO string). */
export function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 19);
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds - minutes * 60).toFixed(1).padStart(4, "0")}`;
}

export function fmtClock(seconds: number): string {
  if (!Number.isFinite(seconds)) return "0:00.0";
  const m = Math.floor(seconds / 60);
  return `${m}:${(seconds - m * 60).toFixed(1).padStart(4, "0")}`;
}

export function fmtFreq(hz: number | null | undefined): string {
  if (hz == null) return "—";
  return (hz / 1_000_000).toFixed(3);
}

export function fmtSim(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(3);
}

export function fmtOffset(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const sign = seconds > 0 ? "+" : seconds < 0 ? "−" : "±";
  return `${sign}${Math.abs(seconds).toFixed(0)}s`;
}

export function fmtCount(n: number): string {
  return n.toLocaleString("en-US");
}

export type DiffOp = { kind: "eq" | "del" | "ins"; text: string };

/**
 * Word-level diff for display (LCS over whitespace tokens, case-insensitive
 * comparison). Purely presentational: agreement is computed by the backend.
 */
export function diffWords(before: string, after: string): DiffOp[] {
  const a = before.split(/\s+/).filter(Boolean);
  const b = after.split(/\s+/).filter(Boolean);
  const key = (t: string) => t.toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = key(a[i]) === key(b[j]) ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (key(a[i]) === key(b[j])) {
      ops.push({ kind: "eq", text: b[j] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      ops.push({ kind: "del", text: a[i++] });
    } else {
      ops.push({ kind: "ins", text: b[j++] });
    }
  }
  while (i < n) ops.push({ kind: "del", text: a[i++] });
  while (j < m) ops.push({ kind: "ins", text: b[j++] });
  return ops;
}
