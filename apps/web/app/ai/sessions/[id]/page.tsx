import { notFound } from "next/navigation";

import { MissionWorkspace } from "@/components/ai/mission-workspace";
import { getAiSession } from "@/lib/ai/api";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function AiSessionPage({ params }: { params: Promise<{ id: string }> }) {
  if (!canUseAiWorkspace(await getCurrentPermissionMap())) return null;
  const { id } = await params;
  let session;
  try {
    session = await getAiSession(id);
  } catch {
    notFound();
  }
  return (
    <MissionWorkspace
      initialSession={session}
      initialMission={session.mission}
      initialCandidates={[]}
      initialEvents={[]}
      initialBundles={[]}
    />
  );
}
