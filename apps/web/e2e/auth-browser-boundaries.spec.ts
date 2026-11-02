import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const mockURL = `http://127.0.0.1:${process.env.PLAYWRIGHT_MOCK_PORT ?? "4100"}`;

async function signIn(page: Page, email: string) {
  await page
    .context()
    .addCookies([{ name: "locale", value: "en", url: appURL }]);
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

async function apiSessionCookie(context: BrowserContext) {
  const cookie = (await context.cookies(appURL)).find(
    (candidate) => candidate.name === "hr_api_session",
  );
  expect(cookie).toBeDefined();
  return cookie!;
}

async function authSessionCookie(context: BrowserContext) {
  const cookie = (await context.cookies(appURL)).find((candidate) =>
    candidate.name.includes("authjs.session-token"),
  );
  expect(cookie).toBeDefined();
  return cookie!;
}

async function expectLocalSessionCleared(context: BrowserContext) {
  await expect
    .poll(async () => {
      const cookies = await context.cookies(appURL);
      return {
        api: cookies.some((cookie) => cookie.name === "hr_api_session"),
        auth: cookies.some((cookie) =>
          cookie.name.includes("authjs.session-token"),
        ),
      };
    })
    .toEqual({ api: false, auth: false });
}

test("local sign out succeeds even when backend revocation is unavailable", async ({
  page,
  context,
}) => {
  await signIn(page, "admin+logout-failure@example.test");

  let signOut = page.getByRole("button", { name: "Sign out", exact: true });
  if (!(await signOut.isVisible())) {
    await page.getByRole("button", { name: "Main navigation" }).click();
    signOut = page.getByRole("button", { name: "Sign out", exact: true });
  }
  const [logoutResponse] = await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/auth/local-logout?scope=current"),
    ),
    signOut.click(),
  ]);
  expect(logoutResponse.status()).toBe(200);
  // Chromium consumes Clear-Site-Data on a navigation and omits it from the
  // Playwright header view; Firefox exposes it. Cookie removal below is the
  // cross-browser behavioral assertion.
  const clearSiteData = logoutResponse.headers()["clear-site-data"];
  if (clearSiteData) expect(clearSiteData).toContain('"cookies"');
  expect(logoutResponse.headers()["cache-control"]).toContain("no-store");
  expect(logoutResponse.headers()["content-security-policy"]).toContain(
    "default-src 'none'",
  );
  expect(logoutResponse.headers()["referrer-policy"]).toBe("no-referrer");
  await expect(page).toHaveURL(/\/api\/auth\/local-logout\?scope=current$/);
  await expect(page.getByRole("status")).toContainText(
    "This browser is signed out",
  );

  await expectLocalSessionCleared(context);

  await page.getByRole("link", { name: "Return to sign in" }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/candidates");
  await expect(page).toHaveURL(/\/login(?:\?|$)/);
});

test("local sign out remains reachable while refresh is unavailable", async ({
  page,
  context,
}) => {
  await signIn(page, "admin+logout-refresh-outage@example.test");
  await page.waitForTimeout(1_500);

  let signOut = page.getByRole("button", { name: "Sign out", exact: true });
  if (!(await signOut.isVisible())) {
    await page.getByRole("button", { name: "Main navigation" }).click();
    signOut = page.getByRole("button", { name: "Sign out", exact: true });
  }
  await signOut.click();

  await expect(page).toHaveURL(/\/api\/auth\/local-logout\?scope=current$/);
  await expect(page.getByRole("status")).toContainText(
    "This browser is signed out",
  );
  await expectLocalSessionCleared(context);
});

test("local logout rejects cross-origin posts without clearing the session", async ({
  page,
  context,
}) => {
  await signIn(page, "admin@example.test");
  const response = await context.request.post(
    `${appURL}/api/auth/local-logout?scope=current`,
    {
      headers: { Origin: "https://cross-origin.invalid" },
      maxRedirects: 0,
    },
  );
  expect(response.status()).toBe(403);
  expect(await apiSessionCookie(context)).toBeDefined();
  await page.goto("/dashboard?after=rejected-logout");
  await expect(page).toHaveURL(/\/dashboard\?after=rejected-logout$/);
});

