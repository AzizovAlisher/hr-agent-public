import { AiPageHeader } from "@/components/ai/ai-page-header";
import { MissionList } from "@/components/ai/mission-list";
import { getHiringMissions } from "@/lib/ai/api";
import { getAiCopy } from "@/lib/ai/copy";
import { getLocale } from "@/lib/i18n";
import type { HiringMission } from "@/lib/ai/types";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function CompletedAiMissionsPage() {
  if (!canUseAiWorkspace(await getCurrentPermissionMap())) return null;
  const locale = await getLocale();
  const copy = getAiCopy(locale);
  let missions: HiringMission[] = [];
  let unavailable = false;
  try {
    const page = await getHiringMissions();
    missions = page.items.filter((mission) => mission.status === "completed" || mission.status === "stopped");
  } catch {
    unavailable = true;
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <AiPageHeader title={copy.page.completedTitle} subtitle={copy.page.completedSubtitle} />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">
        {unavailable ? (
          <div className="rounded-xl border border-score-low/25 bg-score-low/5 p-4 text-[12px] text-score-low">{copy.common.unavailable}</div>
        ) : (
          <MissionList missions={missions} locale={locale} copy={copy} emptyTitle={copy.empty.completedTitle} emptyBody={copy.empty.completedBody} />
        )}
      </div>
    </div>
  );
}
