import { notFound } from "next/navigation";

import { MissionWorkspace } from "@/components/ai/mission-workspace";
import {
  getAiActionBundles,
  getAiSession,
  getHiringMission,
  getMissionCandidates,
  getMissionEvents,
} from "@/lib/ai/api";
import type { AiSessionDetail } from "@/lib/ai/types";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function AiMissionPage({ params }: { params: Promise<{ id: string }> }) {
  if (!canUseAiWorkspace(await getCurrentPermissionMap())) return null;
  const { id } = await params;
  let mission;
  try {
    mission = await getHiringMission(id);
  } catch {
    notFound();
  }

  const [candidateResult, eventResult, bundleResult, sessionResult] = await Promise.allSettled([
    getMissionCandidates(id),
    getMissionEvents(id),
    getAiActionBundles(id),
    mission.session_id ? getAiSession(mission.session_id) : Promise.resolve(null),
  ]);
  const emptySession: AiSessionDetail = {
    id: mission.session_id ?? `mission-${mission.id}`,
    title: mission.title,
    status: "active",
    created_by_user_id: mission.created_by_user_id ?? null,
    creator_user_id: mission.created_by_user_id ?? null,
    mission_id: mission.id,
    mission_status: mission.status,
    messages: [],
    mission,
    created_at: mission.created_at,
    updated_at: mission.updated_at,
    archived_at: null,
  };
  const session = sessionResult.status === "fulfilled" && sessionResult.value
    ? sessionResult.value
    : emptySession;

  return (
    <MissionWorkspace
      initialSession={session}
      initialMission={mission}
      initialCandidates={candidateResult.status === "fulfilled" ? candidateResult.value.items : []}
      initialEvents={eventResult.status === "fulfilled" ? eventResult.value.items : []}
      initialBundles={bundleResult.status === "fulfilled" ? bundleResult.value.items : []}
      partialFailure={[candidateResult, eventResult, bundleResult, sessionResult].some((result) => result.status === "rejected")}
    />
  );
}
