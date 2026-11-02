import type { Metadata } from "next";

import { AccessShell } from "@/components/auth/access-shell";
import { ForgotPasswordForm } from "@/components/auth/forgot-password-form";
import { getLocale } from "@/lib/i18n";

export const metadata: Metadata = {
  title: "Password recovery · Recruiter",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function ForgotPasswordPage() {
  const locale = await getLocale();
  return (
    <AccessShell
      title={locale === "ru" ? "Восстановление доступа" : "Recover your account"}
      subtitle={
        locale === "ru"
          ? "Укажите рабочий email. Ответ не сообщит, существует ли аккаунт с таким адресом."
          : "Enter your work email. The response will not reveal whether an account exists."
      }
    >
      <ForgotPasswordForm locale={locale} />
    </AccessShell>
  );
}
