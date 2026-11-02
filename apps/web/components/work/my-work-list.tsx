"use client";

import Link from "next/link";
import {
  AlarmClock,
  ArrowRight,
  CalendarDays,
  ClipboardList,
  History,
  Loader2,
  MessageSquareWarning,
  Video,
} from "lucide-react";
import { useEffect, useState, useTransition } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { SourceBadge } from "@/components/ui/source-badge";
import { listMyWorkAction } from "@/lib/actions";
import { candDisplayRef } from "@/lib/format";
import type { MyWorkFilter, MyWorkItem, MyWorkPage } from "@/lib/types";
import { cn } from "@/lib/utils";


const EMPTY_FILTERS: MyWorkFilter[] = [
  "all",
  "overdue",
  "today",
  "upcoming",
  "undated",
  "interviews",
  "stale",
  "feedback",
];


export function MyWorkList({ initialPage }: { initialPage: MyWorkPage }) {
  const { locale, m } = useI18n();
  const [filter, setFilter] = useState<MyWorkFilter>("all");
  const [items, setItems] = useState(initialPage.items);
  const [nextCursor, setNextCursor] = useState(initialPage.next_cursor);
  const [error, setError] = useState(false);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    if (filter !== "all") return;
    setItems(initialPage.items);
    setNextCursor(initialPage.next_cursor);
  }, [filter, initialPage]);

  function load(
    nextFilter: MyWorkFilter,
    cursor?: string | null,
    append = false,
  ) {
    startTransition(async () => {
      const result = await listMyWorkAction({
        filter: nextFilter,
        cursor,
        limit: 50,
        staleDays: 7,
      });
      if (!result.ok) {
        setError(true);
        return;
      }
      setError(false);
      setNextCursor(result.data.next_cursor);
      if (!append) {
        setItems(result.data.items);
        return;
      }
      setItems((current) => {
        const seen = new Set(current.map((item) => item.score_id));
        return [
          ...current,
          ...result.data.items.filter((item) => !seen.has(item.score_id)),
        ];
      });
    });
  }

  function selectFilter(next: MyWorkFilter) {
    if (pending || next === filter) return;
    setFilter(next);
    setError(false);
    if (next === "all") {
      setItems(initialPage.items);
      setNextCursor(initialPage.next_cursor);
      return;
    }
    setItems([]);
    setNextCursor(null);
    load(next);
  }

  const filterLabels = m.myWork.filters;
  const counts = initialPage.counts;
  const filterCounts: Record<MyWorkFilter, number> = {
    all: counts.all,
    overdue: counts.overdue,
    today: counts.today,
    upcoming: counts.upcoming,
    undated: counts.undated,
    interviews: counts.interviews_today,
    stale: counts.stale,
    feedback: counts.missing_feedback,
  };
  const cards = [
    { filter: "all" as const, label: m.myWork.counts.assigned, value: counts.all, icon: ClipboardList },
    { filter: "overdue" as const, label: m.myWork.counts.overdue, value: counts.overdue, icon: AlarmClock },
    { filter: "today" as const, label: m.myWork.counts.today, value: counts.today, icon: CalendarDays },
    { filter: "interviews" as const, label: m.myWork.counts.interviews, value: counts.interviews_today, icon: Video },
    { filter: "stale" as const, label: m.myWork.counts.stale, value: counts.stale, icon: History },
    { filter: "feedback" as const, label: m.myWork.counts.feedback, value: counts.missing_feedback, icon: MessageSquareWarning },
  ];

  return (
    <div>
      <div className="mb-5 grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        {cards.map((card, index) => {
          const Icon = card.icon;
          return (
            <button
              key={card.filter}
              type="button"
              onClick={() => selectFilter(card.filter)}
              className={cn(
                "reveal rounded-lg border bg-bg-elevated px-3.5 py-3 text-left transition-colors hover:border-border-strong hover:bg-bg-hover",
                filter === card.filter ? "border-border-strong ring-1 ring-accent-border" : "border-border",
              )}
              style={{ animationDelay: `${index * 35}ms` }}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                  {card.label}
                </span>
                <Icon className="h-3.5 w-3.5 text-text-muted" />
              </div>
              <div className="mt-1.5 font-mono text-[24px] font-semibold leading-none tabular-nums">
                {card.value}
              </div>
            </button>
          );
        })}
      </div>

      <div className="mb-3 flex flex-wrap gap-1.5" role="group">
        {EMPTY_FILTERS.map((value) => (
          <button
            key={value}
            type="button"
            disabled={pending}
            aria-pressed={filter === value}
            onClick={() => selectFilter(value)}
            className={cn(
              "rounded-[6px] border px-2.5 py-[5px] text-[11.5px] transition-colors disabled:opacity-50",
              filter === value
                ? "border-border-strong bg-bg-active text-text"
                : "border-border bg-bg-elevated text-text-muted hover:bg-bg-hover hover:text-text",
            )}
          >
            {filterLabels[value]}
            <span className="ml-1.5 font-mono text-[10.5px] text-text-faint">
              {filterCounts[value]}
            </span>
          </button>
        ))}
      </div>

      <section className="overflow-hidden rounded-lg border border-border bg-bg-elevated">
        {pending && items.length === 0 ? (
          <div className="flex items-center justify-center gap-2 px-5 py-14 text-[12px] text-text-muted">
            <Loader2 className="h-4 w-4 animate-spin" />
            {m.myWork.loading}
          </div>
        ) : error && items.length === 0 ? (
          <div className="flex flex-col items-center gap-3 px-5 py-14 text-center">
            <p className="text-[12px] text-text-muted">{m.myWork.loadMoreFailed}</p>
            <Button size="sm" variant="outline" onClick={() => load(filter)} disabled={pending}>
              {m.myWork.loading}
            </Button>
          </div>
        ) : items.length === 0 ? (
          <div className="px-5 py-14 text-center">
            <h2 className="text-[13px] font-semibold text-text">
              {filter === "all" ? m.myWork.emptyTitle : m.myWork.emptyFiltered}
            </h2>
            <p className="mx-auto mt-1 max-w-md text-[12px] leading-5 text-text-muted">
              {m.myWork.emptyDesc}
            </p>
          </div>
        ) : (
          <div className="divide-y divide-border">
            {items.map((item) => (
              <WorkItemRow
                key={item.score_id}
                item={item}
                locale={locale}
                today={initialPage.today}
              />
            ))}
            {nextCursor ? (
              <div className="flex justify-center px-4 py-3">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={pending}
                  onClick={() => load(filter, nextCursor, true)}
                >
                  {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                  {pending ? m.myWork.loading : m.myWork.loadMore}
                </Button>
              </div>
            ) : null}
            {error ? (
              <p className="px-4 py-3 text-center text-[11px] text-destructive">
                {m.myWork.loadMoreFailed}
              </p>
            ) : null}
          </div>
        )}
      </section>
    </div>
  );
}


