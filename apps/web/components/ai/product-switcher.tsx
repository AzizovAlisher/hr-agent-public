"use client";

import Link from "next/link";
import { Bot, ChevronRight, Users } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { getAiCopy } from "@/lib/ai/copy";
import { cn } from "@/lib/utils";

export function ProductSwitcher({
  current,
  className,
}: {
  current: "human" | "ai";
  className?: string;
}) {
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const isAi = current === "ai";
  const CurrentIcon = isAi ? Bot : Users;

  return (
    <Link
      href={isAi ? "/my-work" : "/ai/new"}
      title={isAi ? copy.backToHuman : copy.aiProduct}
      aria-label={isAi ? copy.backToHuman : copy.aiProduct}
      className={cn(
        "group flex h-10 w-full min-w-[198px] items-center gap-2.5 rounded-lg border border-border bg-bg-elevated px-2 text-left transition-colors hover:border-accent-border hover:bg-bg-hover",
        className,
      )}
    >
      <span
        className={cn(
          "flex h-6 w-6 shrink-0 items-center justify-center rounded-md",
          isAi ? "bg-accent-muted text-primary" : "bg-bg-active text-text-muted",
        )}
      >
        <CurrentIcon className="h-3.5 w-3.5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[9px] font-medium uppercase tracking-[0.08em] text-text-faint">
          {copy.productSwitch}
        </span>
        <span className="block truncate text-[12px] font-medium text-text">
          {isAi ? copy.aiProduct : copy.humanProduct}
        </span>
      </span>
      <ChevronRight className="h-3.5 w-3.5 shrink-0 text-text-faint transition-transform group-hover:translate-x-0.5" />
    </Link>
  );
}

