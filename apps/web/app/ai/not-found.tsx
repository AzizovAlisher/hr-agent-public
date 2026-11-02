import Link from "next/link";
import { FileQuestion } from "lucide-react";

import { getAiCopy } from "@/lib/ai/copy";
import { getLocale } from "@/lib/i18n";

export default async function AiNotFound() {
  const locale = await getLocale();
  const copy = getAiCopy(locale);
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-6 py-12">
      <div className="max-w-sm text-center">
        <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl border border-border bg-bg-elevated text-text-muted"><FileQuestion className="h-5 w-5" /></span>
        <h1 className="mt-4 text-[15px] font-semibold">{locale === "ru" ? "Миссия не найдена" : "Mission not found"}</h1>
        <p className="mt-1.5 text-[11.5px] leading-5 text-text-muted">{locale === "ru" ? "Она могла быть удалена или принадлежит другому воркспейсу." : "It may have been removed or belongs to another workspace."}</p>
        <Link href="/ai/active" className="mt-5 inline-flex h-8 items-center justify-center rounded-lg border border-border px-3 text-[12px] font-medium hover:bg-bg-hover">{copy.active}</Link>
      </div>
    </div>
  );
}

