import { AiPageHeader } from "@/components/ai/ai-page-header";
import { MissionList } from "@/components/ai/mission-list";
import { getAiCopy } from "@/lib/ai/copy";
import { getAiSessions, getHiringMissions } from "@/lib/ai/api";
import { getLocale } from "@/lib/i18n";
import type { AiSession, HiringMission } from "@/lib/ai/types";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function ActiveAiMissionsPage() {
  if (!canUseAiWorkspace(await getCurrentPermissionMap())) return null;
  const locale = await getLocale();
  const copy = getAiCopy(locale);
  let missions: HiringMission[] = [];
  let sessions: AiSession[] = [];
  let unavailable = false;
  const [missionResult, sessionResult] = await Promise.allSettled([
    getHiringMissions(),
    getAiSessions("active"),
  ]);
  if (missionResult.status === "fulfilled") {
    missions = missionResult.value.items.filter((mission) => mission.status !== "completed" && mission.status !== "stopped");
  } else unavailable = true;
  if (sessionResult.status === "fulfilled") sessions = sessionResult.value.items;
  const missionSessionIds = new Set(missions.map((mission) => mission.session_id).filter(Boolean));
  const orphanSessions = sessions.filter((session) => !session.mission_id && !missionSessionIds.has(session.id));

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <AiPageHeader title={copy.page.activeTitle} subtitle={copy.page.activeSubtitle} />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
        {unavailable && !sessions.length ? (
          <div className="rounded-xl border border-score-low/25 bg-score-low/5 p-4 text-[12px] text-score-low">{copy.common.unavailable}</div>
        ) : (
          <MissionList missions={missions} orphanSessions={orphanSessions} locale={locale} copy={copy} emptyTitle={copy.empty.activeTitle} emptyBody={copy.empty.activeBody} />
        )}
      </div>
    </div>
  );
}
