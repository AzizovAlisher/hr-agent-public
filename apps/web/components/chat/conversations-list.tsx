"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { ArrowRight, LayoutGrid, MessageSquare, Search, User as UserIcon } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import type { ChatSessionListItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";

/** @ui-only Conversation-list scope filter. */
type ScopeFilter = "all" | "position" | "candidate" | "free";

export function ConversationsList({ sessions }: { sessions: ChatSessionListItem[] }) {
  const { m } = useI18n();
  const [tab, setTab] = useState<ScopeFilter>("all");
  const [query, setQuery] = useState("");

  const counts = useMemo(() => {
    let pos = 0,
      cand = 0,
      free = 0;
    for (const s of sessions) {
      if (s.scope === "position") pos += 1;
      else if (s.scope === "candidate") cand += 1;
      else if (s.scope === "free") free += 1;
    }
    return { all: sessions.length, pos, cand, free };
  }, [sessions]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return sessions.filter((s) => {
      if (tab !== "all" && s.scope !== tab) return false;
      if (!q) return true;
      const hay =
        `${s.preview ?? ""} ${s.position_title ?? ""} ${s.candidate_ref ?? ""} ${s.title ?? ""}`.toLowerCase();
      return hay.includes(q);
    });
  }, [sessions, tab, query]);

  return (
    <>
      <div className="mb-5 flex flex-wrap items-center gap-2 border-b border-border pb-4">
        <div className="relative max-w-[320px] flex-1">
          <Search className="pointer-events-none absolute left-[11px] top-1/2 h-3 w-3 -translate-y-1/2 text-text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={m.conversations.searchPlaceholder}
            className="w-full rounded-[6px] border border-border bg-bg-elevated py-[7px] pl-8 pr-3 text-[12px] outline-none transition-colors placeholder:text-text-faint focus:border-border-strong"
          />
        </div>
        <Tab label={m.conversations.tabAll} count={counts.all} active={tab === "all"} onClick={() => setTab("all")} />
        <Tab label={m.conversations.tabPosition} count={counts.pos} active={tab === "position"} onClick={() => setTab("position")} />
        <Tab label={m.conversations.tabCandidate} count={counts.cand} active={tab === "candidate"} onClick={() => setTab("candidate")} />
        <Tab label={m.conversations.tabFree} count={counts.free} active={tab === "free"} onClick={() => setTab("free")} />
      </div>

      {filtered.length === 0 ? (
        sessions.length === 0 ? (
          <EmptyState
            icon={<MessageSquare className="h-4 w-4" />}
            title={m.conversations.emptyTitle}
            description={m.conversations.emptyDesc}
          />
        ) : (
          <EmptyState
            title={m.conversations.noMatchesTitle}
            description={m.conversations.noMatchesDesc}
          />
        )
      ) : (
        <ul className="overflow-hidden rounded-xl border border-border bg-bg-elevated">
          {filtered.map((s, i) => (
            <SessionRow
              key={s.id}
              session={s}
              isLast={i === filtered.length - 1}
              delayMs={Math.min(i, 10) * 30}
            />
          ))}
        </ul>
      )}
    </>
  );
}

function Tab({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-[6px] border px-2.5 py-[5px] text-[12px] transition-all",
        active
          ? "border-border-strong bg-bg-active text-text"
          : "border-border bg-bg-elevated text-text-muted hover:bg-bg-hover hover:text-text",
      )}
    >
      {label}
      <span className="ml-1.5 font-mono text-[11px] text-text-faint tabular-nums">
        · {count}
      </span>
    </button>
  );
}

function SessionRow({
  session,
  isLast,
  delayMs,
}: {
  session: ChatSessionListItem;
  isLast: boolean;
  delayMs: number;
}) {
  const { m } = useI18n();
  const href = computeHref(session);
  // A session with no resolvable target (free scope, or a linked entity that's
  // gone/archived) would otherwise be a clickable row that navigates nowhere.
  const dead = href === "/conversations";
  const ScopeIcon =
    session.scope === "position"
      ? LayoutGrid
      : session.scope === "candidate"
        ? UserIcon
        : MessageSquare;
  const scopeLabel =
    session.scope === "position"
      ? m.conversations.scopePosition
      : session.scope === "candidate"
        ? m.conversations.scopeCandidate
        : m.conversations.scopeFree;
  const linked =
    session.scope === "position"
      ? session.position_title ?? "—"
      : session.scope === "candidate"
        ? session.candidate_ref ?? "—"
        : null;

  const body = (
    <>
      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-bg text-text-muted">
        <ScopeIcon className="h-3.5 w-3.5" />
      </span>

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2 text-[12px]">
          <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-text-faint">
            {scopeLabel}
          </span>
          {linked ? (
            <span className="truncate font-mono text-[11.5px] text-text-muted">
              {linked}
            </span>
          ) : null}
        </div>
        <p className="line-clamp-1 text-[12.5px] text-text">
          {session.preview ?? "—"}
        </p>
      </div>

      <span className="hidden w-[110px] shrink-0 text-right font-mono text-[10px] tracking-[0.02em] text-text-faint md:inline">
        {m.conversations.messagesCount(session.message_count)}
      </span>
      <span className="hidden w-[80px] shrink-0 text-right font-mono text-[10px] tracking-[0.02em] text-text-faint md:inline">
        {m.format.relativeTime(session.last_activity ?? session.updated_at)}
      </span>
      {dead ? null : (
        <ArrowRight className="h-3.5 w-3.5 shrink-0 -translate-x-1 text-text-faint opacity-0 transition-all duration-200 ease-out group-hover:translate-x-0 group-hover:text-text group-hover:opacity-100" />
      )}
    </>
  );

  return (
    <li
      className={cn("reveal", !isLast && "border-b border-border")}
      style={{ animationDelay: `${delayMs}ms` }}
    >
      {dead ? (
        <div className="flex cursor-default items-center gap-4 px-5 py-4">
          {body}
        </div>
      ) : (
        <Link
          href={href}
          className="group flex items-center gap-4 px-5 py-4 transition-colors hover:bg-bg-hover"
        >
          {body}
        </Link>
      )}
    </li>
  );
}

function computeHref(s: ChatSessionListItem): string {
  if (s.scope === "position" && s.position_id) {
    return `/positions/${s.position_id}?chat=${s.id}`;
  }
  if (s.scope === "candidate" && s.candidate_id) {
    return `/candidates/${s.candidate_id}?chat=${s.id}`;
  }
  return "/conversations";
}
