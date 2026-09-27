import { defineConfig } from "@playwright/test";
import path from "node:path";

// The stack (tests/e2e/stack.py) recreates the aerochorus_e2e database, seeds a
// synthetic corpus and serves the built UI + API + audio on one port.
const port = Number(process.env.AEROCHORUS_E2E_PORT ?? 8765);
const repo = path.resolve(import.meta.dirname, "..");
const python =
  process.env.AEROCHORUS_E2E_PYTHON ??
  path.join(repo, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
// Use an installed Edge/Chrome when present (no browser download); else `npx playwright install chromium`.
const channel = process.env.PW_CHANNEL ?? (process.platform === "win32" ? "msedge" : undefined);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1, // one shared database
  timeout: 30_000,
  expect: { timeout: 7_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    launchOptions: { args: ["--autoplay-policy=no-user-gesture-required", "--mute-audio"] },
  },
  webServer: {
    command: `"${python}" "${path.join(repo, "tests", "e2e", "stack.py")}" --port ${port}`,
    url: `http://127.0.0.1:${port}/edge/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "pipe",
  },
});
