"use client";

import Link from "next/link";
import {
  Activity,
  ArchiveRestore,
  AlertTriangle,
  ArrowRight,
  Bell,
  CalendarClock,
  CalendarPlus,
  ChevronDown,
  ChevronUp,
  ClipboardCheck,
  Download,
  FileCheck2,
  GitBranch,
  Loader2,
  Mail,
  MessageSquare,
  Merge,
  Mic2,
  RotateCcw,
  Scale,
  Sparkles,
  Tag,
  Trash2,
  UserCheck,
  UserPlus,
  Workflow,
} from "lucide-react";
import { useEffect, useMemo, useState, useTransition } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { listCandidateActivitiesAction } from "@/lib/actions";
import type {
  CandidateActivity,
  CandidateActivityCategory,
  CandidateActivityPage,
} from "@/lib/types";
import type { Messages } from "@/lib/i18n/types";
import { cn } from "@/lib/utils";


/** @ui-only Activity-timeline filter state. */
type FilterCategory = "all" | CandidateActivityCategory;
const RECENT_VISIBLE_LIMIT = 8;
const HIDDEN_TIMELINE_EVENT_TYPES = new Set([
  "application_evaluation_recorded",
  "candidate_label_set",
]);

function payloadString(activity: CandidateActivity, key: string, fallback = "—") {
  const value = activity.payload[key];
  return typeof value === "string" && value.trim() ? value : fallback;
}


