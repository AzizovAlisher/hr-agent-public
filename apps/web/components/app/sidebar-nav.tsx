"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** @ui-only Permission-filtered sidebar navigation item. */
export type NavItem = {
  label: string;
  href: string;
  icon: ReactNode;
  count?: number | string | null;
  disabled?: boolean;
  hint?: string;
  match?: string | string[]; // matches if pathname starts with any value; defaults to href
};

export function SidebarNav({ items }: { items: NavItem[] }) {
  const pathname = usePathname();

  return (
    <nav className="flex flex-col gap-px">
      {items.map((item) => {
        const matchKeys = Array.isArray(item.match)
          ? item.match
          : [item.match ?? item.href];
        const isActive =
          !item.disabled &&
          matchKeys.some(
            (matchKey) =>
              pathname === matchKey || pathname.startsWith(`${matchKey}/`),
          );

        const className = cn(
          "group flex items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-[13px] font-medium transition-colors",
          item.disabled
            ? "cursor-default text-text-faint"
            : isActive
              ? "bg-bg-active text-text"
              : "text-text-muted hover:bg-bg-hover hover:text-text",
        );

        const inner = (
          <>
            <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center opacity-85">
              {item.icon}
            </span>
            <span className="flex-1 truncate">{item.label}</span>
            {item.count != null ? (
              <span className="font-mono text-[11px] text-text-faint tabular-nums">
                {item.count}
              </span>
            ) : item.hint ? (
              <span className="text-[10px] uppercase tracking-[0.06em] text-text-faint">
                {item.hint}
              </span>
            ) : null}
          </>
        );

        if (item.disabled) {
          return (
            <span key={item.label} aria-disabled className={className}>
              {inner}
            </span>
          );
        }
        return (
          <Link
            key={item.label}
            href={item.href}
            aria-current={isActive ? "page" : undefined}
            className={className}
          >
            {inner}
          </Link>
        );
      })}
    </nav>
  );
}
