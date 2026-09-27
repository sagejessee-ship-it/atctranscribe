# AeroChorus UI design system

The authority is [DESIGN_LANGUAGE.md](DESIGN_LANGUAGE.md) (the Phase 5 design brief, kept verbatim). This page
records how `ui/` implements it.

## Stack

| piece | choice |
| --- | --- |
| Framework | React 19, TypeScript 5.9, Vite 7 |
| Data | TanStack Query 5. All filtering, sorting, paging, search and agreement happen on the server. |
| Grid | TanStack Table 8 (manual sorting, row selection, column visibility) and TanStack Virtual 3 (row virtualization within a page) |
| Primitives | shadcn/ui pattern: small, locally owned wrappers over Radix (`radix-ui`) in `src/components/ui.tsx`: Dialog, DropdownMenu, ToggleGroup. They are styled with plain CSS and tokens, not Tailwind, to keep density and styling explicit. |
| Icons | lucide-react. Every icon-only button has an accessible name and a tooltip. |
| Waveform | WaveSurfer.js 7 (Phase 5B) |

Package majors are pinned (`^8` TanStack Table, `^7` React Router and Vite,
`~5.9` TypeScript). Newer majors exist, but their APIs changed; upgrading is a
separate task.

## Tokens (`src/styles/tokens.css`)

Components never use raw colours. Light and dark themes follow
`prefers-color-scheme`.

| token group | values |
| --- | --- |
| Type | UI 13 px, dense tables 12 px, section titles 12 px uppercase, page title 18 px. Monospace (`.num`) for UTC, durations, frequencies, scores, ids and hashes. Transcripts use UI text. |
| Density | rows 32 px, controls 26 px, nav 36 px, toolbar 40 px, radius 3 px |
| Surfaces | `--surface-0..3`, `--border`, `--border-strong` |
| Accent | `--accent` (one blue), `--focus` |

Semantic state colours (`--ok`, `--info`, `--warn`, `--danger`, `--derived`,
`--neutral`, each with a `-bg`) are always paired with text:

| meaning | tone | example |
| --- | --- | --- |
| human verified / accepted | ok (green) | `gold`, `reviewed`, `H` origin |
| selected / informational | info (blue) | active row, `2f` agreement, span count |
| review needed / uncertain | warn (amber) | `unreviewed`, quality flags |
| error / rejected | danger (red) | `E2`, `rejected` |
| model-derived | derived (purple) | `candidate`, `silver`, `M` origin, consensus notes |
| abstained / unavailable | neutral (gray) | `A1`, `—` |

## Component vocabulary

| component | file |
| --- | --- |
| AppShell, PrimaryNav | `components/AppShell.tsx` |
| CorpusToolbar, SearchBox, SavedViewSelector, ActiveFilterChips | `review/CorpusToolbar.tsx` |
| FilterRail | `review/FilterRail.tsx` |
| SegmentDataGrid | `review/SegmentDataGrid.tsx` |
| BatchActionBar | `review/BatchActionBar.tsx` |
| SampleDialog | `review/SampleDialog.tsx` |
| SegmentInspector (identity, prev/next) | `inspector/SegmentInspector.tsx` |
| AudioPlayer | `inspector/AudioPlayer.tsx` |
| AgreementSummary, AgreementBadge | `inspector/AgreementSummary.tsx`, `components/badges.tsx` |
| StatusBadge, TrainingBadge | `components/badges.tsx` |
| HypothesisTable | `inspector/HypothesisTable.tsx` |
| TranscriptDiff | `inspector/TranscriptDiff.tsx` |
| CorrectionEditor (with AnnotationStatusControl and DatasetCandidateControl) | `inspector/CorrectionEditor.tsx` |
| NeighborContext | `inspector/NeighborContext.tsx` |
| AirportContext | `inspector/AirportContext.tsx` |
| WaveformEditor, AdsbContext | Phase 5B / 5C |

## Rules we hold to

- **Agreement badges** read `3f · 4p`: families first (the unit of
  independence), then providers. They are never merged into one
  "confidence".
- **Model confidence** appears only per model, labelled "Model conf.", with
  a tooltip saying it is not comparable across models.
- **Near similarity** is always shown with its threshold and is never called
  confidence.
- **Layout**: no cards, KPI tiles, gradients or glass. Separators are 1 px
  and shadows appear only on popovers and dialogs.
- **Status** is text plus colour, never colour alone.
- **Focus** is always visible (`:focus-visible` outline).
- **Loading and errors**: skeleton placeholders while loading, and explicit
  error boxes that quote the server's `detail` and offer a retry.
- **Persistence**: filters, sort, page, page size and the selected segment
  live in the URL. Per-browser preferences (columns, panel sizes, playback
  rate/volume, annotator label) live in `localStorage` and fall back
  silently.
