# AeroChorus UI Design Language
## Dense Research Workbench for ATC ASR Review and Corpus Curation

**Status:** Design authority for AeroChorus UI phases  
**Primary user:** Technical researcher/annotator working on a desktop/laptop on a trusted local network.

## 1. Product character

AeroChorus is a **dense technical analysis workstation**, not a consumer dashboard.

Optimize for scanning many records quickly, sorting/filtering a large ASR corpus, comparing hypotheses, listening to audio, correcting transcripts, curating training data, and inspecting contextual evidence without losing the review task.

The target is closer to an IDE, Grafana/Kibana, or a professional annotation tool than to a marketing dashboard.

> **Information density is a feature. Visual clutter is not.**

Use compact spacing, restrained styling, persistent context, predictable hierarchy, and progressive disclosure.

## 2. Research-informed patterns

### Label Studio audio annotation
Borrow:
- waveform playback and zoom;
- draggable audio regions;
- looped playback of a selected region;
- keyboard playback;
- per-region transcription;
- region/outliner lists;
- playback speed controls.

References:
- https://labelstud.io/templates/transcribe_audio
- https://labelstud.io/templates/automatic_speech_recognition_segments.html
- https://labelstud.io/templates/react_audio
- https://labelstud.io/videos/labeling-audio-data-with-label-studio/

AeroChorus adaptation: source audio remains immutable; region annotations store start/end offsets and text.

### Prodigy-style annotation
Borrow:
- minimal chrome around the current annotation;
- keyboard-first rapid review;
- keep the judged item visually dominant;
- waveform/spectrogram as expert tools rather than decorative content.

Reference:
- https://support.prodi.gy/t/wavesurfer-spectrogram/4993

### Carbon data tables
Borrow:
- sortable headers;
- compact row density;
- expandable rows;
- persistent toolbar;
- search/filter in table context;
- multi-select and batch actions;
- progressive disclosure.

References:
- https://carbondesignsystem.com/components/data-table/usage/
- https://carbondesignsystem.com/components/data-table/style/

### U.S. Web Design System tables
Borrow:
- minimal styling for dense tabular data;
- predictable column formatting;
- sticky headers;
- horizontal scroll when required;
- compact mode;
- monospace/tabular numerics.

Reference:
- https://designsystem.digital.gov/components/table/

### WaveSurfer.js
Use for the waveform phase:
- Regions;
- Timeline;
- Hover time;
- Zoom;
- Minimap if useful;
- Spectrogram only if later justified.

References:
- https://wavesurfer.xyz/doc/manual/index.html
- https://wavesurfer.xyz/examples/

## 3. Optional agent design skill

If supported, the coding agent may install/use **UI/UX Pro Max**:

https://www.aitmpl.com/component/skills/creative-design/ui-ux-pro-max

```bash
npx claude-code-templates@latest --skill creative-design/ui-ux-pro-max
```

Use it for accessibility, layout, stack-specific implementation, and UX review.

**This AeroChorus design document overrides generic style suggestions from that skill.**

Do not let a generic skill turn AeroChorus into glassmorphism, a bento dashboard, oversized cards, or a sparse consumer UI.

## 4. Visual language

Use a quiet neutral workstation aesthetic:
- gray/slate surfaces;
- one restrained primary accent;
- semantic colors only for state;
- subtle 1px separators;
- very limited shadow;
- small radius controls;
- no decorative gradients;
- no glassmorphism;
- no oversized hero content.

Color is supplementary, never the only signal.

Suggested semantics:
- green: human verified / accepted;
- blue: selected / informational;
- amber: review needed / uncertain;
- red: error / rejected;
- purple: derived/model-generated interpretation;
- gray: abstained / unavailable.

Typography:
- 13–14 px general body;
- 12–13 px dense table text if readable;
- 14–16 px section headings;
- 18–20 px maximum page title for workbench views.

Use monospace/tabular numerics for UTC timestamps, durations, frequencies, scores, hashes, and IDs. Use normal UI text for transcripts.

Default table row height: approximately 30–34 px. Long transcript cells use a single-line preview with ellipsis.

## 5. Primary layout

Use a master-detail workbench:

