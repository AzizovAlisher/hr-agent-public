"use server";

import { revalidatePath } from "next/cache";

import { apiFetch } from "@/lib/api-client";
import { apiProblemMessage, apiUnavailableMessage } from "@/lib/api-problem";
import type { ApiResponseSchema } from "@/lib/generated/contracts";

/** @ui-only Serializable result returned by a Next.js server action. */
export type EnterpriseActionResult<T = unknown> =
  | { ok: true; data: T }
  | { ok: false; error: string };

export type EnterpriseUsageEvent = ApiResponseSchema<"UsageEventOut">;
export type EnterpriseUsagePage = ApiResponseSchema<"UsagePage">;

async function mutate<T>(
  path: string,
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<EnterpriseActionResult<T>> {
  try {
    const response = await apiFetch(path, {
      method,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      return { ok: false, error: await apiProblemMessage(response) };
    }
    const data = response.status === 204 ? (null as T) : ((await response.json()) as T);
    revalidatePath("/enterprise");
    return { ok: true, data };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return { ok: false, error: await apiUnavailableMessage() };
  }
}

export async function updateEnterprisePolicyAction(body: unknown) {
  return mutate("/enterprise/policy", "PATCH", body);
}

export async function putPermissionOverrideAction(body: unknown) {
  return mutate("/enterprise/permissions", "PUT", body);
}

export async function deletePermissionOverrideAction(id: string) {
  return mutate(`/enterprise/permissions/${encodeURIComponent(id)}`, "DELETE");
}

export async function restoreTrashAction(type: string, id: string) {
  return mutate(
    `/enterprise/trash/${encodeURIComponent(type)}/${encodeURIComponent(id)}/restore`,
    "POST",
  );
}

export async function runRetentionAction(includeActiveCandidates: boolean) {
  return mutate("/enterprise/retention/run", "POST", {
    confirmation: "PURGE",
    include_active_candidates: includeActiveCandidates,
  });
}

export async function createApiKeyAction(body: unknown) {
  return mutate<ApiResponseSchema<"ApiKeyCreatedOut">>(
    "/enterprise/api-keys",
    "POST",
    body,
  );
}

export async function revokeApiKeyAction(id: string) {
  return mutate(`/enterprise/api-keys/${encodeURIComponent(id)}/revoke`, "POST");
}

export async function updateModelConfigurationAction(body: unknown) {
  return mutate("/enterprise/model-configuration", "PUT", body);
}

export async function loadUsagePageAction(
  cursor: string | null = null,
): Promise<EnterpriseActionResult<EnterpriseUsagePage>> {
  try {
    const response = await apiFetch(
      cursor
        ? `/enterprise/usage?limit=50&cursor=${encodeURIComponent(cursor)}`
        : "/enterprise/usage?limit=50",
    );
    if (!response.ok) {
      return {
        ok: false,
        error: await apiProblemMessage(response),
      };
    }
    return { ok: true, data: (await response.json()) as EnterpriseUsagePage };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) {
      throw error;
    }
    return { ok: false, error: await apiUnavailableMessage() };
  }
}
