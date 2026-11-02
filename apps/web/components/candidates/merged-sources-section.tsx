"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  Check,
  FileText,
  GitBranch,
  Loader2,
  RefreshCw,
  RotateCcw,
  UserRoundCheck,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { Collapsible } from "@/components/ui/collapsible";
import { ScoreChip } from "@/components/ui/score-chip";
import { SourceBadge } from "@/components/ui/source-badge";
import { reverseCandidateMergeAction } from "@/lib/actions";
import { candDisplayRef } from "@/lib/format";
import type {
  CandidateMergeHistoryItem,
  CandidateMergedSource,
} from "@/lib/types";

export function MergedSourcesSection({
  sources,
  history = [],
  loadFailed = false,
}: {
  sources: CandidateMergedSource[];
  history?: CandidateMergeHistoryItem[];
  loadFailed?: boolean;
}) {
  const { locale, m } = useI18n();
  const router = useRouter();
  const [reversingBatchId, setReversingBatchId] = useState<string | null>(null);
  const aliases = sources.filter((source) => !source.is_primary);
  if (!loadFailed && aliases.length === 0 && history.length === 0) return null;

  const dateFormatter = new Intl.DateTimeFormat(
    locale === "ru" ? "ru-RU" : "en-US",
    { dateStyle: "medium" },
  );
  const formatDate = (value: string) => dateFormatter.format(new Date(value));

  async function reverseMerge(batch: CandidateMergeHistoryItem) {
    if (!batch.can_reverse || reversingBatchId) return;
    if (!window.confirm(m.candidate.mergedSources.confirmUndo)) return;
    setReversingBatchId(batch.id);
    try {
      const result = await reverseCandidateMergeAction(
        batch.id,
        batch.version,
        batch.primary_candidate_id,
      );
      if (!result.ok) {
        toast.error(m.candidate.mergedSources.undoFailed(result.error));
        return;
      }
      toast.success(m.candidate.mergedSources.undoSuccess);
      router.refresh();
    } finally {
      setReversingBatchId(null);
    }
  }

  if (loadFailed) {
    return (
      <section className="rounded-lg border border-score-low/30 bg-score-low/5 px-4 py-3">
        <h2 className="text-[12px] font-medium text-text">
          {m.candidate.mergedSources.title}
        </h2>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <p role="alert" className="text-[11.5px] text-score-low">
            {m.candidate.mergedSources.loadFailed}
          </p>
          <button
            type="button"
            onClick={() => router.refresh()}
            className="inline-flex items-center gap-1.5 rounded-[6px] border border-score-low/30 bg-bg-elevated px-2.5 py-1.5 text-[11px] font-medium text-score-low transition-colors hover:bg-score-low/10"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            {m.candidate.mergedSources.retry}
          </button>
        </div>
      </section>
    );
  }

  return (
    <Collapsible
      title={m.candidate.mergedSources.title}
      meta={m.candidate.mergedSources.summary(aliases.length)}
    >
      <div className="space-y-2 pt-3">
        {history.length > 0 ? (
          <section
            aria-labelledby="candidate-merge-history-heading"
            className="mb-3 rounded-lg border border-border bg-bg p-3"
          >
            <h3
              id="candidate-merge-history-heading"
              className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint"
            >
              {m.candidate.mergedSources.historyTitle}
            </h3>
            <ul className="mt-2 space-y-2">
              {history.map((batch) => {
                const reversing = reversingBatchId === batch.id;
                return (
                  <li
                    key={batch.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-[6px] border border-border bg-bg-elevated px-2.5 py-2"
                  >
                    <div>
                      <p className="text-[10.5px] text-text-muted">
                        {m.candidate.mergedSources.historySummary(
                          batch.member_count,
                          formatDate(batch.created_at),
                        )}
                      </p>
                      <p className="mt-0.5 font-mono text-[9px] text-text-faint">
                        {batch.id.slice(0, 8)} · {batch.status === "reversed"
                          ? m.candidate.mergedSources.reversed
                          : m.candidate.mergedSources.reversible}
                      </p>
                    </div>
                    {batch.can_reverse ? (
                      <button
                        type="button"
                        disabled={reversingBatchId !== null}
                        onClick={() => void reverseMerge(batch)}
                        className="inline-flex min-h-9 items-center gap-1.5 rounded-[6px] border border-border bg-bg px-2.5 py-1.5 text-[10.5px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {reversing ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <RotateCcw className="h-3.5 w-3.5" />
                        )}
                        {reversing
                          ? m.candidate.mergedSources.undoing
                          : m.candidate.mergedSources.undo}
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}
        {sources.map((source) => {
          return (
            <article
              key={source.id}
              className="rounded-lg border border-border bg-bg-elevated p-3"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <SourceBadge source={source.source} />
                    <span className="max-w-[280px] truncate font-mono text-[11.5px] font-medium text-text">
                      {candDisplayRef(
                        source.id,
                        source.source === "identity_review"
                          ? null
                          : source.source_ref,
                      )}
                    </span>
                    <span className="rounded-full border border-border bg-bg px-2 py-0.5 text-[9.5px] text-text-faint">
                      {source.is_primary
                        ? m.candidate.mergedSources.primary
                        : m.candidate.mergedSources.alias}
                    </span>
                  </div>
                  <p className="mt-1.5 text-[10px] text-text-faint">
                    {source.merged_at
                      ? m.candidate.mergedSources.mergedAt(
                          formatDate(source.merged_at),
                        )
                      : m.candidate.mergedSources.addedAt(
                          formatDate(source.created_at),
                        )}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-text-muted">
                  {source.has_cv ? (
                    <span className="inline-flex items-center gap-1 rounded-[5px] border border-border bg-bg px-1.5 py-1">
                      <FileText className="h-3 w-3 text-text-faint" />
                      {m.candidate.mergedSources.cvStored}
                    </span>
                  ) : null}
                  {source.has_contact ? (
                    <span className="inline-flex items-center gap-1 rounded-[5px] border border-border bg-bg px-1.5 py-1">
                      <UserRoundCheck className="h-3 w-3 text-text-faint" />
                      {m.candidate.mergedSources.contactStored}
                    </span>
                  ) : null}
                  {source.github_login ? (
                    <a
                      href={`https://github.com/${source.github_login}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 rounded-[5px] border border-border bg-bg px-1.5 py-1 hover:bg-bg-hover hover:text-text"
                    >
                      <GitBranch className="h-3 w-3 text-text-faint" />
                      @{source.github_login}
                    </a>
                  ) : null}
                </div>
              </div>

              {source.scores.length > 0 ? (
                <div className="mt-3 border-t border-border pt-2.5">
                  <p className="mb-1.5 text-[9.5px] font-medium uppercase tracking-[0.07em] text-text-faint">
                    {m.candidate.mergedSources.preservedScores(
                      source.scores.length,
                    )}
                  </p>
                  <div className="grid gap-1.5 md:grid-cols-2">
                    {source.scores.map((score) => (
                      <Link
                        key={score.id}
                        href={`/positions/${score.position_id}`}
                        className="flex min-w-0 items-center gap-2 rounded-[6px] border border-border bg-bg px-2.5 py-2 transition-colors hover:bg-bg-hover"
                      >
                        <ScoreChip score={score.overall_score} size="sm" />
                        <span className="min-w-0 flex-1 truncate text-[10.5px] text-text-muted">
                          {score.position_title}
                        </span>
                        <span className="rounded-[4px] bg-bg-active px-1.5 py-0.5 text-[9px] text-text-faint">
                          {m.kanban.stages[score.stage]}
                        </span>
                        {score.selected ? (
                          <span
                            className="inline-flex text-score-high"
                            title={m.candidate.mergedSources.selectedScore}
                          >
                            <Check className="h-3.5 w-3.5" />
                          </span>
                        ) : null}
                      </Link>
                    ))}
                  </div>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </Collapsible>
  );
}
