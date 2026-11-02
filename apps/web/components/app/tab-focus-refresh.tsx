"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * Refetches server data whenever the user returns to this tab.
 * Useful for pages that show data that may change in another tab/process
 * (a candidate's score arriving, a teammate's favorite toggle, etc.).
 */
export function TabFocusRefresh({ minIntervalMs = 30_000 }: { minIntervalMs?: number }) {
  const router = useRouter();
  useEffect(() => {
    let lastRefresh = Date.now();
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (Date.now() - lastRefresh < minIntervalMs) return;
      lastRefresh = Date.now();
      router.refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [router, minIntervalMs]);
  return null;
}