```text
┌──────────────────────────────────────────────────────────────────────┐
│ AeroChorus | Review | Training Sets | Models | Runs        settings │
├──────────────────────────────────────────────────────────────────────┤
│ Search ...  Date ▾  Airport ▾  Channel ▾  Agreement ▾  Review ▾    │
├─────────────┬───────────────────────────────────┬────────────────────┤
│ Filter rail │ Dense corpus/results table        │ Segment Inspector  │
│ optional    │                                   │                    │
│             │ time | ch | agree | text | status │ audio              │
│             │                                   │ hypotheses         │
│             │                                   │ correction         │
│             │                                   │ context            │
└─────────────┴───────────────────────────────────┴────────────────────┘
```

Rules:
- keep the grid visible while reviewing;
- inspector is resizable/hideable;
- opening a segment should not destroy filters;
- selected segment should be reflected in the URL;
- browser back/forward should preserve useful state where practical.

## 6. Corpus table

Required:
- server-side filtering/sorting;
- pagination or virtualization;
- sticky header;
- column resizing if straightforward;
- column visibility;
- multi-select;
- batch action bar;
- saved views;
- active filter chips;
- result count.

Default columns:
1. UTC timestamp
2. Airport
3. Channel
4. Duration
5. Model count
6. Exact-match max provider count
7. Exact-match max architecture-family count
8. Near-match max family count
9. Near-match similarity
10. Best/consensus transcript preview
11. Human review status
12. Training/gold status
13. Risk/error indicator

Show provider count and independent-family count separately.

## 7. Search and filters

Persistent transcript search should cover:
- all hypotheses;
- consensus/best;
- human corrections;
- human-verified spans.

Scope options:
- Any transcript
- Human correction
- Consensus/best
- Model hypothesis
- Specific model

Required filters:
- airport;
- channel;
- UTC date/time;
- model/provider;
- architecture family;
- minimum model count;
- exact provider match count >= N;
- exact independent-family match count >= N;
- near-match family count >= N;
- near-match similarity threshold;
- review state;
- candidate/silver/gold/rejected;
- error/abstention;
- available risk/language flags;
- whole segment vs span annotation.

Current default airport may be KBWI, but schema and UI must remain multi-airport capable.

## 8. Agreement UX

Exact agreement uses the existing **comparison/evidence normalizer**, not the scoring normalizer.

For every exact agreement group expose:
- representative text;
- providers;
- architecture families;
- provider count;
- family count.

Near match should reuse the existing order-aware similarity logic where available. The threshold must be explicit/configurable and must never be presented as a probability.

Ship views such as:
- 2+ exact families
- 3+ exact providers
- 3+ near families
- High agreement / unreviewed
- Human corrected
- Silver candidates
- Gold verified
- Model disagreement
- ASR errors/abstentions

## 9. Segment inspector

Answer quickly:
1. What did the audio say?
2. What did each model think?
3. How much independent agreement is there?
4. What should the corpus label be?

Order:

### Source identity
UTC, local time, airport, channel/frequency, duration, segment ID/filename, previous/next.

### Audio
Near-term:
- play/pause;
- seek;
- current time/duration;
- rate;
- volume;
- replay;
- keyboard shortcut.

### Agreement summary
Exact groups, provider/family counts, near group/similarity, abstentions, errors, risk flags.

### Hypotheses
Dense rows containing provider, family, status, transcript, similarity/group, model-specific confidence, and provenance disclosure.

Useful actions:
- use as correction starting text;
- copy transcript.

Original hypotheses are immutable.

### Human annotation
- corrected transcript;
- review status;
- notes;
- training state;
- reason tags.

Suggested reason tags:
- unclear audio;
- clipped boundary;
- overlapping speech;
- controller clear / pilot poor;
- noise;
- non-ATC;
- bad segmentation;
- multiple utterances;
- partial usable span;
- uncertain callsign;
- uncertain number/runway/frequency.

Every save creates/version-controls annotation state rather than overwriting provenance.

## 10. Training label semantics

Keep these distinct:

**Candidate:** machine-selected for possible use.

**Silver:** high-agreement label not necessarily fully human verified.

**Gold:** human-verified full segment or explicitly bounded audio span.

**Rejected:** unsuitable for training.

Model agreement may nominate candidates/silver data but never creates human gold automatically.

## 11. Batch curation

