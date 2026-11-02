"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  CheckCircle2,
  CirclePlus,
  Menu,
  Settings2,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useState, type ReactNode } from "react";

import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { SignOutLink } from "@/components/app/sign-out-link";
import { ThemeToggle } from "@/components/app/theme-toggle";
import { ProductSwitcher } from "@/components/ai/product-switcher";
import { useI18n } from "@/components/i18n/locale-provider";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { getAiCopy } from "@/lib/ai/copy";
import { cn } from "@/lib/utils";

/** @ui-only AI workspace shell properties assembled by the app layout. */
type AiShellProps = {
  children: ReactNode;
  user: { email: string; role: string; initials: string };
  organizationName: string;
  access: { canUse: boolean; canManage: boolean };
};

export function AiShell({ children, user, organizationName, access }: AiShellProps) {
  const pathname = usePathname();
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const [mobileOpen, setMobileOpen] = useState(false);

  const nav = [
    ...(access.canUse
      ? [
          { href: "/ai/new", label: copy.newMission, icon: CirclePlus },
          { href: "/ai/active", label: copy.active, icon: Sparkles },
          { href: "/ai/completed", label: copy.completed, icon: CheckCircle2 },
          { href: "/ai/approvals", label: copy.approvals, icon: ShieldCheck },
        ]
      : []),
    ...(access.canManage
      ? [{ href: "/ai/settings", label: copy.settings, icon: Settings2 }]
      : []),
  ];

  const sidebar = (
    <div className="flex h-full w-[248px] flex-col bg-background px-3 py-4">
      <div className="flex items-center gap-2 px-2 pb-4">
        <span className="relative flex h-7 w-7 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-[0_0_24px_var(--accent-muted)]">
          <Bot className="h-4 w-4" />
          <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full border-2 border-background bg-score-high" />
        </span>
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold tracking-tight">{copy.product}</div>
          <div className="truncate text-[10px] text-text-faint">{organizationName}</div>
        </div>
      </div>

      <ProductSwitcher current="ai" className="mb-4" />

      <div className="px-2 pb-1.5 text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
        {copy.navigation}
      </div>
      <nav className="flex flex-col gap-1">
        {nav.map(({ href, label, icon: Icon }) => {
          const active =
            pathname === href ||
            (href === "/ai/active" &&
              (pathname.startsWith("/ai/missions/") ||
                pathname.startsWith("/ai/sessions/")));
          return (
            <Link
              key={href}
              href={href}
              onClick={() => setMobileOpen(false)}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] font-medium transition-colors",
                active
                  ? "bg-accent-muted text-primary"
                  : "text-text-muted hover:bg-bg-hover hover:text-text",
              )}
            >
              <Icon className="h-4 w-4" />
              <span>{label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="mt-auto border-t border-border pt-3">
        <div className="mb-2 flex items-center justify-between px-2">
          <LocaleSwitcher />
          <ThemeToggle />
        </div>
        <div className="flex items-center gap-2.5 rounded-lg px-2 py-2">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-primary to-[oklch(0.55_0.18_290)] text-[10px] font-semibold text-white">
            {user.initials}
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[11px] font-medium">{user.email}</div>
            <div className="truncate font-mono text-[9px] uppercase tracking-[0.05em] text-text-faint">
              {user.role}
            </div>
          </div>
        </div>
        <SignOutLink />
      </div>
    </div>
  );

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      <aside className="hidden h-screen shrink-0 border-r border-border md:block">{sidebar}</aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex h-[53px] shrink-0 items-center justify-between border-b border-border bg-background px-4 md:hidden">
          <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
            <SheetTrigger
              render={
                <button
                  type="button"
                  aria-label={copy.navigation}
                  aria-expanded={mobileOpen}
                  className="flex h-8 w-8 items-center justify-center rounded-lg text-text-muted hover:bg-bg-hover"
                />
              }
            >
              <Menu className="h-4 w-4" />
            </SheetTrigger>
            <SheetContent
              side="left"
              closeLabel={locale === "ru" ? "Закрыть навигацию" : "Close navigation"}
              className="gap-0 p-0 data-[side=left]:w-[280px]"
            >
              <SheetTitle className="sr-only">{copy.navigation}</SheetTitle>
              <SheetDescription className="sr-only">
                {copy.product}
              </SheetDescription>
              {sidebar}
            </SheetContent>
          </Sheet>
          <div className="flex items-center gap-2 text-[13px] font-semibold">
            <Bot className="h-4 w-4 text-primary" />
            {copy.aiProduct}
          </div>
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-muted text-[10px] font-semibold text-primary">
            {user.initials}
          </span>
        </header>
        {children}
      </main>
    </div>
  );
}
