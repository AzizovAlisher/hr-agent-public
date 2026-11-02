import { expect, test, type Page } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;
const successSession = "80000000-0000-4000-8000-000000000001";
const errorSession = "80000000-0000-4000-8000-000000000002";
const upstreamSecret = "UPSTREAM_AI_SECRET_CANARY_MUST_NOT_REACH_BROWSER";

async function signIn(page: Page) {
  await page
    .context()
    .addCookies([{ name: "locale", value: "en", url: appURL }]);
  await page.goto("/login");
  await page.getByLabel("Email").fill("admin@example.test");
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/my-work(?:\?|$)/);
}

function expectPrivateHeaders(headers: Record<string, string>, stream = false) {
  expect(headers["cache-control"]).toContain("private");
  expect(headers["cache-control"]).toContain("no-store");
  expect(headers["cache-control"]).toContain("max-age=0");
  if (stream) expect(headers["cache-control"]).toContain("no-transform");
  expect(headers.pragma).toBe("no-cache");
  expect(headers["referrer-policy"]).toBe("no-referrer");
  expect(headers["x-content-type-options"]).toBe("nosniff");
}

test("AI BFF never caches private streams, attachment tokens, or errors", async ({
  page,
}) => {
  await signIn(page);

  const messageSuccess = await page.evaluate(async (sessionId) => {
    const response = await fetch(`/api/ai/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: "Review this private candidate" }),
    });
    return {
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      body: await response.text(),
    };
  }, successSession);
  expect(messageSuccess.status).toBe(200);
  expect(messageSuccess.headers["content-type"]).toContain("text/event-stream");
  expectPrivateHeaders(messageSuccess.headers, true);

  const attachmentSuccess = await page.evaluate(async (sessionId) => {
    const formData = new FormData();
    formData.append(
      "file",
      new File(["private candidate CV"], "private-cv.txt", {
        type: "text/plain",
      }),
    );
    const response = await fetch(`/api/ai/sessions/${sessionId}/attachments`, {
      method: "POST",
      body: formData,
    });
    return {
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      body: await response.text(),
    };
  }, successSession);
  expect(attachmentSuccess.status).toBe(200);
  expect(attachmentSuccess.body).toContain("one-time-private-upload-token");
  expectPrivateHeaders(attachmentSuccess.headers);

  for (const endpoint of ["messages", "attachments"] as const) {
    const failed = await page.evaluate(
      async ({ sessionId, endpointName }) => {
        const init: RequestInit = { method: "POST" };
        if (endpointName === "messages") {
          init.headers = { "Content-Type": "application/json" };
          init.body = JSON.stringify({ content: "private failure" });
        } else {
          const formData = new FormData();
          formData.append("file", new File(["private"], "private.txt"));
          init.body = formData;
        }
        const response = await fetch(
          `/api/ai/sessions/${sessionId}/${endpointName}`,
          init,
        );
        return {
          status: response.status,
          headers: Object.fromEntries(response.headers.entries()),
          body: await response.text(),
        };
      },
      { sessionId: errorSession, endpointName: endpoint },
    );
    expect(failed.status).toBe(502);
    expect(failed.body).not.toContain(upstreamSecret);
    expect(failed.body).not.toContain("CV and salary contents");
    expectPrivateHeaders(failed.headers);
  }
});
