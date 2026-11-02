"use server";

import { revalidatePath } from "next/cache";

import { apiFetch } from "@/lib/api-client";
import { apiProblemMessage, apiUnavailableMessage } from "@/lib/api-problem";
import type { ApiResponseSchema } from "@/lib/generated/contracts";

/** @ui-only Serializable result returned by a Next.js server action. */
type Result<T> = { ok: true; data: T } | { ok: false; error: string };
type TalentCandidatePage = ApiResponseSchema<"TalentCandidatePage">;

async function request<T>(path: string, init: RequestInit = {}): Promise<Result<T>> {
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

async function mutate<T>(path: string, method: string, body?: unknown): Promise<Result<T>> {
  const result = await request<T>(path, {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (result.ok) revalidatePath("/talent");
  return result;
}

export async function createTalentPoolAction(body: unknown) {
  return mutate("/talent/pools", "POST", body);
}

export async function deleteTalentPoolAction(id: string) {
  return mutate(`/talent/pools/${encodeURIComponent(id)}`, "DELETE");
}

export async function listTalentPoolMembersAction(id: string, cursor: string | null = null) {
  const params = new URLSearchParams({ limit: "100" });
  if (cursor) params.set("cursor", cursor);
  return request<TalentCandidatePage>(
    `/talent/pools/${encodeURIComponent(id)}/members?${params.toString()}`,
  );
}

export async function addTalentPoolMembersAction(id: string, candidateIds: string[]) {
  return mutate(`/talent/pools/${encodeURIComponent(id)}/members`, "POST", {
    candidate_ids: candidateIds,
  });
}

export async function removeTalentPoolMemberAction(id: string, candidateId: string) {
  return mutate(
    `/talent/pools/${encodeURIComponent(id)}/members/${encodeURIComponent(candidateId)}`,
    "DELETE",
  );
}

export async function searchTalentAction(
  query: string,
  poolId?: string,
  cursor: string | null = null,
) {
  const params = new URLSearchParams({ q: query, limit: "50" });
  if (poolId) params.set("pool_id", poolId);
  if (cursor) params.set("cursor", cursor);
  return request<TalentCandidatePage>(`/talent/search?${params}`);
}

export async function rediscoverTalentAction(
  positionId: string,
  cursor: string | null = null,
) {
  const params = new URLSearchParams({ limit: "50" });
  if (cursor) params.set("cursor", cursor);
  return request<TalentCandidatePage>(
    `/positions/${encodeURIComponent(positionId)}/talent-matches?${params.toString()}`,
  );
}

export async function createNurtureSequenceAction(body: unknown) {
  return mutate("/talent/nurture-sequences", "POST", body);
}

export async function launchNurtureSequenceAction(id: string, positionId?: string) {
  return mutate(`/talent/nurture-sequences/${encodeURIComponent(id)}/launch`, "POST", {
    candidate_ids: null,
    position_id: positionId || null,
  });
}