test("local logout rejects non-navigation posts and GET requests", async ({
  page,
  context,
}) => {
  await signIn(page, "admin+logout-method-boundary@example.test");
  const scriptedPost = await context.request.post(
    `${appURL}/api/auth/local-logout?scope=current`,
    {
      headers: {
        Origin: appURL,
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
      },
      maxRedirects: 0,
    },
  );
  expect(scriptedPost.status()).toBe(403);
  const getResponse = await context.request.get(
    `${appURL}/api/auth/local-logout?scope=current`,
    { maxRedirects: 0 },
  );
  expect(getResponse.status()).toBe(405);
  expect(await apiSessionCookie(context)).toBeDefined();
  await page.goto("/dashboard?after=rejected-logout-methods");
  await expect(page).toHaveURL(/\/dashboard\?after=rejected-logout-methods$/);
});

test("ordinary requests never roll the browser identity cookie", async ({
  page,
  context,
}) => {
  await signIn(page, "admin+fixed-identity-session@example.test");
  const before = await authSessionCookie(context);

  await page.goto("/dashboard?identity=fixed");
  await expect(page).toHaveURL(/\/dashboard\?identity=fixed$/);
  await page.goto("/candidates?identity=fixed");
  await expect(page).toHaveURL(/\/candidates\?identity=fixed$/);
  const sessionResponse = await context.request.get(`${appURL}/api/auth/session`);
  expect(sessionResponse.status()).toBe(200);

  const after = await authSessionCookie(context);
  expect(after.name).toBe(before.name);
  expect(after.value).toBe(before.value);
  expect(after.expires).toBe(before.expires);
});

test("canonical legacy database UUIDs remain valid tenant identities", async ({
  page,
}) => {
  await signIn(page, "admin+legacy-org@example.test");
  await page.goto("/candidates?identity=legacy-database-uuid");
  await expect(page).toHaveURL(
    /\/candidates\?identity=legacy-database-uuid$/,
  );
  await expect(page.getByRole("heading", { name: "Candidates" })).toBeVisible();
});

test("sign out everywhere control invokes global revocation and clears locally", async ({
  page,
  context,
  request,
}) => {
  await request.post(`${mockURL}/__state/logout-requests`);
  await signIn(page, "admin@example.test");
  await page.goto("/settings?tab=security");
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "Sign out everywhere", exact: true })
    .click();
  await expect(page).toHaveURL(/\/api\/auth\/local-logout\?scope=all$/);
  await expect(page.getByRole("status")).toContainText(
    "Signed out on every device",
  );
  await expectLocalSessionCleared(context);

  const state = await request.get(`${mockURL}/__state/logout-requests`);
  const paths = (await state.json()).paths as string[];
  // Projects share one mock server and can reach this check concurrently.
  // Every observed request must be the global endpoint, and this action must
  // have contributed at least one request.
  expect(paths.length).toBeGreaterThan(0);
  expect(new Set(paths)).toEqual(new Set(["/auth/logout-all"]));
});

test("a settled refresh flight never authenticates replay of the old cookie", async ({
  page,
  context,
  request,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "chromium-desktop",
    "The server-side refresh race is exercised once; browser coverage comes from logout.",
  );
  const reset = await request.post(`${mockURL}/__state/refresh-attempts`);
  expect(reset.status()).toBe(200);
  await signIn(page, "refresh-replay@example.test");
  const oldCookie = await apiSessionCookie(context);

  // The fixture token starts 46 seconds from expiry and the BFF refreshes at
  // 45 seconds. Crossing the one-second boundary triggers exactly one rotate.
  await page.waitForTimeout(1_500);
  const concurrentPage = await context.newPage();
  await Promise.all([
    page.goto("/dashboard?refresh=first"),
    concurrentPage.goto("/candidates?refresh=concurrent"),
  ]);
  await expect(page).toHaveURL(/\/dashboard\?refresh=first$/);
  await expect(concurrentPage).toHaveURL(/\/candidates\?refresh=concurrent$/);
  await concurrentPage.close();
  const rotatedCookie = await apiSessionCookie(context);
  expect(rotatedCookie.value).not.toBe(oldCookie.value);

  let state = await request.get(`${mockURL}/__state/refresh-attempts`);
  expect((await state.json()).attempts).toBe(1);

  // Reinstall only the pre-rotation encrypted API cookie while retaining the
  // same Auth.js identity cookie. A completed-flight cache must not return R2.
  await context.addCookies([oldCookie]);
  await page.goto("/dashboard?refresh=replay");
  await expect(page).toHaveURL(/\/login\?expired=1$/);
  expect(
    (await context.cookies(appURL)).some(
      (cookie) => cookie.name === "hr_api_session",
    ),
  ).toBe(false);
  state = await request.get(`${mockURL}/__state/refresh-attempts`);
  expect((await state.json()).attempts).toBe(2);
});
