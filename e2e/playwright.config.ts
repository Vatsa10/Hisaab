import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end run of the whole product on this machine: PWA -> API -> job queue -> fake worker ->
 * Postgres -> Realtime -> PWA. scripts/e2e_local.sh starts the stack and then runs this; on its
 * own it expects the PWA at E2E_BASE_URL (default http://localhost:3110) with the API and the
 * fake worker behind it.
 *
 * One Playwright worker: the stack has one job worker, which handles one job at a time, so tests
 * running side by side would only queue behind each other's OTP prompts.
 */
const baseURL = process.env.E2E_BASE_URL || "http://localhost:3110";

export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  // A connect runs through the queue, the worker and Realtime; each step is waited for, never slept on.
  timeout: 180_000,
  expect: { timeout: 30_000 },
  reporter: [["list"], ["html", { outputFolder: "./playwright-report", open: "never" }]],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
    locale: "en-IN",
  },
  projects: [
    {
      // Safari on an iPhone 14: 390x844 viewport, touch, mobile user agent, WebKit.
      name: "iphone-14",
      use: { ...devices["iPhone 14"] },
    },
  ],
});
