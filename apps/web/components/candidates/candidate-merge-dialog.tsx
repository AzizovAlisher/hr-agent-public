"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import { AlertTriangle, GitMerge, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SourceBadge } from "@/components/ui/source-badge";
import { ScoreChip } from "@/components/ui/score-chip";
import {
  mergeCandidatesAction,
  previewCandidateMergeAction,
} from "@/lib/actions";
import { candDisplayRef } from "@/lib/format";
import type {
  CandidateMergePreview,
  CandidateMergeResult,
  CandidateMergeScoreWinner,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export function CandidateMergeDialog({
  open,
  candidateIds,
  onOpenChange,
  onMerged,
}: {
  open: boolean;
  candidateIds: string[];
  onOpenChange: (open: boolean) => void;
  onMerged: (result: CandidateMergeResult) => void;
}) {
  const { m } = useI18n();
  const [preview, setPreview] = useState<CandidateMergePreview | null>(null);
  const [primaryId, setPrimaryId] = useState("");
  const [scoreWinners, setScoreWinners] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, startLoading] = useTransition();
  const [merging, startMerging] = useTransition();
  const idsKey = useMemo(
    () => [...candidateIds].sort().join(":"),
    [candidateIds],
  );

  useEffect(() => {
    const requestedIds = idsKey.split(":").filter(Boolean);
    if (!open || requestedIds.length < 2) return;
    let current = true;
    setPreview(null);
    setError(null);
    setScoreWinners({});
    startLoading(async () => {
      const result = await previewCandidateMergeAction(requestedIds);
      if (!current) return;
      if (!result.ok) {
        setError(m.candidates.explorer.merge.previewFailed(result.error));
        return;
      }
      setPreview(result.data);
      const oldest = [...result.data.candidates].sort(
        (left, right) =>
          new Date(left.created_at).getTime() -
          new Date(right.created_at).getTime(),
      )[0];
      setPrimaryId(oldest?.id ?? result.data.candidates[0]?.id ?? "");
    });
    return () => {
      current = false;
    };
  }, [idsKey, m.candidates.explorer.merge, open]);

  useEffect(() => {
    if (!preview || !primaryId) return;
    setScoreWinners((current) =>
      Object.fromEntries(
        preview.score_conflicts.map((conflict) => {
          const currentWinner = current[conflict.position_id];
          const winner = conflict.candidate_ids.includes(primaryId)
            ? primaryId
            : currentWinner && conflict.candidate_ids.includes(currentWinner)
              ? currentWinner
              : "";
          return [conflict.position_id, winner];
        }),
      ),
    );
  }, [preview, primaryId]);

  const allScoreWinnersSelected =
    preview?.score_conflicts.every(
      (conflict) =>
        scoreWinners[conflict.position_id] &&
        conflict.candidate_ids.includes(scoreWinners[conflict.position_id]),
    ) ?? false;

  function merge() {
    if (!primaryId || merging || !preview) return;
    const duplicateIds = preview.candidates
      .map((candidate) => candidate.id)
      .filter((candidateId) => candidateId !== primaryId);
    const winners: CandidateMergeScoreWinner[] = preview.score_conflicts.map(
      (conflict) => ({
        position_id: conflict.position_id,
        winner_candidate_id: scoreWinners[conflict.position_id],
      }),
    );
    startMerging(async () => {
      const result = await mergeCandidatesAction(
        primaryId,
        duplicateIds,
        winners,
      );
      if (!result.ok) {
        toast.error(m.candidates.explorer.merge.failed(result.error));
        return;
      }
      toast.success(
        m.candidates.explorer.merge.success(result.data.merged_candidate_ids.length),
      );
      onMerged(result.data);
      onOpenChange(false);
    });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[620px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-[15px]">
            <GitMerge className="h-4 w-4 text-text-muted" />
            {m.candidates.explorer.merge.title}
          </DialogTitle>
          <DialogDescription className="text-[12px] leading-5 text-text-muted">
            {m.candidates.explorer.merge.description}
          </DialogDescription>
        </DialogHeader>

        {loading && !preview ? (
          <div className="flex items-center justify-center gap-2 py-12 text-[12px] text-text-muted">
            <Loader2 className="h-4 w-4 animate-spin" />
            {m.candidates.explorer.loading}
          </div>
        ) : error ? (
          <p className="rounded-lg border border-score-low/30 bg-score-low/5 px-3 py-2.5 text-[12px] text-score-low">
            {error}
          </p>
        ) : preview ? (
          <div className="flex max-h-[56vh] flex-col gap-4 overflow-y-auto pr-1">
            <fieldset>
              <legend className="mb-2 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {m.candidates.explorer.merge.primaryLabel}
              </legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {preview.candidates.map((candidate) => {
                  const selected = candidate.id === primaryId;
                  const label = candDisplayRef(
                    candidate.id,
                    candidate.source === "identity_review"
                      ? null
                      : candidate.source_ref,
                  );
                  return (
                    <label
                      key={candidate.id}
                      className={cn(
                        "cursor-pointer rounded-lg border p-3 transition-colors",
                        selected
                          ? "border-border-strong bg-bg-active ring-1 ring-accent-border"
                          : "border-border bg-bg hover:bg-bg-hover",
                      )}
                    >
                      <div className="flex items-start gap-2.5">
                        <input
                          type="radio"
                          name="primary-candidate"
                          value={candidate.id}
                          checked={selected}
                          onChange={() => setPrimaryId(candidate.id)}
                          className="mt-1 accent-current"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <SourceBadge source={candidate.source} />
                            <span className="truncate font-mono text-[11.5px] font-medium">
                              {label}
                            </span>
                            <ScoreChip score={candidate.best_score} size="sm" />
                          </div>
                          <p className="mt-1 truncate text-[10.5px] text-text-faint">
                            {candidate.best_position_title ?? m.candidates.notScored}
                          </p>
                          {selected ? (
                            <span className="mt-1.5 inline-flex rounded-[4px] bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                              {m.candidates.explorer.merge.keep}
                            </span>
                          ) : null}
                        </div>
                      </div>
                    </label>
                  );
                })}
              </div>
            </fieldset>

            {preview.score_conflicts.length > 0 ? (
              <div className="rounded-lg border border-border bg-bg px-3 py-2.5">
                <div className="flex items-center gap-1.5 text-[11.5px] font-medium">
                  <AlertTriangle className="h-3.5 w-3.5 text-score-mid" />
                  {m.candidates.explorer.merge.scoreConflicts}
                </div>
                <p className="mt-1 text-[10.5px] leading-4 text-text-faint">
                  {m.candidates.explorer.merge.scoreConflictsDesc}
                </p>
                <div className="mt-2 space-y-2">
                  {preview.score_conflicts.map((conflict) => (
                    <label
                      key={conflict.position_id}
                      className="grid gap-1.5 rounded-[6px] border border-border bg-bg-elevated p-2 sm:grid-cols-[1fr_210px] sm:items-center"
                    >
                      <span className="truncate text-[10.5px] text-text-muted">
                        {conflict.position_title}
                      </span>
                      <select
                        value={scoreWinners[conflict.position_id] ?? ""}
                        onChange={(event) =>
                          setScoreWinners((current) => ({
                            ...current,
                            [conflict.position_id]: event.target.value,
                          }))
                        }
                        className="rounded-[5px] border border-border bg-bg px-2 py-1.5 text-[10.5px] text-text outline-none focus:border-border-strong"
                      >
                        <option value="">
                          {m.candidates.explorer.merge.selectScoreWinner}
                        </option>
                        {conflict.candidate_ids.map((candidateId) => {
                          const candidate = preview.candidates.find(
                            (entry) => entry.id === candidateId,
                          );
                          const scoreOption = conflict.options.find(
                            (entry) => entry.candidate_id === candidateId,
                          );
                          const candidateLabel = candidate
                            ? candDisplayRef(
                                candidate.id,
                                candidate.source === "identity_review"
                                  ? null
                                  : candidate.source_ref,
                              )
                            : candidateId.slice(0, 8);
                          return (
                            <option key={candidateId} value={candidateId}>
                              {candidateLabel}
                              {scoreOption
                                ? ` · ${scoreOption.overall_score ?? "—"} · ${m.kanban.stages[scoreOption.stage]}`
                                : ""}
                            </option>
                          );
                        })}
                      </select>
                    </label>
                  ))}
                </div>
              </div>
            ) : null}

            {preview.shared_tags.length > 0 ? (
              <div>
                <div className="mb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                  {m.candidates.explorer.merge.sharedTags}
                </div>
                <div className="flex flex-wrap gap-1">
                  {preview.shared_tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-full border border-border bg-bg px-2 py-0.5 text-[10.5px] text-text-muted"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}

            <p className="rounded-lg border border-border bg-bg px-3 py-2 text-[10.5px] leading-4 text-text-muted">
              {m.candidates.explorer.merge.sourcePreserved}
            </p>
          </div>
        ) : null}

        <DialogFooter className="mt-2 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="rounded-[6px] px-3 py-1.5 text-[12px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
          >
            {m.common.cancel}
          </button>
          <button
            type="button"
            disabled={
              !preview ||
              !primaryId ||
              !allScoreWinnersSelected ||
              merging
            }
            onClick={merge}
            className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {merging ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            {merging
              ? m.candidates.explorer.merge.merging
              : m.candidates.explorer.merge.confirm}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
