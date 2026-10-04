import { expect, test } from "@playwright/test";

// ADR-024: the local adsb.lol store. The stack switches to it for this test and
// imports a deterministic fake day in a thread (tests/fake_adsblol.py), so no
// network is touched; the other ADS-B tests keep the fake OpenSky provider.

test("an unimported day is imported on request, then its traffic shows on the map", async ({ page }) => {
  await page.goto("/review");
  await page.getByRole("grid").getByRole("row").filter({ hasText: "united nine" }).click();
  await expect(page).toHaveURL(/seg=\d+/);
  const segment = new URL(page.url()).searchParams.get("seg");
  const panel = page.locator("details").filter({ hasText: "ADS-B context" });
  const table = panel.getByRole("table", { name: /Nearby aircraft/ });

  // An earlier snapshot from OpenSky (the fake provider here).
  await panel.locator("summary").click();
  await panel.getByRole("button", { name: "Fetch ADS-B context" }).click();
  await expect(table).toContainText("AAL2669");

  await page.request.post(`/api/__e2e/adsb-local?segment_id=${segment}`);
  try {
    await page.reload();
    // The old snapshot shows, and the day can still be imported.
    await expect(table).toContainText("AAL2669");
    await expect(panel.getByRole("status")).toContainText("isn't imported yet");
    const importButton = panel.getByRole("button", { name: /Import 2026-09-08 from adsb\.lol/ });
    await importButton.click();

    // Imported in the background; the panel polls, then replaces the snapshot by itself.
    await expect(table).toContainText("SWA456", { timeout: 20_000 });
    await expect(table).not.toContainText("AAL2669");
    await expect(table.getByRole("row").filter({ hasText: "SWA456" })).toContainText("B737");
    await expect(table).toContainText("C172"); // the aircraft on the ramp
    await expect(table).not.toContainText("A320"); // just beyond the imported 25 nm
    await expect(page.getByTestId("adsb-provenance")).toContainText("adsb.lol");
    await expect(page.getByTestId("adsb-provenance")).toContainText("ODbL");
    await expect(panel.locator("summary")).toContainText("2 aircraft · cached");
    await expect(panel.getByRole("button", { name: /Import/ })).toHaveCount(0);

    const map = page.getByTestId("airport-map");
    await expect(map.getByRole("img", { name: /2 aircraft/ })).toBeVisible();
    const swa = map.getByTestId("map-aircraft-a0f00d");
    await expect(swa).toContainText("SWA456");
    await expect(swa.locator("polyline.amap__trail:not(.amap__trail--future)")).toHaveCount(1);
    await expect(map.getByRole("button", { name: "20 nm" })).toHaveAttribute("aria-pressed", "true");
  } finally {
    await page.request.post("/api/__e2e/adsb-opensky");
  }
});
