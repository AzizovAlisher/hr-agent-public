"use client";

import Link from "next/link";
import { Building2, MessageSquare, Users } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import type { Position } from "@/lib/types";
import { StatusBadge } from "@/components/ui/status-badge";
import { RequirementChips } from "@/components/positions/requirement-chips";

export function PositionCard({ position }: { position: Position }) {
  const { m, locale } = useI18n();
  const statusLabel =
    (m.status as Record<string, string>)[position.status] ?? position.status;

  // `description` is the short summary (1-3 sentences, ≤350 chars). The
  // verbose original lives in `full_description` and never appears on cards.
  const desc = (position.description ?? "").trim();

  // The hiring company within the holding. Stored in requirements.company
  // (excluded from chips); absent on manual/HH/Avito imports → render nothing.
  const company =
    typeof position.requirements?.company === "string"
      ? (position.requirements.company as string).trim()
      : "";

  return (
    <Link
      href={`/positions/${position.id}`}
      className="group lift-card flex h-full min-h-[208px] flex-col overflow-hidden rounded-lg border border-border bg-bg-elevated p-[18px] hover:border-border-strong hover:bg-bg-hover"
    >
      <div className="mb-2.5 flex items-start justify-between gap-3">
        <h3 className="text-[14.5px] font-semibold leading-snug tracking-tight transition-colors group-hover:text-text">
          {position.title}
        </h3>
        <StatusBadge
          status={position.status}
          label={statusLabel}
          className="shrink-0"
        />
      </div>

      {company ? (
        <div
          className="mb-2 flex min-w-0 items-center gap-1 text-[11px] text-text-subtle"
          title={`${m.dashboard.companyLabel}: ${company}`}
        >
          <Building2 className="h-3 w-3 shrink-0" />
          <span className="truncate">{company}</span>
        </div>
      ) : null}

      {desc ? (
        <p className="line-clamp-3 text-[12.5px] leading-[1.55] text-text-muted">
          {desc}
        </p>
      ) : (
        <p className="text-[12px] leading-[1.55] text-text-faint">
          {m.dashboard.noDescription}
        </p>
      )}

      <RequirementChips
        requirements={position.requirements}
        locale={locale}
        max={4}
        className="mt-3"
      />

      <div className="mt-auto flex items-center justify-between gap-2 pt-4">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-[11px] text-text-muted">
            <Users className="h-3 w-3" />
            <span className="font-mono font-medium tabular-nums text-text">
              {position.candidate_count}
            </span>
            <span>{m.dashboard.candidatesCount(position.candidate_count)}</span>
          </div>
          {position.chat_session_count > 0 ? (
            <div className="flex items-center gap-1.5 text-[11px] text-text-muted">
              <MessageSquare className="h-3 w-3" />
              <span className="font-mono font-medium tabular-nums text-text">
                {position.chat_session_count}
              </span>
              <span>
                {m.dashboard.chatsCount(position.chat_session_count)}
              </span>
            </div>
          ) : null}
        </div>
        <span className="font-mono text-[10px] tracking-[0.02em] text-text-faint">
          {m.format.relativeTime(position.updated_at)}
        </span>
      </div>
    </Link>
  );
}
