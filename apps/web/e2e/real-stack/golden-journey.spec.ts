import AxeBuilder from "@axe-core/playwright";
import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type TestInfo,
} from "@playwright/test";

const appURL = `https://localhost:${process.env.REAL_STACK_HTTPS_PORT ?? "3643"}`;
const apiURL = `http://127.0.0.1:${process.env.REAL_STACK_API_PORT ?? "8300"}`;
const positionId = "80000000-0000-4000-8000-000000000101";
const fixturePassword = "RealStack-Only-Password-2026!";
const resumeEvidence =
  "Real Stack Platform Engineer CV with FastAPI PostgreSQL Valkey object storage durable worker and accessible Next.js hiring experience.";

type PublicReadiness = {
  status: "ready" | "not_ready";
  checked_at: string;
};

type DetailedReadiness = {
  status: "ready" | "not_ready";
  checked_at: string;
  components: Record<
    string,
    { status: "ok" | "degraded" | "failed"; data: Record<string, unknown> }
  >;
};

const applicants: Record<
  string,
  { name: string; phone: string; email: string }
> = {
  "chromium-real-stack": {
    name: "Amina Chromium",
    phone: "+992 900 100 001",
    email: "amina.chromium.real-stack@example.com",
  },
  "firefox-real-stack": {
    name: "Amina Firefox",
    phone: "+992 900 100 002",
    email: "amina.firefox.real-stack@example.com",
  },
  "webkit-linux-real-stack": {
    name: "Amina WebKit",
    phone: "+992 900 100 003",
    email: "amina.webkit.real-stack@example.com",
  },
};

async function setLocale(context: BrowserContext, locale: "en" | "ru") {
  await context.addCookies([{ name: "locale", value: locale, url: appURL }]);
}

async function expectNoMaterialAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  const materialViolations = results.violations
    .filter(
      (violation) =>
        violation.impact === "serious" || violation.impact === "critical",
    )
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target.join(" ")),
    }));
  expect(materialViolations).toEqual([]);
  return materialViolations;
}

async function expectNoHorizontalOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);
}

function pdfWithText(text: string): Buffer {
  const escaped = text
    .replaceAll("\\", "\\\\")
    .replaceAll("(", "\\(")
    .replaceAll(")", "\\)");
  const stream = `BT\n/F1 12 Tf\n72 720 Td\n(${escaped}) Tj\nET\n`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}endstream`,
  ];
  let body = "%PDF-1.4\n% deterministic real-stack fixture\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(body));
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xrefOffset = Buffer.byteLength(body);
  body += `xref\n0 ${objects.length + 1}\n`;
  body += "0000000000 65535 f \n";
  for (const offset of offsets.slice(1)) {
    body += `${String(offset).padStart(10, "0")} 00000 n \n`;
  }
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\n`;
  body += `startxref\n${xrefOffset}\n%%EOF\n`;
  return Buffer.from(body, "ascii");
}

