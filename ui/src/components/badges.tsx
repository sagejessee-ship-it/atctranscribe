import clsx from "clsx";

import type { ReviewStatus, TrainingLabel } from "../api/types";

type Tone = "ok" | "info" | "warn" | "danger" | "derived" | "neutral";

export function Badge({
  tone,
  children,
  title,
}: {
  tone: Tone;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <span className={clsx("badge", `badge--${tone}`)} title={title}>
      {children}
    </span>
  );
}

const REVIEW: Record<ReviewStatus, { tone: Tone; label: string }> = {
  unreviewed: { tone: "warn", label: "unreviewed" },
  reviewed: { tone: "ok", label: "reviewed" },
  corrected: { tone: "ok", label: "corrected" },
};

const TRAINING: Record<TrainingLabel, { tone: Tone; label: string; title: string }> = {
  none: { tone: "neutral", label: "—", title: "no training label" },
  candidate: { tone: "derived", label: "candidate", title: "nominated for possible training use" },
  silver: { tone: "derived", label: "silver", title: "high-agreement label, not fully human verified" },
  gold: { tone: "ok", label: "gold", title: "human-verified" },
  rejected: { tone: "danger", label: "rejected", title: "unsuitable for training" },
};

/** Status is always text plus colour, never colour alone. */
export function StatusBadge({ status }: { status: ReviewStatus }) {
  const s = REVIEW[status];
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

export function TrainingBadge({ label }: { label: TrainingLabel }) {
  const t = TRAINING[label];
  if (label === "none") return <span className="muted">—</span>;
  return (
    <Badge tone={t.tone} title={t.title}>
      {label === "gold" ? "★ " : ""}
      {t.label}
    </Badge>
  );
}

/**
 * Independent agreement: families (the independence unit) with provider count
 * beside it. "3f · 4p" = 4 models from 3 architecture families agree.
 */
export function AgreementBadge({
  families,
  providers,
  kind = "exact",
}: {
  families: number;
  providers?: number;
  kind?: "exact" | "near";
}) {
  const tone: Tone = families >= 3 ? "ok" : families === 2 ? "info" : "neutral";
  const title =
    kind === "exact"
      ? `${families} architecture families${providers != null ? `, ${providers} models` : ""} produced the same text (evidence normalization)`
      : `${families} architecture families within the near-match threshold`;
  return (
    <Badge tone={tone} title={title}>
      {families}f{providers != null ? ` · ${providers}p` : ""}
    </Badge>
  );
}
