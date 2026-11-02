import { auth } from "@/auth";
import { AiFeatureGate } from "@/components/ai/ai-feature-gate";
import { AiShell } from "@/components/ai/ai-shell";
import { getAiSettings, getHiringMissions } from "@/lib/ai/api";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace, hasPermission } from "@/lib/permissions";

function initials(email: string): string {
  const local = email.split("@")[0] ?? "";
  const parts = local.replace(/[._-]/g, " ").split(" ").filter(Boolean);
  return ((parts[0]?.[0] ?? email[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

export default async function AiLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  const email = session?.user?.email ?? "guest";
  const permissions = await getCurrentPermissionMap();
  const canUse = canUseAiWorkspace(permissions);
  const canManage = hasPermission(permissions, "ai.manage");
  let enabled = false;
  let unavailable = false;
  try {
    if (canManage) {
      enabled = (await getAiSettings()).feature_enabled;
    } else if (canUse) {
      // The settings document itself requires ai.manage. A small permitted
      // read verifies the tenant feature state for AI users.
      await getHiringMissions();
      enabled = true;
    }
  } catch {
    unavailable = true;
  }
  return (
    <AiShell
      organizationName={session?.user?.organization?.name ?? "Workspace"}
      access={{ canUse, canManage }}
      user={{
        email,
        role: session?.user?.role ?? "user",
        initials: initials(email),
      }}
    >
      <AiFeatureGate
        enabled={enabled}
        unavailable={unavailable}
        canUse={canUse}
        canManage={canManage}
      >
        {children}
      </AiFeatureGate>
    </AiShell>
  );
}
