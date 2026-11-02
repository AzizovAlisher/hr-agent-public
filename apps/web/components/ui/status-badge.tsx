import { cn } from "@/lib/utils";

const TONE: Record<string, { color: string; bg: string; glow?: boolean }> = {
  open: { color: "var(--accent)", bg: "var(--accent-muted)", glow: true },
  paused: { color: "oklch(0.78 0.14 75)", bg: "oklch(0.78 0.14 75 / 0.12)" },
  closed: { color: "var(--text-faint)", bg: "var(--bg-active)" },
  qualified: { color: "var(--accent)", bg: "var(--accent-muted)", glow: true },
};

/** @ui-only Status-badge presentation properties. */
type StatusBadgeProps = {
  status: string;
  /** Optional display label override; defaults to `status`. */
  label?: string;
  className?: string;
};

export function StatusBadge({ status, label, className }: StatusBadgeProps) {
  const tone = TONE[status] ?? TONE.closed;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[4px] px-1.5 py-[3px] font-mono text-[10px] font-medium tracking-wider lowercase",
        className,
      )}
      style={{ color: tone.color, background: tone.bg }}
    >
      <span
        className={cn(
          "h-[5px] w-[5px] rounded-full",
          tone.glow && "pulse-soft",
        )}
        style={{
          background: tone.color,
          boxShadow: tone.glow ? `0 0 6px ${tone.color}` : undefined,
        }}
      />
      {label ?? status}
    </span>
  );
}
