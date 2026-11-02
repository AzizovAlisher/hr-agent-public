import { expect, test, type Page } from "@playwright/test";

const appURL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `https://localhost:${process.env.PLAYWRIGHT_HTTPS_PORT ?? "3443"}`;

const identityA = {
  email: "draft-a@example.test",
  organizationId: "60000000-0000-4000-8000-000000000001",
  userId: "60000000-0000-4000-8000-000000000002",
};
const identityB = {
  email: "draft-b@example.test",
  organizationId: "60000000-0000-4000-8000-000000000003",
  userId: "60000000-0000-4000-8000-000000000004",
};

function storageKey(identity: typeof identityA) {
  const scope = `${identity.organizationId}:${identity.userId}`;
  return `hr.position-drafts.v3:${encodeURIComponent(scope)}`;
}

function draftStore(canary: string) {
  const id = "draft-isolation-fixture";
  return {
    active: id,
    drafts: {
      [id]: {
        id,
        name: canary,
        updatedAt: Date.now(),
        messages: [
          {
            id: "canary-message",
            role: "user",
            content: canary,
            ts: Date.now(),
          },
        ],
        draft: {
          title: canary,
          description: "Confidential draft description",
          requirements: { skills: ["tenant-isolation"] },
          seniority: null,
          work_format: null,
          location: null,
          employment_type: null,
          salary_range: "Confidential salary",
          team_size: null,
          folder: null,
        },
        isComplete: true,
        missing: [],
        quickReplies: [],
      },
    },
  };
}

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

async function switchIdentity(page: Page, email: string) {
  // Keep localStorage to reproduce a shared-browser identity switch while
  // removing both Auth.js and backend session cookies. Leave the protected
  // application document first so its auth redirect cannot race the explicit
  // login navigation after cookies disappear (notably in WebKit).
  await page.goto("about:blank");
  await page.context().clearCookies();
  await signIn(page, email);
}

test("position drafts remain scoped to the same organization and user", async ({
  page,
}) => {
  const tenantACanary = "TENANT_A_CONFIDENTIAL_POSITION_DRAFT";
  const unscopedCanary = "UNSCOPED_LEGACY_DRAFT_MUST_BE_DISCARDED";

  await signIn(page, identityA.email);
  await page.goto("/positions/new");
  await page.evaluate(
    ({ scopedKey, scopedStore, legacyStore }) => {
      const legacySlot = Object.values(legacyStore.drafts)[0];
      window.localStorage.setItem(scopedKey, JSON.stringify(scopedStore));
      window.localStorage.setItem(
        "hr.position-drafts.v2",
        JSON.stringify(legacyStore),
      );
      window.localStorage.setItem(
        "hr.new-position-draft.v1",
        JSON.stringify(legacySlot),
      );
    },
    {
      scopedKey: storageKey(identityA),
      scopedStore: draftStore(tenantACanary),
      legacyStore: draftStore(unscopedCanary),
    },
  );
  await page.reload();

  await expect(page.getByText(tenantACanary, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(unscopedCanary, { exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(() => ({
      v1: window.localStorage.getItem("hr.new-position-draft.v1"),
      v2: window.localStorage.getItem("hr.position-drafts.v2"),
    })),
  ).toEqual({ v1: null, v2: null });

  await switchIdentity(page, identityB.email);
  await page.goto("/positions/new");
  await expect(page.getByText(tenantACanary, { exact: true })).toHaveCount(0);
  expect(await page.evaluate((key) => localStorage.getItem(key), storageKey(identityB))).toBe(
    null,
  );

  await switchIdentity(page, identityA.email);
  await page.goto("/positions/new");
  await expect(page.getByText(tenantACanary, { exact: true }).first()).toBeVisible();
});
