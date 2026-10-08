import { defineConfig, devices } from "@playwright/test";

/** Production-bundle browser checks for authorized reads and account product scope. */
export default defineConfig({
  testDir: "./tests/e2e",
  // layout-check.spec.ts 需要真实的本地 dev 栈（后端 sqlite + vite），由
  // playwright.layout.config.ts 单独驱动，不随本套件运行。
  testIgnore: "**/layout-check.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    locale: "zh-CN",
  },
  projects: [
    {
      name: "chromium",
      // 用完整 Chromium 而非 headless shell：本机 headless shell 二进制因
      // 网络下载问题未安装，完整 Chromium 已就绪，headed 模式同样满足测试。
      use: { ...devices["Desktop Chrome"], channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL ?? "chromium" },
    },
  ],
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    port: 4173,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
