import { expect, test, type Page } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const folderId = "50000000-0000-4000-8000-000000000006";

async function signInForProject(page: Page, projectName: string) {
  const scope = projectName.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  await page
    .context()
    .addCookies([{ name: "locale", value: "en", url: appURL }]);
  await page.goto("/login");
  await page.getByLabel("Email").fill(`admin+project-${scope}@example.test`);
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

test("bulk project creation opens the real folder containing every new role", async ({
  page,
}, testInfo) => {
  await signInForProject(page, testInfo.project.name);
  await page.goto("/positions/project");

  await page.locator('input[type="file"]').setInputFiles({
    name: "project-brief.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Project Phoenix needs an API engineer and a QA engineer."),
  });
  await expect(page.getByText(/Detected 2 positions/)).toBeVisible();
  await expect(page.getByLabel("Folder")).toHaveValue("Pilot Engineering");

  await page.getByRole("button", { name: "Create 2 positions" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/dashboard\\?folder_id=${folderId.replaceAll("-", "\\-")}$`),
  );
  await expect(page.getByText("Project API Engineer", { exact: true })).toBeVisible();
  await expect(page.getByText("Project QA Engineer", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Pilot Engineering/ }).first(),
  ).toBeVisible();

  const visibleCards = page.locator('a[href^="/positions/"]').filter({
    hasText: /Project (API|QA) Engineer/,
  });
  await expect(visibleCards).toHaveCount(2);
  expect(
    await page.evaluate(() => ({
      legacyName: localStorage.getItem("dashboard.folder"),
      legacyId: localStorage.getItem("dashboard.folder_id"),
    })),
  ).toEqual({ legacyName: null, legacyId: null });
});
