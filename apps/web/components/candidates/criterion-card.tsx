import { SCORE_VAR, scoreTier } from "@/lib/score";
import { ScoreBar } from "@/components/ui/score-bar";

/** @ui-only Criterion-card presentation properties. */
type CriterionCardProps = {
  label: string;
  score: number | null;
  comment: string | null;
  /** Localized fallback when there's no comment (server component — passed in). */
  emptyComment?: string;
  delayMs?: number;
};

export function CriterionCard({
  label,
  score,
  comment,
  emptyComment = "No comment",
  delayMs = 0,
}: CriterionCardProps) {
  const tier = scoreTier(score);
  return (
    <div className="h-full rounded-lg border border-border bg-bg-elevated p-5 transition-colors hover:border-border-strong">
      <div className="mb-3.5 flex items-baseline justify-between">
        <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-text">
          {label}
        </span>
        <span
          className="font-mono text-[24px] font-semibold leading-none tracking-[-0.02em] tabular-nums"
          style={{ color: tier ? SCORE_VAR[tier] : "var(--text-faint)" }}
        >
          {score ?? "—"}
        </span>
      </div>
      <ScoreBar score={score} height={3} delayMs={delayMs} className="mb-3.5" />
      {comment ? (
        <p className="text-[12.5px] leading-[1.65] text-text">{comment}</p>
      ) : (
        <p className="text-[12.5px] text-text-faint">{emptyComment}</p>
      )}
    </div>
  );
}
