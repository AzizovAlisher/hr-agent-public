"use client";

import { useEffect, useState } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

const ORDER = ["dark", "light", "system"] as const;
const ICON: Record<(typeof ORDER)[number], React.ReactNode> = {
  dark: <Moon className="h-3.5 w-3.5" />,
  light: <Sun className="h-3.5 w-3.5" />,
  system: <Monitor className="h-3.5 w-3.5" />,
};

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const { m } = useI18n();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const current = mounted
    ? ((theme as (typeof ORDER)[number]) ?? "dark")
    : "dark";

  return (
    <div className="flex items-center gap-px rounded-[6px] border border-border bg-bg-elevated p-[2px]">
      {ORDER.map((t) => {
        const active = current === t;
        return (
          <button
            key={t}
            type="button"
            title={m.app.themeOptions[t]}
            onClick={() => setTheme(t)}
            className={cn(
              "flex h-[22px] w-[22px] items-center justify-center rounded-[4px] transition-colors",
              active
                ? "bg-bg-active text-text"
                : "text-text-muted hover:text-text",
            )}
          >
            {ICON[t]}
          </button>
        );
      })}
    </div>
  );
}
