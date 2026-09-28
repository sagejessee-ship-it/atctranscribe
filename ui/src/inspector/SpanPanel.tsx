import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ToggleGroup } from "radix-ui";
import clsx from "clsx";

import { ApiError, api } from "../api/client";
import type { AnnotationSave, AnnotationThread, Hypothesis, SegmentReview, TrainingLabel } from "../api/types";
import { TrainingBadge } from "../components/badges";
import { Button, Dialog, ErrorBox, Section } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import type { SpanSelection } from "./WaveformEditor";

const LABELS: TrainingLabel[] = ["none", "candidate", "silver", "gold", "rejected"];
const norm = (t: string | null | undefined) => (t ?? "").trim().replace(/\s+/g, " ");
const secs = (ms: number) => (ms / 1000).toFixed(2);

/**
 * Partial-segment annotations: a bounded span with its own transcript and label.
 * The parent segment can stay rejected while a clean phrase inside it is gold.
 */
export function SpanPanel({
  segment,
  selection,
  baseline,
  onSelection,
  onSaved,
}: {
  segment: SegmentReview;
  selection: SpanSelection | null;
  baseline: Hypothesis | null;
  onSelection: (selection: SpanSelection | null) => void;
  onSaved: (message: string) => void;
}) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const benchmark = segment.source_role === "benchmark";
  const spans = segment.span_annotations;
  const thread: AnnotationThread | null = useMemo(
    () => spans.find((s) => s.thread_id === selection?.threadId) ?? null,
    [spans, selection?.threadId],
  );
  const current = thread?.current ?? null;

  const [text, setText] = useState("");
  const [label, setLabel] = useState<TrainingLabel>("none");
  const [notes, setNotes] = useState("");
  const [confirmGold, setConfirmGold] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const draftKey = `${selection?.threadId ?? "draft"}:${current?.id ?? 0}:${selection?.utterance ?? ""}`;
  useEffect(() => {
    setText(current?.text ?? selection?.prefill ?? "");
    setLabel((current?.training_label as TrainingLabel) ?? "none");
    setNotes(current?.notes ?? "");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  const save = useMutation({
    mutationFn: (body: AnnotationSave) => api.saveAnnotation(segment.segment_id, body),
    onSuccess: (saved) => {
      onSaved(
        `Span ${secs(saved.current!.start_ms!)}–${secs(saved.current!.end_ms!)}s saved v${saved.current?.version} · ${saved.current?.training_label}`,
      );
      onSelection({ threadId: saved.thread_id, start_ms: saved.current!.start_ms!, end_ms: saved.current!.end_ms! });
      client.invalidateQueries({ queryKey: ["segment", segment.segment_id] });
      client.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (e) => setError(e as ApiError),
  });

  const modelTexts = new Set(segment.hypotheses.map((h) => norm(h.text)));
  const submit = (confirm = false) => {
    if (!selection) return;
    if (label === "gold" && !confirm) {
      setConfirmGold(true);
      return;
    }
    save.mutate({
      thread_id: selection.threadId,
      scope: "span",
      start_ms: selection.start_ms,
      end_ms: selection.end_ms,
      text: norm(text) || null,
      review_status: norm(text) && !modelTexts.has(norm(text)) ? "corrected" : "reviewed",
      training_label: label,
      notes: notes.trim() || null,
      annotator,
      expected_version: current?.version ?? 0,
      confirm_gold: confirm,
      basis: {
        parent_segment_id: segment.segment_id,
        ...(selection.utterance != null
          ? {
              started_from_utterance: selection.utterance,
              utterance_families: segment.agreement?.utterances?.[selection.utterance]?.families,
              utterance_providers: segment.agreement?.utterances?.[selection.utterance]?.providers,
            }
          : baseline
            ? { prefilled_from: baseline.model }
            : {}),
      },
    });
  };

  const setBound = (key: "start_ms" | "end_ms", value: number) => {
    if (!selection || Number.isNaN(value)) return;
    onSelection({ ...selection, [key]: Math.max(0, Math.round(value)) });
  };

  return (
    <Section
      title="Spans"
      id="spans"
      aside={<span className="muted">{spans.length ? `${spans.length} on this segment` : "none yet"}</span>}
    >
      {spans.length ? (
        <table className="mini-table spans">
          <caption className="sr-only">Span annotations</caption>
          <thead>
            <tr>
              <th scope="col">Bounds (s)</th>
              <th scope="col">Label</th>
              <th scope="col">Text</th>
              <th scope="col">Ver</th>
            </tr>
          </thead>
          <tbody>
            {spans.map((s) => {
              const v = s.current;
              if (!v || v.start_ms == null || v.end_ms == null) return null;
              const active = selection?.threadId === s.thread_id;
              return (
                <tr
                  key={s.thread_id}
                  className={clsx("spans__row", active && "spans__row--active")}
                  onClick={() => onSelection({ threadId: s.thread_id, start_ms: v.start_ms!, end_ms: v.end_ms! })}
                >
                  <td className="num">
                    <button type="button" className="linkish num" aria-label={`Select span ${secs(v.start_ms)} to ${secs(v.end_ms)}`}>
                      {secs(v.start_ms)}–{secs(v.end_ms)}
                    </button>
                  </td>
                  <td>
                    <TrainingBadge label={v.training_label} />
                  </td>
                  <td className="mini-table__text">{v.text ?? <span className="muted">(no text)</span>}</td>
                  <td className="num muted">v{v.version}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : null}

      {selection ? (
        <div className="span-editor" aria-label={thread ? `Edit span ${thread.thread_id}` : "New span"}>
          <div className="span-editor__bounds">
            <strong>{thread ? `Span v${current?.version}` : "New span"}</strong>
            <label className="field field--inline">
              <span>start ms</span>
              <input
                className="input input--num num"
                type="number"
                min={0}
                value={selection.start_ms}
                onChange={(e) => setBound("start_ms", Number(e.target.value))}
                aria-label="Span start (ms)"
              />
            </label>
            <label className="field field--inline">
              <span>end ms</span>
              <input
                className="input input--num num"
                type="number"
                min={0}
                value={selection.end_ms}
                onChange={(e) => setBound("end_ms", Number(e.target.value))}
                aria-label="Span end (ms)"
              />
            </label>
            <span className="num muted">{((selection.end_ms - selection.start_ms) / 1000).toFixed(2)}s</span>
          </div>
          <label className="field">
            <span>Span transcript (exactly what is said inside the bounds)</span>
            <textarea
              className="textarea"
              rows={2}
              value={text}
              spellCheck={false}
              aria-label="Span transcript"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  submit();
                }
                if (e.key === "Escape") (e.target as HTMLTextAreaElement).blur();
              }}
            />
          </label>
          <div className="editor__row">
            <ToggleGroup.Root
              type="single"
              className="segmented"
              aria-label="Span training label"
              value={label}
              onValueChange={(v) => v && setLabel(v as TrainingLabel)}
            >
              {LABELS.map((l) => (
                <ToggleGroup.Item
                  key={l}
                  value={l}
                  className={clsx("segmented__item", `segmented__item--${l}`)}
                  disabled={benchmark && l !== "none" && l !== "rejected"}
                >
                  {l === "none" ? "None" : l[0].toUpperCase() + l.slice(1)}
                </ToggleGroup.Item>
              ))}
            </ToggleGroup.Root>
            {baseline?.text ? (
              <Button size="sm" variant="ghost" onClick={() => setText(baseline.text ?? "")} title="Then trim to the span">
                Prefill from {baseline.model}
              </Button>
            ) : null}
          </div>
          <label className="field">
            <span>Notes</span>
            <input className="input" value={notes} onChange={(e) => setNotes(e.target.value)} aria-label="Span notes" />
          </label>
          {thread && thread.history.length ? (
            <details className="history">
              <summary>History ({thread.history.length} versions)</summary>
              <ol className="history__list">
                {thread.history.map((v) => (
                  <li key={v.id}>
                    <span className="num">v{v.version}</span>{" "}
                    <span className="num muted">
                      {v.start_ms != null ? secs(v.start_ms) : "?"}–{v.end_ms != null ? secs(v.end_ms) : "?"}s ·{" "}
                      {v.created_at.replace("T", " ").slice(0, 19)}Z
                    </span>{" "}
                    <TrainingBadge label={v.training_label} /> <span className="muted">{v.annotator ?? "?"}</span>
                    {v.text ? <div className="history__text">{v.text}</div> : null}
                  </li>
                ))}
              </ol>
            </details>
          ) : null}
          {error ? <ErrorBox title={error.status === 409 ? "Changed since you opened it" : "Span not saved"} detail={error.detail} /> : null}
          <div className="editor__actions">
            <span className="toolbar__spacer" />
            <Button size="sm" variant="ghost" onClick={() => onSelection(null)}>
              Clear selection
            </Button>
            <Button size="sm" variant="primary" onClick={() => submit()} disabled={save.isPending}>
              {save.isPending ? "Saving…" : thread ? "Save span" : "Save new span"}
            </Button>
          </div>
        </div>
      ) : (
        <p className="muted">Drag on the waveform to select a clean phrase, then transcribe and label it.</p>
      )}

      <Dialog
        open={confirmGold}
        onOpenChange={setConfirmGold}
        title="Mark this span as human-verified gold?"
        description="You listened to exactly this span and the transcript is what was said inside it."
      >
        <p className="num">
          {selection ? `${secs(selection.start_ms)}–${secs(selection.end_ms)}s` : ""}
        </p>
        <blockquote className="gold-quote">{norm(text) || "(no transcript)"}</blockquote>
        <div className="dialog__actions">
          <Button onClick={() => setConfirmGold(false)}>Cancel</Button>
          <Button
            variant="primary"
            autoFocus
            disabled={!norm(text)}
            onClick={() => {
              setConfirmGold(false);
              submit(true);
            }}
          >
            Confirm gold span
          </Button>
        </div>
      </Dialog>
    </Section>
  );
}
