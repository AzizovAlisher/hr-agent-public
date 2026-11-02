"use client";

import { createContext, useContext, useMemo, useTransition } from "react";

import { CATALOG } from "@/lib/i18n/catalog";
import { setLocaleAction } from "@/lib/i18n";
import type { Locale, Messages } from "@/lib/i18n/types";

/** @ui-only React locale-context value. */
type LocaleCtx = {
  locale: Locale;
  m: Messages;
  setLocale: (next: Locale) => void;
  changing: boolean;
};

const Ctx = createContext<LocaleCtx | null>(null);

export function LocaleProvider({
  initialLocale,
  children,
}: {
  initialLocale: Locale;
  children: React.ReactNode;
}) {
  const [changing, start] = useTransition();
  const value = useMemo<LocaleCtx>(() => {
    return {
      locale: initialLocale,
      m: CATALOG[initialLocale],
      changing,
      setLocale: (next: Locale) => {
        if (next === initialLocale) return;
        start(async () => {
          await setLocaleAction(next);
        });
      },
    };
  }, [initialLocale, changing]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): LocaleCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n must be used inside <LocaleProvider>");
  return v;
}
