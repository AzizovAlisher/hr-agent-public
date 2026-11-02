import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { confirmPasswordResetAction } from "@/app/(auth)/access-actions";
import { AccessShell } from "@/components/auth/access-shell";
import { PasswordAccessForm } from "@/components/auth/password-access-form";
import { getLocale } from "@/lib/i18n";
import { previewUserAccess } from "@/lib/user-access";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "Reset password · Recruiter",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function ResetPasswordPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const result = await previewUserAccess("password_reset", token);
  if (result.status === "not_found") notFound();
  const locale = await getLocale();
  if (result.status !== "ok") {
    return (
      <AccessShell
        title={locale === "ru" ? "Сброс пароля недоступен" : "Password reset unavailable"}
        subtitle={
          result.status === "gone"
            ? locale === "ru"
              ? "Ссылка истекла, была отозвана или уже использована. Запросите новую ссылку."
              : "This link expired, was revoked, or was already used. Request a new reset link."
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
  const action = confirmPasswordResetAction.bind(null, token);
  return (
    <AccessShell
      title={locale === "ru" ? "Новый пароль" : "Set a new password"}
      subtitle={
        locale === "ru"
          ? "После сохранения все прежние сессии будут завершены."
          : "Saving a new password will revoke every existing session."
      }
    >
      <PasswordAccessForm
        mode="password_reset"
        preview={result.data}
        locale={locale}
        action={action}
      />
    </AccessShell>
  );
}
