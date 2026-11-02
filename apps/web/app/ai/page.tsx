import { redirect } from "next/navigation";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace, hasPermission } from "@/lib/permissions";

export default async function AiPage() {
  const permissions = await getCurrentPermissionMap();
  if (canUseAiWorkspace(permissions)) redirect("/ai/new");
  if (hasPermission(permissions, "ai.manage")) redirect("/ai/settings");
  redirect("/my-work");
}
