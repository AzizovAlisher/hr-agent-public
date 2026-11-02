"use client";

import { useState, useTransition } from "react";
import { Heart } from "lucide-react";
import { toast } from "sonner";

import { setFavoriteAction } from "@/lib/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

/** @ui-only Favorite-button presentation properties. */
type FavoriteButtonProps = {
  candidateId: string;
  initial: boolean;
  size?: "sm" | "md";
  className?: string;
  /** Path to revalidate after toggle, e.g. the parent position. */
  parentPath?: string;
  onChange?: (next: boolean) => void;
};

export function FavoriteButton({
  candidateId,
  initial,
  size = "sm",
  className,
  parentPath,
  onChange,
}: FavoriteButtonProps) {
  const { m } = useI18n();
  // Optimistic local state — keeps UX snappy even before revalidate lands.
  const [isFav, setIsFav] = useState(initial);
  const [pending, start] = useTransition();

  const handle = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    const next = !isFav;
    setIsFav(next);
    onChange?.(next);
    start(async () => {
      const res = await setFavoriteAction(candidateId, next, parentPath);
      if (!res.ok) {
        // Revert on failure + tell the user (every other mutation here does).
        setIsFav(!next);
        onChange?.(!next);
        toast.error(m.candidate.favoriteFailed);
      }
    });
  };

  const dim = size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4";
  return (
    <button
      type="button"
      onClick={handle}
      disabled={pending}
      aria-pressed={isFav}
      aria-label={isFav ? m.candidate.unfavorite : m.candidate.favorite}
      title={isFav ? m.candidate.unfavorite : m.candidate.favorite}
      className={cn(
        "flex shrink-0 items-center justify-center rounded-[6px] p-1 transition-colors",
        size === "sm" ? "h-6 w-6" : "h-7 w-7",
        isFav
          ? "text-primary hover:bg-bg-hover"
          : "text-text-faint hover:bg-bg-hover hover:text-text-muted",
        className,
      )}
    >
      <Heart
        className={cn(dim, "transition-all")}
        strokeWidth={1.6}
        fill={isFav ? "currentColor" : "none"}
      />
    </button>
  );
}