function payloadNumber(activity: CandidateActivity, key: string): number | null {
  const value = activity.payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


function payloadArrayLength(activity: CandidateActivity, key: string): number | null {
  const value = activity.payload[key];
  return Array.isArray(value) ? value.length : null;
}


function stageLabel(value: string, m: Messages): string {
  return (m.kanban.stages as Record<string, string>)[value] ?? value.replaceAll("_", " ");
}


function statusLabel(value: string, m: Messages): string {
  return m.candidateActivity.statuses[value] ?? value.replaceAll("_", " ");
}


function payloadDate(
  activity: CandidateActivity,
  key: string,
  locale: "ru" | "en",
  fallback: string,
): string {
  const value = activity.payload[key];
  if (typeof value !== "string") return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}


function eventDescription(
  activity: CandidateActivity,
  m: Messages,
  locale: "ru" | "en",
): string {
  const e = m.candidateActivity.events;
  switch (activity.event_type) {
    case "candidate_added":
      return e.candidateAdded(payloadString(activity, "source", m.candidateActivity.unknownValue));
    case "candidate_deleted":
      return e.candidateDeleted;
    case "candidate_restored":
      return e.candidateRestored;
    case "candidate_scored":
      return e.candidateScored(payloadNumber(activity, "overall_score"));
    case "candidate_rescored":
      return e.candidateRescored(payloadNumber(activity, "overall_score"));
    case "stage_changed":
      return e.stageChanged(
        stageLabel(payloadString(activity, "from_stage"), m),
        stageLabel(payloadString(activity, "to_stage"), m),
      );
    case "note_added":
      return e.noteAdded;
    case "note_deleted":
      return e.noteDeleted;
    case "tag_added":
      return e.tagAdded(payloadString(activity, "label", m.candidateActivity.unknownValue));
    case "tag_removed":
      return e.tagRemoved(payloadString(activity, "label", m.candidateActivity.unknownValue));
    case "github_attached":
      return e.githubAttached(
        payloadString(activity, "github_login", m.candidateActivity.unknownValue),
        activity.payload.refreshed === true,
      );
    case "github_detached":
      return e.githubDetached(payloadString(activity, "github_login", m.candidateActivity.unknownValue));
    case "interview_generated":
      return e.interviewGenerated(payloadNumber(activity, "question_count"));
    case "interview_status_changed":
      return e.interviewStatusChanged(
        statusLabel(payloadString(activity, "from_status"), m),
        statusLabel(payloadString(activity, "to_status"), m),
      );
    case "interview_deleted":
      return e.interviewDeleted;
    case "recording_added":
      return e.recordingAdded(payloadString(activity, "title", m.candidateActivity.unknownValue));
    case "recording_status_changed":
      return e.recordingStatusChanged(
        payloadString(activity, "title", m.candidateActivity.unknownValue),
        statusLabel(payloadString(activity, "to_status"), m),
      );
    case "recording_deleted":
      return e.recordingDeleted(payloadString(activity, "title", m.candidateActivity.unknownValue));
    case "task_added":
      return e.taskAdded(payloadString(activity, "title", m.candidateActivity.unknownValue));
    case "task_status_changed":
      return e.taskStatusChanged(
        payloadString(activity, "title", m.candidateActivity.unknownValue),
        statusLabel(payloadString(activity, "to_status"), m),
      );
    case "task_evaluated":
      return e.taskEvaluated(
        payloadString(activity, "title", m.candidateActivity.unknownValue),
        payloadNumber(activity, "ai_score"),
      );
    case "task_deleted":
      return e.taskDeleted(payloadString(activity, "title", m.candidateActivity.unknownValue));
    case "candidate_owner_changed":
      return e.candidateOwnerChanged(
        typeof activity.payload.to_owner_user_id === "string" &&
          activity.payload.to_owner_user_id.length > 0,
      );
    case "next_action_due_changed": {
      const dueDate = activity.payload.to_due_date;
      return e.nextActionDueChanged(
        typeof dueDate === "string" && dueDate.length > 0 ? dueDate : null,
      );
    }
    case "candidate_merged":
      return e.candidateMerged(payloadNumber(activity, "merged_count"));
    case "candidate_merge_reversed":
      return e.candidateMergeReversed(
        payloadNumber(activity, "restored_count"),
      );
    case "candidate_exported":
      return e.candidateExported(
        payloadString(activity, "format", m.candidateActivity.unknownValue),
      );
    case "contact_point_added":
      return e.contactPointAdded(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "contact_point_updated":
      return e.contactPointUpdated(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "contact_point_opted_out":
      return e.contactPointOptedOut(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "message_scheduled":
      return e.messageScheduled(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "message_sent":
      return e.messageSent(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "message_failed":
      return e.messageFailed(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
        (() => {
          const code = payloadString(activity, "failure_code", "");
          return m.communications.failureCodes[code] ?? code;
        })(),
      );
    case "message_cancelled":
      return e.messageCancelled(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "message_received":
      return e.messageReceived(
        payloadString(activity, "channel", m.candidateActivity.unknownValue),
      );
    case "interview_scheduling_link_created":
      return e.schedulingLinkCreated(
        payloadNumber(activity, "duration_minutes"),
        payloadNumber(activity, "interviewer_count"),
      );
    case "interview_scheduling_link_revoked":
      return e.schedulingLinkRevoked;
    case "interview_scheduled":
      return e.interviewScheduled(
        payloadDate(activity, "starts_at", locale, m.candidateActivity.unknownValue),
      );
    case "interview_rescheduled":
      return e.interviewRescheduled(
        payloadDate(activity, "starts_at", locale, m.candidateActivity.unknownValue),
      );
    case "interview_cancelled":
      return e.interviewCancelled(
        payloadDate(activity, "starts_at", locale, m.candidateActivity.unknownValue),
      );
    case "interview_reminder_sent":
      {
        const audience = payloadString(
          activity,
          "audience",
          m.candidateActivity.unknownValue,
        );
        const channel = payloadString(
          activity,
          "channel",
          m.candidateActivity.unknownValue,
        );
        return e.interviewReminderSent(
          m.scheduling.common.reminderAudiences[audience] ?? audience,
          m.scheduling.common.reminderChannels[channel] ?? channel,
        );
      }
    case "scorecard_assigned":
      return e.scorecardAssigned(
        payloadString(activity, "template_name", m.candidateActivity.unknownValue),
      );
    case "scorecard_submitted":
      return e.scorecardSubmitted(
        payloadString(activity, "template_name", m.candidateActivity.unknownValue),
      );
    case "hiring_decision_created": {
      const recommendation = payloadString(activity, "recommendation");
      return e.hiringDecisionCreated(
        m.scorecards.recommendations[recommendation] ?? recommendation,
      );
    }
    case "hiring_decision_updated": {
      const recommendation = payloadString(activity, "recommendation");
      return e.hiringDecisionUpdated(
        m.scorecards.recommendations[recommendation] ?? recommendation,
      );
    }
    case "hiring_decision_voted": {
      const vote = payloadString(activity, "vote");
      const decisionStatus = payloadString(activity, "status");
      return e.hiringDecisionVoted(
        vote === "approve" ? m.scorecards.approve : vote === "reject" ? m.scorecards.reject : vote,
        m.scorecards.decisionStatuses[decisionStatus] ?? decisionStatus,
      );
    }
    case "hiring_decision_finalized": {
      const recommendation = payloadString(activity, "recommendation");
      const decisionStatus = payloadString(activity, "status");
      return e.hiringDecisionFinalized(
        m.scorecards.recommendations[recommendation] ?? recommendation,
        m.scorecards.decisionStatuses[decisionStatus] ?? decisionStatus,
      );
    }
    case "workflow_completed":
      return e.workflowCompleted(
        payloadString(activity, "rule_name", m.candidateActivity.unknownValue),
        payloadArrayLength(activity, "action_types"),
      );
    case "workflow_failed": {
      const errorCode = payloadString(activity, "error_code", "internal_error");
      return e.workflowFailed(
        payloadString(activity, "rule_name", m.candidateActivity.unknownValue),
        m.workflows.errorCodes[errorCode] ?? m.workflows.errorCodes.internal_error,
      );
    }
    default:
      return e.unknown;
  }
}


function EventIcon({ eventType }: { eventType: string }) {
  const cls = "h-3.5 w-3.5";
  if (eventType === "candidate_added") return <UserPlus className={cls} />;
  if (eventType === "candidate_deleted") return <Trash2 className={cls} />;
  if (eventType === "candidate_restored") return <ArchiveRestore className={cls} />;
  if (eventType === "candidate_owner_changed") return <UserCheck className={cls} />;
  if (eventType === "next_action_due_changed") return <CalendarClock className={cls} />;
  if (eventType === "candidate_merged") return <Merge className={cls} />;
  if (eventType === "candidate_merge_reversed") return <RotateCcw className={cls} />;
  if (eventType === "candidate_exported") return <Download className={cls} />;
  if (eventType.startsWith("contact_point_") || eventType.startsWith("message_")) {
    return <Mail className={cls} />;
  }
  if (eventType === "interview_reminder_sent") return <Bell className={cls} />;
  if (eventType.startsWith("interview_scheduling_link_")) {
    return <CalendarPlus className={cls} />;
  }
  if (["interview_scheduled", "interview_rescheduled", "interview_cancelled"].includes(eventType)) {
    return <CalendarClock className={cls} />;
  }
  if (eventType.includes("scored") || eventType === "task_evaluated") return <Sparkles className={cls} />;
  if (eventType === "stage_changed") return <ArrowRight className={cls} />;
  if (eventType.startsWith("note_")) return <MessageSquare className={cls} />;
  if (eventType.startsWith("tag_")) return <Tag className={cls} />;
  if (eventType.startsWith("github_")) return <GitBranch className={cls} />;
  if (eventType.startsWith("scorecard_")) return <ClipboardCheck className={cls} />;
  if (eventType.startsWith("hiring_decision_")) return <Scale className={cls} />;
  if (eventType === "workflow_failed") return <AlertTriangle className={cls} />;
  if (eventType === "workflow_completed") return <Workflow className={cls} />;
  if (eventType.startsWith("interview_")) return <ClipboardCheck className={cls} />;
  if (eventType.startsWith("recording_")) return <Mic2 className={cls} />;
  if (eventType.startsWith("task_")) return <FileCheck2 className={cls} />;
  return <Activity className={cls} />;
}


function localDayKey(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}


export function ActivityTimeline({
  candidateId,
  initialPage,
  initialLoadFailed = false,
}: {
  candidateId: string;
  initialPage: CandidateActivityPage;
  initialLoadFailed?: boolean;
}) {
  const { locale, m } = useI18n();
  const [category, setCategory] = useState<FilterCategory>("all");
  const [expanded, setExpanded] = useState(false);
  const [showRecent, setShowRecent] = useState(false);
  const [items, setItems] = useState(initialPage.items);
  const [nextCursor, setNextCursor] = useState(initialPage.next_cursor);
  const [error, setError] = useState(initialLoadFailed);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    if (category !== "all") return;
    setItems(initialPage.items);
    setNextCursor(initialPage.next_cursor);
    setError(initialLoadFailed);
  }, [category, initialLoadFailed, initialPage]);

  const displayItems = useMemo(
    () =>
      items.filter((item) => !HIDDEN_TIMELINE_EVENT_TYPES.has(item.event_type)),
    [items],
  );
  const timelineItems = useMemo(
    () =>
      showRecent ? displayItems : displayItems.slice(0, RECENT_VISIBLE_LIMIT),
    [displayItems, showRecent],
  );
  const dateLocale = locale === "ru" ? "ru-RU" : "en-US";
  const groups = useMemo(() => {
    const result: { key: string; label: string; items: CandidateActivity[] }[] = [];
    const now = new Date();
    const todayKey = localDayKey(now.toISOString());
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const yesterdayKey = localDayKey(yesterday.toISOString());
    for (const item of timelineItems) {
      const key = localDayKey(item.occurred_at);
      let group = result[result.length - 1];
      if (!group || group.key !== key) {
        const label =
          key === todayKey
            ? m.candidateActivity.today
            : key === yesterdayKey
              ? m.candidateActivity.yesterday
              : new Intl.DateTimeFormat(dateLocale, { dateStyle: "medium" }).format(
                  new Date(item.occurred_at),
                );
        group = { key, label, items: [] };
        result.push(group);
      }
      group.items.push(item);
    }
    return result;
  }, [
    dateLocale,
    timelineItems,
    m.candidateActivity.today,
    m.candidateActivity.yesterday,
  ]);

  function load(nextCategory: FilterCategory, cursor?: string | null, append = false) {
    startTransition(async () => {
      const result = await listCandidateActivitiesAction(candidateId, {
        category: nextCategory,
        cursor,
        limit: 30,
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
        const seen = new Set(current.map((item) => item.id));
        return [...current, ...result.data.items.filter((item) => !seen.has(item.id))];
      });
    });
  }

  function selectCategory(next: FilterCategory) {
    if (next === category || pending) return;
    setCategory(next);
    setShowRecent(false);
    if (next === "all") {
      setItems(initialPage.items);
      setNextCursor(initialPage.next_cursor);
      setError(initialLoadFailed);
      return;
    }
    setItems([]);
    setNextCursor(null);
    setError(false);
    load(next);
  }

  const filters: { value: FilterCategory; label: string }[] = [
    { value: "all", label: m.candidateActivity.filters.all },
    { value: "pipeline", label: m.candidateActivity.filters.pipeline },
    { value: "evaluation", label: m.candidateActivity.filters.evaluation },
    { value: "collaboration", label: m.candidateActivity.filters.collaboration },
  ];

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-bg-elevated">
      <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-[13px] font-semibold text-text">{m.candidateActivity.title}</h2>
          <p className="mt-0.5 text-[11px] text-text-faint">{m.candidateActivity.subtitle}</p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {expanded ? (
            <div
              className="flex flex-wrap gap-1"
              role="group"
              aria-label={m.candidateActivity.filterAria}
            >
              {filters.map((filter) => (
                <button
                  key={filter.value}
                  type="button"
                  aria-pressed={category === filter.value}
                  disabled={pending}
                  onClick={() => selectCategory(filter.value)}
                  className={cn(
                    "rounded-[5px] px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50",
                    category === filter.value
                      ? "bg-bg-active text-text"
                      : "text-text-muted hover:bg-bg-hover hover:text-text",
                  )}
                >
                  {filter.label}
                </button>
              ))}
            </div>
          ) : null}
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((current) => !current)}
            className="inline-flex items-center gap-1.5 rounded-[5px] border border-border bg-background px-2.5 py-1 text-[11px] font-medium text-text-muted transition-colors hover:border-border-strong hover:text-text"
          >
            {expanded ? m.candidateActivity.collapse : m.candidateActivity.expand}
            {expanded ? (
              <ChevronUp aria-hidden="true" className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </div>

      {!expanded ? null : pending && items.length === 0 ? (
        <div className="flex items-center justify-center gap-2 px-4 py-10 text-[12px] text-text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {m.candidateActivity.loading}
        </div>
      ) : error && items.length === 0 ? (
        <div className="flex flex-col items-center gap-3 px-4 py-9 text-center">
          <p className="text-[12px] text-text-muted">{m.candidateActivity.unavailable}</p>
          <Button size="sm" variant="outline" onClick={() => load(category)} disabled={pending}>
            {m.candidateActivity.retry}
          </Button>
        </div>
      ) : groups.length === 0 ? (
        <div className="px-4 py-10 text-center text-[12px] text-text-muted">
          {m.candidateActivity.empty}
        </div>
      ) : (
        <div className="px-4 py-2">
          {groups.map((group) => (
            <div key={group.key} className="py-2">
              <div className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {group.label}
              </div>
              <div className="relative ml-2 border-l border-border pl-5">
                {group.items.map((item) => {
                  const positionTitle =
                    item.position?.title ?? payloadString(item, "position_title", "");
                  const exactTime = new Intl.DateTimeFormat(dateLocale, {
                    dateStyle: "medium",
                    timeStyle: "short",
                  }).format(new Date(item.occurred_at));
                  return (
                    <div key={item.id} className="relative pb-4 last:pb-1">
                      <span className="absolute -left-[30px] top-0 flex h-[19px] w-[19px] items-center justify-center rounded-full border border-border bg-background text-text-muted">
                        <EventIcon eventType={item.event_type} />
                      </span>
                      <p className="text-[12.5px] leading-5 text-text">
                        {eventDescription(item, m, locale)}
                      </p>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[10.5px] text-text-faint">
                        <span>
                          {item.actor_kind === "ai"
                            ? m.candidateActivity.aiAssistantActor
                            : item.actor?.email ?? m.candidateActivity.systemOrRemovedActor}
                        </span>
                        <span aria-hidden>·</span>
                        <time dateTime={item.occurred_at} title={exactTime}>
                          {m.format.relativeTime(item.occurred_at)}
                        </time>
                        {positionTitle ? (
                          <>
                            <span aria-hidden>·</span>
                            {item.position ? (
                              <Link
                                href={`/positions/${item.position.id}`}
                                className="text-text-muted underline decoration-border-strong underline-offset-2 hover:text-text"
                              >
                                {positionTitle}
                              </Link>
                            ) : (
                              <span>{positionTitle}</span>
                            )}
                          </>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {!showRecent && displayItems.length > timelineItems.length ? (
            <div className="flex justify-center border-t border-border py-3">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowRecent(true)}
              >
                {m.candidateActivity.showRecent(displayItems.length)}
              </Button>
            </div>
          ) : null}
          {nextCursor && (showRecent || displayItems.length <= RECENT_VISIBLE_LIMIT) ? (
            <div className="flex justify-center border-t border-border py-3">
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() => load(category, nextCursor, true)}
              >
                {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                {pending ? m.candidateActivity.loading : m.candidateActivity.loadMore}
              </Button>
            </div>
          ) : null}
          {error && items.length > 0 ? (
            <p className="border-t border-border py-3 text-center text-[11px] text-destructive">
              {m.candidateActivity.loadMoreFailed}
            </p>
          ) : null}
        </div>
      )}
    </section>
  );
}
