import { describe, expect, it } from "vitest";

import { hundreds, inside, interiorLabelPoint, projector, trend, type Pt } from "./mapGeometry";

describe("projector", () => {
  it("puts the reference point at the centre and north up", () => {
    const { xy, scale } = projector(39.1754, -76.6683, 10, 560, 380);
    expect(scale).toBeCloseTo(19); // 190 px for 10 nm
    expect(xy(39.1754, -76.6683)).toEqual([280, 190]);
    const [, yNorth] = xy(39.1754 + 1 / 60, -76.6683); // one nm north
    expect(yNorth).toBeCloseTo(190 - 19);
    const [xEast] = xy(39.1754, -76.6683 + 1 / 60); // one arc-minute east is shorter than a nm
    expect(xEast - 280).toBeCloseTo(19 * Math.cos((39.1754 * Math.PI) / 180));
  });
});

describe("interiorLabelPoint", () => {
  const square: Pt[] = [
    [100, 100],
    [300, 100],
    [300, 300],
    [100, 300],
    [100, 100],
  ];

  it("finds a point well inside", () => {
    const p = interiorLabelPoint(square, 560, 380)!;
    expect(inside(p, square)).toBe(true);
    expect(p[0]).toBeGreaterThan(150);
    expect(p[0]).toBeLessThan(250);
  });

  it("returns null for polygons outside the frame or too thin to label", () => {
    const offscreen = square.map(([x, y]) => [x + 1000, y] as Pt);
    expect(interiorLabelPoint(offscreen, 560, 380)).toBeNull();
    const sliver: Pt[] = [
      [100, 100],
      [300, 100],
      [300, 105],
      [100, 105],
      [100, 100],
    ];
    expect(interiorLabelPoint(sliver, 560, 380)).toBeNull();
  });

  it("places a stacked shelf's label outside the lower shelf drawn over it", () => {
    const core: Pt[] = [
      [150, 150],
      [250, 150],
      [250, 250],
      [150, 250],
      [150, 150],
    ];
    const p = interiorLabelPoint(square, 560, 380, { exclude: [core] })!;
    expect(inside(p, square)).toBe(true);
    expect(inside(p, core)).toBe(false);
    const q = interiorLabelPoint(square, 560, 380, { blocked: ([x]) => x < 200 })!;
    expect(q[0]).toBeGreaterThanOrEqual(200);
  });

  it("keeps the label inside the visible part of a large shelf", () => {
    const huge: Pt[] = [
      [-2000, -2000],
      [3000, -2000],
      [3000, 3000],
      [-2000, 3000],
      [-2000, -2000],
    ];
    const [x, y] = interiorLabelPoint(huge, 560, 380)!;
    expect(x).toBeGreaterThanOrEqual(18);
    expect(x).toBeLessThanOrEqual(560 - 18);
    expect(y).toBeGreaterThanOrEqual(18);
    expect(y).toBeLessThanOrEqual(380 - 18);
  });
});

describe("chart labels", () => {
  it("formats floors and ceilings in hundreds of feet", () => {
    expect(hundreds(10000, "MSL")).toBe("100");
    expect(hundreds(1500, "MSL")).toBe("15");
    expect(hundreds(0, "SFC")).toBe("SFC");
    expect(hundreds(null, null)).toBe("—");
  });

  it("marks only meaningful climbs and descents", () => {
    expect(trend(-700)).toBe("↓");
    expect(trend(1200)).toBe("↑");
    expect(trend(150)).toBe("");
    expect(trend(null)).toBe("");
  });
});