async function signIn(
  page: Page,
  email: string,
  password: string,
  labels: { email: string; password: string },
) {
  await page.goto("/login");
  await page.getByLabel(labels.email, { exact: true }).fill(email);
  const passwordInput = page.getByLabel(labels.password, { exact: true });
  await passwordInput.fill(password);
  await passwordInput.press("Enter");
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

test("RU/EN public application becomes a tenant-isolated authenticated hire record", async ({
  browser,
  request,
}, testInfo: TestInfo) => {
  const applicant = applicants[testInfo.project.name];
  expect(applicant, `No deterministic applicant for ${testInfo.project.name}`).toBeTruthy();

  const readinessResponse = await request.get(`${apiURL}/health/ready`);
  expect(readinessResponse.status()).toBe(200);
  const publicReadiness = (await readinessResponse.json()) as PublicReadiness &
    Record<string, unknown>;
  expect(publicReadiness.status).toBe("ready");
  expect(publicReadiness.checked_at).toBeTruthy();
  expect(publicReadiness.components).toBeUndefined();

  const operatorLoginResponse = await request.post(`${apiURL}/auth/login`, {
    data: {
      email: "readiness.real-stack@example.com",
      password: fixturePassword,
    },
  });
  expect(operatorLoginResponse.status()).toBe(200);
  const operatorLogin = (await operatorLoginResponse.json()) as {
    access_token: string | null;
    role: string;
  };
  expect(operatorLogin.role).toBe("restricted");
  expect(operatorLogin.access_token).toBeTruthy();

  const detailedReadinessResponse = await request.get(
    `${apiURL}/enterprise/operations/readiness`,
    { headers: { Authorization: `Bearer ${operatorLogin.access_token}` } },
  );
  expect(detailedReadinessResponse.status()).toBe(200);
  const readiness =
    (await detailedReadinessResponse.json()) as DetailedReadiness;
  expect(readiness.status).toBe("ready");
  for (const component of [
    "database",
    "migrations",
    "tenant_isolation",
    "private_storage",
    "malware_scanner",
    "public_rate_limits",
    "workers",
    "queues",
  ]) {
    expect(readiness.components[component]?.status, component).toBe("ok");
  }
  expect(readiness.components.private_storage.data.backend).toBe("s3");
  expect(readiness.components.public_rate_limits.data.backend).toBe("valkey");
  await testInfo.attach("real-stack-readiness", {
    body: Buffer.from(JSON.stringify(readiness, null, 2)),
    contentType: "application/json",
  });

  const publicContext = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 320, height: 640 },
  });
  await setLocale(publicContext, "ru");
  const publicPage = await publicContext.newPage();
  const publicResponse = await publicPage.goto(`/apply/${positionId}`);
  expect(publicResponse?.status()).toBe(200);
  await expect(publicPage.locator("html")).toHaveAttribute("lang", "ru");
  await expect(
    publicPage.getByRole("heading", {
      level: 2,
      name: "Откликнуться на вакансию",
    }),
  ).toBeVisible();

  const languageGroup = publicPage.getByRole("group", {
    name: "Сменить язык",
  });
  const englishButton = languageGroup.getByRole("button", { name: "EN" });
  await englishButton.focus();
  await englishButton.press("Enter");
  await expect(publicPage.locator("html")).toHaveAttribute("lang", "en");
  await expect(
    publicPage.getByRole("heading", { level: 2, name: "Apply for this role" }),
  ).toBeVisible();

  const russianButton = publicPage
    .getByRole("group", { name: "Change language" })
    .getByRole("button", { name: "RU" });
  await russianButton.focus();
  await russianButton.press("Enter");
  await expect(publicPage.locator("html")).toHaveAttribute("lang", "ru");
  await expectNoHorizontalOverflow(publicPage);
  await expectNoMaterialAxeViolations(publicPage);

  const submit = publicPage.getByRole("button", {
    name: "Отправить",
    exact: true,
  });
  await submit.focus();
  await submit.press("Enter");
  await expect(
    publicPage.getByText("Укажите ваше имя.", { exact: true }),
  ).toBeVisible();
  const applicantName = publicPage.getByRole("textbox", { name: /^Имя \*/ });
  await expect(applicantName).toBeFocused();

  await applicantName.fill(applicant.name);
  await publicPage
    .getByRole("textbox", { name: /^Телефон \*/ })
    .fill(applicant.phone);
  await publicPage
    .getByRole("textbox", { name: /^Email ·/ })
    .fill(applicant.email);
  await publicPage.locator('input[type="file"]').setInputFiles({
    name: "real-stack-platform-cv.pdf",
    mimeType: "application/pdf",
    buffer: pdfWithText(resumeEvidence),
  });
  await publicPage
    .getByRole("checkbox", {
      name: /Я ознакомился.*хочу отправить отклик/,
    })
    .check();
  await submit.focus();
  await submit.press("Enter");
  await expect(
    publicPage.getByRole("heading", { level: 3, name: "Отклик получен" }),
  ).toBeVisible({ timeout: 45_000 });
  await expectNoHorizontalOverflow(publicPage);
  await publicContext.close();

  const recruiterContext = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 1280, height: 800 },
  });
  await setLocale(recruiterContext, "en");
  const recruiterPage = await recruiterContext.newPage();
  await recruiterPage.goto("/login");

  await recruiterPage
    .getByLabel("Email", { exact: true })
    .fill("recruiter.real-stack@example.com");
  await recruiterPage
    .getByLabel("Password", { exact: true })
    .fill("incorrect-real-stack-password");
  await recruiterPage.getByLabel("Password", { exact: true }).press("Enter");
  await expect(
    recruiterPage
      .getByRole("alert")
      .filter({ hasText: "The email or password is incorrect." }),
  ).toHaveText("The email or password is incorrect.");
  await expectNoMaterialAxeViolations(recruiterPage);

  await signIn(
    recruiterPage,
    "recruiter.real-stack@example.com",
    fixturePassword,
    { email: "Email", password: "Password" },
  );
  await recruiterPage.goto(`/positions/${positionId}`);
  const candidateLink = recruiterPage.getByRole("link", {
    name: new RegExp(applicant.name),
  });
  await expect(candidateLink.first()).toBeVisible({ timeout: 30_000 });
  await candidateLink.first().click();
  await expect(recruiterPage).toHaveURL(/\/candidates\/[0-9a-f-]+/);
  const candidateId = new URL(recruiterPage.url()).pathname
    .match(/\/candidates\/([0-9a-f-]+)/)?.[1];
  expect(candidateId).toBeTruthy();

  await expect
    .poll(
      async () => {
        await recruiterPage.reload({ waitUntil: "domcontentloaded" });
        return recruiterPage
          .getByRole("button", { name: /Resume text/ })
          .count();
      },
      {
        timeout: 75_000,
        intervals: [1_000, 2_000, 3_000],
        message:
          "the durable worker should read the PDF from object storage and persist extracted text",
      },
    )
    .toBe(1);

  await recruiterPage.getByRole("button", { name: /Resume text/ }).click();
  await expect(recruiterPage.getByText(resumeEvidence, { exact: false })).toBeVisible();

  const exportLink = recruiterPage.getByRole("link", {
    name: "Export",
    exact: true,
  });
  await expect(exportLink).toHaveAttribute(
    "href",
    `/api/candidates/${candidateId}/cv`,
  );
  const resumeResponse = await recruiterContext.request.get(
    `${appURL}/api/candidates/${candidateId}/cv`,
  );
  expect(resumeResponse.status()).toBe(200);
  expect(resumeResponse.headers()["content-type"]).toContain("application/pdf");
  const downloadedResume = await resumeResponse.body();
  expect(downloadedResume.subarray(0, 8).toString("ascii")).toBe("%PDF-1.4");

  await recruiterPage.goto(`/positions/${positionId}`);
  const tableTab = recruiterPage.getByRole("tab", { name: "Table" });
  const kanbanTab = recruiterPage.getByRole("tab", { name: "Kanban" });
  await tableTab.focus();
  await tableTab.press("ArrowRight");
  await expect(kanbanTab).toBeFocused();
  await expect(kanbanTab).toHaveAttribute("aria-selected", "true");

  let card = recruiterPage.locator("div.select-none").filter({
    has: recruiterPage.getByRole("link", { name: new RegExp(applicant.name) }),
  });
  const dragHandle = card
    .locator("xpath=..")
    .getByRole("button", { name: "Drag candidate" });
  const screeningDropZone = recruiterPage
    .getByRole("tabpanel")
    .getByText("Screening", { exact: true })
    .locator("xpath=ancestor::div[contains(@class, 'w-[260px]')][1]")
    .locator("div.border-dashed");
  await screeningDropZone.scrollIntoViewIfNeeded();
  await dragHandle.focus();
  await expect(dragHandle).toBeFocused();
  const sourceBox = await dragHandle.boundingBox();
  const targetBox = await screeningDropZone.boundingBox();
  expect(sourceBox).not.toBeNull();
  expect(targetBox).not.toBeNull();
  const dragStartX = sourceBox!.x + 8;
  const dragStartY = sourceBox!.y + sourceBox!.height - 8;
  await recruiterPage.mouse.move(
    dragStartX,
    dragStartY,
  );
  await recruiterPage.mouse.down();
  await recruiterPage.mouse.move(
    dragStartX + 12,
    dragStartY,
    { steps: 4 },
  );
  await recruiterPage.mouse.move(
    targetBox!.x + targetBox!.width / 2,
    targetBox!.y + targetBox!.height / 2,
    { steps: 16 },
  );
  await recruiterPage.mouse.up();
  await expect(recruiterPage.getByText('Moved to "Screening"')).toBeVisible();

  await recruiterPage.reload();
  await recruiterPage.getByRole("tab", { name: "Kanban" }).click();
  card = recruiterPage.locator("div.select-none").filter({
    has: recruiterPage.getByRole("link", { name: new RegExp(applicant.name) }),
  });
  const persistedScreeningColumn = card.locator(
    "xpath=ancestor::div[contains(@class, 'w-[260px]')][1]",
  );
  await expect(persistedScreeningColumn).toContainText("Screening");
  const kanbanAxeResults = await expectNoMaterialAxeViolations(recruiterPage);
  await testInfo.attach("kanban-accessibility-results", {
    body: Buffer.from(JSON.stringify(kanbanAxeResults, null, 2)),
    contentType: "application/json",
  });
  const candidateActions = card.getByRole("button", {
    name: "Candidate actions",
  });
  await candidateActions.focus();
  await candidateActions.press("Enter");
  const newStageMenuItem = recruiterPage.getByRole("menuitem", {
    name: "New",
  });
  const interviewMenuItem = recruiterPage.getByRole("menuitem", {
    name: "Interview",
  });
  await expect(interviewMenuItem).toBeVisible();
  await expect(newStageMenuItem).toBeFocused();
  await recruiterPage.keyboard.press("ArrowDown");
  await expect(interviewMenuItem).toBeFocused();
  await recruiterPage.keyboard.press("Enter");
  await expect(recruiterPage.getByText('Moved to "Interview"')).toBeVisible();

  await recruiterPage.reload();
  await recruiterPage.getByRole("tab", { name: "Kanban" }).click();
  card = recruiterPage.locator("div.select-none").filter({
    has: recruiterPage.getByRole("link", { name: new RegExp(applicant.name) }),
  });
  const persistedInterviewColumn = card.locator(
    "xpath=ancestor::div[contains(@class, 'w-[260px]')][1]",
  );
  await expect(persistedInterviewColumn).toContainText("Interview");
  await expectNoMaterialAxeViolations(recruiterPage);
  await recruiterContext.close();

  const interviewerContext = await browser.newContext({
    ignoreHTTPSErrors: true,
    viewport: { width: 320, height: 640 },
  });
  await setLocale(interviewerContext, "ru");
  const interviewerPage = await interviewerContext.newPage();
  await signIn(
    interviewerPage,
    "interviewer.real-stack@example.com",
    fixturePassword,
    { email: "Email", password: "Пароль" },
  );
  await expect(interviewerPage.locator("html")).toHaveAttribute("lang", "ru");

  await interviewerPage.goto("/candidates");
  await expect(
    interviewerPage.getByRole("heading", { level: 1, name: "Кандидаты" }),
  ).toBeVisible();
  await expect(interviewerPage.getByText(applicant.name)).toHaveCount(0);
  await expect(
    interviewerPage.getByRole("button", { name: "Добавить кандидата" }),
  ).toHaveCount(0);
  await expectNoHorizontalOverflow(interviewerPage);
  await expectNoMaterialAxeViolations(interviewerPage);

  await interviewerPage.goto(`/positions/${positionId}`);
  await expect(interviewerPage.getByText(applicant.name)).toHaveCount(0);
  await expect(
    interviewerPage.getByRole("heading", { level: 1, name: "404" }),
  ).toBeVisible();
  await interviewerContext.close();
});
