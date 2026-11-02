"use client";

import { useEffect, useRef } from "react";

import { markViewedAction } from "@/lib/actions";

/**
 * Fires markViewed once on mount. Idempotent server-side (just stamps viewed_at).
 * Skipped during fast-refresh re-mounts via a ref guard.
 */
export function MarkViewedOnMount({ candidateId }: { candidateId: string }) {
  const fired = useRef(false);
  useEffect(() => {
    if (fired.current) return;
    fired.current = true;
    void markViewedAction(candidateId);
  }, [candidateId]);
  return null;
}
