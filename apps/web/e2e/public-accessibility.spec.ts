import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const mockURL = `http://127.0.0.1:${process.env.PLAYWRIGHT_MOCK_PORT ?? "4100"}`;
const consentQuestionId = "10000000-0000-4000-8000-000000000003";
const noteQuestionId = "10000000-0000-4000-8000-000000000004";
const singleQuestionId = "10000000-0000-4000-8000-000000000010";
const multiQuestionId = "10000000-0000-4000-8000-000000000011";
const numericQuestionId = "10000000-0000-4000-8000-000000000008";
const longQuestionId = "10000000-0000-4000-8000-000000000012";
const urlQuestionId = "10000000-0000-4000-8000-000000000013";
const fileQuestionId = "10000000-0000-4000-8000-000000000014";
const brandingOrganizationId = "40000000-0000-4000-8000-000000000001";
const brandingVersionId = "40000000-0000-4000-8000-000000000002";
const readOnlyPositionId = "50000000-0000-4000-8000-000000000003";
const brandingProxyPath = `/api/public/branding/${brandingOrganizationId}/${brandingVersionId}.png`;
const pixelPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

type AssessmentFixtureState = {
  events: Array<{
    type: "patch" | "submit";
    received_at_ms: number;
    answers?: Array<{ question_id: string; value: unknown }>;
  }>;
  answers: Array<{ question_id: string; value: unknown }>;
};

async function setLocale(page: Page, locale: "en" | "ru") {
  await page
    .context()
    .addCookies([{ name: "locale", value: locale, url: appURL }]);
}

async function signInAsReadOnlyInterviewer(page: Page) {
  await setLocale(page, "en");
  await page.goto("/login");
  await page.getByLabel("Email").fill("interviewer@example.test");
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

async function expectNoMaterialAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  const material = results.violations
    .filter(
      (violation) =>
        violation.impact === "serious" || violation.impact === "critical",
    )
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      help: violation.help,
      targets: violation.nodes.map((node) => node.target.join(" ")),
    }));
  expect(material).toEqual([]);
}

async function expectNoHorizontalOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
}

async function assessmentFixtureState(
  page: Page,
  token: string,
): Promise<AssessmentFixtureState> {
  const response = await page.request.get(
    `${mockURL}/__state/assessments/${encodeURIComponent(token)}`,
  );
  expect(response.status()).toBe(200);
  return (await response.json()) as AssessmentFixtureState;
}

function expectBaselineSecurityHeaders(headers: Record<string, string>) {
  expect(headers["content-security-policy"]).toContain(
    "frame-ancestors 'none'",
  );
  expect(headers["strict-transport-security"]).toContain("max-age=63072000");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["x-frame-options"]).toBe("DENY");
  expect(headers["x-powered-by"]).toBeUndefined();
}

function expectPrivateTokenHeaders(headers: Record<string, string>) {
  expect(headers["cache-control"]).toContain("no-store");
  expect(headers["referrer-policy"]).toBe("no-referrer");
  expect(headers["x-robots-tag"]).toContain("noindex");
}

