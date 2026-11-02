"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { DragEndEvent, DragStartEvent } from "@dnd-kit/core";
import { GripVertical, MoreHorizontal, MoveRight, Star } from "lucide-react";
import { toast } from "sonner";

import { DispositionDialog } from "@/components/applications/disposition-dialog";
import { useI18n } from "@/components/i18n/locale-provider";
import { updateCandidateStageAction } from "@/lib/actions";
import { candRef } from "@/lib/format";
import { SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  KANBAN_STAGES,
  type ApplicationDisposition,
  type KanbanStage,
  type RankedCandidate,
} from "@/lib/types";

/** @ui-only Kanban-board composition properties. */
type Props = {
  positionId: string;
  candidates: RankedCandidate[];
};

/**
 * Drag-and-drop pipeline board. Six fixed columns; one card per
 * candidate-position score row. Optimistic — the card jumps immediately on
 * drop, then a server action persists the move and reverts on failure.
 */
export function KanbanBoard({ positionId, candidates }: Props) {
  const { m } = useI18n();
  const [items, setItems] = useState<RankedCandidate[]>(candidates);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [pendingRejection, setPendingRejection] =
    useState<RankedCandidate | null>(null);
  const [isMoving, setIsMoving] = useState(false);

  // Resync when the server sends fresh data (CV upload, rescore, edits in
  // another tab) — useState ignores prop changes after mount, which would
  // otherwise leave the board stale vs the table view.
  useEffect(() => {
    setItems(candidates);
  }, [candidates]);

  const sensors = useSensors(
    // Small activation distance so a normal click on the card link doesn't
    // accidentally start a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
  );

  const byStage = useMemo(() => {
    const buckets: Record<KanbanStage, RankedCandidate[]> = {
      new: [],
      screening: [],
      interview: [],
      offer: [],
      hired: [],
      rejected: [],
    };
    for (const c of items) {
      const s: KanbanStage = (KANBAN_STAGES as string[]).includes(c.stage)
        ? c.stage
        : "new";
      buckets[s].push(c);
    }
    // Within a column: highest score first, then by candidate id for stability.
    for (const k of KANBAN_STAGES) {
      buckets[k].sort((a, b) => {
        const sa = a.overall_score ?? -1;
        const sb = b.overall_score ?? -1;
        if (sa !== sb) return sb - sa;
        return a.candidate_id.localeCompare(b.candidate_id);
      });
    }
    return buckets;
  }, [items]);

  function handleDragStart(e: DragStartEvent) {
    if (isMoving) return;
    setActiveId(String(e.active.id));
  }

  function handleDragEnd(e: DragEndEvent) {
    setActiveId(null);
    if (isMoving) return;
    const { active, over } = e;
    if (!over) return;

    const candidateId = String(active.id);
    const newStage = String(over.id) as KanbanStage;
    if (!(KANBAN_STAGES as string[]).includes(newStage)) return;

    const current = items.find((c) => c.candidate_id === candidateId);
    if (!current || current.stage === newStage) return;

    if (newStage === "rejected") {
      setPendingRejection(current);
      return;
    }

    void moveCandidate(current, newStage);
  }

  async function moveCandidate(
    current: RankedCandidate,
    newStage: KanbanStage,
    dispositionCode?: ApplicationDisposition,
    reason?: string,
  ): Promise<boolean> {
    if (isMoving) return false;
    setIsMoving(true);

    // Optimistic update.
    const previousStage = current.stage;
    setItems((prev) =>
      prev.map((c) =>
        c.candidate_id === current.candidate_id ? { ...c, stage: newStage } : c,
      ),
    );

    const res = await updateCandidateStageAction(
      positionId,
      current.candidate_id,
      newStage,
      current.application_version,
      dispositionCode,
      reason,
    );
    if (!res.ok) {
      // Revert.
      setItems((prev) =>
        prev.map((c) =>
          c.candidate_id === current.candidate_id
            ? { ...c, stage: previousStage }
            : c,
        ),
      );
      toast.error(m.kanban.moveFailedToast(res.error));
      setIsMoving(false);
      return false;
    }
    setItems((prev) =>
      prev.map((candidate) =>
        candidate.candidate_id === current.candidate_id
          ? {
              ...candidate,
              stage: newStage,
              application_id: res.data.application_id,
              application_version: res.data.application_version,
            }
          : candidate,
      ),
    );
    toast.success(m.kanban.movedToast(m.kanban.stages[newStage]));
    setIsMoving(false);
    return true;
  }

  const activeCard =
    activeId != null
      ? items.find((c) => c.candidate_id === activeId)
      : undefined;

  return (
    <>
      <DndContext
        sensors={sensors}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div className="flex gap-3 overflow-x-auto pb-2">
          {KANBAN_STAGES.map((stage) => (
            <KanbanColumn
              key={stage}
              stage={stage}
              label={m.kanban.stages[stage]}
              cards={byStage[stage]}
              emptyLabel={m.kanban.emptyColumn}
              draggingId={activeId}
              positionId={positionId}
              isMoving={isMoving}
              onMoveStage={(candidate, stage) => {
                if (stage === "rejected") {
                  setPendingRejection(candidate);
                  return;
                }
                void moveCandidate(candidate, stage);
              }}
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {activeCard ? <CandidateCard match={activeCard} positionId={positionId} dragging /> : null}
        </DragOverlay>
      </DndContext>
      <DispositionDialog
        open={pendingRejection !== null}
        busy={isMoving}
        onOpenChange={(open) => !open && setPendingRejection(null)}
        onConfirm={(disposition, reason) => {
          if (!pendingRejection) return;
          void moveCandidate(
            pendingRejection,
            "rejected",
            disposition,
            reason,
          ).then((moved) => {
            if (moved) setPendingRejection(null);
          });
        }}
      />
    </>
  );
}

function KanbanColumn({
  stage,
  label,
  cards,
  emptyLabel,
  draggingId,
  positionId,
  isMoving,
  onMoveStage,
}: {
  stage: KanbanStage;
  label: string;
  cards: RankedCandidate[];
  emptyLabel: string;
  draggingId: string | null;
  positionId: string;
  isMoving: boolean;
  onMoveStage: (candidate: RankedCandidate, stage: KanbanStage) => void;
}) {
  const { isOver, setNodeRef } = useDroppable({ id: stage });
  return (
    <div className="flex w-[260px] shrink-0 flex-col">
      <div className="mb-2 flex items-center justify-between gap-2 px-1">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              STAGE_DOT[stage],
            )}
          />
          <span className="text-[12px] font-semibold tracking-tight text-text">
            {label}
          </span>
          <span className="font-mono text-[10.5px] text-text-faint tabular-nums">
            {cards.length}
          </span>
        </div>
      </div>
      <div
        ref={setNodeRef}
        className={cn(
          "flex min-h-[140px] flex-col gap-2 rounded-lg border border-dashed p-2 transition-colors",
          isOver
            ? "border-accent-border bg-accent-muted/40"
            : "border-border bg-bg/40",
        )}
      >
        {cards.length === 0 ? (
          <div className="py-5 text-center text-[10.5px] uppercase tracking-wider text-text-faint">
            {emptyLabel}
          </div>
        ) : (
          cards.map((c) => (
            <DraggableCard
              key={c.candidate_id}
              match={c}
              hidden={draggingId === c.candidate_id}
              positionId={positionId}
              isMoving={isMoving}
              onMoveStage={onMoveStage}
            />
          ))
        )}
      </div>
    </div>
  );
}

function DraggableCard({
  match,
  hidden,
  positionId,
  isMoving,
  onMoveStage,
}: {
  match: RankedCandidate;
  hidden: boolean;
  positionId: string;
  isMoving: boolean;
  onMoveStage: (candidate: RankedCandidate, stage: KanbanStage) => void;
}) {
  const { m } = useI18n();
  const {
    attributes,
    listeners,
    setActivatorNodeRef,
    setNodeRef,
    isDragging,
  } = useDraggable({ id: match.candidate_id });
  return (
    <div
      ref={setNodeRef}
      className={cn(
        "relative",
        // While drag is active we render a duplicate card via DragOverlay,
        // so the source slot becomes a half-faded ghost.
        (hidden || isDragging) && "opacity-30",
      )}
    >
      <button
        ref={setActivatorNodeRef}
        type="button"
        {...attributes}
        {...listeners}
        aria-label={m.kanban.dragCandidate}
        className="absolute left-1 top-2 z-10 flex h-7 w-7 touch-none cursor-grab items-center justify-center rounded-md text-text-faint hover:bg-bg-hover hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary active:cursor-grabbing"
      >
        <GripVertical className="h-4 w-4" aria-hidden="true" />
      </button>
      <CandidateCard
        match={match}
        positionId={positionId}
        hasDragHandle
        isMoving={isMoving}
        onMoveStage={onMoveStage}
      />
    </div>
  );
}

function CandidateCard({
  match,
  positionId,
  dragging = false,
  hasDragHandle = false,
  isMoving = false,
  onMoveStage,
}: {
  match: RankedCandidate;
  positionId: string;
  dragging?: boolean;
  hasDragHandle?: boolean;
  isMoving?: boolean;
  onMoveStage?: (candidate: RankedCandidate, stage: KanbanStage) => void;
}) {
  const { m } = useI18n();
  const tier = scoreTier(match.overall_score);
  const scoreColor = tier != null ? SCORE_VAR[tier] : "var(--text-faint)";
  // Prefer the policy-filtered API label. Legacy records may still use
  // source_ref, while restricted readers receive neither contact projection.
  const label =
    match.display_name?.trim() || candRef(match.candidate_id, match.source_ref);
  return (
    <div
      className={cn(
        "select-none rounded-[8px] border bg-bg-elevated py-2.5 pr-3 text-left transition-shadow",
        hasDragHandle ? "pl-9" : "pl-3",
        dragging
          ? "cursor-grabbing border-border-strong shadow-lg ring-1 ring-accent-border"
          : "cursor-grab border-border hover:border-border-strong",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <Link
            href={`/candidates/${match.candidate_id}?position=${positionId}`}
            // Stop the drag listener from swallowing clicks — without this,
            // the link would only fire on a tap-and-hold, which feels broken.
            onPointerDown={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()}
            className="block truncate text-[12.5px] font-medium text-text hover:text-primary"
          >
            {label}
          </Link>
          <div className="mt-0.5 flex items-center gap-1.5 text-[10.5px] text-text-faint">
            <span className="uppercase tracking-wider">{match.source}</span>
            {match.is_favorite ? (
              <Star className="h-2.5 w-2.5 fill-current text-score-mid" />
            ) : null}
          </div>
        </div>
        {match.overall_score != null ? (
          <span
            className="font-mono text-[14px] font-semibold tabular-nums"
            style={{ color: scoreColor }}
          >
            {match.overall_score}
          </span>
        ) : (
          <span className="font-mono text-[10.5px] text-text-faint">—</span>
        )}
        {!dragging && onMoveStage ? (
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={m.kanban.candidateActions}
              disabled={isMoving}
              onPointerDown={(event) => event.stopPropagation()}
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-text-faint hover:bg-bg-hover hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuGroup>
                <DropdownMenuLabel>{m.kanban.moveToStage}</DropdownMenuLabel>
                {KANBAN_STAGES.filter((stage) => stage !== match.stage).map(
                  (stage) => (
                    <DropdownMenuItem
                      key={stage}
                      onClick={() => onMoveStage(match, stage)}
                    >
                      <MoveRight className="h-3.5 w-3.5" />
                      {m.kanban.stages[stage]}
                    </DropdownMenuItem>
                  ),
                )}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
      {match.overall_score != null ? (
        <div className="mt-2 grid grid-cols-4 gap-1">
          <CriterionBar value={match.score_technical_skills} />
          <CriterionBar value={match.score_quality_of_projects} />
          <CriterionBar value={match.score_previous_experience} />
          <CriterionBar value={match.score_impact} />
        </div>
      ) : null}
    </div>
  );
}

function CriterionBar({ value }: { value: number | null }) {
  const tier = scoreTier(value);
  const color = tier != null ? SCORE_VAR[tier] : "var(--border)";
  return (
    <div className="h-1 overflow-hidden rounded-full bg-border">
      <div
        className="h-full rounded-full"
        style={{ width: `${value ?? 0}%`, backgroundColor: color }}
      />
    </div>
  );
}

// Visual dot per column header — colour follows the role of the stage in the
// pipeline rather than introducing a new palette.
const STAGE_DOT: Record<KanbanStage, string> = {
  new: "bg-text-faint",
  screening: "bg-tag-tech",
  interview: "bg-tag-seniority",
  offer: "bg-tag-salary",
  hired: "bg-score-high",
  rejected: "bg-score-low",
};
