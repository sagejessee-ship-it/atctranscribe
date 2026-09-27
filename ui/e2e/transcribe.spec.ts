import { expect, test } from "@playwright/test";

// Transcribe page: choose a portion + models, preview, queue, control. The e2e
// stack has no worker, so a queued run stays queued (nothing is transcribed).

test("select a portion and models, preview, queue a run, pause and cancel it", async ({ page }) => {
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("/transcribe");
  const form = page.getByRole("form", { name: "New transcription run" });

  // Default: every enabled voting model is selected.
  for (const model of ["canary-e2e", "canary-e2e-beam", "parakeet-e2e", "whisper-e2e"]) {
    await expect(form.getByRole("checkbox", { name: `Run ${model}`, exact: true })).toBeChecked();
  }
  await form.getByRole("button", { name: "None" }).click();
  await form.getByRole("checkbox", { name: "Run parakeet-e2e", exact: true }).check();
  await form.getByRole("checkbox", { name: "Run whisper-e2e", exact: true }).check();

  await form.getByLabel("Day", { exact: true }).fill("2026-09-08");
  await form.getByRole("checkbox", { name: "TWR", exact: true }).check();
  const preview = form.getByRole("status").first();
  // 4 TWR segments on 2026-09-08; whisper has results on some but parakeet not on all.
  await expect(preview).toContainText("segments");
  await expect(preview).toContainText("2 models");
  const segments = Number((await preview.locator("strong").first().textContent())!.replace(/,/g, ""));
  expect(segments).toBeGreaterThan(0);

  await expect(form.getByRole("button", { name: "Queue transcription run" })).toBeDisabled();
  await form.getByRole("checkbox", { name: /Allow models not yet qualified/ }).check();
  await form.getByLabel("Run name (optional)").fill("e2e twr run");
  await form.getByRole("button", { name: "Queue transcription run" }).click();
  await expect(form.getByRole("status").filter({ hasText: "Queued run" })).toContainText(`${segments} segments × 2 models`);

  const run = page.getByRole("row").filter({ hasText: "e2e twr run" });
  await expect(run).toContainText("queued");
  await expect(run).toContainText("parakeet-e2e");
  await expect(run.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
  await run.getByRole("button", { name: "Pause" }).click();
  await expect(run).toContainText("paused");
  await run.getByRole("button", { name: "Resume" }).click();
  await expect(run).toContainText("queued");
  await run.getByRole("button", { name: "Cancel" }).click();
  await expect(run).toContainText("cancelled");
});
