import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { acceptInvitationAction } from "@/app/(auth)/access-actions";
import { AccessShell } from "@/components/auth/access-shell";
import { PasswordAccessForm } from "@/components/auth/password-access-form";
import { getLocale } from "@/lib/i18n";
import { previewUserAccess } from "@/lib/user-access";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "Team invitation · Recruiter",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function InvitationPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const result = await previewUserAccess("invitation", token);
  if (result.status === "not_found") notFound();
  const locale = await getLocale();
  if (result.status !== "ok") {
    return (
      <AccessShell
        title={locale === "ru" ? "Приглашение недоступно" : "Invitation unavailable"}
        subtitle={
          result.status === "gone"
            ? locale === "ru"
              ? "Ссылка истекла, была отозвана или уже использована. Запросите новое приглашение у администратора."
              : "This link expired, was revoked, or was already used. Ask an administrator for a new invitation."
            : locale === "ru"
              ? "Сервис временно недоступен. Попробуйте позже."
              : "The account service is temporarily unavailable. Try again later."
        }
      >
        <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
          {result.status === "gone" ? "410" : "503"}
        </p>
      </AccessShell>
    );
  }
  const action = acceptInvitationAction.bind(null, token);
  return (
    <AccessShell
      title={locale === "ru" ? "Присоединиться к команде" : "Join your team"}
      subtitle={
        locale === "ru"
          ? "Задайте пароль, чтобы активировать приглашённый аккаунт."
          : "Set a password to activate your invited account."
      }
    >
      <PasswordAccessForm
        mode="invitation"
        preview={result.data}
        locale={locale}
        action={action}
      />
    </AccessShell>
  );
}
