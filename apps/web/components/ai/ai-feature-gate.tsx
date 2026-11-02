"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BotOff, Settings2, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { getAiCopy } from "@/lib/ai/copy";

export function AiFeatureGate({
  enabled,
  unavailable,
  canUse,
  canManage,
  children,
}: {
  enabled: boolean;
  unavailable: boolean;
  canUse: boolean;
  canManage: boolean;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const { locale } = useI18n();
  const copy = getAiCopy(locale);
  const settingsRoute = pathname === "/ai/settings";
  if ((settingsRoute && canManage) || (!settingsRoute && canUse && enabled)) {
    return children;
  }

  const denied = settingsRoute ? !canManage : !canUse;

  const title = denied
    ? locale === "ru"
      ? "Нет доступа к AI"
      : "AI access is not available"
    : unavailable
    ? copy.common.unavailable
    : locale === "ru"
      ? "AI-воркспейс выключен"
      : "AI workspace is disabled";
  const body = denied
    ? locale === "ru"
      ? "Для этого раздела нужны дополнительные разрешения вашей организации."
      : "Your organization has not granted the permissions required for this section."
    : unavailable
    ? locale === "ru"
      ? "HR-платформа продолжает работать. Попробуйте открыть AI позже."
      : "The Human Platform is still available. Try opening AI again later."
    : locale === "ru"
      ? "Администратор может включить его в настройках AI. HR-платформа продолжает работать без изменений."
      : "An administrator can enable it in AI Settings. The Human Platform continues to work unchanged.";

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center px-6 py-12">
      <div className="max-w-md rounded-2xl border border-border bg-bg-elevated p-7 text-center shadow-xl">
        <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl border border-border bg-bg-hover text-text-muted">
          {unavailable ? <TriangleAlert className="h-5 w-5 text-score-mid" /> : <BotOff className="h-5 w-5" />}
        </span>
        <h1 className="mt-5 text-[18px] font-semibold">{title}</h1>
        <p className="mt-2 text-[12px] leading-5 text-text-muted">{body}</p>
        <div className="mt-6 flex flex-col justify-center gap-2 sm:flex-row">
          <Link href="/my-work" className="inline-flex h-8 items-center justify-center rounded-lg border border-border px-3 text-[12px] font-medium hover:bg-bg-hover">
            {copy.backToHuman}
          </Link>
          {canManage && !settingsRoute && !unavailable ? (
            <Link href="/ai/settings" className="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg bg-primary px-3 text-[12px] font-medium text-primary-foreground">
              <Settings2 className="h-3.5 w-3.5" />{copy.settings}
            </Link>
          ) : null}
        </div>
      </div>
    </div>
  );
}
