"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import {
  BarChart3,
  Briefcase,
  CornerDownLeft,
  Globe,
  ListTodo,
  LogOut,
  MessageSquare,
  Monitor,
  Moon,
  Plus,
  Search,
  Settings,
  Star,
  Sun,
  User as UserIcon,
} from "lucide-react";

import { Dialog as DialogRoot } from "@base-ui/react/dialog";
import { useI18n } from "@/components/i18n/locale-provider";
import { useCommandPaletteEvent } from "@/components/app/command-palette-trigger";
import {
  fetchCommandPaletteData,
  type PaletteData,
} from "@/lib/palette-actions";
import { cn } from "@/lib/utils";
import {
  canUseAiWorkspace,
  getCandidateWorkspaceAccess,
  hasPermission,
  type PermissionMap,
} from "@/lib/permissions";
import { submitLocalLogout } from "@/lib/local-logout";

/** @ui-only Command-palette presentation grouping. */
type ItemKind = "action" | "position" | "candidate";

/** @ui-only Normalized command-palette row. */
type Item = {
  id: string;
  kind: ItemKind;
  group: string;
  title: string;
  subtitle?: string;
  iconKey: string;
  searchTokens: string;
  run: () => void;
};

const ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  briefcase: Briefcase,
  user: UserIcon,
  star: Star,
  plus: Plus,
  sun: Sun,
  moon: Moon,
  monitor: Monitor,
  globe: Globe,
  settings: Settings,
  logout: LogOut,
  message: MessageSquare,
  chart: BarChart3,
  work: ListTodo,
};

const GROUP_ORDER = ["actions", "positions", "candidates"] as const;

