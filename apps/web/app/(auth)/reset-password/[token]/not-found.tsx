import { AccessShell } from "@/components/auth/access-shell";
import { getLocale } from "@/lib/i18n";

export default async function PasswordResetNotFound() {
  const locale = await getLocale();
  return (
    <AccessShell
      title={locale === "ru" ? "Ссылка не найдена" : "Reset link not found"}
      subtitle={
        locale === "ru"
          ? "Проверьте ссылку или запросите сброс пароля ещё раз."
          : "Check the link or request another password reset."
      }
    >
      <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
        404
      </p>
    </AccessShell>
  );
}
