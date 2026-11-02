"use client";

import { useEffect } from "react";

import type { Locale } from "@/lib/i18n/types";

export function DocumentLanguage({ language }: { language: Locale }) {
  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  return null;
}