export function CommandPalette({ permissions }: { permissions: PermissionMap }) {
  const router = useRouter();
  const { m, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const [data, setData] = useState<PaletteData | null>(null);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // ⌘K / Ctrl+K toggles palette
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Topbar trigger button dispatches a custom event we listen to here
  useCommandPaletteEvent(useCallback(() => setOpen((v) => !v), []));

  // Lazy fetch on open; refresh each open so newly created data shows up
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    fetchCommandPaletteData()
      .then(setData)
      .finally(() => setLoading(false));
  }, [open]);

  // Reset on open
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIdx(0);
      // focus is requested via autoFocus; nothing else to do.
    }
  }, [open]);

  const close = useCallback(() => setOpen(false), []);

  const t = m.commandPalette;
  const candidateAccess = getCandidateWorkspaceAccess(permissions);
  const canUseAi = canUseAiWorkspace(permissions);
  const canReadConversations =
    hasPermission(permissions, "communications.read") || canUseAi;

  // Build the full item list, then filter by query.
  const items: Item[] = useMemo(() => {
    const out: Item[] = [];

    // Actions
    if (candidateAccess.createPositions) {
      out.push({
        id: "act:new-position",
        kind: "action",
        group: t.groupActions,
        title: t.newPosition,
        iconKey: "plus",
        searchTokens: `${t.newPosition}`,
        run: () => router.push("/positions/new"),
      });
    }
    if (hasPermission(permissions, "account.read")) {
      out.push({
        id: "act:goto-my-work",
        kind: "action",
        group: t.groupActions,
        title: m.app.nav.myWork,
        iconKey: "work",
        searchTokens: `${m.app.nav.myWork} my work мои задачи моя работа`,
        run: () => router.push("/my-work"),
      });
    }
    if (candidateAccess.readCandidates) {
      out.push(
        {
          id: "act:goto-dashboard",
          kind: "action",
          group: t.groupActions,
          title: t.goToDashboard,
          iconKey: "briefcase",
          searchTokens: `${t.goToDashboard}`,
          run: () => router.push("/dashboard"),
        },
        {
          id: "act:goto-candidates",
          kind: "action",
          group: t.groupActions,
          title: t.goToCandidates,
          iconKey: "user",
          searchTokens: `${t.goToCandidates}`,
          run: () => router.push("/candidates"),
        },
        {
          id: "act:goto-favorites",
          kind: "action",
          group: t.groupActions,
          title: t.goToFavorites,
          iconKey: "star",
          searchTokens: `${t.goToFavorites}`,
          run: () => router.push("/candidates?tab=favorites"),
        },
      );
    }
    if (canReadConversations) {
      out.push({
        id: "act:goto-conversations",
        kind: "action",
        group: t.groupActions,
        title: t.goToConversations,
        iconKey: "message",
        searchTokens: `${t.goToConversations}`,
        run: () => router.push("/conversations"),
      });
    }
    if (hasPermission(permissions, "analytics.read")) {
      out.push({
        id: "act:goto-analytics",
        kind: "action",
        group: t.groupActions,
        title: t.goToAnalytics,
        iconKey: "chart",
        searchTokens: `${t.goToAnalytics} analytics аналитика`,
        run: () => router.push("/analytics"),
      });
    }
    out.push({
      id: "act:goto-settings",
      kind: "action",
      group: t.groupActions,
      title: t.goToSettings,
      iconKey: "settings",
      searchTokens: `${t.goToSettings}`,
      run: () => router.push("/settings"),
    });

    if (theme !== "light") {
      out.push({
        id: "act:theme-light",
        kind: "action",
        group: t.groupActions,
        title: t.switchToLight,
        iconKey: "sun",
        searchTokens: `${t.switchToLight} theme light`,
        run: () => setTheme("light"),
      });
    }
    if (theme !== "dark") {
      out.push({
        id: "act:theme-dark",
        kind: "action",
        group: t.groupActions,
        title: t.switchToDark,
        iconKey: "moon",
        searchTokens: `${t.switchToDark} theme dark`,
        run: () => setTheme("dark"),
      });
    }
    if (theme !== "system") {
      out.push({
        id: "act:theme-system",
        kind: "action",
        group: t.groupActions,
        title: t.switchToSystem,
        iconKey: "monitor",
        searchTokens: `${t.switchToSystem} theme system`,
        run: () => setTheme("system"),
      });
    }
    if (locale !== "en") {
      out.push({
        id: "act:lang-en",
        kind: "action",
        group: t.groupActions,
        title: t.switchLanguageEn,
        iconKey: "globe",
        searchTokens: `${t.switchLanguageEn} english en`,
        run: () => setLocale("en"),
      });
    }
    if (locale !== "ru") {
      out.push({
        id: "act:lang-ru",
        kind: "action",
        group: t.groupActions,
        title: t.switchLanguageRu,
        iconKey: "globe",
        searchTokens: `${t.switchLanguageRu} russian ru русский`,
        run: () => setLocale("ru"),
      });
    }
    out.push({
      id: "act:signout",
      kind: "action",
      group: t.groupActions,
      title: t.signOut,
      iconKey: "logout",
      searchTokens: `${t.signOut}`,
      run: () => submitLocalLogout(),
    });

    // Positions
    if (data && candidateAccess.readCandidates) {
      for (const p of data.positions) {
        const status =
          p.status === "open"
            ? t.statusOpen
            : p.status === "paused"
              ? t.statusPaused
              : p.status === "closed"
                ? t.statusClosed
                : p.status;
        const subtitleParts: string[] = [status];
        if (p.folder) subtitleParts.push(p.folder);
        if (p.candidateCount > 0) {
          subtitleParts.push(`${p.candidateCount}`);
        }
        out.push({
          id: `pos:${p.id}`,
          kind: "position",
          group: t.groupPositions,
          title: p.title,
          subtitle: subtitleParts.join(" · "),
          iconKey: "briefcase",
          searchTokens: `${p.title} ${p.folder ?? ""} ${p.status}`,
          run: () => router.push(`/positions/${p.id}`),
        });
      }
    }

    // Candidates
    if (data && candidateAccess.readCandidates) {
      for (const c of data.candidates) {
        const subtitleParts: string[] = [];
        if (c.bestPositionTitle) subtitleParts.push(c.bestPositionTitle);
        if (c.bestScore != null) subtitleParts.push(`${c.bestScore.toFixed(1)}`);
        out.push({
          id: `cand:${c.id}`,
          kind: "candidate",
          group: t.groupCandidates,
          title: c.label,
          subtitle: subtitleParts.length ? subtitleParts.join(" · ") : undefined,
          iconKey: c.isFavorite ? "star" : "user",
          searchTokens: `${c.label} ${c.bestPositionTitle ?? ""}`,
          run: () => router.push(`/candidates/${c.id}`),
        });
      }
    }

    return out;
  }, [
    canReadConversations,
    candidateAccess.createPositions,
    candidateAccess.readCandidates,
    data,
    locale,
    m.app.nav.myWork,
    permissions,
    router,
    setLocale,
    setTheme,
    t,
    theme,
  ]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => it.searchTokens.toLowerCase().includes(q));
  }, [items, query]);

  // Group filtered items in stable order
  const grouped = useMemo(() => {
    const map = new Map<string, Item[]>();
    for (const it of filtered) {
      const key =
        it.kind === "action"
          ? "actions"
          : it.kind === "position"
            ? "positions"
            : "candidates";
      const existing = map.get(key) ?? [];
      existing.push(it);
      map.set(key, existing);
    }
    return GROUP_ORDER.flatMap((g) => {
      const arr = map.get(g);
      if (!arr || arr.length === 0) return [];
      return [{ group: g, items: arr }];
    });
  }, [filtered]);

  // Clamp activeIdx into bounds when filtered changes
  useEffect(() => {
    if (filtered.length === 0) {
      setActiveIdx(0);
      return;
    }
    setActiveIdx((idx) => Math.min(idx, filtered.length - 1));
  }, [filtered.length]);

  // Auto-scroll the active row into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(
      `[data-idx="${activeIdx}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [activeIdx]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => (i + 1) % Math.max(filtered.length, 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) =>
        (i - 1 + Math.max(filtered.length, 1)) % Math.max(filtered.length, 1),
      );
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = filtered[activeIdx];
      if (item) {
        item.run();
        close();
      }
    }
  }

  function runAt(idx: number) {
    const item = filtered[idx];
    if (item) {
      item.run();
      close();
    }
  }

  return (
    <DialogRoot.Root open={open} onOpenChange={setOpen}>
      <DialogRoot.Portal>
        <DialogRoot.Backdrop
          className="fixed inset-0 z-50 bg-black/30 supports-backdrop-filter:backdrop-blur-sm data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0 duration-100"
        />
        <DialogRoot.Popup
          aria-label={t.triggerLabel}
          className={cn(
            "fixed left-1/2 top-[14vh] z-50 -translate-x-1/2 outline-none",
            "w-[calc(100%-2rem)] max-w-[600px]",
            "rounded-xl border border-border bg-bg-elevated shadow-2xl ring-1 ring-foreground/5",
            "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95",
            "data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95 duration-100",
          )}
          onKeyDown={onKeyDown}
        >
          <DialogRoot.Title className="sr-only">{t.triggerLabel}</DialogRoot.Title>
          <DialogRoot.Description className="sr-only">
            {t.searchPlaceholder}
          </DialogRoot.Description>

          {/* Search input */}
          <div className="flex items-center gap-2.5 border-b border-border px-3.5 py-3">
            <Search className="h-4 w-4 shrink-0 text-text-faint" />
            <input
              ref={inputRef}
              autoFocus
              aria-label={t.searchPlaceholder}
              aria-controls="command-palette-results"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActiveIdx(0);
              }}
              placeholder={t.searchPlaceholder}
              className="flex-1 bg-transparent text-[14px] outline-none placeholder:text-text-faint"
            />
            {loading ? (
              <span
                className="font-mono text-[10px] text-text-faint"
                role="status"
                aria-live="polite"
              >
                {t.loading}
              </span>
            ) : null}
          </div>

          {/* Results */}
          <div
            id="command-palette-results"
            ref={listRef}
            className="max-h-[55vh] overflow-y-auto py-1.5"
            role="listbox"
            aria-busy={loading}
          >
            {filtered.length === 0 ? (
              <div className="px-4 py-10 text-center text-[12.5px] text-text-faint">
                {t.empty}
              </div>
            ) : (
              grouped.map(({ group, items: groupItems }) => (
                <div key={group} className="px-1.5 pb-1">
                  <div className="px-3 pt-2.5 pb-1 font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
                    {group === "actions"
                      ? t.groupActions
                      : group === "positions"
                        ? t.groupPositions
                        : t.groupCandidates}
                  </div>
                  {groupItems.map((it) => {
                    const idx = filtered.indexOf(it);
                    const Icon = ICONS[it.iconKey] ?? Briefcase;
                    const active = idx === activeIdx;
                    return (
                      <button
                        key={it.id}
                        data-idx={idx}
                        type="button"
                        role="option"
                        aria-selected={active}
                        onMouseEnter={() => setActiveIdx(idx)}
                        onClick={() => runAt(idx)}
                        className={cn(
                          "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors",
                          active
                            ? "bg-accent-muted text-text"
                            : "text-text-muted hover:bg-bg-hover hover:text-text",
                        )}
                      >
                        <Icon
                          className={cn(
                            "h-4 w-4 shrink-0",
                            active ? "text-text" : "text-text-muted",
                          )}
                        />
                        <span className="flex min-w-0 flex-1 flex-col">
                          <span className="truncate text-[13px] font-medium text-text">
                            {it.title}
                          </span>
                          {it.subtitle ? (
                            <span className="truncate font-mono text-[10.5px] text-text-faint">
                              {it.subtitle}
                            </span>
                          ) : null}
                        </span>
                        {active ? (
                          <CornerDownLeft className="h-3.5 w-3.5 shrink-0 text-text-faint" />
                        ) : null}
                      </button>
                    );
                  })}
                </div>
              ))
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between gap-3 border-t border-border px-3.5 py-2 font-mono text-[10px] text-text-faint">
            <span>{t.navHint}</span>
            <div className="flex items-center gap-3">
              <span>{t.selectHint}</span>
              <span>{t.closeHint}</span>
            </div>
          </div>
        </DialogRoot.Popup>
      </DialogRoot.Portal>
    </DialogRoot.Root>
  );
}
