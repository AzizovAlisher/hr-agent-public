"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  useEffect,
  useRef,
  useState,
  type FocusEvent,
  type ReactNode,
} from "react";
import { Menu, Pin, PinOff } from "lucide-react";

import type { NavItem } from "@/components/app/sidebar-nav";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";

const PIN_KEY = "hr.sidebar.pinned";

/** @ui-only Responsive sidebar shell properties. */
export type SidebarShellProps = {
  brand: { name: string; version: string };
  workspaceLabel: string;
  workspace: NavItem[];
  settingsLabel: string;
  settings: NavItem[];
  signOut: ReactNode;
  themeLabel: string;
  themeToggle: ReactNode;
  user: { email: string; role: string; initials: string };
  pinLabel: string;
  unpinLabel: string;
  menuLabel: string;
  closeMenuLabel: string;
  productSwitcher?: ReactNode;
};

export function SidebarShell(props: SidebarShellProps) {
  const [pinned, setPinned] = useState(false);
  const [hovering, setHovering] = useState(false);
  const [focused, setFocused] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeTimer = useRef<number | null>(null);

  useEffect(() => {
    try {
      setPinned(window.localStorage.getItem(PIN_KEY) === "1");
    } catch {
      /* Persistence is optional. */
    }
    return () => {
      if (closeTimer.current) window.clearTimeout(closeTimer.current);
    };
  }, []);

  function togglePin() {
    setPinned((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(PIN_KEY, next ? "1" : "0");
      } catch {
        /* Persistence is optional. */
      }
      return next;
    });
  }

  function onEnter() {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
    setHovering(true);
  }

  function onLeave() {
    closeTimer.current = window.setTimeout(() => setHovering(false), 120);
  }

  function onBlur(event: FocusEvent<HTMLElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setFocused(false);
    }
  }

  const expanded = pinned || hovering || focused;
  const labelClass = expanded
    ? "opacity-100 transition-opacity duration-200 delay-150 ease-out"
    : "opacity-0 transition-opacity duration-100 ease-out";

  return (
    <>
      <aside
        aria-label={props.menuLabel}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        onFocusCapture={() => setFocused(true)}
        onBlurCapture={onBlur}
        className={cn(
          "relative hidden h-screen shrink-0 overflow-hidden border-r border-border bg-background transition-[width] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] md:block",
          expanded ? "w-[224px]" : "w-14",
        )}
      >
        <div className="h-full w-[224px]">
          <SidebarContents
            {...props}
            labelClass={labelClass}
            pinned={pinned}
            showPin
            onTogglePin={togglePin}
          />
        </div>
      </aside>

      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetTrigger
          render={
            <button
              type="button"
              aria-label={props.menuLabel}
              aria-expanded={mobileOpen}
              className="fixed left-3 top-2 z-40 flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-bg-elevated text-text shadow-sm transition-colors hover:bg-bg-hover md:hidden"
            />
          }
        >
          <Menu className="h-4 w-4" />
        </SheetTrigger>
        <SheetContent
          side="left"
          closeLabel={props.closeMenuLabel}
          className="gap-0 p-0 data-[side=left]:w-[min(88vw,320px)]"
        >
          <SheetTitle className="sr-only">{props.menuLabel}</SheetTitle>
          <SheetDescription className="sr-only">
            {props.workspaceLabel} · {props.settingsLabel}
          </SheetDescription>
          <SidebarContents
            {...props}
            labelClass="opacity-100"
            pinned={false}
            showPin={false}
            onTogglePin={() => undefined}
            onNavigate={() => setMobileOpen(false)}
          />
        </SheetContent>
      </Sheet>
    </>
  );
}

