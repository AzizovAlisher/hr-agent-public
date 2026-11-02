"use server";

import { randomUUID } from "node:crypto";
import { revalidatePath } from "next/cache";

import { apiJsonOperation } from "@/lib/api-client";
import { apiUnavailableMessage } from "@/lib/api-problem";
import {
  normalizeAiActionBundle,
  normalizeAiSession,
  normalizeAiSettings,
  normalizeHiringMission,
} from "@/lib/ai/normalize";
import type {
  AiActionBundle,
  AiOrganizationSettings,
  AiSession,
  HiringMission,
} from "@/lib/ai/types";

/** @ui-only Serializable result returned by a Next.js server action. */
export type AiActionResult<T = unknown> =
  | { ok: true; data: T }
  | { ok: false; error: string };

async function normalizedResult<T>(
  request: () => Promise<unknown>,
  normalize: (value: unknown) => T,
): Promise<AiActionResult<T>> {
  try {
    return { ok: true, data: normalize(await request()) };
  } catch (error) {
    if ((error as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw error;
    const message = error instanceof Error ? error.message : "";
    return {
      ok: false,
      error:
        message && !/fetch failed|ECONN|ENOTFOUND|stack|trace/i.test(message)
          ? message
          : await apiUnavailableMessage(),
    };
  }
}

export async function createAiSessionAction(input?: {
  title?: string;
}): Promise<AiActionResult<AiSession>> {
  const result = await normalizedResult(
    () => apiJsonOperation({
      template: "/ai/v1/sessions",
      path: "/ai/v1/sessions",
      method: "post",
      body: input ?? {},
    }),
    normalizeAiSession,
  );
  if (result.ok) revalidatePath("/ai");
  return result;
}

export async function archiveAiSessionAction(
  sessionId: string,
): Promise<AiActionResult<AiSession>> {
  const result = await normalizedResult(
    () => apiJsonOperation({
      template: "/ai/v1/sessions/{session_id}/archive",
      path: `/ai/v1/sessions/${encodeURIComponent(sessionId)}/archive`,
      method: "post",
    }),
    normalizeAiSession,
  );
  if (result.ok) revalidatePath("/ai", "layout");
  return result;
}

async function missionMutation(
  missionId: string,
  action: "stop" | "retry",
): Promise<AiActionResult<HiringMission>> {
  return normalizedResult(
    () =>
      action === "stop"
        ? apiJsonOperation({
            template: "/ai/v1/missions/{mission_id}/stop",
            path: `/ai/v1/missions/${encodeURIComponent(missionId)}/stop`,
            method: "post",
          })
        : apiJsonOperation({
            template: "/ai/v1/missions/{mission_id}/retry",
            path: `/ai/v1/missions/${encodeURIComponent(missionId)}/retry`,
            method: "post",
          }),
    normalizeHiringMission,
  );
}

export async function stopHiringMissionAction(
  missionId: string,
): Promise<AiActionResult<HiringMission>> {
  const result = await missionMutation(missionId, "stop");
  if (result.ok) revalidatePath(`/ai/missions/${missionId}`);
  return result;
}

export async function retryHiringMissionAction(
  missionId: string,
): Promise<AiActionResult<HiringMission>> {
  const result = await missionMutation(missionId, "retry");
  if (result.ok) revalidatePath(`/ai/missions/${missionId}`);
  return result;
}

export async function replanHiringMissionAction(
  missionId: string,
  request?: string,
): Promise<AiActionResult<HiringMission>> {
  const result = await normalizedResult(
    () => apiJsonOperation({
      template: "/ai/v1/missions/{mission_id}/replan",
      path: `/ai/v1/missions/${encodeURIComponent(missionId)}/replan`,
      method: "post",
      body: request ? { request } : null,
    }),
    normalizeHiringMission,
  );
  if (result.ok) revalidatePath(`/ai/missions/${missionId}`);
  return result;
}

export async function decideAiActionBundleAction(
  bundleId: string,
  decision: "approve" | "reject",
): Promise<AiActionResult<AiActionBundle>> {
  const headers = { "Idempotency-Key": randomUUID() };
  const result = await normalizedResult(
    () =>
      decision === "approve"
        ? apiJsonOperation({
            template: "/ai/v1/action-bundles/{bundle_id}/approve",
            path: `/ai/v1/action-bundles/${encodeURIComponent(bundleId)}/approve`,
            method: "post",
            headers,
            body: {},
          })
        : apiJsonOperation({
            template: "/ai/v1/action-bundles/{bundle_id}/reject",
            path: `/ai/v1/action-bundles/${encodeURIComponent(bundleId)}/reject`,
            method: "post",
            headers,
            body: {},
          }),
    normalizeAiActionBundle,
  );
  if (result.ok) {
    revalidatePath("/ai/approvals");
    revalidatePath(`/ai/missions/${result.data.mission_id}`);
  }
  return result;
}

export async function updateAiSettingsAction(
  settings: AiOrganizationSettings,
): Promise<AiActionResult<AiOrganizationSettings>> {
  const { updated_at: _updatedAt, ...body } = settings;
  const result = await normalizedResult(
    () => apiJsonOperation({
      template: "/ai/v1/settings",
      path: "/ai/v1/settings",
      method: "put",
      body,
    }),
    normalizeAiSettings,
  );
  if (result.ok) revalidatePath("/ai", "layout");
  return result;
}
