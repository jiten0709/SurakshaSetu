import { defineConfig } from "@playwright/test";

// Step 30's browser smoke test (`make ui-e2e`, outside `make check`): Chromium against the dev server,
// which proxies /v1 to the API on :8000 (`make serve`). A running `make ui` is reused; otherwise
// Playwright starts the dev server and stops it afterwards. Failures name a step, never message text
// (e2e/smoke.e2e.ts), and no trace, screenshot or video is kept.
export default defineConfig({
  testDir: "e2e",
  testMatch: "*.e2e.ts", // not *.test.ts or *.spec.ts: vitest must never collect it
  outputDir: ".cache/playwright", // gitignored
  workers: 1,
  retries: 0,
  reporter: "list",
  timeout: 300_000, // ~30 turns paced for the API's rate limit, S3 on CPU retrieval
  expect: { timeout: 30_000 },
  use: {
    baseURL: "http://127.0.0.1:5173",
    browserName: "chromium",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  webServer: { command: "npm run dev", url: "http://127.0.0.1:5173", reuseExistingServer: true },
});
