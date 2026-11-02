import { cn } from "@/lib/utils";

const ABBREV: Record<string, string> = {
  hh: "HH",
  avito: "AVT",
  manual: "MAN",
  other: "OTH",
  identity_review: "REV",
};

/** @ui-only Source-badge presentation properties. */
type SourceBadgeProps = {
  source: string;
  className?: string;
};

export function SourceBadge({ source, className }: SourceBadgeProps) {
  const label = ABBREV[source.toLowerCase()] ?? source.slice(0, 3).toUpperCase();
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-[4px] border border-border bg-bg-elevated px-1.5 py-[2px] font-mono text-[9px] font-semibold uppercase tracking-[0.06em] text-text-muted",
        className,
      )}
    >
      {label}
    </span>
  );
}
