"use client";

import Link from "next/link";
import { useEffect, useState, useTransition } from "react";
import { ArrowRight, ChevronDown, Sparkles, Target } from "lucide-react";
import { toast } from "sonner";

import {
  getBestPositionsAction,
  getCandidateProcessingJobAction,
  scoreAllOpenAction,
  scoreCandidateAction,
} from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";
import type { PositionMatch } from "@/lib/types";

/** @ui-only Best-position section composition properties. */
type Props = {
  candidateId: string;
  initial: PositionMatch[];
  canScore: boolean;
};

/**
 * Reverse matching: shows every open position in the org with this candidate's
 * score against it (or "—" + a "Score" button if not yet scored).
 *
 * Hydrates from the SSR-fetched `initial` list, then lets the recruiter score
 * one position at a time or fan out to "all open" with a single click.
 */
// How many rows to show before the "show more" toggle kicks in. Chosen
// to fit the section in roughly one viewport on a 13" laptop.
const COLLAPSED_ROWS = 6;
const JOB_POLL_ATTEMPTS = 30;
const JOB_POLL_INTERVAL_MS = 2_000;

function wait(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

export function BestPositionsSection({ candidateId, initial, canScore }: Props) {
  const { m } = useI18n();
  const [matches, setMatches] = useState<PositionMatch[]>(initial);
  const [bulk, startBulk] = useTransition();
  const [bulkQueued, setBulkQueued] = useState(false);
  const [scoringPositionId, setScoringPositionId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => setMatches(initial), [initial]);

  const scored = matches.filter((m) => m.overall_score != null);
  const unscored = matches.filter((m) => m.overall_score == null);
  const best = scored[0] ?? null;

  // Don't repeat the best-match in the list — it's already in the callout.
  const listMatches = best
    ? matches.filter((m) => m.position_id !== best.position_id)
    : matches;
  const visibleMatches = expanded
    ? listMatches
    : listMatches.slice(0, COLLAPSED_ROWS);
  const hiddenCount = listMatches.length - visibleMatches.length;

  function scoreOne(positionId: string) {
    if (scoringPositionId) return;
    setScoringPositionId(positionId);
    void scoreCandidateAction(candidateId, positionId).then(async (res) => {
      if (!res.ok) {
        setScoringPositionId(null);
        toast.error(m.bestPositions.failedToast(res.error));
        return;
      }
      toast.success(m.bestPositions.queuedToast);

      let lastError: string | null = null;
      for (let attempt = 0; attempt < JOB_POLL_ATTEMPTS; attempt += 1) {
        await wait(JOB_POLL_INTERVAL_MS);
        const job = await getCandidateProcessingJobAction(candidateId, res.data.id);
        if (!job.ok) {
          lastError = job.error;
          continue;
        }
        if (job.data.status === "dead_letter" || job.data.status === "cancelled") {
          setScoringPositionId(null);
          toast.error(
            m.bestPositions.failedToast(
              job.data.failure_detail || job.data.failure_code || job.data.status,
            ),
          );
          return;
        }
        if (job.data.status !== "succeeded") continue;

        const refreshed = await getBestPositionsAction(candidateId);
        setScoringPositionId(null);
        if (refreshed.ok) {
          setMatches(refreshed.data);
        } else {
          toast.error(m.bestPositions.failedToast(refreshed.error));
        }
        return;
      }

      setScoringPositionId(null);
      toast.info(
        lastError
          ? m.bestPositions.failedToast(lastError)
          : m.bestPositions.stillProcessingToast,
      );
    });
  }

  function scoreAll() {
    if (bulk || bulkQueued) return;
    startBulk(async () => {
      const res = await scoreAllOpenAction(candidateId);
      if (!res.ok) {
        toast.error(m.bestPositions.failedToast(res.error));
        return;
      }
      if (res.data.queued === 0) {
        toast.info(m.bestPositions.noJobsQueuedToast);
        return;
      }
      setBulkQueued(true);
      toast.success(m.bestPositions.queuedAllToast(res.data.queued));

      let lastError: string | null = null;
      for (let attempt = 0; attempt < JOB_POLL_ATTEMPTS; attempt += 1) {
        await wait(JOB_POLL_INTERVAL_MS);
        const refreshed = await getBestPositionsAction(candidateId);
        if (!refreshed.ok) {
          lastError = refreshed.error;
          continue;
        }
        setMatches(refreshed.data);
        if (refreshed.data.every((match) => match.overall_score != null)) {
          setBulkQueued(false);
          toast.success(m.bestPositions.scoredAllToast);
          return;
        }
      }

      setBulkQueued(false);
      toast.info(
        lastError
          ? m.bestPositions.failedToast(lastError)
          : m.bestPositions.stillProcessingToast,
      );
    });
  }

  return (
    <div className="rounded-lg border border-border bg-bg-elevated">
      <header className="flex items-center justify-between gap-3 border-b border-border px-5 py-3.5">
        <div className="flex min-w-0 items-center gap-2">
          <Target className="h-3.5 w-3.5 text-text-muted" />
          <h2 className="text-[13px] font-semibold tracking-tight">
            {m.bestPositions.title}
          </h2>
          {matches.length > 0 ? (
            <span className="font-mono text-[10px] text-text-faint">
              · {matches.length}
            </span>
          ) : null}
        </div>
        {canScore && unscored.length > 0 ? (
          <button
            type="button"
            onClick={scoreAll}
            disabled={bulk || bulkQueued || !!scoringPositionId}
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2.5 py-1 text-[11px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover disabled:opacity-40"
          >
            <Sparkles className="h-3 w-3" />
            {bulk || bulkQueued
              ? m.bestPositions.scoringAll(unscored.length)
              : m.bestPositions.scoreAll(unscored.length)}
          </button>
        ) : null}
      </header>

      {/* Best match callout (if any scores exist) */}
      {best ? (
        <div className="border-b border-border bg-accent-muted/40 px-5 py-3">
          <div className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {m.bestPositions.bestMatchLabel}
          </div>
          <div className="mt-0.5 flex items-center gap-3">
            <Link
              href={`/positions/${best.position_id}`}
              className="text-[14px] font-semibold text-text transition-colors hover:text-primary"
            >
              {best.position_title}
            </Link>
            <span
              className="font-mono text-[18px] font-semibold tabular-nums"
              style={{
                color:
                  scoreTier(best.overall_score) != null
                    ? SCORE_VAR[scoreTier(best.overall_score)!]
                    : "var(--text-faint)",
              }}
            >
              {best.overall_score}
            </span>
          </div>
        </div>
      ) : null}

      {matches.length === 0 ? (
        <div className="px-5 py-8 text-center text-[12px] text-text-muted">
          {m.bestPositions.empty}
        </div>
      ) : (
        <>
          {listMatches.length === 0 ? null : (
            <ul className="flex flex-col">
              {visibleMatches.map((p) => (
                <PositionRow
                  key={p.position_id}
                  match={p}
                  scoring={scoringPositionId === p.position_id}
                  disabled={!!scoringPositionId || bulk}
                  canScore={canScore}
                  onScore={() => scoreOne(p.position_id)}
                />
              ))}
            </ul>
          )}
          {hiddenCount > 0 || (expanded && listMatches.length > COLLAPSED_ROWS) ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="flex w-full items-center justify-center gap-1.5 border-t border-border px-5 py-2.5 text-[11.5px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
            >
              <ChevronDown
                className={cn(
                  "h-3 w-3 transition-transform",
                  expanded && "rotate-180",
                )}
              />
              {expanded
                ? m.bestPositions.showLess
                : m.bestPositions.showMore(hiddenCount)}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}

function PositionRow({
  match,
  scoring,
  disabled,
  canScore,
  onScore,
}: {
  match: PositionMatch;
  scoring: boolean;
  disabled: boolean;
  canScore: boolean;
  onScore: () => void;
}) {
  const { m } = useI18n();
  const tier = scoreTier(match.overall_score);
  return (
    <li className="flex items-center gap-3 border-b border-border px-5 py-2.5 last:border-b-0">
      <Link
        href={`/positions/${match.position_id}`}
        className="flex flex-1 min-w-0 items-center gap-2 text-left"
      >
        <span className="truncate text-[12.5px] font-medium text-text transition-colors hover:text-primary">
          {match.position_title}
        </span>
      </Link>
      {match.overall_score != null ? (
        <span
          className="font-mono text-[14px] font-semibold tabular-nums"
          style={{
            color: tier != null ? SCORE_VAR[tier] : "var(--text-faint)",
          }}
        >
          {match.overall_score}
        </span>
      ) : canScore ? (
        <button
          type="button"
          onClick={onScore}
          disabled={disabled}
          className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2 py-1 text-[11px] font-medium text-text-muted transition-colors hover:border-border-strong hover:bg-bg-hover hover:text-text disabled:opacity-40"
        >
          {scoring ? m.bestPositions.scoring : m.bestPositions.score}
          {!scoring ? <ArrowRight className="h-3 w-3" /> : null}
        </button>
      ) : (
        <span className="font-mono text-[12px] text-text-faint">—</span>
      )}
    </li>
  );
}
