import { defineConfig, devices } from "@playwright/test";

/**
 * Local-only config for the layout check: it targets the already-running dev
 * stack (vite on 5173 + backend on 8000) instead of building and serving the
 * production bundle, so no webServer is started here.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  testMatch: "layout-check.spec.ts",
  use: {
    baseURL: process.env.LAYOUT_BASE ?? "http://127.0.0.1:5173",
    // No trace: this is a quick local visual check, and the screenshots in
    // tests/e2e/artifacts/ are the artefact worth keeping.
    trace: "off",
    locale: "zh-CN",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chromium" } }],
});
