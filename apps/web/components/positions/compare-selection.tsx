"use client";

import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import { GitCompare, X } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

const MAX_COMPARE = 4;
const MIN_COMPARE = 2;

export function useCompareSelection() {
  const [selected, setSelected] = useState<string[]>([]);

  const toggle = useCallback((id: string) => {
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= MAX_COMPARE) return prev;
      return [...prev, id];
    });
  }, []);

  const isSelected = useCallback(
    (id: string) => selected.includes(id),
    [selected],
  );

  const isFull = selected.length >= MAX_COMPARE;

  const clear = useCallback(() => setSelected([]), []);

  return { selected, toggle, isSelected, isFull, clear };
}

export function CompareCheckbox({
  checked,
  disabled,
  onChange,
  ariaLabel,
}: {
  checked: boolean;
  disabled: boolean;
  onChange: () => void;
  ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled && !checked}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        onChange();
      }}
      className={cn(
        "flex h-4 w-4 shrink-0 items-center justify-center rounded-[3px] border transition-colors",
        checked
          ? "border-text bg-text text-bg"
          : "border-border bg-bg-elevated text-transparent hover:border-border-strong",
        disabled && !checked && "cursor-not-allowed opacity-30",
      )}
    >
      <svg
        viewBox="0 0 12 12"
        className="h-2.5 w-2.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M2 6.5l2.5 2.5L10 3" />
      </svg>
    </button>
  );
}

export function CompareBar({
  positionId,
  selected,
  onClear,
}: {
  positionId: string;
  selected: string[];
  onClear: () => void;
}) {
  const router = useRouter();
  const { m } = useI18n();
  const t = m.compare;

  const visible = selected.length > 0;
  const ready = selected.length >= MIN_COMPARE;

  const message = useMemo(() => {
    if (!ready) return t.selectMore;
    return t.selectionCount(selected.length);
  }, [ready, selected.length, t]);

  if (!visible) return null;

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-30 flex justify-center px-4">
      <div className="pointer-events-auto flex items-center gap-2 rounded-full border border-border bg-bg-elevated px-2.5 py-2 shadow-2xl ring-1 ring-foreground/5">
        <span className="flex items-center gap-2 px-2.5 text-[12.5px] text-text-muted">
          <GitCompare className="h-3.5 w-3.5 text-text-faint" />
          {message}
        </span>
        <button
          type="button"
          onClick={onClear}
          className="rounded-full px-2.5 py-1 text-[11.5px] text-text-muted transition-colors hover:bg-bg-hover hover:text-text"
        >
          <X className="-mt-px inline h-3 w-3" /> {t.clearSelection}
        </button>
        <button
          type="button"
          disabled={!ready}
          onClick={() => {
            const params = new URLSearchParams();
            params.set("ids", selected.join(","));
            router.push(`/positions/${positionId}/compare?${params.toString()}`);
          }}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full bg-text px-3 py-1.5 text-[12px] font-medium text-bg transition-opacity",
            ready ? "hover:opacity-90" : "opacity-40",
          )}
        >
          {t.compareNow}
          <span className="font-mono text-[10.5px] opacity-70">
            ({selected.length})
          </span>
        </button>
      </div>
    </div>
  );
}

export const COMPARE_LIMITS = {
  min: MIN_COMPARE,
  max: MAX_COMPARE,
};
