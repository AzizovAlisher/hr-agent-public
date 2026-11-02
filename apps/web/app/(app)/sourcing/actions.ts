"use server";

import { revalidatePath } from "next/cache";

import { apiFetch, type CursorPage } from "@/lib/api-client";
import { apiProblemMessage, apiUnavailableMessage } from "@/lib/api-problem";
import type {
  ReferralLink,
  ReferralLinkCreated,
  SourceAnalytics,
  SourceImportRun,
  SourceImportFilePreview,
  SourceImportOutcome,
  SourceImportRunPage,
  SourceIntegration,
  SourceIntegrationCreated,
  SourceIntegrationPage,
  SourceJob,
  SourceProvider,
  SourcePublication,
  SourceWebhookSecret,
} from "@/app/(app)/sourcing/types";

/** @ui-only Serializable result returned by a Next.js server action. */
export type SourcingActionResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<SourcingActionResult<T>> {
  try {
    const response = await apiFetch(path, init);
    if (!response.ok) return { ok: false, error: await apiProblemMessage(response) };
    const data = response.status === 204 ? (null as T) : ((await response.json()) as T);
    return { ok: true, data };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return { ok: false, error: await apiUnavailableMessage() };
  }
}

async function requestAll<T>(path: string): Promise<SourcingActionResult<T[]>> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const [pathname, rawQuery = ""] = path.split("?", 2);
    const params = new URLSearchParams(rawQuery);
    if (!params.has("limit")) params.set("limit", "100");
    if (cursor) params.set("cursor", cursor);
    const result = await request<CursorPage<T>>(
      `${pathname}?${params.toString()}`,
    );
    if (!result.ok) return result;
    items.push(...result.data.items);
    cursor = result.data.next_cursor;
    if (!cursor) return { ok: true, data: items };
    if (seen.has(cursor)) {
      return { ok: false, error: "API returned a repeated collection cursor" };
    }
    seen.add(cursor);
  }
  return { ok: false, error: "API collection exceeded the safe page traversal limit" };
}

async function mutate<T>(
  path: string,
  method: "POST" | "PATCH",
  body?: unknown,
): Promise<SourcingActionResult<T>> {
  const result = await request<T>(path, {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (result.ok) revalidatePath("/sourcing");
  return result;
}

export async function createSourceIntegrationAction(body: {
  provider: SourceProvider;
  name: string;
  connection_id: string | null;
  configuration: Record<string, unknown>;
}) {
  return mutate<SourceIntegrationCreated>("/source-integrations", "POST", body);
}

export async function updateSourceIntegrationAction(
  integrationId: string,
  body: {
    name?: string;
    connection_id?: string | null;
    configuration?: Record<string, unknown>;
    is_active?: boolean;
  },
) {
  return mutate<SourceIntegration>(
    `/source-integrations/${encodeURIComponent(integrationId)}`,
    "PATCH",
    body,
  );
}

export async function rotateSourceWebhookSecretAction(integrationId: string) {
  return mutate<SourceWebhookSecret>(
    `/source-integrations/${encodeURIComponent(integrationId)}/rotate-webhook-secret`,
    "POST",
  );
}

export async function importSourceFileAction(
  integrationId: string,
  formData: FormData,
) {
  const file = formData.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, error: "Select a CSV or XLSX file" } satisfies SourcingActionResult<SourceImportOutcome>;
  }
  const upload = new FormData();
  upload.set("file", file, file.name);
  const mapping = formData.get("mapping");
  if (typeof mapping === "string" && mapping.trim()) {
    upload.set("mapping", mapping);
  }
  const rawKey = formData.get("idempotency_key");
  const idempotencyKey =
    typeof rawKey === "string" && /^[A-Za-z0-9._:-]{1,128}$/.test(rawKey)
      ? rawKey
      : `source-import-${crypto.randomUUID()}`;
  const result = await request<SourceImportOutcome>(
    `/source-integrations/${encodeURIComponent(integrationId)}/import-file`,
    {
      method: "POST",
      body: upload,
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (result.ok) revalidatePath("/sourcing");
  return result;
}

export async function previewSourceFileAction(
  integrationId: string,
  formData: FormData,
) {
  const file = formData.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, error: "Select a CSV or XLSX file" } satisfies SourcingActionResult<SourceImportFilePreview>;
  }
  const upload = new FormData();
  upload.set("file", file, file.name);
  const mapping = formData.get("mapping");
  if (typeof mapping === "string" && mapping.trim()) {
    upload.set("mapping", mapping);
  }
  return request<SourceImportFilePreview>(
    `/source-integrations/${encodeURIComponent(integrationId)}/import-file/preview`,
    { method: "POST", body: upload },
  );
}

export async function listSourceImportRunsAction() {
  return request<SourceImportRunPage>("/source-integrations/runs?limit=50");
}

export async function listSourceIntegrationsAction(cursor: string) {
  return request<SourceIntegrationPage>(
    `/source-integrations?limit=50&cursor=${encodeURIComponent(cursor)}`,
  );
}

export async function enqueueHeadHunterImportAction(
  integrationId: string,
  body: {
    position_id: string;
    vacancy_id: string;
    collection_ids: string[];
    max_pages: number;
    max_records: number;
    resume_from_cursor: boolean;
    idempotency_key: string;
  },
) {
  return mutate<SourceJob>(
    `/source-integrations/${encodeURIComponent(integrationId)}/hh/imports`,
    "POST",
    body,
  );
}

export async function listReferralLinksAction(positionId: string) {
  return requestAll<ReferralLink>(
    `/positions/${encodeURIComponent(positionId)}/referral-links`,
  );
}

export async function createReferralLinkAction(
  positionId: string,
  body: {
    label: string;
    campaign: string | null;
    source: string;
    expires_at: string | null;
    max_applications: number | null;
  },
) {
  return mutate<ReferralLinkCreated>(
    `/positions/${encodeURIComponent(positionId)}/referral-links`,
    "POST",
    body,
  );
}

export async function revokeReferralLinkAction(linkId: string) {
  return mutate<ReferralLink>(
    `/referral-links/${encodeURIComponent(linkId)}/revoke`,
    "POST",
  );
}

export async function rotateReferralLinkAction(linkId: string) {
  return mutate<ReferralLinkCreated>(
    `/referral-links/${encodeURIComponent(linkId)}/rotate`,
    "POST",
    { expires_in_days: 30 },
  );
}

export async function previewSourcePublicationAction(
  integrationId: string,
  positionId: string,
  locale: "ru" | "en",
) {
  return mutate<SourcePublication>(
    `/source-integrations/${encodeURIComponent(integrationId)}/publications/preview`,
    "POST",
    { position_id: positionId, locale },
  );
}

export async function publishSourcePublicationAction(
  integrationId: string,
  positionId: string,
  locale: "ru" | "en",
) {
  return mutate<SourcePublication>(
    `/source-integrations/${encodeURIComponent(integrationId)}/publications`,
    "POST",
    { position_id: positionId, locale },
  );
}

export async function getSourceAnalyticsAction() {
  return request<SourceAnalytics>("/sourcing/analytics");
}
