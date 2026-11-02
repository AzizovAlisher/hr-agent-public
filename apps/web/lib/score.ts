/**
 * Score tier mapping. Aligns with the design language: green ≥80, amber 60–79, red <60.
 */
/** @ui-only Visual score band. */
export type ScoreTier = "high" | "mid" | "low";

export function scoreTier(score: number | null | undefined): ScoreTier | null {
  if (score == null) return null;
  if (score >= 80) return "high";
  if (score >= 60) return "mid";
  return "low";
}

export const SCORE_VAR: Record<ScoreTier, string> = {
  high: "var(--score-high)",
  mid: "var(--score-mid)",
  low: "var(--score-low)",
};

export const SCORE_TINT_BG: Record<ScoreTier, string> = {
  high: "oklch(0.7 0.15 162 / 0.08)",
  mid: "oklch(0.78 0.14 75 / 0.08)",
  low: "oklch(0.68 0.18 25 / 0.08)",
};

export const SCORE_TINT_BORDER: Record<ScoreTier, string> = {
  high: "oklch(0.7 0.15 162 / 0.3)",
  mid: "oklch(0.78 0.14 75 / 0.3)",
  low: "oklch(0.68 0.18 25 / 0.3)",
};

export function recommendation(score: number | null): string {
  if (score == null) return "Awaiting score";
  if (score >= 90) return "Exceptional fit";
  if (score >= 80) return "Strong fit";
  if (score >= 70) return "Good fit";
  if (score >= 60) return "Possible fit";
  return "Low fit";
}
