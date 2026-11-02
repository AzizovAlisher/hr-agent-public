import { expect, test, type Page } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const candidateId = "50000000-0000-4000-8000-000000000004";
const positionId = "50000000-0000-4000-8000-000000000003";

async function setLocale(page: Page, locale: "en" | "ru") {
  await page
    .context()
    .addCookies([{ name: "locale", value: locale, url: appURL }]);
}

async function signIn(page: Page, email: string) {
  await setLocale(page, "en");
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

test("OAuth callback failures show localized recovery copy without raw codes", async ({
  page,
}) => {
  await signIn(page, "admin+oauth-recovery@example.test");

  const providerCode = "provider-authorization-code-canary";
  await page.goto(
    `/integrations/oauth/callback?state=${"s".repeat(40)}&code=${providerCode}`,
  );
  await expect(page.locator("p[role=alert]")).toHaveText(
    "Try connecting again from Integrations. If the problem continues, contact your administrator.",
  );
  await expect(page).toHaveURL(/\/integrations\/oauth\/callback$/);
  const englishBody = await page.locator("body").innerText();
  expect(englishBody).not.toContain(providerCode);
  expect(englishBody).not.toContain("oauth_provider_unknown_internal_code");

  await setLocale(page, "ru");
  await page.goto("/integrations/oauth/callback?state=invalid&code=second-canary");
  await expect(page.locator("p[role=alert]")).toHaveText(
    "Повторите подключение со страницы интеграций. Если ошибка повторится, обратитесь к администратору.",
  );
  const russianBody = await page.locator("body").innerText();
  expect(russianBody).not.toContain("oauth_callback_invalid");
  expect(russianBody).not.toContain("second-canary");
});

test("settings keep legacy rows read-only and destructive labels imperative", async ({
  page,
}) => {
  await signIn(page, "admin+ui-surfaces@example.test");

  await page.goto("/settings?tab=communications");
  const legacyRow = page
    .getByRole("tabpanel", { name: "Communications" })
    .getByRole("listitem")
    .filter({ hasText: "Legacy WhatsApp reminder" });
  await expect(legacyRow).toHaveCount(1);
  await expect(legacyRow).toContainText(
    "Hello {candidate.first_name}, your interview is tomorrow.",
  );
  await expect(legacyRow).toContainText("Unavailable");
  await expect(legacyRow).toContainText("Stored state: enabled");
  await expect(legacyRow).toContainText(
    "This legacy WhatsApp template is kept for reference.",
  );
  await expect(legacyRow.getByRole("button")).toHaveCount(0);

  await page.goto("/settings?tab=scorecards");
  await expect(
    page.getByText("No scorecard templates yet.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Scorecard templates couldn't be loaded. Other settings remain available.",
      { exact: true },
    ),
  ).toHaveCount(0);

  await page.goto(`/candidates/${candidateId}?position=${positionId}`);
  const scorecardPanel = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Hiring-team scorecards" }) });
  const assign = scorecardPanel.getByRole("button", {
    name: "Assign scorecards",
  });
  await expect(assign).toBeDisabled();
  await expect(assign).toHaveAttribute(
    "title",
    "Create an active scorecard template in Settings first.",
  );
  await expect(scorecardPanel).toContainText(
    "Create an active scorecard template in Settings first.",
  );
  await expect(scorecardPanel).not.toContainText(
    "Scorecard templates couldn't be loaded.",
  );

  await page.goto("/settings?tab=team");
  const recruiterRow = page
    .locator("li")
    .filter({ hasText: "recruiter@example.test" });
  await recruiterRow.getByRole("button", { name: "Delete user" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete this user?" });
  await expect(
    dialog.getByRole("button", { name: "Delete user", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "User deleted", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();

  await setLocale(page, "ru");
  await page.goto("/settings?tab=team");
  await expect(
    page
      .locator("li")
      .filter({ hasText: "recruiter@example.test" })
      .getByRole("button", { name: "Удалить пользователя" }),
  ).toBeVisible();
});

test("scorecard template load failures never masquerade as an empty setup", async ({
  page,
}) => {
  await signIn(page, "admin+scorecard-load-failure@example.test");

  await page.goto("/settings?tab=scorecards");
  const mainContent = page.locator("#main-content");
  await expect(
    mainContent.getByText(
      "Scorecard templates couldn't be loaded. Other settings remain available.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    mainContent.getByText("No scorecard templates yet.", { exact: true }),
  ).toHaveCount(0);

  await page.goto(`/candidates/${candidateId}?position=${positionId}`);
  const scorecardPanel = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Hiring-team scorecards" }) });
  const assign = scorecardPanel.getByRole("button", {
    name: "Assign scorecards",
  });
  await expect(assign).toBeDisabled();
  await expect(assign).toHaveAttribute(
    "title",
    "Scorecard templates couldn't be loaded. Try again before assigning scorecards.",
  );
  await expect(assign).toHaveAccessibleDescription(
    "Scorecard templates couldn't be loaded. Try again before assigning scorecards.",
  );
  await expect(scorecardPanel).toContainText(
    "Scorecard templates couldn't be loaded. Try again before assigning scorecards.",
  );
  await expect(scorecardPanel).not.toContainText(
    "Create an active scorecard template in Settings first.",
  );
});

test("candidate AI controls disappear when the API denies scoped context", async ({
  page,
}) => {
  await signIn(page, "admin+chat-denied@example.test");
  const response = await page.goto(
    `/candidates/${candidateId}?position=${positionId}`,
  );
  expect(response?.status()).toBe(200);
  await expect(
    page.getByRole("button", { name: "Open chat", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /^Score all \(/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Score", exact: true }),
  ).toHaveCount(0);

  await page.goto(`/positions/${positionId}`);
  await expect(
    page.getByRole("button", { name: "Open chat", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Actions", exact: true }).click();
  await expect(
    page.getByRole("menuitem", {
      name: "Re-score all candidates",
      exact: true,
    }),
  ).toBeVisible();
});

test("AI controls disappear when tenant capability is unavailable", async ({
  page,
}) => {
  await signIn(page, "admin+ai-unavailable@example.test");

  await page.goto(`/candidates/${candidateId}?position=${positionId}`);
  await expect(
    page.getByRole("button", { name: "Open chat", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /^Score all \(/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Score", exact: true }),
  ).toHaveCount(0);

  await page.goto(`/positions/${positionId}`);
  await expect(
    page.getByRole("button", { name: "Open chat", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Actions", exact: true }).click();
  await expect(
    page.getByRole("menuitem", {
      name: "Re-score all candidates",
      exact: true,
    }),
  ).toHaveCount(0);
});
