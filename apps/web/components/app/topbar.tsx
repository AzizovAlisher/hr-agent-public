"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { CommandPaletteTrigger } from "@/components/app/command-palette-trigger";
import { NotificationsBell } from "@/components/app/notifications-bell";
import { cn } from "@/lib/utils";
import { useI18n } from "@/components/i18n/locale-provider";

/** @ui-only Topbar breadcrumb presentation item. */
export type Crumb = {
  label: string;
  href?: string;
  mono?: boolean;
};

/** @ui-only Topbar composition properties. */
type TopbarProps = {
  crumbs: Crumb[];
  actions?: ReactNode;
};

export function Topbar({ crumbs, actions }: TopbarProps) {
  const { m } = useI18n();
  return (
    <header className="sticky top-0 z-10 flex h-[53px] items-center justify-between gap-2 border-b border-border bg-background py-0 pl-14 pr-3 sm:gap-4 sm:pr-5 md:px-7">
      <nav
        aria-label={m.common.breadcrumb}
        className="flex min-w-0 items-center gap-2 text-[13px] text-text-muted"
      >
        {crumbs.map((c, i) => {
          const isLast = i === crumbs.length - 1;
          const cls = cn(
            "truncate transition-colors",
            isLast ? "text-text font-medium" : "hover:text-text",
            c.mono && "font-mono text-[12px]",
          );
          return (
            <span key={c.label + i} className="flex min-w-0 items-center gap-2">
              {i > 0 ? (
                <span className="font-mono text-[11px] text-text-faint">/</span>
              ) : null}
              {c.href && !isLast ? (
                <Link href={c.href} className={cls}>
                  {c.label}
                </Link>
              ) : (
                <span className={cls} aria-current={isLast ? "page" : undefined}>
                  {c.label}
                </span>
              )}
            </span>
          );
        })}
      </nav>
      <div className="flex shrink-0 items-center gap-1 sm:gap-2">
        {actions}
        <CommandPaletteTrigger />
        <NotificationsBell />
      </div>
    </header>
  );
}
