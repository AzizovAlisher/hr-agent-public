import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const mockURL = `http://127.0.0.1:${process.env.PLAYWRIGHT_MOCK_PORT ?? "4100"}`;

function fixtureScope(testInfo: TestInfo) {
  return testInfo.project.name.replace(/[^A-Za-z0-9_-]/g, "_");
}

async function signInAsAdmin(page: Page, testInfo: TestInfo) {
  await page
    .context()
    .addCookies([{ name: "locale", value: "en", url: appURL }]);
  await page.goto("/login");
  await page
    .getByLabel("Email")
    .fill(`admin+${fixtureScope(testInfo)}@example.test`);
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

async function expectNoMaterialAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    results.violations.filter(
      (violation) =>
        violation.impact === "serious" || violation.impact === "critical",
    ),
  ).toEqual([]);
}

async function expectNoHorizontalOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
}

async function openDetailedResult(page: Page, label: string) {
  const button = page.getByRole("button", { name: label });
  await expect(button).toBeVisible();
  await button.click();
  await expect(page.locator("#assessment-detailed-result")).toBeFocused();
}

test.beforeEach(async ({ request }, testInfo) => {
  const response = await request.post(
    `${mockURL}/__state/assessment-review?scope=${fixtureScope(testInfo)}`,
  );
  expect(response.status()).toBe(200);
});

test("admin adjudicates a knockout in RU and EN with keyboard and accessible evidence", async ({
  page,
}, testInfo: TestInfo) => {
  await signInAsAdmin(page, testInfo);
  const response = await page.goto("/assessments?view=results");
  expect(response?.status()).toBe(200);
  await expect(
    page.getByRole("heading", { level: 1, name: "Candidate assessments" }),
  ).toBeVisible();
  await openDetailedResult(page, "Open detailed result");

  await expect(
    page.getByRole("group", { name: "Knockout adjudication" }),
  ).toBeVisible();
  await expect(
    page.getByText("Rule evidence retained for human review").first(),
  ).toBeVisible();
  await expect(page.getByText("Awaiting human adjudication")).toBeVisible();

  await page.getByRole("button", { name: "Save assessment review" }).click();
  const confirm = page.getByRole("radio", {
    name: "Confirm the match (assessment remains not passed)",
  });
  await expect(confirm).toBeFocused();
  await expect(confirm).toHaveAttribute("aria-invalid", "true");
  await expect(
    page.locator("#assessment-knockout-review-error"),
  ).toBeVisible();

  const clear = page.getByRole("radio", {
    name: "Clear the match (score threshold applies)",
  });
  await clear.focus();
  await page.keyboard.press("Space");
  await expect(clear).toBeChecked();
  await page
    .getByLabel("Decision reason")
    .fill("  Verified source evidence clears the automated match.  ");
  await expectNoMaterialAxeViolations(page);
  await page.getByRole("button", { name: "Save assessment review" }).click();
  await expect(page.getByText("Assessment review saved", { exact: true })).toBeVisible();
  await expect(page.getByText("Cleared by a reviewer")).toBeVisible();
  await expect(page.getByText("Passed", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText("Rule evidence retained for human review").first(),
  ).toBeVisible();

  const fixture = (await (
    await page.request.get(
      `${mockURL}/__state/assessment-review?scope=${fixtureScope(testInfo)}`,
    )
  ).json()) as {
    review: { outcome: string; reason: string };
    requests: Array<{
      grades: unknown[];
      knockout_review: { outcome: string; reason: string };
    }>;
  };
  expect(fixture.review).toMatchObject({
    outcome: "cleared",
    reason: "Verified source evidence clears the automated match.",
  });
  expect(fixture.requests).toHaveLength(1);
  expect(fixture.requests[0]).toMatchObject({
    grades: [],
    knockout_review: {
      outcome: "cleared",
      reason: "Verified source evidence clears the automated match.",
    },
  });

  await page
    .context()
    .addCookies([{ name: "locale", value: "ru", url: appURL }]);
  await page.reload();
  await expect(
    page.getByRole("heading", { level: 1, name: "Оценивания кандидатов" }),
  ).toBeVisible();
  await openDetailedResult(page, "Открыть подробный результат");
  await expect(page.getByText("Снято проверяющим")).toBeVisible();
  const russianConfirm = page.getByRole("radio", {
    name: "Подтвердить срабатывание (оценивание не пройдено)",
  });
  await russianConfirm.focus();
  await page.keyboard.press("Space");
  await page
    .getByLabel("Обоснование решения")
    .fill("Исходные документы подтверждают применимость условия.");
  await page
    .getByRole("button", { name: "Сохранить проверку оценивания" })
    .click();
  await expect(
    page.getByText("Проверка оценивания сохранена", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Подтверждено проверяющим")).toBeVisible();
  await expect(page.getByText("Не пройдено", { exact: true }).first()).toBeVisible();
  await expectNoMaterialAxeViolations(page);
  await expectNoHorizontalOverflow(page);

  await page.getByRole("link", { name: "Приглашения" }).click();
  await expect(
    page.getByText("Перерывы разрешены (таймер продолжает идти)"),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Зафиксировать запрос на поддержку программы чтения с экрана",
    ),
  ).toBeVisible();
});

test("candidate accommodation copy states timer and support behavior truthfully", async ({
  page,
}, testInfo: TestInfo) => {
  const project = testInfo.project.name.replace(/[^A-Za-z0-9_-]/g, "_");
  const token = `asm_e2e_valid_${project}_accommodations_1234567890`;

  await page.goto(`/assessment/launch/${token}?lang=en`);
  await expect(page.getByText("You may take breaks; the timer continues")).toBeVisible();
  await expect(
    page.getByText("Your screen-reader support preference was recorded"),
  ).toBeVisible();
  await expectNoMaterialAxeViolations(page);

  await page.goto(`/assessment/launch/${token}?lang=ru`);
  await expect(
    page.getByText("Можно делать перерывы; таймер продолжает идти"),
  ).toBeVisible();
  await expect(
    page.getByText("Запрос на поддержку программы чтения с экрана зафиксирован"),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
