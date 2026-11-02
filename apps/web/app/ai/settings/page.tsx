import { AiPageHeader } from "@/components/ai/ai-page-header";
import { AiSettingsForm } from "@/components/ai/ai-settings-form";
import { getAiSettings } from "@/lib/ai/api";
import { getAiCopy } from "@/lib/ai/copy";
import { getLocale } from "@/lib/i18n";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { hasPermission } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function AiSettingsPage() {
  if (!hasPermission(await getCurrentPermissionMap(), "ai.manage")) return null;
  const locale = await getLocale();
  const copy = getAiCopy(locale);
  let settings = null;
  try {
    settings = await getAiSettings();
  } catch {
    // The feature gate already distinguishes disabled from unavailable.
  }
  return <div className="flex min-h-0 flex-1 flex-col"><AiPageHeader title={copy.page.settingsTitle} subtitle={copy.page.settingsSubtitle} /><div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">{settings ? <AiSettingsForm initialSettings={settings} /> : <div className="rounded-xl border border-score-low/25 bg-score-low/5 p-4 text-[12px] text-score-low">{copy.common.unavailable}</div>}</div></div>;
}
