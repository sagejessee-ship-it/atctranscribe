import { highlightNote, transcriptSource } from "./transcriptSource";

const base = {
  human_text: null,
  representative_source: null,
  representative_models: [] as string[],
  representative_families: [] as string[],
  representative_highlights: [],
};

describe("transcriptSource", () => {
  it("names the models whose words are shown", () => {
    expect(
      transcriptSource({
        ...base,
        representative_source: "exact",
        representative_models: ["canary-1b", "parakeet-tdt"],
        representative_families: ["canary", "parakeet"],
      }),
    ).toBe("2 model families wrote exactly this (ignoring case and punctuation): canary-1b, parakeet-tdt.");
    expect(
      transcriptSource({
        ...base,
        representative_source: "near",
        representative_models: ["whisper-large"],
        representative_families: ["canary", "whisper"],
      }),
    ).toBe("whisper-large's words. 2 families nearly agree with it (canary, whisper).");
    expect(transcriptSource({ ...base, representative_source: "medoid", representative_models: ["qwen3-asr"] })).toContain(
      "No two model families agree. These are qwen3-asr's words",
    );
    expect(transcriptSource({ ...base, representative_source: "single", representative_models: ["granite"] })).toBe(
      "Only granite produced words.",
    );
  });

  it("puts a reviewer's text first and survives missing details", () => {
    expect(transcriptSource({ ...base, human_text: "delta one two three", representative_source: "exact" })).toMatch(/^Saved by a reviewer/);
    expect(transcriptSource({ ...base, representative_source: "medoid" })).toContain("one model's words");
    expect(transcriptSource(base)).toBe("");
  });

  it("explains the marks only when there are some", () => {
    expect(highlightNote(base)).toBeNull();
    expect(highlightNote({ ...base, representative_highlights: [{ start: 0, end: 2, utterance: 0, family_count: 2 }] })).toMatch(
      /at least two model families/,
    );
  });
});
