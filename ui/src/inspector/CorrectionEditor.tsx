import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ToggleGroup } from "radix-ui";
import clsx from "clsx";

import { ApiError, api } from "../api/client";
import type { AnnotationSave, Hypothesis, ReviewStatus, SegmentReview, TrainingLabel } from "../api/types";
import { StatusBadge, TrainingBadge } from "../components/badges";
import { Button, Dialog, ErrorBox, Kbd, Section } from "../components/ui";
import { useAnnotator } from "../lib/annotator";
import { fmtUtc } from "../lib/format";
import { describeOutcome } from "../review/BatchActionBar";
import { TranscriptDiff } from "./TranscriptDiff";

export const REASON_TAGS = [
  "unclear audio",
  "clipped boundary",
  "overlapping speech",
  "controller clear / pilot poor",
  "noise",
  "non-ATC",
  "bad segmentation",
  "multiple utterances",
  "partial usable span",
  "uncertain callsign",
  "uncertain number/runway/frequency",
];

const LABELS: { value: TrainingLabel; label: string; title: string }[] = [
  { value: "none", label: "None", title: "no training use decided" },
  { value: "candidate", label: "Candidate", title: "possible training use" },
  { value: "silver", label: "Silver", title: "good enough for silver training data (S)" },
  { value: "gold", label: "Gold", title: "human-verified: you listened and the text is exact (G)" },
  { value: "rejected", label: "Rejected", title: "unsuitable for training (X)" },
];

export interface EditorHandle {
  focus(): void;
  /** Fill the editor; keyboard flow (A) keeps focus where it is so S/K still work. */
  useText(text: string, focus?: boolean): void;
  markSilver(): void;
  markGold(): void;
  reject(): void;
}

const norm = (t: string | null | undefined) => (t ?? "").trim().replace(/\s+/g, " ");

/**
 * Human correction + review status + training label. Every save appends a
 * version (optimistic concurrency on the version number); nothing is shown as
 * saved until the API confirms it.
 */
export const CorrectionEditor = forwardRef<
  EditorHandle,
  { segment: SegmentReview; baseline: Hypothesis | null; onSaved: (message: string) => void }
