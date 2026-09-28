import { useCallback, useSyncExternalStore } from "react";

// Local annotator identity (a label, not authentication). Per-browser convenience.
const KEY = "aerochorus.annotator";
const listeners = new Set<() => void>();

function read(): string {
  try {
    return localStorage.getItem(KEY) || "local";
  } catch {
    return "local";
  }
}

export function useAnnotator(): [string, (name: string) => void] {
  const value = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read,
    () => "local",
  );
  const set = useCallback((name: string) => {
    try {
      localStorage.setItem(KEY, name);
    } catch {
      /* storage unavailable: keep the default */
    }
    listeners.forEach((l) => l());
  }, []);
  return [value, set];
}

export function readPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(`aerochorus.${key}`);
    return raw == null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writePref(key: string, value: unknown): void {
  try {
    localStorage.setItem(`aerochorus.${key}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}