async function expectPublicCapabilityError(
  page: Page,
  options: {
    path: string;
    status: 404 | 410 | 503;
    title: string;
    locale?: "en" | "ru";
    audit?: boolean;
  },
) {
  const locale = options.locale ?? "en";
  await setLocale(page, locale);
  const response = await page.goto(options.path);
  expect(response?.status()).toBe(options.status);
  expect(response?.url()).toBe(new URL(options.path, appURL).toString());
  const headers = response?.headers() ?? {};
  expectBaselineSecurityHeaders(headers);
  expectPrivateTokenHeaders(headers);
  expect(headers["content-language"]).toBe(locale);
  expect(headers.location).toBeUndefined();
  if (options.status === 503) expect(headers["retry-after"]).toBe("30");

  await expect(page.locator("html")).toHaveAttribute("lang", locale);
  await expect(
    page.getByRole("heading", { level: 1, name: options.title }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toHaveText(String(options.status));
  const token = decodeURIComponent(options.path.split("/").at(-1) ?? "");
  expect(await page.locator("body").innerText()).not.toContain(token);
  await expectNoHorizontalOverflow(page);
  if (options.audit) await expectNoMaterialAxeViolations(page);
}

test("authentication boundary is fail-closed and login is keyboard accessible", async ({
  page,
}) => {
  if (test.info().project.name === "chromium-tablet-dark-reduced") {
    await expect
      .poll(() =>
        page.evaluate(() => ({
          dark: matchMedia("(prefers-color-scheme: dark)").matches,
          reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
        })),
      )
      .toEqual({ dark: true, reducedMotion: true });
  }
  await setLocale(page, "en");
  const protectedResponse = await page.request.get("/dashboard", {
    headers: { "x-hr-api-access": "forged-browser-token" },
    maxRedirects: 0,
  });
  expect(protectedResponse.status()).toBeGreaterThanOrEqual(300);
  expect(protectedResponse.status()).toBeLessThan(400);
  const loginRedirect = new URL(
    protectedResponse.headers().location ?? "",
    appURL,
  );
  expect(loginRedirect.origin).toBe(new URL(appURL).origin);
  expect(loginRedirect.pathname).toBe("/login");

  const crossOriginAssessment = await page.request.post(
    "/api/assessment/attempt",
    { headers: { Origin: "https://attacker.invalid" } },
  );
  expect(crossOriginAssessment.status()).toBe(403);
  expect(crossOriginAssessment.headers()["cache-control"]).toContain(
    "no-store",
  );
  expect(crossOriginAssessment.headers()["referrer-policy"]).toBe(
    "no-referrer",
  );

  const response = await page.goto("/login");
  expect(response?.status()).toBe(200);
  const headers = response?.headers() ?? {};
  expectBaselineSecurityHeaders(headers);
  expectPrivateTokenHeaders(headers);

  await expect(page.getByRole("main", { name: "Sign in" })).toBeVisible();
  await expect(
    page.getByRole("heading", { level: 1, name: "Sign in" }),
  ).toBeVisible();
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByLabel("Password")).toBeVisible();

  await page.getByLabel("Email").focus();
  await page.keyboard.press("Shift+Tab");
  await expect(
    page.getByRole("link", { name: "Skip to main content" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);
});

test("screen-reader landmarks and 200/400 percent reflow remain complete", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "chromium-desktop",
    "One desktop browser exercises the shared assistive-technology contract.",
  );

  await setLocale(page, "en");
  for (const effectiveZoom of [
    { percent: 200, width: 640, height: 450 },
    { percent: 400, width: 320, height: 568 },
  ]) {
    await page.setViewportSize({
      width: effectiveZoom.width,
      height: effectiveZoom.height,
    });
    const response = await page.goto("/login");
    expect(response?.status()).toBe(200);

    const main = page.getByRole("main", { name: "Sign in" });
    await expect(main).toBeVisible();
    await expect(
      main.getByRole("heading", { level: 1, name: "Sign in" }),
    ).toBeVisible();
    await expect(main.getByRole("textbox", { name: "Email" })).toBeVisible();
    await expect(main.getByLabel("Password")).toBeVisible();
    await expect(
      main.getByRole("link", { name: "Forgot password?" }),
    ).toBeVisible();
    await expect(
      main.getByRole("button", { name: "Sign in", exact: true }),
    ).toBeVisible();

    const accessibilityTree = await page.locator("body").ariaSnapshot();
    expect(accessibilityTree).toContain('main "Sign in"');
    expect(accessibilityTree).toContain('heading "Sign in" [level=1]');
    expect(accessibilityTree).toContain('textbox "Email"');
    expect(accessibilityTree).toContain('textbox "Password"');
    expect(accessibilityTree).toContain('link "Forgot password?"');
    expect(accessibilityTree).toContain('button "Sign in"');

    const email = main.getByRole("textbox", { name: "Email" });
    const password = main.getByLabel("Password");
    await email.focus();
    await page.keyboard.press("Tab");
    await expect(password).toBeFocused();

    await expectNoHorizontalOverflow(page);
    await expectNoMaterialAxeViolations(page);
    await testInfo.attach(`effective-${effectiveZoom.percent}-percent-reflow`, {
      body: Buffer.from(
        JSON.stringify({
          effective_zoom_percent: effectiveZoom.percent,
          viewport: {
            width: effectiveZoom.width,
            height: effectiveZoom.height,
          },
          horizontal_overflow: false,
          screen_reader_semantics: true,
        }),
      ),
      contentType: "application/json",
    });
  }
});

test("read-only interviewer sees hiring data without mutation surfaces", async ({
  page,
}) => {
  await signInAsReadOnlyInterviewer(page);

  const candidatesResponse = await page.goto("/candidates");
  expect(candidatesResponse?.status()).toBe(200);
  await expect(
    page.getByRole("heading", { level: 1, name: "Candidates" }),
  ).toBeVisible();
  const readOnlyCandidateLink = page.getByRole("link", {
    name: /read-only-candidate@example\.test/,
  });
  await expect(readOnlyCandidateLink).toHaveCount(1);
  await expect(readOnlyCandidateLink).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add candidate", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Bulk import", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Select visible candidates" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", {
      name: "Select read-only-candidate@example.test",
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save view", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Favorite", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Unfavorite", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Delete", exact: true }),
  ).toHaveCount(0);
  for (const label of [
    "Add tag",
    "Remove tag",
    "Move",
    "Reject",
    "Merge duplicates",
    "Export CSV",
  ]) {
    await expect(
      page.getByRole("button", { name: label, exact: true }),
    ).toHaveCount(0);
  }
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  const dashboardResponse = await page.goto("/dashboard");
  expect(dashboardResponse?.status()).toBe(200);
  await expect(
    page.getByRole("heading", { level: 1, name: "Positions" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: /Read-only Platform Engineer/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "New position", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "New project", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "New folder", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Folder actions", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Move position", exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Search and actions" }).click();
  const palette = page.getByRole("dialog", { name: "Search and actions" });
  await expect(palette).toBeVisible();
  await expect(
    palette.getByRole("option", { name: /^New position$/ }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  for (const deniedPath of ["/positions/new", "/positions/project"]) {
    const denied = await page.goto(deniedPath);
    expect(denied?.status()).toBe(404);
    await expect(page.getByText("404", { exact: true }).first()).toBeVisible();
  }

  const positionResponse = await page.goto(
    `/positions/${readOnlyPositionId}?edit=1`,
  );
  expect(positionResponse?.status()).toBe(200);
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Read-only Platform Engineer",
    }),
  ).toBeVisible();
  await expect(readOnlyCandidateLink).toHaveCount(1);
  await expect(readOnlyCandidateLink).toBeVisible();
  for (const label of [
    "Bulk import",
    "Share link",
    "Share shortlist",
    "Edit",
    "Actions",
    "Re-score all candidates",
    "Add a candidate",
    "Favorite",
    "Unfavorite",
    "Open chat",
    "Generate",
    "Regenerate",
  ]) {
    await expect(
      page.getByRole("button", { name: label, exact: true }),
    ).toHaveCount(0);
  }
  await expect(page.getByRole("tablist", { name: "View" })).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  await readOnlyCandidateLink.click();
  await expect(page).toHaveURL(new RegExp(`/candidates/[0-9a-f-]+`));
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: /read-only-candidate@example\.test/,
    }),
  ).toBeVisible();
  for (const label of [
    "Delete",
    "Favorite",
    "Unfavorite",
    "+ tag",
    "Add recording",
    "New task",
    "Open chat",
  ]) {
    await expect(
      page.getByRole("button", { name: label, exact: true }),
    ).toHaveCount(0);
  }
  await expect(
    page.getByRole("button", { name: /^Score all \(/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Score", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save", exact: true }),
  ).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);
});

