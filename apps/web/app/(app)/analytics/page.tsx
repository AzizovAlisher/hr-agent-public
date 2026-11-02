import Link from "next/link";
import { Download, Filter } from "lucide-react";

import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { AnalyticsDashboard } from "@/components/dashboard/pipeline-analytics";
import { apiGet } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getMessages } from "@/lib/i18n";
import { hasPermission } from "@/lib/permissions";
import type {
  AnalyticsDrilldownDimension,
  ApplicationAnalytics,
} from "@/lib/types";

export const dynamic = "force-dynamic";

/** @ui-only Next.js analytics-filter query state. */
type SearchParams = Record<string, string | string[] | undefined>;

const DRILLDOWNS = new Set<AnalyticsDrilldownDimension>([
  "all",
  "stage_current",
  "stage_entered",
  "source",
  "owner",
  "disposition",
]);

function first(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

function buildAnalyticsQuery(raw: SearchParams): URLSearchParams {
  const query = new URLSearchParams();
  const timezone = first(raw.timezone) || "Asia/Dushanbe";
  query.set("timezone", timezone);
  for (const key of [
    "start_at",
    "end_at",
    "start_date",
    "end_date",
    "position_id",
    "source",
    "owner",
    "drilldown_key",
    "drilldown_cursor",
  ]) {
    const value = first(raw[key]);
    if (value) query.set(key, value);
  }
  const dimension = first(raw.drilldown_dimension) as
    | AnalyticsDrilldownDimension
    | undefined;
  if (dimension && DRILLDOWNS.has(dimension)) {
    query.set("drilldown_dimension", dimension);
  }
  return query;
}

export default async function AnalyticsPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const raw = await searchParams;
  const query = buildAnalyticsQuery(raw);
  const [m, permissionMap] = await Promise.all([
    getMessages(),
    getCurrentPermissionMap(),
  ]);
  const t = m.analytics;

  let analytics: ApplicationAnalytics | null = null;
  let error: string | null = null;
  try {
    analytics = await apiGet<ApplicationAnalytics>(
      `/analytics/pipeline?${query.toString()}`,
    );
  } catch (cause) {
    if ((cause as { digest?: string })?.digest?.startsWith("NEXT_REDIRECT")) {
      throw cause;
    }
    error = (cause as Error).message;
  }

  const resolvedQuery = new URLSearchParams(query);
  if (
    analytics &&
    !resolvedQuery.has("start_at") &&
    !resolvedQuery.has("start_date")
  ) {
    resolvedQuery.set("start_at", analytics.start_at);
    resolvedQuery.set("end_at", analytics.end_at);
  }
  const exportQuery = new URLSearchParams(resolvedQuery);
  exportQuery.delete("drilldown_cursor");
  const canExport = hasPermission(permissionMap, "analytics.export");
  const startDate =
    first(raw.start_date) || analytics?.daily[0]?.date || "";
  const endDate =
    first(raw.end_date) || analytics?.daily.at(-1)?.date || "";
  const selectedPosition = first(raw.position_id) || "";
  const selectedSource = first(raw.source) || "";
  const selectedOwner = first(raw.owner) || "";
  const timezone = first(raw.timezone) || "Asia/Dushanbe";

  return (
    <>
      <Topbar
        crumbs={[{ label: m.app.nav.analytics }]}
        actions={
          <>
            {analytics && canExport ? (
              <a
                href={`/api/analytics/export?${exportQuery.toString()}`}
                download
                className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium hover:bg-bg-hover focus-visible:outline focus-visible:outline-2"
              >
                <Download className="h-3.5 w-3.5" aria-hidden="true" />
                {t.exportCsv}
              </a>
            ) : null}
            <LocaleSwitcher />
          </>
        }
      />

      <main className="flex-1 overflow-y-auto overflow-x-hidden px-4 py-6 sm:px-7 sm:py-8">
        <header className="mb-6">
          <h1 className="mb-1 text-[24px] font-semibold tracking-[-0.025em]">
            {t.pageTitle}
          </h1>
          <p className="text-[13px] text-text-muted">{t.pageSubtitle}</p>
          {analytics ? (
            <p className="mt-2 font-mono text-[10px] text-text-muted">
              {t.reportWindow(
                analytics.daily[0]?.date || analytics.start_at.slice(0, 10),
                analytics.daily.at(-1)?.date || analytics.end_at.slice(0, 10),
                analytics.timezone,
              )}
            </p>
          ) : null}
        </header>

        <form
          action="/analytics"
          method="get"
          className="mb-5 rounded-xl border border-border bg-bg-elevated p-4"
        >
          <div className="mb-3 flex items-center gap-2 text-xs font-semibold text-text-muted">
            <Filter className="h-3.5 w-3.5" aria-hidden="true" />
            {t.filtersTitle}
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-6">
            <FilterField label={t.startDate}>
              <input className="control" type="date" name="start_date" defaultValue={startDate} required />
            </FilterField>
            <FilterField label={t.endDate}>
              <input className="control" type="date" name="end_date" defaultValue={endDate} required />
            </FilterField>
            <FilterField label={t.timezone}>
              <select className="control" name="timezone" defaultValue={timezone}>
                <option value="Asia/Dushanbe">Asia/Dushanbe</option>
                <option value="UTC">UTC</option>
                <option value="Europe/Moscow">Europe/Moscow</option>
                <option value="Asia/Almaty">Asia/Almaty</option>
              </select>
            </FilterField>
            <FilterField label={t.position}>
              <select className="control" name="position_id" defaultValue={selectedPosition}>
                <option value="">{t.allPositions}</option>
                {analytics?.filter_options.positions.map((item) => (
                  <option key={item.key} value={item.key}>{item.label || item.key} ({item.count})</option>
                ))}
              </select>
            </FilterField>
            <FilterField label={t.source}>
              <select className="control" name="source" defaultValue={selectedSource}>
                <option value="">{t.allSources}</option>
                {analytics?.filter_options.sources.map((item) => (
                  <option key={item.key} value={item.key}>{item.label || item.key} ({item.count})</option>
                ))}
              </select>
            </FilterField>
            <FilterField label={t.owner}>
              <select className="control" name="owner" defaultValue={selectedOwner}>
                <option value="">{t.allOwners}</option>
                {analytics?.filter_options.owners.map((item) => (
                  <option key={item.key} value={item.key}>{item.label || item.key} ({item.count})</option>
                ))}
              </select>
            </FilterField>
          </div>
          <div className="mt-4 flex flex-wrap justify-end gap-2">
            <Link href="/analytics" className="inline-flex min-h-11 items-center rounded-lg border border-border px-4 text-xs font-medium hover:bg-bg-hover focus-visible:outline focus-visible:outline-2">
              {t.resetFilters}
            </Link>
            <button type="submit" className="inline-flex min-h-11 items-center rounded-lg bg-primary px-4 text-xs font-semibold text-white focus-visible:outline focus-visible:outline-2">
              {t.applyFilters}
            </button>
          </div>
        </form>

        {error ? (
          <div role="alert" className="rounded-lg border border-danger/40 bg-danger/10 p-5 text-sm text-danger">
            {t.loadError}
          </div>
        ) : analytics ? (
          <AnalyticsDashboard data={analytics} baseQuery={resolvedQuery.toString()} />
        ) : null}
      </main>
    </>
  );
}

function FilterField({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5 text-[10px] font-medium uppercase tracking-[0.06em] text-text-muted [&_.control]:min-h-11 [&_.control]:w-full [&_.control]:rounded-lg [&_.control]:border [&_.control]:border-border [&_.control]:bg-bg [&_.control]:px-3 [&_.control]:text-xs [&_.control]:font-normal [&_.control]:normal-case [&_.control]:tracking-normal [&_.control]:text-text [&_.control]:focus-visible:outline [&_.control]:focus-visible:outline-2">
      {label}
      {children}
    </label>
  );
}
