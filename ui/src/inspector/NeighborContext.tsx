import { useEffect, useRef, useState } from "react";
import { ExternalLink, Play, Square } from "lucide-react";

import { audioProblem, audioUrl } from "../api/client";
import type { NeighborView } from "../api/types";
import { IconButton, Section } from "../components/ui";
import { fmtDuration, fmtOffset } from "../lib/format";

const RELATION = { previous: "prev", next: "next", nearby: "near" } as const;

/** Conversational context: listen to the neighbours; never changes this segment's label. */
export function NeighborContext({
  neighbors,
  onOpen,
  onBeforePlay,
}: {
  neighbors: NeighborView[];
  onOpen: (id: number) => void;
  onBeforePlay: () => void;
}) {
  const player = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<number | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(
    () => () => {
      player.current?.pause();
    },
    [neighbors],
  );

  const toggle = (id: number) => {
    if (playing === id) {
      player.current?.pause();
      setPlaying(null);
      return;
    }
    onBeforePlay();
    player.current?.pause();
    const audio = new Audio(audioUrl(id));
    audio.onended = () => setPlaying(null);
    audio.onerror = () => {
      setPlaying(null);
      audioProblem(id).then((p) => setProblem(`segment ${id}: ${p}`));
    };
    player.current = audio;
    setProblem(null);
    setPlaying(id);
    audio.play().catch(() => setPlaying(null));
  };

  return (
    <Section title="Neighbor context" id="neighbors" aside={<span className="muted">same source · context only</span>}>
      {neighbors.length === 0 ? <p className="muted">No neighbouring segments.</p> : null}
      {problem ? <p className="note note--danger">{problem}</p> : null}
      <table className="mini-table neighbors">
        <caption className="sr-only">Neighbouring segments</caption>
        <tbody>
          {neighbors.map((n) => (
            <tr key={n.segment_id} className={n.relation !== "nearby" ? "neighbors__same" : undefined}>
              <td className="muted">{RELATION[n.relation]}</td>
              <td className="num">{fmtOffset(n.offset_seconds)}</td>
              <td>{n.channel ?? "—"}</td>
              <td className="num muted">{fmtDuration(n.duration_ms)}</td>
              <td className="mini-table__text">{n.preview ?? <span className="muted">(no transcript)</span>}</td>
              <td className="neighbors__actions">
                <IconButton
                  label={playing === n.segment_id ? "Stop neighbour" : `Play neighbour ${fmtOffset(n.offset_seconds)}`}
                  onClick={() => toggle(n.segment_id)}
                >
                  {playing === n.segment_id ? <Square size={12} /> : <Play size={12} />}
                </IconButton>
                <IconButton label={`Open segment ${n.segment_id}`} onClick={() => onOpen(n.segment_id)}>
                  <ExternalLink size={12} />
                </IconButton>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}
