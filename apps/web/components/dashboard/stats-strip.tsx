"use client";

import { useEffect, useRef, useState } from "react";
import {
  Briefcase,
  CheckCircle2,
  Sparkles,
  TrendingUp,
} from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import type { CandidateSummary, Position } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Top-of-dashboard KPI tiles. Four glance-able numbers that animate from 0 to
 * their target on mount + on each value change. Compact, monospaced, calm.
 *
 * Tiles intentionally don't link anywhere — they're glanceable signal, not
 * navigation. The clickable affordances live below in the positions grid.
 */
export function StatsStrip({
  positions,
  candidates,
}: {
  positions: Position[];
  candidates: CandidateSummary[];
}) {
  const { m } = useI18n();

  const openRoles = positions.filter((p) => p.status === "open").length;
  const qualified = candidates.filter((c) => c.best_score != null).length;
  const topTier = candidates.filter(
    (c) => c.best_score != null && c.best_score >= 80,
  ).length;
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  const thisWeek = candidates.filter(
    (c) => Date.parse(c.created_at) > weekAgo,
  ).length;

  const tiles = [
    {
      key: "open",
      value: openRoles,
      label: m.dashboard.stats.openRoles,
      icon: Briefcase,
      tint: "text-text",
    },
    {
      key: "qualified",
      value: qualified,
      label: m.dashboard.stats.qualified,
      icon: CheckCircle2,
      tint: "text-score-high",
    },
    {
      key: "topTier",
      value: topTier,
      label: m.dashboard.stats.topTier,
      icon: Sparkles,
      tint: "text-primary",
    },
    {
      key: "thisWeek",
      value: thisWeek,
      label: m.dashboard.stats.thisWeek,
      icon: TrendingUp,
      tint: "text-score-mid",
    },
  ];

  return (
    <div className="mb-6 grid grid-cols-2 gap-2 sm:grid-cols-4">
      {tiles.map((t, i) => {
        const Icon = t.icon;
        return (
          <div
            key={t.key}
            className="group relative overflow-hidden rounded-lg border border-border bg-bg-elevated px-4 py-3.5 transition-colors hover:border-border-strong hover:bg-bg-hover"
          >
            {/* Hairline accent on hover */}
            <span
              aria-hidden
              className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent to-transparent opacity-0 transition-opacity duration-300 group-hover:opacity-60"
            />
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-text-faint">
                {t.label}
              </span>
              <Icon className={cn("h-3 w-3 opacity-70", t.tint)} />
            </div>
            <div className="mt-1.5 flex items-baseline gap-1.5">
              <CountUp
                value={t.value}
                className={cn(
                  "font-mono text-[26px] font-semibold leading-none tracking-[-0.03em] tabular-nums",
                  t.tint,
                )}
                delayMs={i * 70}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Counts from 0 to `value` over ~600ms with an ease-out curve. Lightweight
 * (just requestAnimationFrame, no library). Resets if `value` changes.
 */
function CountUp({
  value,
  className,
  delayMs = 0,
}: {
  value: number;
  className?: string;
  delayMs?: number;
}) {
  const [display, setDisplay] = useState(value > 0 ? 0 : value);
  const rafRef = useRef<number | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (value === 0) {
      setDisplay(0);
      return;
    }
    if (timerRef.current) window.clearTimeout(timerRef.current);
    if (rafRef.current) cancelAnimationFrame(rafRef.current);

    timerRef.current = window.setTimeout(() => {
      const duration = 700;
      const start = performance.now();
      const from = 0;
      const step = (now: number) => {
        const t = Math.min(1, (now - start) / duration);
        // ease-out cubic
        const eased = 1 - Math.pow(1 - t, 3);
        setDisplay(Math.round(from + (value - from) * eased));
        if (t < 1) rafRef.current = requestAnimationFrame(step);
      };
      rafRef.current = requestAnimationFrame(step);
    }, delayMs);

    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [value, delayMs]);

  return <span className={className}>{display}</span>;
}
