import { expect, test } from "@playwright/test";

// Agreement v2: an utterance agreed by 2+ families inside a segment whose whole
// transcript disagrees ("united nine cleared to land" vs "jetblue nine is cleared to land").

test("partial agreement: highlighted, and U starts a prefilled span", async ({ page }) => {
  await page.goto("/review");
  await page.getByLabel("Saved view").selectOption("partial-agreement");
  const row = page.getByRole("grid").getByRole("row").filter({ hasText: "united nine" });
  await expect(row).toBeVisible();
  await expect(row.locator("mark.agree-mark")).toHaveText("cleared to land");
  await expect(row).toContainText("U2");
  await row.click();

  const hypotheses = page.getByRole("table", { name: /Model hypotheses/ });
  await expect(hypotheses.locator("mark.agree-mark").first()).toHaveText("cleared to land");
  const utterances = page.getByRole("table").filter({ hasText: "Agreed utterances" });
  await expect(utterances).toContainText("cleared to land");

  await expect(page.getByRole("button", { name: /^(Play|Pause) \(Space\)$/ })).toBeEnabled({ timeout: 10_000 });
  await page.getByRole("grid").focus();
  await page.keyboard.press("u");
  const editor = page.locator(".span-editor");
  await expect(editor.getByLabel("Span transcript")).toHaveValue("cleared to land");
  await expect(page.getByTestId("selection-readout")).toBeVisible();

  await editor.getByLabel("Span transcript").fill("is cleared to land");
  await editor.getByRole("radio", { name: "Silver" }).click();
  await editor.getByRole("button", { name: "Save new span" }).click();
  await expect(page.locator(".toast")).toContainText("saved v1 · silver");
});
