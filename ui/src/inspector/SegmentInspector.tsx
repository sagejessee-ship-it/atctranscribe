import { useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { api } from "../api/client";
import type { Hypothesis, SegmentReview } from "../api/types";
import { Badge } from "../components/badges";
import { ErrorBox, IconButton, Kbd } from "../components/ui";
import { fmtDuration, fmtFreq, fmtLocal, fmtUtc } from "../lib/format";
import { AgreementSummary } from "./AgreementSummary";
import { AirportContext } from "./AirportContext";
import { CorrectionEditor, type EditorHandle } from "./CorrectionEditor";
import { HypothesisTable } from "./HypothesisTable";
import { NeighborContext } from "./NeighborContext";
import { SpanPanel } from "./SpanPanel";
import { WaveformEditor, type AudioHandle, type SpanSelection } from "./WaveformEditor";

export interface InspectorCommands {
  togglePlay(): void;
  replay(): void;
  toggleLoop(): void;
  focusCorrection(): void;
  useSelectedHypothesis(): void;
  markSilver(): void;
  markGold(): void;
  reject(): void;
}

/** The representative hypothesis: first current result in exact group 0, else the best-similarity one. */
function defaultHypothesis(segment: SegmentReview): Hypothesis | null {
  const current = segment.hypotheses.filter((h) => !h.superseded && h.text);
  return (
    current.find((h) => h.exact_group === 0) ??
    [...current].sort((a, b) => (b.similarity_to_representative ?? 0) - (a.similarity_to_representative ?? 0))[0] ??
    null
  );
}

function Identity({
  segment,
  onPrev,
  onNext,
}: {
  segment: SegmentReview;
  onPrev?: () => void;
  onNext?: () => void;
}) {
  return (
    <div className="identity">
      <div className="identity__head">
        <IconButton label="Previous segment (J)" onClick={onPrev} disabled={!onPrev}>
          <ChevronLeft size={15} />
        </IconButton>
        <h2 className="identity__title num">
          {fmtUtc(segment.capture_start_utc)}Z
          <span className="identity__sep">·</span>
          {segment.channel ?? "—"}
        </h2>
        <IconButton label="Next segment (K)" onClick={onNext} disabled={!onNext}>
          <ChevronRight size={15} />
        </IconButton>
        {segment.source_role === "benchmark" ? <Badge tone="warn">benchmark</Badge> : null}
      </div>
      <dl className="kv">
        <dt>local</dt>
        <dd className="num">
          {fmtLocal(segment.capture_local)} <span className="muted">{segment.local_timezone ?? ""}</span>
        </dd>
        <dt>airport</dt>
        <dd className="num">{segment.airport ?? segment.station ?? "—"}</dd>
        <dt>channel</dt>
        <dd>
          <span className="num">{fmtFreq(segment.frequency_hz)} MHz</span>{" "}
          {segment.channel_service ? <span className="muted">{segment.channel_service}</span> : null}
        </dd>
        <dt>duration</dt>
        <dd className="num">{fmtDuration(segment.duration_ms)}</dd>
        <dt>segment</dt>
        <dd className="num identity__path" title={`${segment.source_key}/${segment.relative_path}`}>
          #{segment.segment_id} · {segment.relative_path}
        </dd>
        <dt>time</dt>
        <dd className="muted">{segment.temporal_status}</dd>
      </dl>
    </div>
  );
}

export function SegmentInspector({
  segmentId,
  commands,
  onPrev,
  onNext,
  onOpen,
  onToast,
}: {
  segmentId: number | null;
  commands: MutableRefObject<InspectorCommands | null>;
  onPrev?: () => void;
  onNext?: () => void;
  onOpen: (id: number) => void;
  onToast: (message: string) => void;
}) {
  const query = useQuery({
    queryKey: ["segment", segmentId],
    queryFn: () => api.segment(segmentId!),
    enabled: segmentId != null,
  });
  const segment = query.data;
  const player = useRef<AudioHandle>(null);
  const editor = useRef<EditorHandle>(null);
  const [selectedHyp, setSelectedHyp] = useState<string | null>(null);
  const [span, setSpan] = useState<SpanSelection | null>(null);

  const baseline = useMemo(() => {
    if (!segment) return null;
    return segment.hypotheses.find((h) => h.result_id === selectedHyp) ?? defaultHypothesis(segment);
  }, [segment, selectedHyp]);
  useEffect(() => {
    setSelectedHyp(null);
    setSpan(null);
  }, [segmentId]);

  useEffect(() => {
    commands.current = {
      togglePlay: () => player.current?.toggle(),
      replay: () => player.current?.replay(),
      toggleLoop: () => player.current?.toggleLoop(),
      focusCorrection: () => editor.current?.focus(),
      useSelectedHypothesis: () => baseline?.text && editor.current?.useText(baseline.text),
      markSilver: () => editor.current?.markSilver(),
      markGold: () => editor.current?.markGold(),
      reject: () => editor.current?.reject(),
    };
    return () => {
      commands.current = null;
    };
  }, [commands, baseline]);

  if (segmentId == null) {
    return (
      <div className="inspector inspector--empty">
        <p className="muted">
          Select a segment. <Kbd>J</Kbd>/<Kbd>K</Kbd> move through the list, <Kbd>?</Kbd> shows all shortcuts.
        </p>
      </div>
    );
  }
  if (query.isPending) {
    return (
      <div className="inspector" aria-busy="true">
        <div className="skeleton skeleton--title" />
        <div className="skeleton" />
        <div className="skeleton" />
      </div>
    );
  }
  if (query.isError || !segment) {
    return (
      <div className="inspector">
        <ErrorBox title={`Segment ${segmentId} could not be loaded`} detail={(query.error as Error)?.message} />
      </div>
    );
  }
  return (
    <div className="inspector" aria-label={`Segment ${segment.segment_id}`}>
      <Identity segment={segment} onPrev={onPrev} onNext={onNext} />
      <WaveformEditor
        ref={player}
        segmentId={segment.segment_id}
        durationMs={segment.duration_ms}
        spans={segment.span_annotations}
        selection={span}
        onSelection={setSpan}
      />
      <AgreementSummary agreement={segment.agreement} />
      <HypothesisTable
        hypotheses={segment.hypotheses}
        selectedId={baseline?.result_id ?? null}
        onSelect={setSelectedHyp}
        onUse={(text) => editor.current?.useText(text, true)}
      />
      <CorrectionEditor ref={editor} segment={segment} baseline={baseline} onSaved={onToast} />
      <SpanPanel segment={segment} selection={span} baseline={baseline} onSelection={setSpan} onSaved={onToast} />
      <NeighborContext neighbors={segment.neighbors} onOpen={onOpen} onBeforePlay={() => player.current?.pause()} />
      <AirportContext profile={segment.airport_profile} frequencyHz={segment.frequency_hz} />
    </div>
  );
}
