"use client";

import { useMemo, useState } from "react";
import { Heart, ListFilter } from "lucide-react";

import type { RankedCandidate } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useI18n } from "@/components/i18n/locale-provider";
import { CandidateRow } from "@/components/positions/candidate-row";
import {
  CompareBar,
  useCompareSelection,
} from "@/components/positions/compare-selection";
import { EmptyState } from "@/components/ui/empty-state";

export function RankedCandidatesList({
  candidates,
  positionId,
  canWriteCandidates,
}: {
  candidates: RankedCandidate[];
  positionId: string;
  canWriteCandidates: boolean;
}) {
  const { m } = useI18n();
  const [favOnly, setFavOnly] = useState(false);
  const [hideViewed, setHideViewed] = useState(false);
  const compare = useCompareSelection();

  const favCount = useMemo(
    () => candidates.filter((c) => c.is_favorite).length,
    [candidates],
  );
  const newCount = useMemo(
    () => candidates.filter((c) => !c.viewed_at).length,
    [candidates],
  );

  const visible = useMemo(() => {
    return candidates.filter((c) => {
      if (favOnly && !c.is_favorite) return false;
      if (hideViewed && c.viewed_at) return false;
      return true;
    });
  }, [candidates, favOnly, hideViewed]);

  return (
    <>
      <div className="mb-3 flex items-baseline justify-between gap-3 px-0.5">
        <div className="flex items-center gap-2">
          <ListFilter className="h-3.5 w-3.5 text-text-muted" />
          <h2 className="text-[13px] font-semibold tracking-tight">
            {m.position.ranked}
          </h2>
          <span className="font-mono text-[11px] text-text-faint">
            {m.position.rankedSubtitle(candidates.length)}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Toggle
            active={favOnly}
            onClick={() => setFavOnly((v) => !v)}
            count={favCount}
          >
            <Heart
              className="h-3 w-3"
              fill={favOnly ? "currentColor" : "none"}
              strokeWidth={1.6}
            />
            {m.position.favorites}
          </Toggle>
          <Toggle
            active={hideViewed}
            onClick={() => setHideViewed((v) => !v)}
            count={newCount}
          >
            {m.position.newOnly}
          </Toggle>
        </div>
      </div>

      {visible.length === 0 ? (
        candidates.length === 0 ? (
          <EmptyState
            title={m.position.emptyCandidatesTitle}
            description={m.position.emptyCandidatesDesc}
          />
        ) : (
          <EmptyState
            title={m.position.noMatchesTitle}
            description={m.position.noMatchesDesc}
          />
        )
      ) : (
        <div className="flex flex-col gap-2">
          {visible.map((c, i) => (
            <CandidateRow
              key={c.candidate_id}
              candidate={c}
              rank={i + 1}
              delayMs={Math.min(i, 8) * 40}
              positionId={positionId}
              canFavorite={canWriteCandidates}
              selection={{
                isSelected: compare.isSelected(c.candidate_id),
                isFull: compare.isFull,
                onToggle: () => compare.toggle(c.candidate_id),
              }}
            />
          ))}
        </div>
      )}
      <CompareBar
        positionId={positionId}
        selected={compare.selected}
        onClear={compare.clear}
      />
    </>
  );
}

function Toggle({
  active,
  onClick,
  count,
  children,
}: {
  active: boolean;
  onClick: () => void;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[6px] border px-2 py-[5px] text-[11.5px] transition-all",
        active
          ? "border-border-strong bg-bg-active text-text"
          : "border-border bg-bg-elevated text-text-muted hover:bg-bg-hover hover:text-text",
      )}
    >
      {children}
      <span className="font-mono text-[10px] text-text-faint tabular-nums">
        {count}
      </span>
    </button>
  );
}
