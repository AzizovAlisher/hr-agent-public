"use client";

import { useEffect, useState, useTransition } from "react";
import { LayoutGrid, Loader2, Table2 } from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { KanbanBoard } from "@/components/positions/kanban-board";
import { RankedCandidatesList } from "@/components/positions/ranked-candidates-list";
import { cn } from "@/lib/utils";
import type { RankedCandidate } from "@/lib/types";
import { listPositionApplicationsAction } from "@/lib/actions";

/** @ui-only Candidate pipeline view selector. */
type View = "table" | "kanban";

/**
 * Owns the table / kanban toggle on the position detail page. The two
 * views read the same `candidates` list but render it very differently —
 * the table is for ranking + filtering, the kanban is for moving people
 * through hiring stages.
 */
export function CandidatesView({
  candidates,
  nextCursor: initialNextCursor,
  positionId,
  canWriteCandidates,
  canChangeStage,
}: {
  candidates: RankedCandidate[];
  nextCursor: string | null;
  positionId: string;
  canWriteCandidates: boolean;
  canChangeStage: boolean;
}) {
  const { m } = useI18n();
  const [view, setView] = useState<View>("table");
  const [items, setItems] = useState(candidates);
  const [nextCursor, setNextCursor] = useState(initialNextCursor);
  const [isLoading, startLoading] = useTransition();

  useEffect(() => {
    setItems(candidates);
    setNextCursor(initialNextCursor);
  }, [candidates, initialNextCursor]);

  function loadMore() {
    if (!nextCursor || isLoading) return;
    startLoading(async () => {
      const result = await listPositionApplicationsAction(positionId, nextCursor);
      if (!result.ok) {
        toast.error(m.position.loadMoreCandidatesFailed);
        return;
      }
      setItems((current) => {
        const byId = new Map(current.map((item) => [item.application_id, item]));
        for (const item of result.data.items) byId.set(item.application_id, item);
        return [...byId.values()];
      });
      setNextCursor(result.data.next_cursor);
    });
  }

  return (
    <div className="flex flex-col">
      {canChangeStage ? <div className="mb-3 flex items-center justify-end">
        <div
          role="tablist"
          aria-label={`${m.kanban.viewTable} / ${m.kanban.viewKanban}`}
          className="inline-flex rounded-[6px] border border-border bg-bg-elevated p-0.5"
        >
          <ViewButton
            active={view === "table"}
            onClick={() => setView("table")}
            onKeyDown={(event) => handleTabKey(event, "table", setView)}
            id="position-pipeline-table-tab"
            controls="position-pipeline-table-panel"
            icon={<Table2 className="h-3 w-3" />}
            label={m.kanban.viewTable}
          />
          <ViewButton
            active={view === "kanban"}
            onClick={() => setView("kanban")}
            onKeyDown={(event) => handleTabKey(event, "kanban", setView)}
            id="position-pipeline-kanban-tab"
            controls="position-pipeline-kanban-panel"
            icon={<LayoutGrid className="h-3 w-3" />}
            label={m.kanban.viewKanban}
          />
        </div>
      </div> : null}

      <div
        id="position-pipeline-table-panel"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby="position-pipeline-table-tab"
        hidden={view !== "table"}
      >
        <RankedCandidatesList
          candidates={items}
          positionId={positionId}
          canWriteCandidates={canWriteCandidates}
        />
      </div>
      {canChangeStage ? (
        <div
          id="position-pipeline-kanban-panel"
          role="tabpanel"
          tabIndex={0}
          aria-labelledby="position-pipeline-kanban-tab"
          hidden={view !== "kanban"}
        >
          <KanbanBoard positionId={positionId} candidates={items} />
        </div>
      ) : null}
      {nextCursor ? (
        <button
          type="button"
          disabled={isLoading}
          onClick={loadMore}
          className="mt-4 inline-flex min-h-11 items-center justify-center gap-2 rounded-md border border-border px-4 py-2 text-xs font-medium hover:bg-bg-hover disabled:opacity-50"
        >
          {isLoading ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          {m.position.loadMoreCandidates}
        </button>
      ) : null}
    </div>
  );
}

function ViewButton({
  active,
  onClick,
  icon,
  label,
  onKeyDown,
  id,
  controls,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  onKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
  id: string;
  controls: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      id={id}
      aria-controls={controls}
      aria-selected={active}
      tabIndex={active ? 0 : -1}
      onKeyDown={onKeyDown}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[5px] px-2.5 py-1 text-[11.5px] font-medium transition-colors",
        active
          ? "bg-bg text-text shadow-sm"
          : "text-text-muted hover:text-text",
      )}
    >
      {icon}
      {label}
    </button>
  );
}

function handleTabKey(
  event: React.KeyboardEvent<HTMLButtonElement>,
  current: View,
  select: (view: View) => void,
) {
  let next: View | null = null;
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    next = current === "table" ? "kanban" : "table";
  } else if (event.key === "Home") {
    next = "table";
  } else if (event.key === "End") {
    next = "kanban";
  }
  if (!next) return;
  event.preventDefault();
  select(next);
  requestAnimationFrame(() => {
    document.getElementById(`position-pipeline-${next}-tab`)?.focus();
  });
}
