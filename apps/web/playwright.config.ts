import { createHash } from "node:crypto";

import { defineConfig, devices } from "@playwright/test";

const appPort = Number(process.env.PLAYWRIGHT_APP_PORT ?? "3100");
const httpsPort = Number(process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443");
const mockPort = Number(process.env.PLAYWRIGHT_MOCK_PORT ?? "4100");
const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ?? `https://localhost:${httpsPort}`;
const internalAppURL = `http://127.0.0.1:${appPort}`;
const mockURL = `http://127.0.0.1:${mockPort}`;

function fixtureSecret(scope: string) {
  return createHash("sha512")
    .update(`public-playwright-fixture:${scope}`)
    .digest("base64url");
}

const authSecret = fixtureSecret("auth");
const apiSessionSecret = fixtureSecret("api-session");
const assessmentSessionSecret = fixtureSecret("assessment-session");

export default defineConfig({
  testDir: "./e2e",
  // The isolated real-stack journey owns its own compose lifecycle, ports,
  // teardown, timeouts, and browser matrix. Never collect it into the mocked
  // page suite, where it would target an API process that is intentionally not
  // running and make the ordinary CI job nondeterministic.
  testIgnore: "real-stack/**",
  outputDir: "./output/playwright/test-results",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: process.env.CI ? 1 : 2,
  timeout: 40_000,
  expect: { timeout: 8_000 },
  reporter: process.env.CI
    ? [
        ["line"],
        ["html", { outputFolder: "output/playwright/report", open: "never" }],
      ]
    : "list",
  use: {
    baseURL,
    locale: "en-US",
    timezoneId: "UTC",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ignoreHTTPSErrors: true,
  },
  webServer: [
    {
      command: "node e2e/fixtures/mock-api.mjs",
      url: `${mockURL}/__health`,
      env: { MOCK_API_PORT: String(mockPort) },
      reuseExistingServer: false,
      timeout: 20_000,
    },
    {
      command: `pnpm start --port ${appPort}`,
      url: `${internalAppURL}/login`,
      env: {
        AUTH_SECRET: authSecret,
        NEXTAUTH_SECRET: authSecret,
        AUTH_SESSION_SECRET: apiSessionSecret,
        ASSESSMENT_SESSION_SECRET: assessmentSessionSecret,
        NEXTAUTH_URL: baseURL,
        API_BASE_URL: mockURL,
        NEXT_PUBLIC_API_BASE_URL: mockURL,
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    ...(process.env.PLAYWRIGHT_BASE_URL
      ? []
      : [
          {
            command: "node e2e/fixtures/https-proxy.mjs",
            url: `${baseURL}/login`,
            env: {
              PLAYWRIGHT_HTTPS_PORT: String(httpsPort),
              PLAYWRIGHT_PROXY_TARGET: internalAppURL,
            },
            reuseExistingServer: false,
            timeout: 30_000,
            ignoreHTTPSErrors: true,
          },
        ]),
  ],
  projects: [
    {
      name: "chromium-desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
    {
      name: "chromium-mobile-320",
      use: {
        browserName: "chromium",
        viewport: { width: 320, height: 568 },
        hasTouch: true,
        deviceScaleFactor: 2,
      },
    },
    {
      name: "firefox-desktop",
      use: {
        ...devices["Desktop Firefox"],
        viewport: { width: 1024, height: 768 },
      },
    },
    {
      name: "chromium-tablet-dark-reduced",
      use: {
        browserName: "chromium",
        viewport: { width: 768, height: 1024 },
        hasTouch: true,
        colorScheme: "dark",
        contextOptions: { reducedMotion: "reduce" },
      },
    },
    // The Playwright 1.62 WebKit binary currently exits with SIGSEGV before
    // creating a page on this macOS runner. Linux remains the release
    // authority: both this mocked matrix and the isolated real-stack workflow
    // execute WebKit on Ubuntu CI. Keep a force switch for local diagnosis.
    ...(process.platform === "darwin" &&
    process.env.PLAYWRIGHT_FORCE_WEBKIT !== "1"
      ? []
      : [
          {
            name: "webkit-mobile",
            use: {
              browserName: "webkit" as const,
              viewport: { width: 390, height: 844 },
              hasTouch: true,
              deviceScaleFactor: 2,
            },
          },
        ]),
  ],
});
