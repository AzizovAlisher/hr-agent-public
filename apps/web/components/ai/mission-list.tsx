import Link from "next/link";
import { ArrowRight, Bot, CalendarClock, Target, Users } from "lucide-react";

import { MissionStatusBadge } from "@/components/ai/ai-status";
import type { AiCopy } from "@/lib/ai/copy";
import type { AiSession, HiringMission } from "@/lib/ai/types";
import type { Locale } from "@/lib/i18n/types";

export function MissionList({
  missions,
  orphanSessions = [],
  locale,
  copy,
  emptyTitle,
  emptyBody,
}: {
  missions: HiringMission[];
  orphanSessions?: AiSession[];
  locale: Locale;
  copy: AiCopy;
  emptyTitle: string;
  emptyBody: string;
}) {
  if (!missions.length && !orphanSessions.length) {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center py-20 text-center">
        <span className="flex h-11 w-11 items-center justify-center rounded-xl border border-border bg-bg-elevated text-text-muted">
          <Bot className="h-5 w-5" />
        </span>
        <h2 className="mt-4 text-[15px] font-semibold">{emptyTitle}</h2>
        <p className="mt-1.5 text-[12px] leading-5 text-text-muted">{emptyBody}</p>
        <Link href="/ai/new" className="mt-5 inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[12px] font-medium text-primary-foreground">
          {copy.newMission}
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </div>
    );
  }

  return (
    <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-3">
      {missions.map((mission) => (
        <Link
          key={mission.id}
          href={`/ai/missions/${mission.id}`}
          className="group flex min-h-[170px] flex-col rounded-xl border border-border bg-bg-elevated p-4 transition-all hover:-translate-y-0.5 hover:border-accent-border hover:shadow-lg"
        >
          <div className="flex items-start justify-between gap-3">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-muted text-primary">
              <Target className="h-4 w-4" />
            </span>
            <MissionStatusBadge status={mission.status} locale={locale} />
          </div>
          <h2 className="mt-4 line-clamp-2 text-[14px] font-semibold leading-5">
            {mission.title || mission.brief?.role_title || copy.common.noTitle}
          </h2>
          <p className="mt-1 line-clamp-2 text-[11.5px] leading-5 text-text-muted">
            {mission.original_request}
          </p>
          <div className="mt-auto flex flex-wrap items-center gap-x-4 gap-y-1 pt-4 text-[10.5px] text-text-faint">
            <span className="inline-flex items-center gap-1"><Users className="h-3 w-3" />{mission.candidate_count ?? 0} {copy.common.candidates}</span>
            <span className="inline-flex items-center gap-1"><Target className="h-3 w-3" />{mission.shortlisted_count ?? 0}/{mission.shortlist_size}</span>
            <span className="ml-auto inline-flex items-center gap-1"><CalendarClock className="h-3 w-3" />{new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", { dateStyle: "medium" }).format(new Date(mission.updated_at))}</span>
          </div>
        </Link>
      ))}

      {orphanSessions.map((session) => (
        <Link
          key={session.id}
          href={`/ai/sessions/${session.id}`}
          className="group flex min-h-[170px] flex-col rounded-xl border border-dashed border-border bg-bg-elevated/60 p-4 transition-colors hover:border-accent-border hover:bg-bg-elevated"
        >
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-bg-hover text-text-muted"><Bot className="h-4 w-4" /></span>
          <h2 className="mt-4 line-clamp-2 text-[14px] font-semibold">{session.title || copy.common.noTitle}</h2>
          <p className="mt-1 line-clamp-2 text-[11.5px] text-text-muted">{copy.empty.messages}</p>
          <div className="mt-auto pt-4 text-[10px] uppercase tracking-[0.06em] text-text-faint">{locale === "ru" ? "Диалог" : "Conversation"}</div>
        </Link>
      ))}
    </div>
  );
}
