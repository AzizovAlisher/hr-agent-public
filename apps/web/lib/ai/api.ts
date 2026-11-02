import "server-only";

import { apiGetOperation } from "@/lib/api-client";
import {
  normalizeAiActionBundle,
  normalizeAiSession,
  normalizeAiSessionDetail,
  normalizeAiSettings,
  normalizeHiringMission,
  normalizeMissionAssessment,
} from "@/lib/ai/normalize";
import type {
  AiActionBundle,
  AiListPage,
  AiOrganizationSettings,
  AiSession,
  AiSessionDetail,
  HiringMission,
  MissionCandidateAssessment,
  MissionEvent,
} from "@/lib/ai/types";

export async function getAiSessions(
  status?: "active" | "archived",
): Promise<AiListPage<AiSession>> {
  const query = status ? `?status=${status}` : "";
  const page = await apiGetOperation({
    template: "/ai/v1/sessions",
    path: `/ai/v1/sessions${query}`,
  });
  return {
    items: page.items.map(normalizeAiSession),
    next_cursor: page.next_cursor,
  };
}

export async function getAiSession(id: string): Promise<AiSessionDetail> {
  const value = await apiGetOperation({
    template: "/ai/v1/sessions/{session_id}",
    path: `/ai/v1/sessions/${encodeURIComponent(id)}`,
  });
  return normalizeAiSessionDetail(value);
}

export async function getHiringMissions(
  status?: "active" | "completed",
): Promise<AiListPage<HiringMission>> {
  const query = status ? `?status=${status}` : "";
  const page = await apiGetOperation({
    template: "/ai/v1/missions",
    path: `/ai/v1/missions${query}`,
  });
  return {
    items: page.items.map(normalizeHiringMission),
    next_cursor: page.next_cursor,
  };
}

export async function getHiringMission(id: string): Promise<HiringMission> {
  const value = await apiGetOperation({
    template: "/ai/v1/missions/{mission_id}",
    path: `/ai/v1/missions/${encodeURIComponent(id)}`,
  });
  return normalizeHiringMission(value);
}

export async function getMissionCandidates(
  id: string,
): Promise<AiListPage<MissionCandidateAssessment>> {
  const page = await apiGetOperation({
    template: "/ai/v1/missions/{mission_id}/candidates",
    path: `/ai/v1/missions/${encodeURIComponent(id)}/candidates`,
  });
  return {
    items: page.items.map(normalizeMissionAssessment),
    next_cursor: page.next_cursor,
  };
}

export async function getMissionEvents(id: string): Promise<AiListPage<MissionEvent>> {
  const page = await apiGetOperation({
    template: "/ai/v1/missions/{mission_id}/events",
    path: `/ai/v1/missions/${encodeURIComponent(id)}/events`,
  });
  return { items: page.items, next_cursor: page.next_cursor };
}

export async function getAiActionBundles(
  missionId?: string,
): Promise<AiListPage<AiActionBundle>> {
  const query = missionId ? `?mission_id=${encodeURIComponent(missionId)}` : "";
  const page = await apiGetOperation({
    template: "/ai/v1/action-bundles",
    path: `/ai/v1/action-bundles${query}`,
  });
  return {
    items: page.items.map(normalizeAiActionBundle),
    next_cursor: page.next_cursor,
  };
}

export async function getAiSettings(): Promise<AiOrganizationSettings> {
  const value = await apiGetOperation({
    template: "/ai/v1/settings",
    path: "/ai/v1/settings",
  });
  return normalizeAiSettings(value);
}
