import { diffWords, fmtDuration, fmtFreq, fmtOffset, fmtUtc } from "./format";

describe("diffWords", () => {
  it("marks substitutions as delete + insert and ignores case/edge punctuation", () => {
    const ops = diffWords("Southwest four five six, contact", "southwest four fifty six contact");
    expect(ops.filter((o) => o.kind === "del").map((o) => o.text)).toEqual(["five"]);
    expect(ops.filter((o) => o.kind === "ins").map((o) => o.text)).toEqual(["fifty"]);
    expect(ops.filter((o) => o.kind === "eq")).toHaveLength(4);
  });

  it("handles empty sides", () => {
    expect(diffWords("", "roger")).toEqual([{ kind: "ins", text: "roger" }]);
    expect(diffWords("roger", "")).toEqual([{ kind: "del", text: "roger" }]);
  });
});

describe("formatting", () => {
  it("uses UTC and tabular-friendly forms", () => {
    expect(fmtUtc("2026-09-08T12:00:24+00:00")).toBe("2026-09-08 12:00:24");
    expect(fmtUtc(null)).toBe("—");
    expect(fmtDuration(3200)).toBe("3.2s");
    expect(fmtDuration(65_300)).toBe("1:05.3");
    expect(fmtFreq(119_400_000)).toBe("119.400");
    expect(fmtOffset(-4.2)).toBe("−4s");
    expect(fmtOffset(0)).toBe("±0s");
  });
});