Enable:
1. filter `exact independent family count >= 2`;
2. add channel/date/similarity constraints;
3. multi-select;
4. batch `Add as candidate` or `Mark silver`;
5. sample/listen/review;
6. promote human-reviewed items to gold;
7. export a versioned training dataset.

Add `Sample N from current filters` with stored filter definition + random seed for reproducible QC sampling.

## 12. Keyboard-first workflow

Suggested shortcuts when focus is not inside an editor:
- Space: play/pause
- J/K: previous/next
- R: replay
- C: focus correction
- A: use selected hypothesis as correction starting text
- S: mark silver
- G: mark human-verified gold after confirmation
- X: reject
- ?: help

Truth-changing actions should be reversible or confirmed.

## 13. Neighbor context

ATC is conversational. Show previous/next same-channel segments and optionally a small time neighborhood.

Each neighbor:
- relative time;
- transcript preview;
- play button;
- channel.

This is context, not automatic truth.

## 14. Waveform / partial-gold design

Use WaveSurfer.js with waveform, timeline, zoom, seek, draggable regions, loop region, and start/end readout.

A span annotation stores:
- parent segment ID;
- start_ms;
- end_ms;
- transcript;
- status;
- annotator;
- provenance;
- notes;
- supporting models/families where relevant.

Do **not** cut or overwrite source WAVs when an annotation is created. Materialize derived clips only during export.

A whole segment may be low quality while one clean controller phrase becomes a gold span.

Optional action:
`Re-transcribe selected region` through one model or a model suite, preserving parent/offset provenance.

## 15. Airport context

Keep airport data in a compact context panel.

For BWI/KBWI include:
- ICAO/IATA/FAA identifiers;
- official name;
- reference point;
- timezone;
- runway pairs and ends;
- dimensions/bearings where useful;
- tower/ground/clearance/ATIS frequencies;
- voice-friendly spoken forms;
- data source/effective date.

Generate spoken aliases such as:
- 10 -> "runway one zero"
- 28 -> "runway two eight"
- 15L -> "runway one five left"
- 33R -> "runway three three right"

Current FAA material identifies runway pairs 10/28, 15L/33R, and 15R/33L. The implementation must still retrieve/verify live authoritative FAA data.

## 16. ADS-B context

ADS-B is optional context and must not slow ordinary review.

Initial interaction:
`Fetch ADS-B Context`

Display a compact table:
- callsign;
- ICAO24;
- time offset;
- lat/lon;
- altitude;
- heading;
- velocity;
- vertical rate;
- on-ground;
- source/provenance.

No map required initially.

ADS-B context may help identify plausible callsigns or movement but must not silently alter transcript labels.

## 17. Accessibility/reliability

Required:
- visible keyboard focus;
- accessible icon names;
- status not conveyed by color alone;
- no hover-only essential information;
- sufficient contrast;
- keyboard-accessible audio;
- stable loading placeholders;
- explicit error states;
- no success indication before API confirmation.

Desktop-first, but usable at laptop widths.

## 18. Anti-patterns

Do not:
- create a card per transcript;
- hide core filters behind modal chains;
- lead with giant KPI cards;
- display one fake cross-model confidence percentage;
- call similarity "confidence";
- force navigation to a new page per segment;
- auto-promote model agreement to gold;
- overwrite hypotheses;
- hide provenance;
- preload all ADS-B history;
- rely on mouse-only review;
- use decorative animation or excessive whitespace.

## 19. Stable component vocabulary

Build/reuse canonical components:
- `AppShell`
- `PrimaryNav`
- `CorpusToolbar`
- `FilterRail`
- `ActiveFilterChips`
- `SegmentDataGrid`
- `BatchActionBar`
- `SegmentInspector`
- `AudioPlayer`
- `WaveformEditor`
- `AgreementBadge`
- `StatusBadge`
- `HypothesisTable`
- `TranscriptDiff`
- `CorrectionEditor`
- `AnnotationStatusControl`
- `NeighborContext`
- `AirportContext`
- `AdsbContext`
- `SavedViewSelector`
- `DatasetCandidateControl`

## 20. Success criterion

The UI should make this workflow fast:

> Show me BWI Tower recordings from last week where at least two independent ASR families produced an exact match, let me listen to each, correct rare mistakes, promote the good items into a versioned training set, and move to the next mostly from the keyboard.
