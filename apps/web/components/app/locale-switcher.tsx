"use client";

import { Globe } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

export function LocaleSwitcher() {
  const { locale, setLocale, changing } = useI18n();
  const next = locale === "ru" ? "en" : "ru";

  return (
    <button
      type="button"
      onClick={() => setLocale(next)}
      disabled={changing}
      title={locale === "ru" ? "Switch to English" : "Переключить на русский"}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50",
        "text-text-muted hover:bg-bg-hover hover:text-text",
      )}
    >
      <Globe className="h-3 w-3" />
      <span className="font-mono uppercase tracking-[0.06em]">
        {locale}
      </span>
    </button>
  );
}
