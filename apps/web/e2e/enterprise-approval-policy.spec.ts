import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const mockURL = `http://127.0.0.1:${process.env.PLAYWRIGHT_MOCK_PORT ?? "4100"}`;

async function signInAsAdmin(page: Page) {
  await page
    .context()
    .addCookies([{ name: "locale", value: "en", url: appURL }]);
  await page.goto("/login");
  await page.getByLabel("Email").fill("admin@example.test");
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

async function savedSeparationPolicy(page: Page) {
  const response = await page.request.get(`${mockURL}/__state/enterprise-policy`);
  expect(response.status()).toBe(200);
  return (await response.json()).approval_separation_of_duties as Record<
    string,
    boolean
  >;
}

test.beforeEach(async ({ request }) => {
  const response = await request.post(`${mockURL}/__state/enterprise-policy`);
  expect(response.status()).toBe(200);
});

test("admin configures exact approval separation policy in English and Russian", async ({
  page,
}) => {
  await signInAsAdmin(page);
  const response = await page.goto("/enterprise");
  expect(response?.status()).toBe(200);

  await expect(
    page.getByRole("heading", { level: 1, name: "Enterprise controls" }),
  ).toBeVisible();
  await expect(
    page.getByRole("group", { name: "Approval separation of duties" }).first(),
  ).toBeVisible();

  const requisitions = page.getByRole("checkbox", {
    name: "Require a different approver for requisitions",
  });
  const offers = page.getByRole("checkbox", {
    name: "Require a different approver for candidate offers",
  });
  const hiringDecisions = page.getByRole("checkbox", {
    name: "Require a different approver for hiring decisions",
  });
  await expect(requisitions).toBeChecked();
  await expect(offers).toBeChecked();
  await expect(hiringDecisions).toBeChecked();

  await offers.uncheck();
  await hiringDecisions.uncheck();
  const englishRefresh = page.waitForResponse((candidate) => {
    const request = candidate.request();
    const headers = request.headers();
    const url = new URL(candidate.url());
    return (
      request.method() === "GET" &&
      headers.rsc === "1" &&
      headers["next-router-state-tree"]?.includes("refetch") === true &&
      url.pathname === "/enterprise" &&
      url.searchParams.has("_rsc")
    );
  });
  const saveButton = page.getByRole("button", { name: "Save changes" });
  await saveButton.click();
  await expect(page.getByText("Changes saved", { exact: true })).toBeVisible();
  const englishRefreshResponse = await englishRefresh;
  expect(englishRefreshResponse.ok()).toBe(true);
  await expect(saveButton).toBeEnabled();
  expect(await savedSeparationPolicy(page)).toEqual({
    requisitions: true,
    offers: false,
    hiring_decisions: false,
  });

  await page.reload();
  await expect(requisitions).toBeChecked();
  await expect(offers).not.toBeChecked();
  await expect(hiringDecisions).not.toBeChecked();

  await page
    .context()
    .addCookies([{ name: "locale", value: "ru", url: appURL }]);
  await page.reload();
  await expect(
    page.getByRole("heading", { level: 1, name: "Контроль и безопасность" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("group", {
        name: "Разделение обязанностей при согласовании",
      })
      .first(),
  ).toBeVisible();

  const russianHiringDecisions = page.getByRole("checkbox", {
    name: "Другой согласующий для решений о найме",
  });
  await russianHiringDecisions.focus();
  await expect(russianHiringDecisions).toBeFocused();
  await page.keyboard.press("Space");
  await expect(russianHiringDecisions).toBeChecked();
  const russianRefresh = page.waitForResponse((candidate) => {
    const request = candidate.request();
    const headers = request.headers();
    const url = new URL(candidate.url());
    return (
      request.method() === "GET" &&
      headers.rsc === "1" &&
      headers["next-router-state-tree"]?.includes("refetch") === true &&
      url.pathname === "/enterprise" &&
      url.searchParams.has("_rsc")
    );
  });
  const russianSaveButton = page.getByRole("button", { name: "Сохранить" });
  await russianSaveButton.click();
  await expect(page.getByText("Изменения сохранены", { exact: true })).toBeVisible();
  const russianRefreshResponse = await russianRefresh;
  expect(russianRefreshResponse.ok()).toBe(true);
  await expect(russianSaveButton).toBeEnabled();
  await expect(
    page.locator("[data-sonner-toast]").filter({
      hasText: "Изменения сохранены",
    }),
  ).toHaveCSS("opacity", "1");
  expect(await savedSeparationPolicy(page)).toEqual({
    requisitions: true,
    offers: false,
    hiring_decisions: true,
  });

  const axe = await new AxeBuilder({ page }).analyze();
  expect(
    axe.violations.filter(
      (violation) =>
        violation.impact === "serious" || violation.impact === "critical",
    ),
  ).toEqual([]);
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
});
