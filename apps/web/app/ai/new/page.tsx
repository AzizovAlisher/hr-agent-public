import { NewMissionForm } from "@/components/ai/new-mission-form";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function NewAiMissionPage() {
  if (!canUseAiWorkspace(await getCurrentPermissionMap())) return null;
  return <div className="min-h-0 flex-1 overflow-y-auto"><NewMissionForm /></div>;
}
