import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api } from "../api/client";
import type { ReviewFilters, SampleView } from "../api/types";
import { Button, Dialog, ErrorBox } from "../components/ui";
import { describeFilters } from "./filterMeta";

/** "Sample N from current filters": stored filter definition + seed + chosen ids. */
export function SampleDialog({
  open,
  onOpenChange,
  filters,
  total,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  filters: ReviewFilters;
  total: number | undefined;
  onCreated: (sample: SampleView) => void;
}) {
  const [n, setN] = useState(50);
  const [seed, setSeed] = useState(() => Math.floor(Math.random() * 1_000_000));
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () =>
      api.createSample({
        filters: { ...filters, sample_id: null },
        n,
        seed,
        name: name.trim() || undefined,
      }),
    onSuccess: (sample) => {
      onCreated(sample);
      onOpenChange(false);
    },
  });
  const chips = describeFilters({ ...filters, sample_id: null });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Sample N from current filters"
      description="Reproducible: the filter definition, seed and chosen segment ids are stored."
    >
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="form__filters">
          {chips.length ? chips.map((c) => <span key={c.key} className="chip">{c.label}</span>) : "No filters: all transcribed segments."}
        </div>
        <div className="form__row">
          <label className="field">
            <span>N</span>
            <input className="input num" type="number" min={1} max={5000} value={n} onChange={(e) => setN(Number(e.target.value))} />
          </label>
          <label className="field">
            <span>Seed</span>
            <input className="input num" type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} />
          </label>
          <label className="field field--grow">
            <span>Name (optional)</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
        </div>
        <p className="muted">
          Drawing {Math.min(n, total ?? n)} of {total ?? "?"} matching segments.
        </p>
        {create.error ? <ErrorBox title="Sample not created" detail={(create.error as Error).message} /> : null}
        <div className="dialog__actions">
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={create.isPending || n < 1}>
            {create.isPending ? "Sampling…" : "Create sample"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
