import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Runs against tests/e2e/stack.py: a fresh database with a synthetic BWI corpus.
// Tests share that database and run in file order (workers: 1).

const toast = (page: Page) => page.locator(".toast");
const audio = (page: Page) => page.getByTestId("segment-audio");
const row = (page: Page, text: string) => page.getByRole("row").filter({ hasText: text });
const annotationAside = (page: Page) => page.locator("#annotation-title").locator("..");

async function audioReady(page: Page) {
  await expect.poll(() => audio(page).evaluate((el: HTMLAudioElement) => el.readyState)).toBeGreaterThanOrEqual(1);
}

test("high-agreement review: listen, accept, mark silver, move next", async ({ page }) => {
  await page.goto("/review");
  await page.getByLabel("Saved view").selectOption("exact-2-families");
  await expect(page.locator(".count")).toHaveText(/^3 segments/);

  await row(page, "delta one two three").click();
  await expect(page.getByRole("heading", { level: 2 })).toContainText("GND");
  await audioReady(page);
  await page.keyboard.press("Space");
  await expect
    .poll(() => audio(page).evaluate((el: HTMLAudioElement) => el.currentTime > 0 || el.ended))
    .toBe(true);

  await page.keyboard.press("a"); // accept the selected hypothesis as the starting text
  await expect(page.getByLabel(/Corrected transcript/)).toHaveValue(/delta one two three/i);
  await page.keyboard.press("s");
  await expect(toast(page)).toContainText("Saved v1 · reviewed · silver");

  const before = page.url();
  await page.keyboard.press("k");
  await expect(page).not.toHaveURL(before);
  await expect(page.getByRole("heading", { level: 2 })).toContainText("TWR");
});

test("search, correct, confirm gold; survives reload; hypotheses unchanged", async ({ page }) => {
  await page.goto("/review");
  const search = page.getByRole("searchbox", { name: "Search transcripts" });
  await search.fill("niner");
  await search.press("Enter");
  await expect(page.locator(".count")).toHaveText(/^1 segment$/);
  await row(page, "southwest").click();

  const editor = page.getByLabel(/Corrected transcript/);
  await expect(page.getByRole("table", { name: /Model hypotheses/ })).toContainText("southwest");
  await page.keyboard.press("c");
  await expect(editor).toBeFocused();
  const corrected = "southwest four fifty six contact departure one one niner point four";
  await editor.fill(corrected);
  await expect(page.getByLabel(/Differences from/)).toContainText("fifty");
  await editor.press("Control+Enter");
  await expect(toast(page)).toContainText("Saved v1 · corrected · none");
  await expect(annotationAside(page)).toContainText("v1");

  await editor.press("Escape");
  await page.keyboard.press("g");
  const dialog = page.getByRole("dialog", { name: "Mark as human-verified gold?" });
  await expect(dialog).toContainText(corrected);
  await dialog.getByRole("button", { name: "Confirm gold" }).click();
  await expect(toast(page)).toContainText("Saved v2 · corrected · gold");

  await page.reload();
  await expect(page.getByLabel(/Corrected transcript/)).toHaveValue(corrected);
  await expect(annotationAside(page)).toContainText("gold");
  const hypotheses = page.getByRole("table", { name: /Model hypotheses/ });
  await expect(hypotheses).toContainText("southwest four five six contact departure one one niner point four");
  await expect(hypotheses).not.toContainText("fifty");

  // The grid shows the human text, marked as human.
  await expect(row(page, "southwest four fifty six")).toContainText("gold");
});

test("unavailable source audio is a clear, recoverable error", async ({ page }) => {
  const filters = encodeURIComponent(JSON.stringify({ source_keys: ["offline_archive"] }));
  await page.goto(`/review?f=${filters}`);
  await row(page, "spirit five one seven").click();
  const error = page.getByRole("alert").filter({ hasText: "Source audio unavailable" });
  await expect(error).toContainText("not mounted");
  await expect(error.getByRole("button", { name: "Retry" })).toBeVisible();
  // Review still works without audio.
  await expect(page.getByLabel(/Corrected transcript/)).toBeEnabled();
  await expect(page.getByRole("table", { name: /Model hypotheses/ })).toContainText("spirit five one seven");
});

test("batch silver needs agreement; gold is never a batch action", async ({ page }) => {
  await page.goto("/review");
  await page.getByLabel(/Select segment/).first().waitFor();
  for (const text of ["American 2669", "united nine"]) {
    await row(page, text).getByRole("checkbox").check();
  }
  const bar = page.getByRole("region", { name: "Batch actions" });
  await expect(bar).toContainText("2 selected");
  await expect(bar.getByRole("button", { name: /gold/i })).toHaveCount(0);
  await bar.getByRole("button", { name: "Mark silver" }).click();
  await expect(toast(page)).toContainText("1 updated; skipped 1 no 2-family agreement");
  await expect(row(page, "American 2669")).toContainText("silver");
});

test("neighbor and airport context", async ({ page }) => {
  await page.goto("/review");
  await row(page, "southwest").click();
  const neighbors = page.getByRole("table", { name: "Neighbouring segments" });
  await expect(neighbors.getByRole("row").filter({ hasText: "prev" })).toContainText("American 2669");
  await expect(neighbors.getByRole("row").filter({ hasText: "next" })).toContainText("united nine");
  await expect(page.getByText("119.400 MHz")).toBeVisible();
  await expect(page.getByText("Baltimore Tower (LCL/P)")).toBeVisible();
  await page.getByText(/Airport context · KBWI/).click();
  await expect(page.getByRole("table", { name: "Runway ends" })).toContainText("runway three three left");
});

test("keyboard navigation, shortcut help and accessibility", async ({ page }) => {
  await page.goto("/review");
  await page.getByLabel(/Select segment/).first().waitFor();
  await page.keyboard.press("k");
  const first = page.url();
  await page.keyboard.press("k");
  await expect(page).not.toHaveURL(first);
  await page.keyboard.press("j");
  await expect(page).toHaveURL(first);

  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "critical" || v.impact === "serious");
  expect(serious.map((v) => `${v.id}: ${v.nodes.length} nodes`)).toEqual([]);
});