function WorkItemRow({
  item,
  locale,
  today,
}: {
  item: MyWorkItem;
  locale: "ru" | "en";
  today: string;
}) {
  const { m } = useI18n();
  const due = item.next_action_due_date;
  const overdue = due != null && due < today;
  const dueToday = due === today;
  const interviewToday = item.interview_starts_at != null;
  const interviewTime = item.interview_starts_at
    ? new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(item.interview_starts_at))
    : null;
  const dateLocale = locale === "ru" ? "ru-RU" : "en-US";
  const dueText = due
    ? dueToday
      ? m.myWork.dueToday
      : new Intl.DateTimeFormat(dateLocale, {
          day: "numeric",
          month: "short",
          year: "numeric",
        }).format(new Date(`${due}T12:00:00`))
    : m.myWork.noDue;
  const stageLabel =
    (m.kanban.stages as Record<string, string>)[item.stage] ?? item.stage;

  return (
    <Link
      href={`/candidates/${item.candidate.id}?position=${item.position.id}`}
      className="group grid gap-3 px-4 py-3.5 transition-colors hover:bg-bg-hover md:grid-cols-[minmax(180px,1.15fr)_minmax(180px,1fr)_120px_84px_minmax(150px,.8fr)_18px] md:items-center"
    >
      <div className="min-w-0">
        <div className="truncate font-mono text-[12.5px] font-semibold text-text">
          {candDisplayRef(item.candidate.id, item.candidate.source_ref) || m.myWork.candidateFallback}
        </div>
        <div className="mt-1 flex items-center gap-2">
          <SourceBadge source={item.candidate.source} />
          <span className="text-[10.5px] text-text-faint">
            {m.myWork.lastActivity(m.format.relativeTime(item.last_activity_at))}
          </span>
        </div>
      </div>

      <div className="min-w-0">
        <div className="truncate text-[12.5px] font-medium text-text">
          {item.position.title}
        </div>
      </div>

      <span className="w-fit rounded-[4px] bg-bg-active px-2 py-1 font-mono text-[10px] text-text-muted">
        {stageLabel}
      </span>

      <div className="font-mono text-[12px] font-semibold tabular-nums text-text-muted">
        {item.overall_score == null ? "—" : `${item.overall_score}/100`}
      </div>

      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span
          className={cn(
            "rounded-[4px] px-2 py-1 text-[10.5px]",
            overdue
              ? "bg-destructive/10 text-destructive"
              : dueToday
                ? "bg-accent-muted text-primary"
                : "bg-bg-active text-text-muted",
          )}
        >
          {dueText}
        </span>
        {interviewToday ? (
          <Signal
            label={`${m.myWork.interviewToday}${interviewTime ? ` · ${interviewTime}` : ""}`}
          />
        ) : null}
        {item.is_missing_feedback ? <Signal label={m.myWork.missingFeedback} /> : null}
        {item.is_stale ? <Signal label={m.myWork.stale} /> : null}
      </div>

      <ArrowRight className="hidden h-3.5 w-3.5 text-text-faint transition-transform group-hover:translate-x-0.5 group-hover:text-text md:block" />
    </Link>
  );
}


function Signal({ label }: { label: string }) {
  return (
    <span className="rounded-[4px] border border-border px-1.5 py-0.5 text-[9.5px] text-text-faint">
      {label}
    </span>
  );
}
