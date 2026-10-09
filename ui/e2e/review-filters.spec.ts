import { expect, test, type Page } from "@playwright/test";

// The Models filter: readable entries, two match modes, and the (i) definitions.

const rows = (page: Page) => page.getByRole("grid").getByRole("row");
const modelsGroup = (page: Page) => page.getByRole("group", { name: "Models" });

test("model entries are readable: each on its own rows, with family and voting", async ({ page }) => {
  await page.goto("/review");
  const entries = modelsGroup(page).locator(".rail__checks").first().locator(".rail__check");
  await expect(entries.first()).toBeVisible();
  await expect(entries.filter({ hasText: "parakeet-e2e" })).toContainText("parakeet · voting");
  // No entry overlaps the next one (they used to be squeezed into one-line rows).
  const boxes = [];
  for (const entry of await entries.all()) boxes.push((await entry.boundingBox())!);
  for (let i = 1; i < boxes.length; i++) expect(boxes[i].y).toBeGreaterThanOrEqual(boxes[i - 1].y + boxes[i - 1].height - 0.5);
});

test("family filter: 'agrees' narrows to where the family is in the agreeing group", async ({ page }) => {
  await page.goto("/review");
  const group = modelsGroup(page);
  await group.getByRole("checkbox", { name: "whisper", exact: true }).check(); // the family
  // Has a result (default): every segment whisper transcribed, agreeing or not.
  await expect(rows(page).filter({ hasText: "American 2669" })).toHaveCount(1);
  await expect(rows(page).filter({ hasText: /united nine|jetblue nine/ })).toHaveCount(1);
  await expect(rows(page).filter({ hasText: "southwest" })).toHaveCount(0); // whisper didn't transcribe it

  await group.getByRole("radio", { name: "agrees" }).check();
  await expect(rows(page).filter({ hasText: /united nine|jetblue nine/ })).toHaveCount(0); // whisper heard "thank you"
  await expect(rows(page).filter({ hasText: "American 2669" })).toHaveCount(1); // 3 families agree exactly
});

test("the (i) buttons explain the columns and the filters", async ({ page }) => {
  await page.goto("/review");
  await page.getByRole("button", { name: "Help: Column guide" }).click();
  const guide = page.getByRole("dialog");
  for (const term of ["Mdl", "ExP", "ExF", "NrF", "NrSim"]) await expect(guide).toContainText(term);
  await expect(guide).toContainText("different architecture families");
  await page.keyboard.press("Escape");
  await expect(guide).toHaveCount(0);

  await page.getByRole("button", { name: "Help: Agreement" }).click();
  await expect(page.getByRole("dialog")).toContainText("Near similarity");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Help: Models" }).click();
  await expect(page.getByRole("dialog")).toContainText("Only voting models can agree");
  // Hovering an abbreviated header shows its definition too.
  await expect(page.getByRole("columnheader", { name: "ExF" })).toHaveAttribute("title", /ExF = Exact families/);
});
