"use server";

import { revalidatePath } from "next/cache";

import type {
  IntegrationConnection,
  IntegrationConnectionCreated,
  IntegrationConnectionPage,
  IntegrationCredentials,
  IntegrationHealth,
  IntegrationJob,
  IntegrationJobPage,
  IntegrationOAuthStart,
  IntegrationProvider,
} from "@/app/(app)/integrations/types";
import { apiFetch } from "@/lib/api-client";
import {
  apiProblemMessage,
  apiUnavailableMessage,
  readApiProblem,
} from "@/lib/api-problem";

/** @ui-only Serializable result returned by a Next.js server action. */
export type IntegrationActionResult<T> =
  | { ok: true; data: T }
  | { ok: false; code: string; error: string };

async function responseError(response: Response): Promise<{ code: string; error: string }> {
  const [problem, error] = await Promise.all([
    readApiProblem(response.clone()),
    apiProblemMessage(response),
  ]);
  return { code: problem.code, error };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<IntegrationActionResult<T>> {
  try {
    const response = await apiFetch(path, init);
    if (!response.ok) return { ok: false, ...(await responseError(response)) };
    return { ok: true, data: (await response.json()) as T };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    return {
      ok: false,
      code: "integration_unavailable",
      error: await apiUnavailableMessage(),
    };
  }
}

async function mutate<T>(path: string, method: "POST" | "PATCH" | "PUT", body?: unknown) {
  const result = await request<T>(path, {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (result.ok) {
    revalidatePath("/integrations");
    revalidatePath("/sourcing");
  }
  return result;
}

export async function listIntegrationConnectionsAction(cursor: string) {
  return request<IntegrationConnectionPage>(
    `/integrations?limit=50&cursor=${encodeURIComponent(cursor)}`,
  );
}

export async function listIntegrationJobsAction(cursor: string) {
  return request<IntegrationJobPage>(
    `/integrations/jobs?limit=50&cursor=${encodeURIComponent(cursor)}`,
  );
}

export async function createIntegrationConnectionAction(body: {
  provider: IntegrationProvider;
  name: string;
  configuration: Record<string, unknown>;
}) {
  return mutate<IntegrationConnectionCreated>("/integrations", "POST", body);
}

export async function updateIntegrationConnectionAction(
  connectionId: string,
  body: { name?: string; configuration?: Record<string, unknown> },
) {
  return mutate<IntegrationConnection>(
    `/integrations/${encodeURIComponent(connectionId)}`,
    "PATCH",
    body,
  );
}

export async function setIntegrationCredentialsAction(
  connectionId: string,
  credentials: Record<string, string>,
) {
  return mutate<IntegrationCredentials>(`/integrations/${encodeURIComponent(connectionId)}/credentials`, "PUT", {
    credentials,
  });
}

export async function startIntegrationOAuthAction(
  connectionId: string,
  redirectUri: string,
) {
  return mutate<IntegrationOAuthStart>(
    `/integrations/${encodeURIComponent(connectionId)}/oauth/start`,
    "POST",
    { redirect_uri: redirectUri },
  );
}

export async function finishIntegrationOAuthAction(state: string, code: string) {
  return mutate<IntegrationConnection>("/integrations/oauth/callback", "POST", {
    state,
    code,
  });
}

export async function checkIntegrationHealthAction(connectionId: string) {
  return mutate<IntegrationHealth>(
    `/integrations/${encodeURIComponent(connectionId)}/health`,
    "POST",
  );
}

export async function revokeIntegrationConnectionAction(connectionId: string) {
  return mutate<IntegrationConnection>(
    `/integrations/${encodeURIComponent(connectionId)}/revoke`,
    "POST",
  );
}

export async function cancelIntegrationJobAction(jobId: string) {
  return mutate<IntegrationJob>(
    `/integrations/jobs/${encodeURIComponent(jobId)}/cancel`,
    "POST",
  );
}

export async function replayIntegrationJobAction(jobId: string) {
  return mutate<IntegrationJob>(
    `/integrations/jobs/${encodeURIComponent(jobId)}/replay`,
    "POST",
  );
}

export async function runIntegrationJobAction(jobId: string) {
  return mutate<IntegrationJob>(
    `/integrations/jobs/${encodeURIComponent(jobId)}/run`,
    "POST",
  );
}
