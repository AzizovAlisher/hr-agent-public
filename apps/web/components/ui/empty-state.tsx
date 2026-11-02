import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** @ui-only Empty-state presentation properties. */
type EmptyStateProps = {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
};

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "reveal relative isolate flex flex-col items-center gap-3 overflow-hidden rounded-lg border border-border bg-bg-elevated px-6 py-14 text-center",
        className,
      )}
    >
      {/* Texture: dot grid + a faint radial fade so the centre stays clean. */}
      <span
        aria-hidden
        className="dot-grid pointer-events-none absolute inset-0 -z-10 text-text-faint opacity-[0.18]"
        style={{
          maskImage:
            "radial-gradient(circle at center, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.4) 55%, transparent 85%)",
          WebkitMaskImage:
            "radial-gradient(circle at center, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.4) 55%, transparent 85%)",
        }}
      />
      {icon ? (
        <div className="relative mb-1 flex h-10 w-10 items-center justify-center rounded-[10px] border border-border bg-bg text-text-muted shadow-[inset_0_1px_0_0_var(--bg-elevated)]">
          {icon}
          <span
            aria-hidden
            className="pointer-events-none absolute inset-0 rounded-[10px] ring-1 ring-inset ring-accent-border opacity-50"
          />
        </div>
      ) : null}
      <div className="space-y-1">
        <h3 className="text-[13.5px] font-medium tracking-tight">{title}</h3>
        {description ? (
          <p className="mx-auto max-w-sm text-[12px] leading-[1.6] text-text-muted">
            {description}
          </p>
        ) : null}
      </div>
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}
