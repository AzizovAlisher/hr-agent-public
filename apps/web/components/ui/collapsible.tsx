"use client";

import { useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

/** @ui-only Collapsible presentation properties. */
type CollapsibleProps = {
  title: string;
  meta?: string;
  defaultOpen?: boolean;
  children: ReactNode;
  bodyClassName?: string;
};

export function Collapsible({
  title,
  meta,
  defaultOpen = false,
  children,
  bodyClassName,
}: CollapsibleProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-bg-elevated">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-bg-hover"
        aria-expanded={open}
      >
        <span className="flex items-center gap-2.5">
          <ChevronRight
            className={cn(
              "h-3 w-3 text-text-muted transition-transform duration-200",
              open && "rotate-90",
            )}
          />
          <span className="text-[13px] font-medium">{title}</span>
        </span>
        {meta ? (
          <span className="font-mono text-[10px] tracking-[0.04em] text-text-faint">
            {meta}
          </span>
        ) : null}
      </button>
      {open ? (
        <div
          className={cn(
            "border-t border-border bg-bg px-4 pb-4 pt-1",
            bodyClassName,
          )}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}
