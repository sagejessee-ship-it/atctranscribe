import { expect, test, type Page } from "@playwright/test";

// Phase 5C: on-demand ADS-B context. The main stack runs a deterministic fake
// OpenSky provider (tests/e2e/stack.py --fake-adsb); a second stack has none.

const plainURL = `http://127.0.0.1:${Number(process.env.AEROCHORUS_E2E_PORT ?? 8765) + 1}`;
const row = (page: Page, text: string) => page.getByRole("grid").getByRole("row").filter({ hasText: text });
const adsbCalls = async (page: Page) => (await (await page.request.get("/api/__e2e/adsb-calls")).json()).calls;

test("ADS-B fetched on demand, cached on reopen, refreshed explicitly", async ({ page }) => {
  const before = await adsbCalls(page);
  await page.goto("/review");
  await row(page, "southwest").click();
  const panel = page.locator("details").filter({ hasText: "ADS-B context" });
  await expect(panel.locator("summary")).toContainText("on demand");
  await panel.locator("summary").click();
  await expect(panel.getByRole("button", { name: "Fetch ADS-B context" })).toBeVisible();
  expect(await adsbCalls(page)).toBe(before); // opening a segment never queries

  await panel.getByRole("button", { name: "Fetch ADS-B context" }).click();
  const table = panel.getByRole("table", { name: /Nearby aircraft/ });
  await expect(table).toContainText("AAL2669");
  await expect(table.getByRole("row").nth(1)).toContainText("+2s"); // nearest in time first
  await expect(table).toContainText("SWA456");
  await expect(table).toContainText("4000"); // metres → feet
  const provenance = page.getByTestId("adsb-provenance");
  await expect(provenance).toContainText("minio.osky.state_vectors_data4");
  await expect(provenance).toContainText("10 nm of KBWI");
  expect(await adsbCalls(page)).toBe(before + 1);
  const snapshot = (await provenance.textContent())!.match(/snapshot #(\d+)/)![1];

  // Reopen: same snapshot, no external call.
  await page.reload();
  await expect(page.getByTestId("adsb-provenance")).toContainText(`snapshot #${snapshot}`);
  expect(await adsbCalls(page)).toBe(before + 1);

  await page.getByRole("button", { name: "Refresh context" }).click();
  await expect(page.getByTestId("adsb-provenance")).not.toContainText(`snapshot #${snapshot}`);
  expect(await adsbCalls(page)).toBe(before + 2);

  // The map: runways from surveyed ends, stacked airspace, traffic with trails.
  const map = page.getByTestId("airport-map");
  await expect(map.getByRole("img", { name: /Simplified map of KBWI: 3 runways, 2 controlled airspace areas, 2 aircraft/ })).toBeVisible();
  await expect(map.getByTestId("map-runway-10/28")).toHaveCount(1);
  await expect(map.locator('[data-class="B"] path')).toHaveCount(1);
  await expect(map.locator('[data-class="D"] path')).toHaveCount(1);
  const swa = map.getByTestId("map-aircraft-a0f00d");
  await expect(swa).toContainText("SWA456");
  await expect(swa).toContainText("040"); // hundreds of feet
  await expect(swa.locator("polyline.amap__trail:not(.amap__trail--future)")).toHaveCount(1); // track so far
  // Hovering a table row highlights the aircraft on the map, and back.
  await page.getByRole("table", { name: /Nearby aircraft/ }).getByRole("row").filter({ hasText: "SWA456" }).hover();
  await expect(swa).toHaveClass(/amap__ac--hi/);
  await map.getByRole("button", { name: "5 nm" }).click();
  await expect(map.getByRole("img", { name: /5 nautical mile range/ })).toBeVisible();
  await expect(map.locator(".amap__rwy text", { hasText: "33L" })).toHaveCount(1); // idents when zoomed in

  // Context never touches the transcript or the label.
  await expect(page.getByLabel(/Corrected transcript/)).not.toHaveValue(/AAL2669/);
});

test("ADS-B unavailable without credentials; review still works", async ({ page }) => {
  await page.goto(`${plainURL}/review`);
  await row(page, "delta one two three").click();
  const panel = page.locator("details").filter({ hasText: "ADS-B context" });
  await expect(panel.locator("summary")).toContainText("unavailable");
  await panel.locator("summary").click();
  await expect(panel.getByRole("status")).toContainText("not configured");
  await expect(panel.getByRole("button", { name: "Fetch ADS-B context" })).toHaveCount(0);
  // The airport map still works without a traffic provider.
  await expect(panel.getByRole("img", { name: /Simplified map of KBWI: 3 runways/ })).toBeVisible();
  await expect(panel.locator(".amap__ac")).toHaveCount(0);

  // Review continues: correct and save.
  const editor = page.getByLabel(/Corrected transcript/);
  await editor.fill("delta one two three taxi via alpha");
  await editor.press("Control+Enter");
  await expect(page.locator(".toast")).toContainText("Saved v1");
});