>(function CorrectionEditor({ segment, baseline, onSaved }, ref) {
  const client = useQueryClient();
  const [annotator] = useAnnotator();
  const thread = segment.segment_annotation;
  const current = thread?.current ?? null;
  const benchmark = segment.source_role === "benchmark";

  const [text, setText] = useState("");
  const [label, setLabel] = useState<TrainingLabel>("none");
  const [tags, setTags] = useState<string[]>([]);
  const [notes, setNotes] = useState("");
  const [confirmGold, setConfirmGold] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);

  // Reset the draft whenever a different segment or a newer saved version arrives.
  const versionKey = `${segment.segment_id}:${current?.id ?? 0}`;
  useEffect(() => {
    setText(current?.text_origin === "human" ? (current.text ?? "") : "");
    setLabel((current?.training_label as TrainingLabel) ?? "none");
    setTags(current?.reason_tags ?? []);
    setNotes(current?.notes ?? "");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [versionKey]);

  const modelTexts = useMemo(
    () => new Set(segment.hypotheses.filter((h) => h.text).map((h) => norm(h.text))),
    [segment.hypotheses],
  );
  // Accepting a hypothesis verbatim is "reviewed"; any edit is "corrected".
  const status: ReviewStatus = !norm(text) ? "reviewed" : modelTexts.has(norm(text)) ? "reviewed" : "corrected";

  const save = useMutation({
    mutationFn: (body: AnnotationSave) => api.saveAnnotation(segment.segment_id, body),
    onSuccess: (saved) => {
      setError(null);
      onSaved(`Saved v${saved.current?.version} · ${saved.current?.review_status} · ${saved.current?.training_label}`);
      client.invalidateQueries({ queryKey: ["segment", segment.segment_id] });
      client.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (e) => setError(e as ApiError),
  });
  const batchSilver = useMutation({
    mutationFn: () => api.batch({ action: "silver", segment_ids: [segment.segment_id], annotator }),
    onSuccess: (outcome) => {
      onSaved(`Silver: ${describeOutcome(outcome)}`);
      client.invalidateQueries({ queryKey: ["segment", segment.segment_id] });
      client.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (e) => setError(e as ApiError),
  });

  const submit = (overrides: Partial<AnnotationSave> = {}) => {
    const body: AnnotationSave = {
      text: norm(text) || null,
      review_status: status,
      training_label: label,
      reason_tags: tags,
      notes: notes.trim() || null,
      annotator,
      expected_version: current?.version ?? 0,
      basis: baseline ? { started_from: baseline.model, started_from_result: baseline.result_id } : {},
      ...overrides,
    };
    if (body.training_label === "gold" && !overrides.confirm_gold) {
      setConfirmGold(true);
      return;
    }
    save.mutate(body);
  };

  const markSilver = () => {
    if (benchmark) return;
    if (norm(text)) submit({ training_label: "silver" });
    else batchSilver.mutate(); // no human text: nominate the model consensus, with its basis
  };
  const markGold = () => {
    if (benchmark) return;
    if (!norm(text)) {
      setError(new ApiError(422, "Gold needs the exact transcript: type it, or press A to start from a hypothesis."));
      textarea.current?.focus();
      return;
    }
    setLabel("gold");
    setConfirmGold(true);
  };

  useImperativeHandle(ref, () => ({
    focus: () => textarea.current?.focus(),
    useText: (value: string, focus = false) => {
      setText(value);
      if (focus) requestAnimationFrame(() => textarea.current?.focus());
    },
    markSilver,
    markGold,
    reject: () => submit({ training_label: "rejected" }),
  }));

  const busy = save.isPending || batchSilver.isPending;
  const dirty =
    norm(text) !== norm(current?.text_origin === "human" ? current.text : "") ||
    label !== (current?.training_label ?? "none") ||
    notes.trim() !== (current?.notes ?? "") ||
    tags.join("|") !== (current?.reason_tags ?? []).join("|");

  return (
    <Section
      title="Human annotation"
      id="annotation"
      aside={
        current ? (
          <span className="annot-state">
            <span className="num muted">v{current.version}</span>
            <StatusBadge status={current.review_status} />
            <TrainingBadge label={current.training_label} />
          </span>
        ) : (
          <span className="muted">not yet reviewed</span>
        )
      }
    >
      {current?.text_origin === "model_consensus" ? (
        <p className="note note--derived">
          Nominated from model consensus ({String(current.basis.representative_source ?? "agreement")}): “{current.text}”.
          Accept it with <Kbd>A</Kbd> after listening to make it a human transcript.
        </p>
      ) : null}
      <label className="field">
        <span>
          Corrected transcript <Kbd>C</Kbd>
        </span>
        <textarea
          ref={textarea}
          className="textarea editor__text"
          rows={3}
          value={text}
          spellCheck={false}
          placeholder="Type what was said, or press A to start from the selected hypothesis"
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
      {baseline?.text && norm(text) ? <TranscriptDiff before={baseline.text} after={text} label={baseline.model} /> : null}

      <div className="editor__row">
        <span className="muted">Training</span>
        <ToggleGroup.Root
          type="single"
          className="segmented"
          aria-label="Training label"
          value={label}
          onValueChange={(v) => v && setLabel(v as TrainingLabel)}
        >
          {LABELS.map((l) => (
            <ToggleGroup.Item
              key={l.value}
              value={l.value}
              title={l.title}
              className={clsx("segmented__item", `segmented__item--${l.value}`)}
              disabled={benchmark && l.value !== "none" && l.value !== "rejected"}
            >
              {l.label}
            </ToggleGroup.Item>
          ))}
        </ToggleGroup.Root>
      </div>
      {benchmark ? (
        <p className="note note--warn">Benchmark source: may be reviewed, never labelled for training.</p>
      ) : null}

      <details className="editor__more" open={tags.length > 0 || !!notes}>
        <summary>Reason tags &amp; notes</summary>
        <div className="tags" role="group" aria-label="Reason tags">
          {REASON_TAGS.map((tag) => (
            <label key={tag} className={clsx("tag", tags.includes(tag) && "tag--on")}>
              <input
                type="checkbox"
                checked={tags.includes(tag)}
                onChange={(e) => setTags(e.target.checked ? [...tags, tag] : tags.filter((t) => t !== tag))}
              />
              {tag}
            </label>
          ))}
        </div>
        <label className="field">
          <span>Notes</span>
          <textarea className="textarea" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </label>
      </details>

      {error ? (
        <ErrorBox
          title={error.status === 409 ? "Changed since you opened it" : "Not saved"}
          detail={error.detail}
          action={
            error.status === 409 ? (
              <Button size="sm" onClick={() => client.invalidateQueries({ queryKey: ["segment", segment.segment_id] })}>
                Reload annotation
              </Button>
            ) : undefined
          }
        />
      ) : null}

      <div className="editor__actions">
        <span className="muted">
          saves as <strong>{status}</strong>
          {label !== "none" ? (
            <>
              {" "}
              · <strong>{label}</strong>
            </>
          ) : null}
        </span>
        <span className="toolbar__spacer" />
        <Button size="sm" onClick={markSilver} disabled={busy || benchmark} title="Mark silver (S)">
          Silver <Kbd>S</Kbd>
        </Button>
        <Button size="sm" onClick={markGold} disabled={busy || benchmark} title="Mark human-verified gold (G)">
          Gold… <Kbd>G</Kbd>
        </Button>
        <Button size="sm" variant="danger" onClick={() => submit({ training_label: "rejected" })} disabled={busy}>
          Reject <Kbd>X</Kbd>
        </Button>
        <Button size="sm" variant="primary" onClick={() => submit()} disabled={busy || (!dirty && !!current)}>
          {busy ? "Saving…" : "Save"} <Kbd>Ctrl ↵</Kbd>
        </Button>
      </div>

      {thread && thread.history.length ? (
        <details className="history">
          <summary>History ({thread.history.length} versions)</summary>
          <ol className="history__list">
            {thread.history.map((v) => (
              <li key={v.id}>
                <span className="num">v{v.version}</span> <span className="num muted">{fmtUtc(v.created_at)}Z</span>{" "}
                <span className="muted">{v.action}</span> <StatusBadge status={v.review_status} />{" "}
                <TrainingBadge label={v.training_label} /> <span className="muted">{v.annotator ?? "?"}</span>
                {v.text ? (
                  <div className={clsx("history__text", v.text_origin === "model_consensus" && "history__text--model")}>
                    {v.text_origin === "model_consensus" ? "model: " : ""}
                    {v.text}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}

      <Dialog
        open={confirmGold}
        onOpenChange={(open) => {
          setConfirmGold(open);
          if (!open && current?.training_label !== "gold") setLabel((current?.training_label as TrainingLabel) ?? "none");
        }}
        title="Mark as human-verified gold?"
        description="Gold is your assertion that you listened and this transcript is exactly what was said. It is never set from model agreement alone."
      >
        <blockquote className="gold-quote">{norm(text) || "(no transcript)"}</blockquote>
        <div className="dialog__actions">
          <Button onClick={() => setConfirmGold(false)}>Cancel</Button>
          <Button
            variant="primary"
            autoFocus
            disabled={!norm(text) || busy}
            onClick={() => {
              setConfirmGold(false);
              submit({ training_label: "gold", confirm_gold: true });
            }}
          >
            Confirm gold
          </Button>
        </div>
      </Dialog>
    </Section>
  );
});
