import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";
import HoverPlugin from "wavesurfer.js/dist/plugins/hover.esm.js";
import RegionsPlugin, { type Region } from "wavesurfer.js/dist/plugins/regions.esm.js";
import TimelinePlugin from "wavesurfer.js/dist/plugins/timeline.esm.js";
import { Pause, Play, Repeat, RotateCcw, Scissors, Volume2, X, ZoomIn } from "lucide-react";

import { audioProblem, audioUrl } from "../api/client";
import type { AnnotationThread, TrainingLabel } from "../api/types";
import { Button, ErrorBox, IconButton } from "../components/ui";
import { readPref, writePref } from "../lib/annotator";
import { fmtClock } from "../lib/format";

export interface AudioHandle {
  toggle(): void;
  replay(): void;
  pause(): void;
  toggleLoop(): void;
}

/** The region being edited: a saved span thread, or an unsaved draft (threadId null). */
export interface SpanSelection {
  threadId: number | null;
  start_ms: number;
  end_ms: number;
}

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];
const DRAFT = "draft";
const spanId = (threadId: number) => `span-${threadId}`;

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888888";
}

function alpha(color: string, a: number): string {
  const hex = color.replace("#", "");
  if (!/^[0-9a-f]{6}$/i.test(hex)) return color;
  const n = parseInt(hex, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

const LABEL_TOKEN: Record<TrainingLabel, string> = {
  gold: "--ok",
  silver: "--derived",
  candidate: "--derived",
  rejected: "--danger",
  none: "--neutral",
};

function regionColor(label: TrainingLabel | null, active: boolean): string {
  return alpha(token(label ? LABEL_TOKEN[label] : "--info"), active ? 0.38 : 0.2);
}

/**
 * Waveform playback and span selection (WaveSurfer.js 7). Source audio is only
 * read (through the edge's /audio endpoint); regions are offsets, never cuts.
 */
export const WaveformEditor = forwardRef<
  AudioHandle,
  {
    segmentId: number;
    durationMs: number | null;
    spans: AnnotationThread[];
    selection: SpanSelection | null;
    onSelection: (selection: SpanSelection | null) => void;
  }
>(function WaveformEditor({ segmentId, durationMs, spans, selection, onSelection }, ref) {
  const container = useRef<HTMLDivElement>(null);
  const ws = useRef<WaveSurfer | null>(null);
  const regions = useRef<RegionsPlugin | null>(null);
  const loopRef = useRef(false);
  const activeRef = useRef<string | null>(null);
  const onSelectionRef = useRef(onSelection);
  onSelectionRef.current = onSelection;

  const [ready, setReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState((durationMs ?? 0) / 1000);
  const [error, setError] = useState<string | null>(null);
  // Looping is on by default: the selected span if any, otherwise the whole file.
  const [loop, setLoop] = useState<boolean>(() => readPref("loop", true));
  const [zoom, setZoom] = useState<number>(() => readPref("zoom", 0));
  const [rate, setRate] = useState<number>(() => readPref("rate", 1));
  const [volume, setVolume] = useState<number>(() => readPref("volume", 1));
  const [attempt, setAttempt] = useState(0);

  const emit = useCallback((region: Region) => {
    const threadId = region.id.startsWith("span-") ? Number(region.id.slice(5)) : null;
    activeRef.current = region.id;
    onSelectionRef.current({
      threadId,
      start_ms: Math.round(region.start * 1000),
      end_ms: Math.round(region.end * 1000),
    });
  }, []);

  // One WaveSurfer per segment.
  useEffect(() => {
    if (!container.current) return;
    setReady(false);
    setPlaying(false);
    setCurrent(0);
    setError(null);
    const regionsPlugin = RegionsPlugin.create();
    const instance = WaveSurfer.create({
      container: container.current,
      url: audioUrl(segmentId),
      height: 72,
      waveColor: token("--border-strong"),
      progressColor: token("--accent"),
      cursorColor: token("--text"),
      cursorWidth: 1,
      normalize: true,
      dragToSeek: false,
      minPxPerSec: zoom,
      plugins: [
        regionsPlugin,
        TimelinePlugin.create({ height: 14, style: { fontSize: "10px", color: token("--text-muted") } }),
        HoverPlugin.create({
          lineColor: token("--text-muted"),
          labelColor: token("--surface-1"),
          labelBackground: token("--text"),
          labelSize: "10px",
        }),
      ],
    });
    ws.current = instance;
    regions.current = regionsPlugin;
    regionsPlugin.enableDragSelection({ color: regionColor(null, true) }, 3);

    instance.on("ready", (d) => {
      setDuration(d);
      setReady(true);
      instance.setPlaybackRate(readPref("rate", 1), true);
      instance.setVolume(readPref("volume", 1));
    });
    instance.on("timeupdate", (t) => setCurrent(t));
    instance.on("play", () => setPlaying(true));
    instance.on("pause", () => setPlaying(false));
    instance.on("finish", () => {
      if (loopRef.current && !activeRef.current) {
        instance.setTime(0);
        instance.play().catch(() => undefined);
      } else setPlaying(false);
    });
    instance.on("error", () => {
      setPlaying(false);
      audioProblem(segmentId).then(setError);
    });
    regionsPlugin.on("region-created", (region) => {
      if (region.id.startsWith("span-")) return;
      for (const other of regionsPlugin.getRegions()) {
        if (other !== region && other.id === DRAFT) other.remove();
      }
      region.setOptions({ id: DRAFT });
      emit(region);
    });
    regionsPlugin.on("region-updated", (region) => emit(region));
    regionsPlugin.on("region-clicked", (region, event) => {
      event.stopPropagation();
      emit(region);
      region.play(true);
    });
    regionsPlugin.on("region-out", (region) => {
      if (loopRef.current && region.id === activeRef.current) region.play();
    });
    return () => {
      instance.destroy();
      ws.current = null;
      regions.current = null;
    };
    // zoom is applied live below; recreating on zoom would reload audio
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segmentId, attempt, emit]);

  // Saved spans -> regions (after every refetch).
  useEffect(() => {
    const plugin = regions.current;
    if (!plugin || !ready) return;
    for (const region of plugin.getRegions()) if (region.id.startsWith("span-")) region.remove();
    for (const span of spans) {
      const v = span.current;
      if (!v || v.start_ms == null || v.end_ms == null) continue;
      const id = spanId(span.thread_id);
      plugin.addRegion({
        id,
        start: v.start_ms / 1000,
        end: v.end_ms / 1000,
        color: regionColor(v.training_label, activeRef.current === id),
        drag: true,
        resize: true,
        minLength: 0.05,
      });
    }
  }, [spans, ready]);

  // Selection from the form (typed bounds, list clicks, clear) -> regions.
  useEffect(() => {
    const plugin = regions.current;
    if (!plugin || !ready) return;
    const id = selection ? (selection.threadId == null ? DRAFT : spanId(selection.threadId)) : null;
    activeRef.current = id;
    for (const region of plugin.getRegions()) {
      const saved = spans.find((s) => spanId(s.thread_id) === region.id);
      if (region.id === DRAFT && id !== DRAFT) {
        region.remove();
        continue;
      }
      const label = saved?.current?.training_label ?? null;
      const isActive = region.id === id;
      const update: Parameters<Region["setOptions"]>[0] = { color: regionColor(label, isActive) };
      if (isActive && selection) {
        const start = selection.start_ms / 1000;
        const end = selection.end_ms / 1000;
        if (Math.abs(region.start - start) > 0.001) update.start = start;
        if (Math.abs(region.end - end) > 0.001) update.end = end;
      }
      region.setOptions(update);
    }
    if (id === DRAFT && selection && !plugin.getRegions().some((r) => r.id === DRAFT)) {
      plugin.addRegion({
        id: DRAFT,
        start: selection.start_ms / 1000,
        end: selection.end_ms / 1000,
        color: regionColor(null, true),
        minLength: 0.05,
      });
    }
  }, [selection, spans, ready]);

  useEffect(() => {
    loopRef.current = loop;
    writePref("loop", loop);
  }, [loop]);
  useEffect(() => {
    if (ready) ws.current?.zoom(zoom);
    writePref("zoom", zoom);
  }, [zoom, ready]);
  useEffect(() => {
    ws.current?.setPlaybackRate(rate, true);
    writePref("rate", rate);
  }, [rate]);
  useEffect(() => {
    ws.current?.setVolume(volume);
    writePref("volume", volume);
  }, [volume]);

  const activeRegion = () => regions.current?.getRegions().find((r) => r.id === activeRef.current);
  const toggle = useCallback(() => {
    const instance = ws.current;
    if (!instance) return;
    if (instance.isPlaying()) return instance.pause();
    const region = activeRegion();
    const t = instance.getCurrentTime();
    // Inside a selected region (or looping), play the region; else continue from the cursor.
    if (region && (loopRef.current || t < region.start || t >= region.end - 0.01)) region.play(!loopRef.current);
    else instance.play().catch(() => undefined);
  }, []);
  const replay = useCallback(() => {
    const instance = ws.current;
    if (!instance) return;
    const region = activeRegion();
    if (region) region.play(!loopRef.current);
    else {
      instance.setTime(0);
      instance.play().catch(() => undefined);
    }
  }, []);
  useImperativeHandle(
    ref,
    () => ({ toggle, replay, pause: () => ws.current?.pause(), toggleLoop: () => setLoop((v) => !v) }),
    [toggle, replay],
  );

  return (
    <div className="player" aria-label="Audio and waveform">
      {error ? (
        <ErrorBox
          title="Source audio unavailable"
          detail={error}
          action={
            <Button size="sm" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </Button>
          }
        />
      ) : null}
      <div
        className="waveform"
        ref={container}
        data-testid="waveform"
        aria-label="Waveform: drag to select a span; drag region edges to adjust"
        role="application"
      />
      {!ready && !error ? <div className="waveform__loading muted">Loading waveform…</div> : null}
      <div className="player__row">
        <IconButton label={playing ? "Pause (Space)" : "Play (Space)"} variant="primary" onClick={toggle} disabled={!ready}>
          {playing ? <Pause size={15} /> : <Play size={15} />}
        </IconButton>
        <IconButton label="Replay (R): selection or start" onClick={replay} disabled={!ready}>
          <RotateCcw size={14} />
        </IconButton>
        <IconButton
          label={loop ? "Turn looping off (L): selection or whole file" : "Turn looping on (L)"}
          aria-pressed={loop}
          onClick={() => setLoop((v) => !v)}
          disabled={!ready}
          className={loop ? "btn--on" : undefined}
        >
          <Repeat size={14} />
        </IconButton>
        <span className="num player__time" data-testid="player-time">
          {fmtClock(current)} / {fmtClock(duration)}
        </span>
        {selection ? (
          <span className="num player__sel" data-testid="selection-readout">
            <Scissors size={12} aria-hidden /> {(selection.start_ms / 1000).toFixed(2)}–{(selection.end_ms / 1000).toFixed(2)}s
            <IconButton label="Clear selection" onClick={() => onSelection(null)}>
              <X size={12} />
            </IconButton>
          </span>
        ) : (
          <span className="muted player__hint">drag on the waveform to select a span</span>
        )}
      </div>
      <div className="player__row player__row--secondary">
        <label className="player__opt">
          <ZoomIn size={13} aria-hidden />
          <input
            type="range"
            aria-label="Zoom"
            min={0}
            max={600}
            step={20}
            value={zoom}
            onChange={(e) => setZoom(Number(e.target.value))}
          />
        </label>
        <label className="player__opt">
          <span>Rate</span>
          <select className="select" value={rate} onChange={(e) => setRate(Number(e.target.value))} aria-label="Playback rate">
            {RATES.map((r) => (
              <option key={r} value={r}>
                {r}×
              </option>
            ))}
          </select>
        </label>
        <label className="player__opt">
          <Volume2 size={13} aria-hidden />
          <input
            type="range"
            aria-label="Volume"
            min={0}
            max={1}
            step={0.05}
            value={volume}
            onChange={(e) => setVolume(Number(e.target.value))}
          />
        </label>
      </div>
    </div>
  );
});
