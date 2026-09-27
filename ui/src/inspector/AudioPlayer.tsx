import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Pause, Play, RotateCcw, Volume2 } from "lucide-react";

import { audioProblem, audioUrl } from "../api/client";
import { Button, ErrorBox, IconButton } from "../components/ui";
import { readPref, writePref } from "../lib/annotator";
import { fmtClock } from "../lib/format";

export interface AudioHandle {
  toggle(): void;
  replay(): void;
  pause(): void;
}

const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];

/**
 * Plays the real source segment through the review edge server (read-only,
 * sha256-verified). Errors from the edge (not mounted, missing, changed) are
 * shown verbatim with a retry.
 */
export const AudioPlayer = forwardRef<AudioHandle, { segmentId: number; durationMs: number | null }>(
  function AudioPlayer({ segmentId, durationMs }, ref) {
    const audio = useRef<HTMLAudioElement>(null);
    const [playing, setPlaying] = useState(false);
    const [current, setCurrent] = useState(0);
    const [duration, setDuration] = useState((durationMs ?? 0) / 1000);
    const [rate, setRate] = useState<number>(() => readPref("rate", 1));
    const [volume, setVolume] = useState<number>(() => readPref("volume", 1));
    const [autoplay, setAutoplay] = useState<boolean>(() => readPref("autoplay", false));
    const [error, setError] = useState<string | null>(null);
    const [ready, setReady] = useState(false);

    useEffect(() => {
      setPlaying(false);
      setCurrent(0);
      setError(null);
      setReady(false);
      setDuration((durationMs ?? 0) / 1000);
    }, [segmentId, durationMs]);

    useEffect(() => {
      if (audio.current) audio.current.playbackRate = rate;
      writePref("rate", rate);
    }, [rate, segmentId]);
    useEffect(() => {
      if (audio.current) audio.current.volume = volume;
      writePref("volume", volume);
    }, [volume, segmentId]);

    const play = useCallback(() => {
      const el = audio.current;
      if (!el) return;
      el.play().catch((e: Error) => {
        if (e.name !== "AbortError") setError(`playback blocked: ${e.message}`);
      });
    }, []);
    const toggle = useCallback(() => {
      const el = audio.current;
      if (!el) return;
      if (el.paused) play();
      else el.pause();
    }, [play]);
    const replay = useCallback(() => {
      const el = audio.current;
      if (!el) return;
      el.currentTime = 0;
      play();
    }, [play]);
    useImperativeHandle(ref, () => ({ toggle, replay, pause: () => audio.current?.pause() }), [toggle, replay]);

    return (
      <div className="player" aria-label="Audio player">
        <audio
          ref={audio}
          src={audioUrl(segmentId)}
          preload="auto"
          data-testid="segment-audio"
          onLoadedMetadata={(e) => {
            const d = e.currentTarget.duration;
            if (Number.isFinite(d) && d > 0) setDuration(d);
            e.currentTarget.playbackRate = rate;
            e.currentTarget.volume = volume;
            setReady(true);
            if (autoplay) play();
          }}
          onTimeUpdate={(e) => setCurrent(e.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={() => {
            setPlaying(false);
            audioProblem(segmentId).then(setError);
          }}
        />
        {error ? (
          <ErrorBox
            title="Source audio unavailable"
            detail={error}
            action={
              <Button
                size="sm"
                onClick={() => {
                  setError(null);
                  audio.current?.load();
                }}
              >
                Retry
              </Button>
            }
          />
        ) : null}
        <div className="player__row">
          <IconButton
            label={playing ? "Pause (Space)" : "Play (Space)"}
            variant="primary"
            onClick={toggle}
            disabled={!!error}
          >
            {playing ? <Pause size={15} /> : <Play size={15} />}
          </IconButton>
          <IconButton label="Replay from start (R)" onClick={replay} disabled={!!error}>
            <RotateCcw size={14} />
          </IconButton>
          <input
            className="player__seek"
            type="range"
            aria-label="Seek"
            min={0}
            max={duration || 0}
            step={0.01}
            value={Math.min(current, duration || 0)}
            disabled={!ready}
            onChange={(e) => {
              if (audio.current) audio.current.currentTime = Number(e.target.value);
            }}
          />
          <span className="num player__time" aria-live="off">
            {fmtClock(current)} / {fmtClock(duration)}
          </span>
        </div>
        <div className="player__row player__row--secondary">
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
          <label className="player__opt">
            <input
              type="checkbox"
              checked={autoplay}
              onChange={(e) => {
                setAutoplay(e.target.checked);
                writePref("autoplay", e.target.checked);
              }}
            />
            <span>Autoplay on open</span>
          </label>
        </div>
      </div>
    );
  },
);
