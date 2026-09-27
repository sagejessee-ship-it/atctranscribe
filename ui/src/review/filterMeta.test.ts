import { describeFilters } from "./filterMeta";
import { compactFilters } from "./useReviewState";

describe("compactFilters", () => {
  it("drops empty values and server defaults so URLs stay short", () => {
    expect(
      compactFilters({
        channels: [],
        q: "",
        scope: "any",
        include_benchmark: false,
        min_models: 1,
        min_exact_families: 2,
        airport: null,
      }),
    ).toEqual({ min_exact_families: 2 });
    expect(compactFilters({ min_models: 0 })).toEqual({ min_models: 0 });
  });
});

describe("describeFilters", () => {
  it("gives one removable chip per constraint", () => {
    const filters = { min_exact_families: 2, channels: ["TWR"], q: "niner", scope: "hypotheses" as const };
    const chips = describeFilters(filters);
    expect(chips.map((c) => c.label)).toEqual([
      "“niner” in Hypotheses",
      "channel: TWR",
      "exact families ≥ 2",
    ]);
    const withoutSearch = chips[0].clear(filters);
    expect(withoutSearch).toEqual({ min_exact_families: 2, channels: ["TWR"] });
  });

  it("never describes the provider and family counts as the same thing", () => {
    const labels = describeFilters({ min_exact_providers: 3, min_exact_families: 2 }).map((c) => c.label);
    expect(labels).toEqual(["exact providers ≥ 3", "exact families ≥ 2"]);
  });
});
