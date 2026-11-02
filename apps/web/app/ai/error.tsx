"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";
import { Button } from "@/components/ui/button";
import { getAiCopy } from "@/lib/ai/copy";

export default function AiError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-6 py-12">
      <div className="max-w-sm text-center">
        <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl border border-score-low/25 bg-score-low/5 text-score-low"><AlertTriangle className="h-5 w-5" /></span>
        <h1 className="mt-4 text-[15px] font-semibold">{copy.common.unavailable}</h1>
        <Button className="mt-5" variant="outline" size="sm" onClick={reset}><RefreshCw />{copy.common.retry}</Button>
      </div>
    </div>
  );
}

