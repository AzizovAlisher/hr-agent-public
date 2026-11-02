"use client";

import Link from "next/link";
import { ArrowRight, Eye } from "lucide-react";

import { candRef } from "@/lib/format";
import type { RankedCandidate } from "@/lib/types";
import { ScoreChip } from "@/components/ui/score-chip";
import { SourceBadge } from "@/components/ui/source-badge";
import { FavoriteButton } from "@/components/candidates/favorite-button";
import { useI18n } from "@/components/i18n/locale-provider";
import { CompareCheckbox } from "@/components/positions/compare-selection";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";

/**
 * Compact ranked-candidate row. The intent is glance-able signal — overall score,
 * who they are, and 4 thin tier-colored bars per criterion as a visual mix.
 * Click → /candidates/{id} for the full breakdown + comments.
 */
export function CandidateRow({
  candidate,
  rank,
  delayMs = 0,
  positionId,
  selection,
  canFavorite = true,
}: {
  candidate: RankedCandidate;
  rank: number;
  delayMs?: number;
  positionId?: string;
  canFavorite?: boolean;
  selection?: {
    isSelected: boolean;
    isFull: boolean;
    onToggle: () => void;
  };
}) {
  const { m } = useI18n();
  const ref =
    candidate.display_name?.trim() ||
    candRef(candidate.candidate_id, candidate.source_ref);
  const viewed = candidate.viewed_at != null;

  const criteriaScores = [
    candidate.score_technical_skills,
    candidate.score_quality_of_projects,
    candidate.score_previous_experience,
    candidate.score_impact,
  ];

  return (
    <Link
      href={`/candidates/${candidate.candidate_id}${positionId ? `?position=${positionId}` : ""}`}
      className={cn(
        "group flex items-center gap-3.5 rounded-lg border border-border bg-bg-elevated px-4 py-3 transition-colors hover:border-border-strong hover:bg-bg-hover",
        // The eye marker already communicates the viewed state. Dimming the
        // whole row also dimmed score/source text below WCAG contrast.
        viewed && "bg-bg",
      )}
      style={{ animationDelay: `${delayMs}ms` }}
    >
      {selection ? (
        <CompareCheckbox
          checked={selection.isSelected}
          disabled={selection.isFull}
          onChange={selection.onToggle}
          ariaLabel={ref}
        />
      ) : null}

      <span className="w-[18px] shrink-0 font-mono text-[11px] text-text-muted tabular-nums">
        {rank.toString().padStart(2, "0")}
      </span>

      <ScoreChip score={candidate.overall_score} />

      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <SourceBadge source={candidate.source} />
        <span className="truncate font-mono text-[12px] font-medium text-text">
          {ref}
        </span>
        {viewed ? (
          <span
            title={m.position.viewedAgo(
              m.format.relativeTime(candidate.viewed_at!),
            )}
            className="flex items-center text-text-faint"
          >
            <Eye className="h-3 w-3" />
          </span>
        ) : null}
      </div>

      {/* Compact 4-bar criterion strip — no labels, no comments. Just visual mix. */}
      <div className="hidden items-center gap-[3px] sm:flex" aria-hidden>
        {criteriaScores.map((s, i) => {
          const t = scoreTier(s);
          const pct = s == null ? 0 : Math.max(0, Math.min(100, s)) / 100;
          return (
            <span
              key={i}
              className="relative h-3 w-[22px] overflow-hidden rounded-[2px] bg-border"
              title={s != null ? String(s) : undefined}
            >
              <span
                className="bar-fill absolute bottom-0 left-0 h-full"
                style={{
                  width: `${pct * 100}%`,
                  background: t ? SCORE_VAR[t] : "var(--text-faint)",
                  animationDelay: `${delayMs + 80 + i * 40}ms`,
                }}
              />
            </span>
          );
        })}
      </div>

      {canFavorite ? (
        <FavoriteButton
          candidateId={candidate.candidate_id}
          initial={candidate.is_favorite}
          parentPath={positionId ? `/positions/${positionId}` : undefined}
        />
      ) : null}

      <ArrowRight className="h-3.5 w-3.5 shrink-0 -translate-x-1 text-text-faint opacity-0 transition-all duration-200 ease-out group-hover:translate-x-0 group-hover:text-text group-hover:opacity-100" />
    </Link>
  );
}
