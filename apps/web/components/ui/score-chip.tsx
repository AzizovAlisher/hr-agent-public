import { SCORE_TINT_BG, SCORE_TINT_BORDER, SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";

/** @ui-only Score-chip presentation properties. */
type ScoreChipProps = {
  score: number | null;
  size?: "sm" | "md";
  className?: string;
};

export function ScoreChip({ score, size = "md", className }: ScoreChipProps) {
  const tier = scoreTier(score);
  const color = tier ? SCORE_VAR[tier] : "var(--text-faint)";
  const bg = tier ? SCORE_TINT_BG[tier] : "var(--bg-active)";
  const border = tier ? SCORE_TINT_BORDER[tier] : "var(--border)";

  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border font-mono font-semibold tabular-nums transition-all duration-200 ease-out",
        size === "sm"
          ? "px-2 py-[2px] text-[11px]"
          : "px-2.5 py-1 text-[13px]",
        "group-hover:[box-shadow:0_0_0_3px_var(--accent-muted)]",
        className,
      )}
      style={{ color: "var(--text)", background: bg, borderColor: border }}
    >
      <span
        className="relative h-1.5 w-1.5 rounded-full"
        style={{
          background: color,
          boxShadow: tier === "high" ? `0 0 6px ${color}` : undefined,
        }}
      />
      {score ?? "—"}
    </span>
  );
}
