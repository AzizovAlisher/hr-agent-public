import { AiPageHeader } from "@/components/ai/ai-page-header";
import { ApprovalInbox } from "@/components/ai/approval-inbox";
import { getAiActionBundles } from "@/lib/ai/api";
import { getAiCopy } from "@/lib/ai/copy";
import { getLocale } from "@/lib/i18n";
import type { AiActionBundle } from "@/lib/ai/types";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { canUseAiWorkspace } from "@/lib/permissions";

export const dynamic = "force-dynamic";

export default async function AiApprovalsPage() {
  if (!canUseAiWorkspace(await getCurrentPermissionMap())) return null;
  const locale = await getLocale();
  const copy = getAiCopy(locale);
  let bundles: AiActionBundle[] = [];
  let error = false;
  try {
    bundles = (await getAiActionBundles()).items;
  } catch {
    error = true;
  }
  return <div className="flex min-h-0 flex-1 flex-col"><AiPageHeader title={copy.page.approvalsTitle} subtitle={copy.page.approvalsSubtitle} /><div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8">{error ? <div className="rounded-xl border border-score-low/25 bg-score-low/5 p-4 text-[12px] text-score-low">{copy.common.unavailable}</div> : <ApprovalInbox initialBundles={bundles} />}</div></div>;
}
