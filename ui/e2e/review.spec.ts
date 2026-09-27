import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Runs against tests/e2e/stack.py: a fresh database with a synthetic BWI corpus.
// Tests share that database and run in file order (workers: 1).

const toast = (page: Page) => page.locator(".toast");
const row = (page: Page, text: string) => page.getByRole("grid").getByRole("row").filter({ hasText: text });
const annotationAside = (page: Page) => page.locator("#annotation-title").locator("..");
const playButton = (page: Page) => page.getByRole("button", { name: /^(Play|Pause) \(Space\)$/ });

/** Current playback position in seconds, from the player's "m:ss.s / m:ss.s" readout. */
async function position(page: Page): Promise<number> {
  const text = (await page.getByTestId("player-time").textContent()) ?? "";
  const [m, s] = text.split("/")[0].trim().split(":");
  return Number(m) * 60 + Number(s);
}

async function audioReady(page: Page) {
  await expect(playButton(page)).toBeEnabled({ timeout: 10_000 });
}

test("high-agreement review: listen, accept, mark silver, move next", async ({ page }) => {
  await page.goto("/review");
  await page.getByLabel("Saved view").selectOption("exact-2-families");
  await expect(page.locator(".count")).toHaveText(/^3 segments/);

  await row(page, "delta one two three").click();
  await expect(page.getByRole("heading", { level: 2 })).toContainText("GND");
  await audioReady(page);
  await page.keyboard.press("Space");
  await expect.poll(() => position(page)).toBeGreaterThan(0);

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

test("partial gold: select a waveform span, loop it, transcribe, confirm gold; survives reload", async ({ page }) => {
  await page.goto("/review");
  await row(page, "united nine").click();
  await audioReady(page);

  // Drag-select on the waveform, then set exact bounds.
  const wave = page.getByTestId("waveform");
  const box = (await wave.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.2, box.y + box.height * 0.4);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.4, { steps: 6 });
  await page.mouse.up();
  await expect(page.getByTestId("selection-readout")).toBeVisible();
  const editor = page.locator(".span-editor");
  await editor.getByLabel("Span start (ms)").fill("300");
  await editor.getByLabel("Span end (ms)").fill("900");
  await expect(page.getByTestId("selection-readout")).toContainText("0.30–0.90s");

  // Loop the selection: playback stays inside the span.
  await page.getByRole("button", { name: /Loop the selection/ }).click();
  await playButton(page).click();
  const seen: number[] = [];
  for (let i = 0; i < 8; i++) {
    await page.waitForTimeout(200);
    seen.push(await position(page));
  }
  await playButton(page).click(); // pause
  expect(seen.some((t) => t > 0.3)).toBe(true);
  expect(seen.every((t) => t >= 0.25 && t <= 0.95), `positions ${seen}`).toBe(true);

  await editor.getByLabel("Span transcript").fill("united nine cleared to land");
  await editor.getByRole("radio", { name: "Gold" }).click();
  await editor.getByRole("button", { name: "Save new span" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm gold span" }).click();
  await expect(toast(page)).toContainText("Span 0.30–0.90s saved v1 · gold");

  // The whole segment itself is rejected: gold span + non-gold parent coexist.
  await page.getByRole("grid").focus();
  await page.keyboard.press("x");
  await expect(toast(page)).toContainText("Saved v1 · reviewed · rejected");

  await page.reload();
  const spans = page.getByRole("table", { name: "Span annotations" });
  await expect(spans).toContainText("0.30–0.90");
  await expect(spans).toContainText("united nine cleared to land");
  await expect(spans).toContainText("gold");
  await spans.getByRole("button", { name: /Select span 0.30 to 0.90/ }).click();
  await expect(page.getByTestId("selection-readout")).toContainText("0.30–0.90s");

  await page.getByLabel("Saved view").selectOption("partial-usable");
  await expect(page.locator(".count")).toHaveText(/^1 segment$/);
  await expect(row(page, "united nine")).toContainText("rejected");
});

test("training page: freeze a dataset version from the labels", async ({ page }) => {
  await page.goto("/training");
  const labelled = page.locator("#labelled-title").locator("../..");
  await expect(labelled).toContainText("gold");
  const form = page.getByRole("form", { name: "Create dataset version" });
  await form.getByRole("checkbox", { name: "silver" }).check();
  await form.getByRole("button", { name: "Freeze new version" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Froze bwi-atc v1" })).toContainText("items");
  const table = page.getByRole("table", { name: "Dataset versions" });
  await expect(table).toContainText("bwi-atc");
  await expect(table).toContainText("aerochorus dataset export");
});
