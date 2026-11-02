import Link from "next/link";
import { Folders, Sparkles } from "lucide-react";

import { apiGetAll } from "@/lib/api-client";
import { getMessages } from "@/lib/i18n";
import type { CandidateSummary, Folder, Position } from "@/lib/types";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { StatsStrip } from "@/components/dashboard/stats-strip";
import { PositionsList } from "@/components/positions/positions-list";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getCandidateWorkspaceAccess } from "@/lib/permissions";

export const dynamic = "force-dynamic";

/** @ui-only Next.js route query state; it never crosses the API transport boundary. */
type DashboardPageProps = {
  searchParams: Promise<{ folder_id?: string | string[] }>;
};

export default async function DashboardPage({
  searchParams,
}: DashboardPageProps) {
  const rawSearchParams = await searchParams;
  const initialFolderId =
    typeof rawSearchParams.folder_id === "string"
      ? rawSearchParams.folder_id
      : null;
  const [m, permissionMap] = await Promise.all([
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  const access = getCandidateWorkspaceAccess(permissionMap);
  let positions: Position[] = [];
  let candidates: CandidateSummary[] = [];
  let folders: Folder[] = [];
  let error: string | null = null;
  try {
    [positions, candidates, folders] = await Promise.all([
      // Dashboard opts into archived-exclusion; the bare /positions used
      // elsewhere (new-position picker, etc.) is left unchanged.
      apiGetAll<Position>("/positions?exclude_archived=true"),
      apiGetAll<CandidateSummary>("/candidates"),
      apiGetAll<Folder>("/folders"),
    ]);
  } catch (e) {
    if ((e as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) throw e;
    error = (e as Error).message;
  }

  const openCount = positions.filter((p) => p.status === "open").length;
  // Count UNIQUE candidates that have been scored against at least one
  // position. The previous implementation summed `candidate_count` across
  // positions, which double-counts anyone scored against multiple roles.
  const totalCandidates = candidates.filter(
    (c) => c.best_score != null,
  ).length;

  return (
    <>
      <Topbar
        crumbs={[{ label: m.app.nav.positions }]}
        actions={
          <>
            <LocaleSwitcher />
            {access.createPositions ? (
              <>
                <Link
                  href="/positions/project"
                  className="press inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg-elevated px-3 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover"
                >
                  <Folders className="h-3.5 w-3.5" />
                  {m.newProject.dashboardCta}
                </Link>
                <Link
                  href="/positions/new"
                  className="press group relative inline-flex items-center gap-1.5 overflow-hidden rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-all hover:opacity-90"
                >
                  <Sparkles className="h-3.5 w-3.5 transition-transform group-hover:rotate-12" />
                  {m.dashboard.newPosition}
                </Link>
              </>
            ) : null}
          </>
        }
      />

      <div className="flex-1 overflow-y-auto overflow-x-hidden px-7 py-8">
        <header className="mb-7 flex items-end justify-between gap-6">
          <div>
            <h1 className="mb-1 text-[24px] font-semibold tracking-[-0.025em]">
              {m.dashboard.title}
            </h1>
            {/* Suppressed on error so we don't show a fabricated "0 · 0" summary
                above the error card. */}
            {error ? null : (
              <p className="text-[13px] text-text-muted">
                {m.dashboard.summary(openCount, totalCandidates)}
              </p>
            )}
          </div>
        </header>

        {error ? (
          <div className="rounded-lg border border-border bg-bg-elevated p-5 text-[13px] text-score-low">
            {error}
          </div>
        ) : (
          <>
            <StatsStrip positions={positions} candidates={candidates} />
            <PositionsList
              positions={positions}
              folders={folders}
              canManage={access.writeCandidates}
              initialFolderId={initialFolderId}
            />
          </>
        )}
      </div>
    </>
  );
}
