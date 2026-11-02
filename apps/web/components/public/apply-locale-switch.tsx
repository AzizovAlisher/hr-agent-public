"use client";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";
import type { Locale } from "@/lib/i18n/types";

const LABELS: Record<Locale, string> = { ru: "RU", en: "EN" };
const ORDER: Locale[] = ["ru", "en"];

export function ApplyLocaleSwitch() {
  const { locale, setLocale, m, changing } = useI18n();
  return (
    <div
      role="group"
      aria-label={m.apply.localeSwitch}
      className={cn(
        "inline-flex items-center rounded-full border border-border bg-bg-elevated p-0.5 font-mono text-[10.5px] tracking-[0.04em] transition-opacity",
        changing && "opacity-60",
      )}
    >
      {ORDER.map((loc) => (
        <button
          key={loc}
          type="button"
          onClick={() => setLocale(loc)}
          aria-pressed={loc === locale}
          className={cn(
            "min-h-6 min-w-6 rounded-full px-2.5 py-[3px] transition-colors",
            loc === locale
              ? "bg-text text-bg"
              : "text-text-muted hover:text-text",
          )}
        >
          {LABELS[loc]}
        </button>
      ))}
    </div>
  );
}
