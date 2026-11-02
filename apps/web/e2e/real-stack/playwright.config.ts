import { defineConfig, devices } from "@playwright/test";

const apiPort = Number(process.env.REAL_STACK_API_PORT ?? "8300");
const webPort = Number(process.env.REAL_STACK_WEB_PORT ?? "3300");
const httpsPort = Number(process.env.REAL_STACK_HTTPS_PORT ?? "3643");
const baseURL = `https://localhost:${httpsPort}`;
const apiURL = `http://127.0.0.1:${apiPort}`;

export default defineConfig({
  testDir: ".",
  testMatch: "golden-journey.spec.ts",
  globalTeardown: "./global-teardown.cjs",
  outputDir: "../../output/playwright/real-stack-results",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI
    ? [
        ["line"],
        [
          "html",
          {
            outputFolder: "../../output/playwright/real-stack-report",
            open: "never",
          },
        ],
      ]
    : "list",
  use: {
    baseURL,
    locale: "en-US",
    timezoneId: "UTC",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  webServer: [
    {
      command: "node ../fixtures/real-stack/stack.mjs",
      url: `${apiURL}/public/positions/80000000-0000-4000-8000-000000000101`,
      env: {
        REAL_STACK_API_PORT: String(apiPort),
        REAL_STACK_WEB_PORT: String(webPort),
        REAL_STACK_HTTPS_PORT: String(httpsPort),
      },
      reuseExistingServer: false,
      timeout: 660_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: "node ../fixtures/real-stack/https-proxy.mjs",
      url: `${baseURL}/login`,
      env: {
        REAL_STACK_HTTPS_PORT: String(httpsPort),
        REAL_STACK_PROXY_TARGET: `http://127.0.0.1:${webPort}`,
      },
      reuseExistingServer: false,
      timeout: 660_000,
      ignoreHTTPSErrors: true,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
  projects: [
    {
      name: "chromium-real-stack",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
    {
      name: "firefox-real-stack",
      use: {
        ...devices["Desktop Firefox"],
        viewport: { width: 1280, height: 800 },
      },
    },
    {
      name: "webkit-linux-real-stack",
      use: {
        browserName: "webkit",
        viewport: { width: 390, height: 844 },
        hasTouch: true,
        deviceScaleFactor: 2,
      },
    },
  ],
});
