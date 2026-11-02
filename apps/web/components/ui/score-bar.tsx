import { SCORE_VAR, scoreTier } from "@/lib/score";
import { cn } from "@/lib/utils";

/** @ui-only Score-bar presentation properties. */
type ScoreBarProps = {
  score: number | null;
  height?: number;
  className?: string;
  delayMs?: number;
};

export function ScoreBar({
  score,
  height = 2,
  className,
  delayMs = 0,
}: ScoreBarProps) {
  const tier = scoreTier(score);
  const color = tier ? SCORE_VAR[tier] : "var(--text-faint)";
  const pct =
    score == null ? 0 : Math.max(0, Math.min(100, score)) / 100;

  return (
    <div
      className={cn(
        "w-full overflow-hidden rounded-[2px] bg-border",
        className,
      )}
      style={{ height }}
    >
      <div
        className="bar-fill h-full rounded-[2px]"
        style={{
          width: `${pct * 100}%`,
          background: color,
          animationDelay: delayMs ? `${delayMs}ms` : undefined,
        }}
      />
    </div>
  );
}