test("public application completes in RU and EN with focused validation", async ({
  page,
}) => {
  await setLocale(page, "ru");
  const response = await page.goto("/apply/e2e-position");
  expect(response?.status()).toBe(200);
  expectBaselineSecurityHeaders(response?.headers() ?? {});
  expectPrivateTokenHeaders(response?.headers() ?? {});

  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  await expect(
    page.getByRole("heading", { level: 2, name: "Откликнуться на вакансию" }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  const language = page.getByRole("group", { name: "Сменить язык" });
  await language.getByRole("button", { name: "EN" }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(
    page.getByRole("heading", { level: 2, name: "Apply for this role" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Submit", exact: true }).click();
  const name = page.getByPlaceholder("Jane Doe");
  await expect(name).toBeFocused();
  await expect(name).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#apply-form-error")).toContainText(
    "Please enter your name",
  );

  await name.fill("Alex Example");
  await page.getByPlaceholder("+992 90 123 4567").fill("+992 90 123 4567");
  await page.getByPlaceholder("jane@example.com").fill("alex@example.test");
  await page.getByRole("button", { name: "Text", exact: true }).click();
  await page
    .getByPlaceholder(/Paste your resume as text/)
    .fill(
      "Platform engineer with five years of experience building reliable services, accessible products, and secure integrations.",
    );
  await page.getByRole("button", { name: "Submit", exact: true }).click();

  const privacyConsent = page.getByRole("checkbox", {
    name: "I have read the privacy notice and want to submit this application.",
  });
  await expect(privacyConsent).toBeFocused();
  await expect(privacyConsent).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#apply-form-error")).toContainText(
    "Please acknowledge the privacy notice",
  );
  await privacyConsent.check();
  await page.getByRole("button", { name: "Submit", exact: true }).click();

  await expect(
    page.getByRole("heading", { level: 3, name: "Application received" }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);
});

test("public branding uses the same-origin proxy without leaking private storage", async ({
  page,
}) => {
  await setLocale(page, "en");

  const invalid = await page.request.get(
    "/api/public/branding/not-a-valid-key.png",
  );
  expect(invalid.status()).toBe(404);

  await page.goto("/apply/e2e-relative-logo");
  const proxiedLogo = page.getByRole("img", { name: "Northstar Hiring" });
  await expect(proxiedLogo).toHaveAttribute("src", brandingProxyPath);
  await expect
    .poll(() =>
      proxiedLogo.evaluate((image: HTMLImageElement) => image.naturalWidth),
    )
    .toBeGreaterThan(0);
  expect(await proxiedLogo.getAttribute("src")).not.toContain("127.0.0.1:4100");
  await expectNoMaterialAxeViolations(page);

  await page.route("https://cdn.example.test/**", (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: pixelPng }),
  );
  await page.goto("/apply/e2e-cdn-logo");
  await expect(
    page.getByRole("img", { name: "Northstar CDN" }),
  ).toHaveAttribute("src", "https://cdn.example.test/brand.png");

  await page.goto("/apply/e2e-confidential");
  await expect(page.getByRole("img")).toHaveCount(0);
  await expect(page.getByText(/The employer is not disclosed/)).toBeVisible();

  await page.goto("/apply/e2e-position");
  await expect(page.getByRole("img")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("private assessment preserves language and flushes a cleared answer before submit", async ({
  page,
}, testInfo: TestInfo) => {
  const project = testInfo.project.name.replace(/[^A-Za-z0-9_-]/g, "_");
  const token = `asm_e2e_valid_${project}_manual_review_token_1234567890`;
  const response = await page.goto(`/assessment/launch/${token}?lang=en`);
  expect(response?.status()).toBe(200);
  expectBaselineSecurityHeaders(response?.headers() ?? {});
  expectPrivateTokenHeaders(response?.headers() ?? {});

  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(
    page.getByRole("heading", { level: 1, name: "Pilot screening" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { level: 2, name: "Before you start" }),
  ).toBeVisible();
  await expectNoMaterialAxeViolations(page);

  await page.getByRole("button", { name: "Start assessment" }).click();
  await expect(
    page.getByRole("heading", { level: 2, name: "Core questions" }),
  ).toBeVisible();
  await expect(page.getByRole("progressbar")).toBeVisible();
  await expectNoHorizontalOverflow(page);

  await page
    .getByRole("navigation", { name: "Language" })
    .getByRole("link", { name: "ru", exact: true })
    .click();
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  await expect(page.getByRole("navigation", { name: "Язык" })).toBeVisible();
  await expect(
    page.getByText("Проверить и отправить", { exact: true }).first(),
  ).toBeVisible();
  await page
    .getByRole("navigation", { name: "Язык" })
    .getByRole("link", { name: "en", exact: true })
    .click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(
    page.getByRole("navigation", { name: "Language" }),
  ).toBeVisible();

  await page.getByRole("radio", { name: "Yes" }).check();
  const noteQuestion = page.locator(`#question-${noteQuestionId}`);
  const staleValue = "This optional answer must not survive clearing.";
  await noteQuestion.getByRole("textbox").fill(staleValue);
  await expect
    .poll(async () => {
      const state = await assessmentFixtureState(page, token);
      return state.answers.find(
        (answer) => answer.question_id === noteQuestionId,
      )?.value;
    })
    .toBe(staleValue);

  const raceStartedAt = Date.now();
  await noteQuestion.getByRole("button", { name: "Clear answer" }).click();
  await page.getByRole("button", { name: "Review and submit" }).click();
  const receiptPromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/assessment\/attempt\/[^/]+\/submit$/.test(response.url()),
  );
  await page.getByRole("button", { name: "Submit final answers" }).click();
  const receipt = (await (await receiptPromise).json()) as {
    result: Record<string, unknown>;
  };
  expect(Object.keys(receipt.result).sort()).toEqual([
    "attempt_id",
    "needs_manual_review",
    "passed",
    "percentage",
    "submitted_at",
  ]);
  expect(Date.now() - raceStartedAt).toBeLessThan(800);
  await expect(
    page.getByRole("heading", { level: 1, name: "Assessment submitted" }),
  ).toBeVisible();
  await expect(page.getByText("Manual review required").first()).toBeVisible();
  await expect(page.getByText("Not passed", { exact: true })).toHaveCount(0);
  await expect(page.getByText("0.0%", { exact: true })).toHaveCount(0);
  await expect
    .poll(async () =>
      (await page.context().cookies()).some(
        (cookie) => cookie.name === "hr_assessment_session",
      ),
    )
    .toBe(false);

  const finalState = await assessmentFixtureState(page, token);
  const nullPatchIndex = finalState.events.findIndex(
    (event) =>
      event.type === "patch" &&
      event.answers?.some(
        (answer) =>
          answer.question_id === noteQuestionId && answer.value === null,
      ),
  );
  const submitIndex = finalState.events.findIndex(
    (event) => event.type === "submit",
  );
  expect(nullPatchIndex).toBeGreaterThanOrEqual(0);
  expect(submitIndex).toBe(nullPatchIndex + 1);
  expect(
    finalState.answers.find((answer) => answer.question_id === noteQuestionId)
      ?.value,
  ).toBeNull();
  expect(
    finalState.answers.find(
      (answer) => answer.question_id === consentQuestionId,
    )?.value,
  ).toBe(true);
  expect(JSON.stringify(finalState.answers)).not.toContain(staleValue);
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  const revisit = await page.goto(`/assessment/launch/${token}?lang=en`);
  expect(revisit?.status()).toBe(410);
  expectPrivateTokenHeaders(revisit?.headers() ?? {});
  await expect(
    page.getByRole("heading", { level: 1, name: "Assessment unavailable" }),
  ).toBeVisible();
  await expect(page.getByText("Pilot screening")).toHaveCount(0);
  await expect(page.getByText("Core questions")).toHaveCount(0);
  expect(await page.locator("body").innerText()).not.toContain(staleValue);
  expect(await page.locator("body").innerText()).not.toContain("100.00");
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "hr_assessment_session",
    ),
  ).toBe(false);
});

test("assessment numeric equality and any-branch visibility match backend semantics", async ({
  page,
}, testInfo: TestInfo) => {
  const project = testInfo.project.name.replace(/[^A-Za-z0-9_-]/g, "_");
  const token = `asm_e2e_valid_${project}_numeric_conditions_1234567890`;

  await page.goto(`/assessment/launch/${token}?lang=en`);
  await page.getByRole("button", { name: "Start assessment" }).click();
  await page.getByRole("radio", { name: "Yes" }).check();

  const branch = page.getByText("Describe one achievement from those two years.");
  await expect(branch).toHaveCount(0);

  await page.getByRole("spinbutton").fill("2");
  await expect(branch).toBeVisible();
  await expectNoMaterialAxeViolations(page);

  await page.getByRole("spinbutton").fill("3");
  await expect(branch).toHaveCount(0);
});

test("assessment renders, saves, and submits every native question type", async ({
  page,
}, testInfo: TestInfo) => {
  const project = testInfo.project.name.replace(/[^A-Za-z0-9_-]/g, "_");
  const token = `asm_e2e_valid_${project}_all_types_1234567890`;

  await page.goto(`/assessment/launch/${token}?lang=en`);
  await page.getByRole("button", { name: "Start assessment" }).click();
  await page.getByRole("radio", { name: "Yes" }).check();
  await page.locator(`#question-${noteQuestionId}`).getByRole("textbox").fill("Platform work");
  await page.locator(`#question-${singleQuestionId}`).getByRole("radio", { name: "Backend" }).check();
  await page.locator(`#question-${multiQuestionId}`).getByRole("checkbox", { name: "Python" }).check();
  await page.locator(`#question-${multiQuestionId}`).getByRole("checkbox", { name: "SQL" }).check();
  await page.locator(`#question-${numericQuestionId}`).getByRole("spinbutton").fill("5");
  await page
    .locator(`#question-${longQuestionId}`)
    .getByRole("textbox")
    .fill("I build reliable and accessible hiring platforms.");
  await page
    .locator(`#question-${urlQuestionId}`)
    .getByRole("textbox")
    .fill("https://example.test/portfolio");
  await page.locator(`#question-${fileQuestionId} input[type=file]`).setInputFiles({
    name: "proof.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("assessment work sample"),
  });

  await expect(page.getByText("8 of 8 answered", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Review and submit" }).click();
  await page.getByRole("button", { name: "Submit final answers" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Assessment submitted" }),
  ).toBeVisible();
  await expectNoMaterialAxeViolations(page);
});

test("public scheduling books a slot and preserves private-link controls", async ({
  page,
}) => {
  await setLocale(page, "ru");
  const response = await page.goto("/schedule/e2e-schedule");
  expect(response?.status()).toBe(200);
  expectBaselineSecurityHeaders(response?.headers() ?? {});
  expectPrivateTokenHeaders(response?.headers() ?? {});

  const main = page.locator("#main-content");
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  await expect(
    main.getByText("Выберите время интервью", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  const language = page.getByRole("group", { name: "Сменить язык" });
  await language.getByRole("button", { name: "EN" }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(
    main.getByText("Schedule your interview", { exact: true }),
  ).toBeVisible();

  await page
    .getByRole("button", { name: /\d{1,2}:\d{2}/ })
    .first()
    .click();
  await page.getByRole("button", { name: "Confirm interview" }).click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Your interview is confirmed",
    }),
  ).toBeVisible();
  const manage = page.getByRole("link", { name: "Manage appointment" });
  await expect(manage).toHaveAttribute("href", "/schedule/manage/e2e-manage");
  await expect(manage).toHaveAttribute("referrerpolicy", "no-referrer");
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  await manage.click();
  await expect(page).toHaveURL(/\/schedule\/manage\/e2e-manage$/);
  await expect(
    page.getByRole("heading", { level: 1, name: "Platform Engineer" }),
  ).toBeVisible();
  await expect(
    page.getByText("Manage your interview", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);
});

test("valid shortlist and offer links render and the offer response persists", async ({
  page,
}, testInfo: TestInfo) => {
  await setLocale(page, "en");
  const project = testInfo.project.name.replace(/[^A-Za-z0-9_-]/g, "_");

  const shortlistToken = `e2e-shortlist-valid-${project}`;
  const shortlist = await page.goto(`/shortlist/${shortlistToken}`);
  expect(shortlist?.status()).toBe(200);
  expectPrivateTokenHeaders(shortlist?.headers() ?? {});
  await expect(
    page.getByRole("heading", { level: 1, name: "Platform Engineer" }),
  ).toBeVisible();
  const candidateHeading = page.getByRole("heading", {
    level: 2,
    name: "Candidate #01",
    exact: true,
  });
  await expect(candidateHeading).toHaveCount(1);
  await expect(candidateHeading).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain(shortlistToken);
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  const offerToken = `e2e-offer-valid-${project}`;
  const offer = await page.goto(`/offer/${offerToken}`);
  expect(offer?.status()).toBe(200);
  expectPrivateTokenHeaders(offer?.headers() ?? {});
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Offer for Platform Engineer",
    }),
  ).toBeVisible();
  await page.getByLabel("Typed signature").fill("Alex Example");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Accept offer" }).click();
  await expect(
    page.getByRole("heading", { level: 2, name: "Offer accepted" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Accept offer" }),
  ).toHaveCount(0);

  const revisit = await page.reload();
  expect(revisit?.status()).toBe(200);
  await expect(
    page.getByRole("heading", { level: 2, name: "Offer accepted" }),
  ).toBeVisible();
  expect(await page.locator("body").innerText()).not.toContain(offerToken);
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);
});

test("invalid and expired public capabilities render non-enumerating states", async ({
  page,
}) => {
  await setLocale(page, "en");

  const expiredAccess = await page.goto(
    "/invite/e2e_expired_invitation_token_1234567890",
  );
  expect(expiredAccess?.status()).toBe(410);
  expectBaselineSecurityHeaders(expiredAccess?.headers() ?? {});
  expectPrivateTokenHeaders(expiredAccess?.headers() ?? {});
  await expect(
    page.getByRole("heading", { level: 1, name: "Invitation unavailable" }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("410");
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);

  const apply = await page.goto("/apply/missing-position");
  expect(apply?.status()).toBe(404);
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "This position is no longer accepting applications",
    }),
  ).toBeVisible();
  await expectNoMaterialAxeViolations(page);

  const referral = await page.goto("/r/missing-referral");
  expect(referral?.status()).toBe(404);
  expectPrivateTokenHeaders(referral?.headers() ?? {});
  await expect(
    page.getByRole("heading", { level: 1, name: "Referral link unavailable" }),
  ).toBeVisible();
  await expectNoMaterialAxeViolations(page);

  const expiredToken = "asm_e2e_expired_assessment_token_1234567890";
  const assessment = await page.goto(
    `/assessment/launch/${expiredToken}?lang=en`,
  );
  expect(assessment?.status()).toBe(410);
  expectPrivateTokenHeaders(assessment?.headers() ?? {});
  await expect(
    page.getByRole("heading", { level: 1, name: "Assessment unavailable" }),
  ).toBeVisible();
  await expect(
    page.getByText("This assessment invitation has expired."),
  ).toBeVisible();
  await page.getByRole("link", { name: "RU", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  await expect(
    page.getByText("Срок действия приглашения истёк."),
  ).toBeVisible();
  await page.getByRole("link", { name: "EN", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");

  const capabilityStates: Array<{
    path: string;
    status: 404 | 410 | 503;
    title: string;
  }> = [
    {
      path: "/shortlist/e2e-shortlist-missing-token",
      status: 404,
      title: "Shortlist unavailable",
    },
    {
      path: "/shortlist/e2e-shortlist-revoked-token",
      status: 404,
      title: "Shortlist unavailable",
    },
    {
      path: "/shortlist/e2e-shortlist-expired-token",
      status: 410,
      title: "Shortlist unavailable",
    },
    {
      path: "/shortlist/e2e-shortlist-outage-token",
      status: 503,
      title: "Service temporarily unavailable",
    },
    {
      path: "/schedule/missing-schedule",
      status: 404,
      title: "Scheduling link unavailable",
    },
    {
      path: "/schedule/e2e-schedule-revoked-token",
      status: 404,
      title: "Scheduling link unavailable",
    },
    {
      path: "/schedule/e2e-schedule-expired-token",
      status: 410,
      title: "Scheduling link unavailable",
    },
    {
      path: "/schedule/e2e-schedule-consumed-token",
      status: 410,
      title: "Scheduling link unavailable",
    },
    {
      path: "/schedule/e2e-schedule-outage-token",
      status: 503,
      title: "Service temporarily unavailable",
    },
    {
      path: "/schedule/manage/missing-manage-token",
      status: 404,
      title: "Appointment link unavailable",
    },
    {
      path: "/schedule/manage/e2e-manage-revoked-token",
      status: 404,
      title: "Appointment link unavailable",
    },
    {
      path: "/schedule/manage/e2e-manage-expired-token",
      status: 410,
      title: "Appointment link unavailable",
    },
    {
      path: "/schedule/manage/e2e-manage-outage-token",
      status: 503,
      title: "Service temporarily unavailable",
    },
    {
      path: "/offer/missing-offer-token",
      status: 404,
      title: "Offer unavailable",
    },
    {
      path: "/offer/e2e-offer-revoked-token",
      status: 404,
      title: "Offer unavailable",
    },
    {
      path: "/offer/e2e-offer-expired-token",
      status: 410,
      title: "Offer unavailable",
    },
    {
      path: "/offer/e2e-offer-outage-token",
      status: 503,
      title: "Service temporarily unavailable",
    },
    {
      path: "/offer/e2e-offer-redirect-token",
      status: 503,
      title: "Service temporarily unavailable",
    },
  ];

  for (const [index, state] of capabilityStates.entries()) {
    await expectPublicCapabilityError(page, {
      ...state,
      audit: index === 0,
    });
  }

  await expectPublicCapabilityError(page, {
    path: "/schedule/manage/e2e-manage-revoked-ru-token",
    status: 404,
    title: "Ссылка на встречу недоступна",
    locale: "ru",
    audit: true,
  });
});

test("public journey recovers after an offline interval and slow reconnect", async ({
  context,
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "chromium-desktop",
    "One browser exercises the shared network-recovery contract.",
  );

  await setLocale(page, "en");
  await page.goto("/apply/e2e-position");
  await expect(
    page.getByRole("heading", { level: 2, name: "Apply for this role" }),
  ).toBeVisible();

  await context.setOffline(true);
  await expect(page.goto("/apply/e2e-position")).rejects.toThrow();
  await context.setOffline(false);

  await page.route("**/apply/e2e-position", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250));
    await route.continue();
  });
  const recovered = await page.goto("/apply/e2e-position");
  expect(recovered?.status()).toBe(200);
  await expect(
    page.getByRole("heading", { level: 2, name: "Apply for this role" }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expectNoMaterialAxeViolations(page);
});
