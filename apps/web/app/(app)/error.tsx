"use client";

import Link from "next/link";
import { useEffect } from "react";
import { AlertTriangle } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const { m } = useI18n();
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") {
      console.error("App error boundary:", error);
    }
  }, [error]);

  return (
    <div className="flex min-h-full flex-1 items-center justify-center p-8">
      <div className="flex max-w-md flex-col items-center gap-4 rounded-xl border border-border bg-bg-elevated px-8 py-10 text-center">
        <span className="flex h-10 w-10 items-center justify-center rounded-full border border-score-low/40 bg-score-low/10 text-score-low">
          <AlertTriangle className="h-5 w-5" />
        </span>
        <div className="space-y-1.5">
          <h2 className="text-[15px] font-semibold tracking-tight">
            {m.errors.appBoundaryTitle}
          </h2>
          <p className="text-[12.5px] leading-[1.6] text-text-muted">
            {m.errors.appBoundaryDesc}
          </p>
          {error.digest ? (
            <p className="font-mono text-[10px] text-text-faint">
              {error.digest}
            </p>
          ) : null}
        </div>
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={reset}
            className={cn(
              "inline-flex items-center gap-2 rounded-md bg-text px-3 py-1.5 text-[12.5px] font-medium text-bg transition-opacity hover:opacity-90",
            )}
          >
            {m.errors.retry}
          </button>
          <Link
            href="/dashboard"
            className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-1.5 text-[12.5px] text-text-muted transition-colors hover:border-border-strong hover:text-text"
          >
            {m.errors.goHome}
          </Link>
        </div>
      </div>
    </div>
  );
}
