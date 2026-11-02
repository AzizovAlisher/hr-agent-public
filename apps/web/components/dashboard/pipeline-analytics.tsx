"use client";

import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  BriefcaseBusiness,
  CheckCircle2,
  Clock3,
  PlugZap,
  Send,
  Users,
} from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import type {
  AnalyticsBreakdown,
  AnalyticsDrilldownDimension,
  ApplicationAnalytics,
  KanbanStage,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** @ui-only Localized analytics presentation copy. */
type AnalyticsCopy = ReturnType<typeof useI18n>["m"]["analytics"];

const STAGES: KanbanStage[] = [
  "new",
  "screening",
  "interview",
  "offer",
  "hired",
  "rejected",
];

const STAGE_COLOR: Record<KanbanStage, string> = {
  new: "var(--text-faint)",
  screening: "var(--score-low-color, #f5a623)",
  interview: "var(--score-mid-color, #5b9dff)",
  offer: "var(--score-high-color, #34d399)",
  hired: "var(--primary)",
  rejected: "var(--danger, #ef4444)",
};

function fmt(value: number): string {
  return value.toLocaleString("en-US");
}

function formatPercent(value: number | null): string {
  if (value == null) return "—";
  return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}%`;
}

function formatHours(value: number | null, t: AnalyticsCopy): string {
  if (value == null) return "—";
  if (value < 24) return t.hours(Math.round(value * 10) / 10);
  return t.days(Math.round((value / 24) * 10) / 10);
}

function formatCost(microusd: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: microusd < 10_000 ? 4 : 2,
    maximumFractionDigits: 4,
  }).format(microusd / 1_000_000);
}

function stageLabel(stage: KanbanStage, t: AnalyticsCopy): string {
  return {
    new: t.stageNew,
    screening: t.stageScreening,
    interview: t.stageInterview,
    offer: t.stageOffer,
    hired: t.stageHired,
    rejected: t.stageRejected,
  }[stage];
}

function analyticsHref(
  baseQuery: string,
  changes: Record<string, string | null | undefined>,
): string {
  const params = new URLSearchParams(baseQuery);
  params.delete("drilldown_cursor");
  for (const [key, value] of Object.entries(changes)) {
    if (value == null || value === "") params.delete(key);
    else params.set(key, value);
  }
  const query = params.toString();
  return query ? `/analytics?${query}` : "/analytics";
}

function drilldownHref(
  baseQuery: string,
  dimension: AnalyticsDrilldownDimension,
  key: string | null,
): string {
  return analyticsHref(baseQuery, {
    drilldown_dimension: dimension,
    drilldown_key: key,
  });
}

export function AnalyticsDashboard({
  data,
  baseQuery,
}: {
  data: ApplicationAnalytics;
  baseQuery: string;
}) {
  const { m, locale } = useI18n();
  const t = m.analytics;
  const reconciliationIssues =
    data.reconciliation.applications_without_initial_event +
    data.reconciliation.duplicate_initial_events +
    data.reconciliation.stage_mismatches;

  return (
    <div className="flex flex-col gap-5">
      <section aria-label={t.sectionSummary} className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
        <Kpi label={t.kpiApplications} value={fmt(data.total_applications)} icon={<BriefcaseBusiness />} accent />
        <Kpi label={t.kpiUniqueCandidates} value={fmt(data.unique_candidates)} icon={<Users />} />
        <Kpi label={t.kpiHired} value={fmt(data.timing.hired_applications)} icon={<CheckCircle2 />} />
        <Kpi label={t.kpiTimeToHire} value={formatHours(data.timing.median_hours_to_hire, t)} icon={<Clock3 />} />
        <Kpi label={t.kpiOfferAcceptance} value={formatPercent(data.offers.acceptance_rate_percent)} icon={<Send />} />
        <Kpi label={t.kpiAssessmentCompletion} value={formatPercent(data.assessments.completion_rate_percent)} icon={<Activity />} />
      </section>

      {reconciliationIssues > 0 ? (
        <ReconciliationWarning data={data.reconciliation} t={t} />
      ) : null}

      <SectionHeader label={t.sectionFunnel} />
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[1.35fr_1fr]">
        <FunnelCard data={data} baseQuery={baseQuery} t={t} />
        <DailyActivityCard data={data} t={t} />
      </div>

      <SectionHeader label={t.sectionOutcomes} />
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <TimingCard data={data} t={t} />
        <OfferCard data={data} t={t} />
        <AssessmentCard data={data} t={t} />
      </div>

      <SectionHeader label={t.sectionAttribution} />
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <DimensionCard
          title={t.sourcesTitle}
          rows={data.by_source}
          empty={t.noData}
          href={(key) => drilldownHref(baseQuery, "source", key)}
        />
        <DimensionCard
          title={t.positionsTitle}
          rows={data.by_position}
          empty={t.noData}
          href={(key) => analyticsHref(baseQuery, { position_id: key, drilldown_dimension: null, drilldown_key: null })}
        />
        <DimensionCard
          title={t.dispositionsTitle}
          rows={data.dispositions}
          empty={t.noData}
          href={(key) => drilldownHref(baseQuery, "disposition", key)}
        />
      </div>
      <OwnerWorkloadCard data={data} baseQuery={baseQuery} t={t} />

      {data.integration_health || data.ai_usage ? (
        <>
          <SectionHeader label={t.sectionOperations} />
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {data.integration_health ? (
              <IntegrationHealthCard health={data.integration_health} t={t} />
            ) : null}
            {data.ai_usage ? <AiUsageCard usage={data.ai_usage} t={t} /> : null}
          </div>
        </>
      ) : null}

      {data.drilldown ? (
        <DrilldownCard data={data} baseQuery={baseQuery} t={t} locale={locale} />
      ) : null}
    </div>
  );
}

function Kpi({
  label,
  value,
  icon,
  accent,
}: {
  label: string;
  value: string;
  icon: React.ReactElement;
  accent?: boolean;
}) {
  return (
    <div className="flex min-h-28 flex-col justify-between rounded-xl border border-border bg-bg-elevated p-4">
      <span className="flex items-center gap-1.5 text-[11px] font-medium text-text-muted [&_svg]:h-3.5 [&_svg]:w-3.5">
        {icon}
        {label}
      </span>
      <strong className={cn("font-mono text-2xl tabular-nums", accent && "text-primary")}>
        {value}
      </strong>
    </div>
  );
}

function FunnelCard({
  data,
  baseQuery,
  t,
}: {
  data: ApplicationAnalytics;
  baseQuery: string;
  t: AnalyticsCopy;
}) {
  return (
    <Card title={t.funnelTitle} description={t.funnelDescription}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] border-collapse text-left text-xs">
          <caption className="sr-only">{t.funnelDescription}</caption>
          <thead>
            <tr className="border-b border-border text-[10px] uppercase tracking-[0.06em] text-text-muted">
              <th className="px-2 py-2 font-medium">{t.stage}</th>
              <th className="px-2 py-2 text-right font-medium">{t.entered}</th>
              <th className="px-2 py-2 text-right font-medium">{t.current}</th>
              <th className="px-2 py-2 text-right font-medium">{t.conversion}</th>
              <th className="px-2 py-2 text-right font-medium">{t.medianStage}</th>
              <th className="px-2 py-2 text-right font-medium">{t.medianFromApply}</th>
            </tr>
          </thead>
          <tbody>
            {data.stages.map((row) => (
              <tr key={row.stage} className="border-b border-border/70 last:border-0">
                <th className="px-2 py-3 font-medium text-text">
                  <span className="flex items-center gap-2">
                    <span className="h-2 w-2 rounded-full" style={{ background: STAGE_COLOR[row.stage] }} />
                    {stageLabel(row.stage, t)}
                  </span>
                </th>
                <td className="px-2 py-3 text-right font-mono tabular-nums">
                  <Link className="inline-flex min-h-6 min-w-6 items-center justify-end rounded text-primary underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2" href={drilldownHref(baseQuery, "stage_entered", row.stage)}>
                    {fmt(row.entered)}
                  </Link>
                </td>
                <td className="px-2 py-3 text-right font-mono tabular-nums">
                  <Link className="inline-flex min-h-6 min-w-6 items-center justify-end rounded text-primary underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2" href={drilldownHref(baseQuery, "stage_current", row.stage)}>
                    {fmt(row.current)}
                  </Link>
                </td>
                <td className="px-2 py-3 text-right font-mono text-text-muted tabular-nums">{formatPercent(row.conversion_from_previous_percent)}</td>
                <td className="px-2 py-3 text-right font-mono text-text-muted tabular-nums">{formatHours(row.median_hours_in_stage, t)}</td>
                <td className="px-2 py-3 text-right font-mono text-text-muted tabular-nums">{formatHours(row.median_hours_from_application, t)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function DailyActivityCard({ data, t }: { data: ApplicationAnalytics; t: AnalyticsCopy }) {
  const visible = data.daily;
  const max = Math.max(1, ...visible.map((row) => row.applications + row.transitions));
  return (
    <Card title={t.dailyTitle} description={t.dailyDescription}>
      {visible.every((row) => row.applications + row.transitions === 0) ? (
        <Empty label={t.noData} />
      ) : (
        <div className="overflow-x-auto pb-1">
          <ol className="flex h-44 min-w-max items-end gap-1" aria-label={t.dailyDescription}>
            {visible.map((row) => {
              const total = row.applications + row.transitions;
              const height = Math.max(2, (total / max) * 120);
              return (
                <li key={row.date} className="flex w-6 flex-col items-center gap-1">
                  <span className="sr-only">{t.dailyPoint(row.date, row.applications, row.transitions)}</span>
                  <span aria-hidden="true" className="font-mono text-[9px] text-text-muted">{total}</span>
                  <span aria-hidden="true" className="w-3 rounded-t-sm bg-primary" style={{ height }} />
                  <time aria-hidden="true" className="-rotate-45 font-mono text-[8px] text-text-muted" dateTime={row.date}>
                    {row.date.slice(5)}
                  </time>
                </li>
              );
            })}
          </ol>
        </div>
      )}
      <div className="mt-auto flex flex-wrap gap-3 text-[10px] text-text-muted">
        <span>{t.applications}: {fmt(data.daily.reduce((sum, row) => sum + row.applications, 0))}</span>
        <span>{t.transitions}: {fmt(data.daily.reduce((sum, row) => sum + row.transitions, 0))}</span>
        <span>{t.hired}: {fmt(data.daily.reduce((sum, row) => sum + row.hired, 0))}</span>
        <span>{t.rejected}: {fmt(data.daily.reduce((sum, row) => sum + row.rejected, 0))}</span>
      </div>
    </Card>
  );
}

function TimingCard({ data, t }: { data: ApplicationAnalytics; t: AnalyticsCopy }) {
  return (
    <Card title={t.timingTitle} description={t.timingDescription}>
      <Metric label={t.kpiTimeToHire} value={formatHours(data.timing.median_hours_to_hire, t)} detail={t.hiredApplications(data.timing.hired_applications)} />
      <Metric label={t.kpiTimeToFill} value={formatHours(data.timing.median_hours_to_fill, t)} detail={t.filledPositions(data.timing.filled_positions)} />
    </Card>
  );
}

function OfferCard({ data, t }: { data: ApplicationAnalytics; t: AnalyticsCopy }) {
  return (
    <Card title={t.offersTitle} description={t.offersDescription}>
      <Metric label={t.acceptanceRate} value={formatPercent(data.offers.acceptance_rate_percent)} detail={t.offerResponses(data.offers.accepted, data.offers.declined)} />
      <div className="grid grid-cols-3 gap-2 text-center">
        <SmallMetric label={t.sent} value={data.offers.sent} />
        <SmallMetric label={t.accepted} value={data.offers.accepted} />
        <SmallMetric label={t.declined} value={data.offers.declined} />
      </div>
    </Card>
  );
}

function AssessmentCard({ data, t }: { data: ApplicationAnalytics; t: AnalyticsCopy }) {
  return (
    <Card title={t.assessmentsTitle} description={t.nativeAssessmentScope}>
      <Metric label={t.completionRate} value={formatPercent(data.assessments.completion_rate_percent)} detail={t.assessmentProgress(data.assessments.completed, data.assessments.invitations)} />
      <div className="grid grid-cols-3 gap-2 text-center">
        <SmallMetric label={t.started} value={data.assessments.started} />
        <SmallMetric label={t.completed} value={data.assessments.completed} />
        <SmallMetric label={t.droppedOff} value={data.assessments.dropped_off} danger={data.assessments.dropped_off > 0} />
      </div>
    </Card>
  );
}

function DimensionCard({
  title,
  rows,
  empty,
  href,
}: {
  title: string;
  rows: AnalyticsBreakdown[];
  empty: string;
  href: (key: string) => string;
}) {
  const max = Math.max(1, ...rows.map((row) => row.count));
  return (
    <Card title={title}>
      {rows.length === 0 ? (
        <Empty label={empty} />
      ) : (
        <ul className="flex flex-col gap-1">
          {rows.slice(0, 10).map((row) => (
            <li key={row.key}>
              <Link href={href(row.key)} className="group flex min-h-11 items-center gap-3 rounded-md px-2 py-2 hover:bg-bg-hover focus-visible:outline focus-visible:outline-2">
                <span className="min-w-0 flex-1 truncate text-xs text-text">{row.label || row.key}</span>
                <span className="h-1.5 w-20 overflow-hidden rounded-full bg-border" aria-hidden="true">
                  <span className="block h-full rounded-full bg-primary" style={{ width: `${(row.count / max) * 100}%` }} />
                </span>
                <span className="w-10 text-right font-mono text-xs text-text-muted tabular-nums">{fmt(row.count)}</span>
                <ArrowRight className="h-3 w-3 text-text-muted group-hover:text-text" aria-hidden="true" />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function OwnerWorkloadCard({ data, baseQuery, t }: { data: ApplicationAnalytics; baseQuery: string; t: AnalyticsCopy }) {
  return (
    <Card title={t.ownerWorkloadTitle} description={t.entryOwnerScope}>
      {data.owner_workload.length === 0 ? <Empty label={t.noData} /> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[620px] text-left text-xs">
            <caption className="sr-only">{t.entryOwnerScope}</caption>
            <thead><tr className="border-b border-border text-[10px] uppercase tracking-[0.06em] text-text-muted"><th className="px-2 py-2 font-medium">{t.owner}</th><th className="px-2 py-2 text-right font-medium">{t.active}</th><th className="px-2 py-2 text-right font-medium">{t.terminal}</th>{STAGES.map((stage) => <th key={stage} className="px-2 py-2 text-right font-medium">{stageLabel(stage, t)}</th>)}</tr></thead>
            <tbody>{data.owner_workload.map((row) => (
              <tr key={row.owner_user_id || "unassigned"} className="border-b border-border/70 last:border-0">
                <th className="px-2 py-3 font-medium"><Link className="inline-flex min-h-6 items-center text-primary hover:underline" href={drilldownHref(baseQuery, "owner", row.owner_user_id || "unassigned")}>{row.label}</Link></th>
                <td className="px-2 py-3 text-right font-mono tabular-nums">{row.active}</td>
                <td className="px-2 py-3 text-right font-mono text-text-muted tabular-nums">{row.terminal}</td>
                {STAGES.map((stage) => <td key={stage} className="px-2 py-3 text-right font-mono text-text-muted tabular-nums">{row.by_stage[stage]}</td>)}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function IntegrationHealthCard({ health, t }: { health: NonNullable<ApplicationAnalytics["integration_health"]>; t: AnalyticsCopy }) {
  return (
    <Card title={t.integrationTitle} description={t.integrationCurrentScope} icon={<PlugZap />}>
      <div className="grid grid-cols-3 gap-2">
        <SmallMetric label={t.connections} value={health.total} />
        <SmallMetric label={t.connected} value={health.connected} />
        <SmallMetric label={t.attentionRequired} value={health.attention_required} danger={health.attention_required > 0} />
      </div>
      <BreakdownPills rows={health.by_status} />
    </Card>
  );
}

function AiUsageCard({ usage, t }: { usage: NonNullable<ApplicationAnalytics["ai_usage"]>; t: AnalyticsCopy }) {
  return (
    <Card title={t.aiUsageTitle} description={t.aiOrganizationScope} icon={<Bot />}>
      <div className="grid grid-cols-3 gap-2">
        <SmallMetric label={t.calls} value={usage.calls} />
        <SmallMetric label={t.tokens} value={usage.tokens} />
        <SmallMetric label={t.cost} value={formatCost(usage.cost_microusd)} />
      </div>
      {usage.by_provider.length > 0 ? (
        <ul className="mt-2 divide-y divide-border text-xs">
          {usage.by_provider.map((row) => <li key={row.provider} className="flex items-center justify-between gap-3 py-2"><span className="truncate text-text">{row.provider}</span><span className="font-mono text-text-muted tabular-nums">{t.providerUsage(row.calls, row.tokens, formatCost(row.cost_microusd))}</span></li>)}
        </ul>
      ) : null}
    </Card>
  );
}

function DrilldownCard({ data, baseQuery, t, locale }: { data: ApplicationAnalytics; baseQuery: string; t: AnalyticsCopy; locale: "ru" | "en" }) {
  const drilldown = data.drilldown;
  if (!drilldown) return null;
  const closeHref = analyticsHref(baseQuery, { drilldown_dimension: null, drilldown_key: null, drilldown_cursor: null });
  return (
    <section id="analytics-drilldown" aria-labelledby="analytics-drilldown-title" className="rounded-xl border border-primary/30 bg-bg-elevated p-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div><h2 id="analytics-drilldown-title" className="text-sm font-semibold">{t.drilldownTitle}</h2><p className="mt-1 text-xs text-text-muted">{t.drilldownCount(drilldown.total)}</p></div>
        <Link href={closeHref} className="inline-flex min-h-11 items-center rounded-lg border border-border px-3 py-2 text-xs font-medium hover:bg-bg-hover focus-visible:outline focus-visible:outline-2">{t.closeDrilldown}</Link>
      </div>
      {drilldown.items.length === 0 ? <Empty label={t.noData} /> : (
        <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-xs"><thead><tr className="border-b border-border text-[10px] uppercase tracking-[0.06em] text-text-muted"><th className="px-2 py-2 font-medium">{t.appliedAt}</th><th className="px-2 py-2 font-medium">{t.candidate}</th><th className="px-2 py-2 font-medium">{t.stage}</th><th className="px-2 py-2 font-medium">{t.source}</th><th className="px-2 py-2 font-medium">{t.owner}</th><th className="px-2 py-2 font-medium">{t.disposition}</th></tr></thead><tbody>{drilldown.items.map((row) => <tr key={row.application_id} className="border-b border-border/70 last:border-0"><td className="px-2 py-3 font-mono text-text-muted">{new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", { dateStyle: "medium", timeStyle: "short", timeZone: data.timezone }).format(new Date(row.applied_at))}</td><td className="px-2 py-3"><Link href={`/candidates/${row.candidate_id}?position=${row.position_id}`} className="inline-flex min-h-6 items-center font-mono text-primary hover:underline">{row.candidate_id.slice(0, 8)}…</Link></td><td className="px-2 py-3">{stageLabel(row.current_stage, t)}</td><td className="px-2 py-3">{row.source}</td><td className="px-2 py-3">{row.owner_label}</td><td className="px-2 py-3">{row.disposition_code || "—"}</td></tr>)}</tbody></table></div>
      )}
      {drilldown.next_cursor ? <div className="mt-4 flex justify-end"><Link href={analyticsHref(baseQuery, { drilldown_cursor: drilldown.next_cursor })} className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-white focus-visible:outline focus-visible:outline-2">{t.nextPage}<ArrowRight className="h-3.5 w-3.5" /></Link></div> : null}
    </section>
  );
}

function ReconciliationWarning({ data, t }: { data: ApplicationAnalytics["reconciliation"]; t: AnalyticsCopy }) {
  return (
    <aside className="flex items-start gap-3 rounded-xl border border-warning/40 bg-warning/10 p-4 text-xs" role="status">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
      <div><strong className="font-semibold text-text">{t.reconciliationTitle}</strong><p className="mt-1 text-text-muted">{t.reconciliationSummary(data.applications_without_initial_event, data.duplicate_initial_events, data.stage_mismatches)}</p></div>
    </aside>
  );
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="flex items-end justify-between gap-4 border-b border-border pb-3 last:border-0 last:pb-0"><div><p className="text-[11px] text-text-muted">{label}</p><p className="mt-1 text-[10px] text-text-muted">{detail}</p></div><strong className="font-mono text-xl tabular-nums">{value}</strong></div>;
}

function SmallMetric({ label, value, danger }: { label: string; value: number | string; danger?: boolean }) {
  return <div className="rounded-lg border border-border bg-bg/50 px-2 py-3"><strong className={cn("block font-mono text-lg tabular-nums", danger && "text-danger")}>{typeof value === "number" ? fmt(value) : value}</strong><span className="mt-1 block text-[10px] text-text-muted">{label}</span></div>;
}

function BreakdownPills({ rows }: { rows: AnalyticsBreakdown[] }) {
  return <div className="mt-2 flex flex-wrap gap-2">{rows.map((row) => <span key={row.key} className="rounded-full border border-border bg-bg/50 px-2.5 py-1 font-mono text-[10px] text-text-muted">{row.label || row.key}: {row.count}</span>)}</div>;
}

function Card({ title, description, icon, children }: { title: string; description?: string; icon?: React.ReactElement; children: React.ReactNode }) {
  return <section className="flex min-h-48 flex-col gap-4 rounded-xl border border-border bg-bg-elevated p-5"><header><h3 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.06em] text-text-muted [&_svg]:h-3.5 [&_svg]:w-3.5">{icon}{title}</h3>{description ? <p className="mt-1.5 text-[11px] leading-relaxed text-text-muted">{description}</p> : null}</header>{children}</section>;
}

function Empty({ label }: { label: string }) {
  return <div className="flex min-h-24 flex-1 items-center justify-center text-xs text-text-muted">{label}</div>;
}

export function SectionHeader({ label }: { label: string }) {
  return <h2 className="px-1 pt-2 font-mono text-[11px] uppercase tracking-[0.08em] text-text-muted">{label}</h2>;
}
