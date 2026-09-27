import { expect, test, type Page } from "@playwright/test";

// Model adjudication (ADR-022). The stack prices with fixed test prices and has no
// runner; the test plays the runner through the real claim/result endpoints.

const row = (page: Page, text: string) => page.getByRole("grid").getByRole("row").filter({ hasText: text });
const toast = (page: Page) => page.locator(".toast");
const TRANSCRIPT = "roger delta one two three";

test("price, confirm and queue a batch; review the answer; accept it as silver", async ({ page }) => {
  await page.goto("/review");
  await row(page, "roger").getByRole("checkbox").check();
  await page.getByRole("region", { name: "Batch actions" }).getByRole("button", { name: "Adjudicate…" }).click();

  const dialog = page.getByRole("dialog", { name: "Adjudicate with Gemini" });
  const plan = dialog.getByTestId("adjudication-plan");
  await expect(plan).toContainText("~google/gemini-pro-latest");
  await expect(plan).toContainText("1 of 1 segments");
  await expect(plan).toContainText("e2e fixed prices");
  await expect(dialog.getByRole("status")).toContainText("No adjudication runner has checked in");
  const send = dialog.getByRole("button", { name: "Send 1 to Gemini" });
  await expect(send).toBeDisabled(); // nothing is sent without the explicit confirmation
  await dialog.getByRole("checkbox", { name: /Send 1 clips and their transcripts to OpenRouter/ }).check();
  await send.click();
  await expect(toast(page)).toContainText(/Adjudication batch #\d+ queued: 1 segments/);

  // Play the runner (it would read the audio and call OpenRouter).
  const claims = await (await page.request.post("/api/v1/adjudication-items/claim", { data: { runner: "e2e/runner", limit: 4 } })).json();
  expect(claims).toHaveLength(1);
  expect(claims[0].bundle.hypotheses.map((h: { text: string }) => h.text)).toContain("roger");
  const result = await page.request.post(`/api/v1/adjudication-items/${claims[0].item_id}/result`, {
    data: {
      runner: "e2e/runner",
      status: "done",
      transcript: TRANSCRIPT,
      result: { speech_present: true, confidence: 0.93, uncertain_words: ["three"], callsigns: ["DAL123"], notes: "" },
      usage: { cost_source: "openrouter" },
      cost_usd: 0.0123,
    },
  });
  expect(result.ok()).toBeTruthy();

  // The inspector shows it; the human can take it into the editor or accept it as silver.
  await row(page, "roger").click();
  const panel = page.locator("#adjudication-title").locator("../..");
  await expect(panel).toContainText(TRANSCRIPT);
  await expect(panel).toContainText("conf 0.93");
  await expect(panel).toContainText("callsigns DAL123");
  await panel.getByRole("button", { name: "Use as correction" }).click();
  await expect(page.getByLabel(/Corrected transcript/)).toHaveValue(TRANSCRIPT);
  await panel.getByRole("button", { name: "Accept as silver" }).click();
  await expect(toast(page)).toContainText("Accepted as silver");
  await expect(panel).toContainText("accepted as silver");
  await expect(row(page, "roger")).toContainText("silver");

  // The batches page: progress, spend against the cap, the runner that answered.
  await page.goto("/adjudicate");
  const batches = page.getByRole("table", { name: "Adjudication batches" });
  await expect(batches).toContainText("done");
  await expect(batches).toContainText("$0.0123");
  await expect(page.getByRole("table", { name: /Items of batch/ })).toContainText(TRANSCRIPT);
  await expect(page.getByText(/Runner: e2e\/runner/)).toBeVisible();
});

test("a filter sample is priced before anything is queued, and the cap is bounded", async ({ page }) => {
  await page.goto("/review");
  await page.getByRole("button", { name: "Adjudicate…" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Adjudicate with Gemini" });
  await dialog.getByLabel("Random sample N").fill("2");
  await expect(dialog.getByTestId("adjudication-plan")).toContainText("of 2 segments");
  await dialog.getByLabel("Cost cap (USD)").fill("500");
  await expect(dialog.getByRole("checkbox", { name: /Send .* clips/ })).toBeDisabled(); // above the batch limit
  await dialog.getByRole("button", { name: "Cancel" }).click();
  const batches = await (await page.request.get("/api/v1/adjudications")).json();
  expect(batches.filter((b: { item_count: number }) => b.item_count === 2)).toHaveLength(0);
});