function SidebarContents({
  brand,
  workspaceLabel,
  workspace,
  settingsLabel,
  settings,
  signOut,
  themeLabel,
  themeToggle,
  user,
  pinLabel,
  unpinLabel,
  productSwitcher,
  labelClass,
  pinned,
  showPin,
  onTogglePin,
  onNavigate,
}: SidebarShellProps & {
  labelClass: string;
  pinned: boolean;
  showPin: boolean;
  onTogglePin: () => void;
  onNavigate?: () => void;
}) {
  return (
    <div className="flex h-full w-full flex-col overflow-y-auto px-3 py-4">
      <div className="mb-2 flex items-center gap-2 px-2 pb-4">
        <span className="relative flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-md bg-text text-[11px] font-bold tracking-tight text-bg">
          R
          <span className="absolute -right-[3px] -top-[3px] h-1.5 w-1.5 rounded-full bg-primary" />
        </span>
        <span
          className={cn(
            "min-w-0 truncate text-[13px] font-semibold tracking-tight",
            labelClass,
          )}
        >
          {brand.name}
        </span>
        <span
          className={cn(
            "ml-auto rounded bg-bg-hover px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.04em] text-text-faint",
            labelClass,
          )}
        >
          {brand.version}
        </span>
        {showPin ? (
          <button
            type="button"
            onClick={onTogglePin}
            title={pinned ? unpinLabel : pinLabel}
            aria-label={pinned ? unpinLabel : pinLabel}
            aria-pressed={pinned}
            className={cn(
              "flex h-6 w-6 items-center justify-center rounded transition-colors",
              pinned
                ? "bg-accent-muted text-primary"
                : "text-text-faint hover:bg-bg-hover hover:text-text",
              labelClass,
            )}
          >
            {pinned ? <Pin className="h-3 w-3" /> : <PinOff className="h-3 w-3" />}
          </button>
        ) : null}
      </div>

      {productSwitcher ? <div className="mb-1">{productSwitcher}</div> : null}

      <SectionLabel labelClass={labelClass}>{workspaceLabel}</SectionLabel>
      <CollapsibleNav
        ariaLabel={workspaceLabel}
        items={workspace}
        labelClass={labelClass}
        onNavigate={onNavigate}
      />

      <SectionLabel labelClass={labelClass}>{settingsLabel}</SectionLabel>
      <CollapsibleNav
        ariaLabel={settingsLabel}
        items={settings}
        labelClass={labelClass}
        onNavigate={onNavigate}
      />
      <div className={cn("mt-px", labelClass)}>{signOut}</div>

      <div className="mt-auto flex flex-col gap-2 border-t border-border pt-3">
        <div className={cn("flex items-center justify-between px-2", labelClass)}>
          <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
            {themeLabel}
          </span>
          {themeToggle}
        </div>
        <div className="flex items-center gap-2.5 rounded-md px-2 py-1.5">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-[oklch(0.5_0.05_162)] to-[oklch(0.35_0.08_200)] text-[11px] font-semibold text-white">
            {user.initials}
          </span>
          <div className={cn("flex min-w-0 flex-1 flex-col", labelClass)}>
            <span className="truncate text-[12px] font-medium leading-tight">
              {user.email}
            </span>
            <span className="truncate font-mono text-[10px] tracking-[0.04em] text-text-faint">
              {user.role}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SectionLabel({
  children,
  labelClass,
}: {
  children: ReactNode;
  labelClass: string;
}) {
  return (
    <div
      className={cn(
        "px-2 pb-1.5 pt-3 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint",
        labelClass,
      )}
    >
      {children}
    </div>
  );
}

function CollapsibleNav({
  ariaLabel,
  items,
  labelClass,
  onNavigate,
}: {
  ariaLabel: string;
  items: NavItem[];
  labelClass: string;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();

  return (
    <nav aria-label={ariaLabel} className="flex flex-col gap-px">
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
          "group flex min-h-8 items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px] font-medium transition-colors",
          item.disabled
            ? "cursor-not-allowed text-text-faint"
            : isActive
              ? "bg-bg-active text-text"
              : "text-text-muted hover:bg-bg-hover hover:text-text",
        );
        const inner = (
          <>
            <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center opacity-85">
              {item.icon}
            </span>
            <span className={cn("flex-1 truncate", labelClass)}>{item.label}</span>
            {item.count != null ? (
              <span
                className={cn(
                  "font-mono text-[11px] tabular-nums text-text-faint",
                  labelClass,
                )}
              >
                {item.count}
              </span>
            ) : item.hint ? (
              <span
                className={cn(
                  "text-[10px] uppercase tracking-[0.06em] text-text-faint",
                  labelClass,
                )}
              >
                {item.hint}
              </span>
            ) : null}
          </>
        );

        if (item.disabled) {
          return (
            <span key={item.label} aria-disabled="true" className={className}>
              {inner}
            </span>
          );
        }
        return (
          <Link
            key={item.label}
            href={item.href}
            title={item.label}
            aria-current={isActive ? "page" : undefined}
            onClick={onNavigate}
            className={className}
          >
            {inner}
          </Link>
        );
      })}
    </nav>
  );
}
