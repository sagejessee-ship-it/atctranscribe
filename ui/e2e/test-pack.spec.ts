import { expect, test, type Page } from "@playwright/test";

// Selected segments -> one zip (audio + chat-ready adjudication prompt each), saved by
// the browser. Read-only: nothing in the shared database changes.

const row = (page: Page, text: string) => page.getByRole("grid").getByRole("row").filter({ hasText: text });

test("download selected segments with their adjudication prompts", async ({ page }) => {
  await page.goto("/review");
  for (const text of ["American 2669", "united nine"]) {
    await row(page, text).getByRole("checkbox").check();
  }
  const bar = page.getByRole("region", { name: "Batch actions" });
  const downloading = page.waitForEvent("download");
  await bar.getByRole("button", { name: "Download for testing" }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/^aerochorus-test-pack-2-segments-\d{8}-\d{6}\.zip$/);
  const path = await download.path();
  expect(path).toBeTruthy();
  await expect(page.locator(".toast")).toContainText("2 segments (audio + prompt)");
  // The zip's contents (audio bytes, prompts, manifest) are checked in tests/integration/test_edge.py.
  const pack = await page.request.post("/edge/test-pack", { data: { segment_ids: [1] } });
  expect(pack.headers()["content-type"]).toBe("application/zip");
});
