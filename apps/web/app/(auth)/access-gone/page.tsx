import type { Metadata } from "next";

import { AccessShell } from "@/components/auth/access-shell";
import { getLocale } from "@/lib/i18n";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "Private link unavailable · Recruiter",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function AccessGonePage({
  searchParams,
}: {
  searchParams: Promise<{ purpose?: string }>;
}) {
  const [locale, query] = await Promise.all([getLocale(), searchParams]);
  const invitation = query.purpose === "invitation";
  const title = invitation
    ? locale === "ru"
      ? "Приглашение недоступно"
      : "Invitation unavailable"
    : locale === "ru"
      ? "Сброс пароля недоступен"
      : "Password reset unavailable";
  const message = invitation
    ? locale === "ru"
      ? "Ссылка истекла, была отозвана или уже использована. Запросите новое приглашение у администратора."
      : "This link expired, was revoked, or was already used. Ask an administrator for a new invitation."
    : locale === "ru"
      ? "Ссылка истекла, была отозвана или уже использована. Запросите новую ссылку."
      : "This link expired, was revoked, or was already used. Request a new reset link.";

  return (
    <AccessShell title={title} subtitle={message}>
      <p
        role="alert"
        className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive"
      >
        410
      </p>
    </AccessShell>
  );
}
