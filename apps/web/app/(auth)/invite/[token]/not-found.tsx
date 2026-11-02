import { AccessShell } from "@/components/auth/access-shell";
import { getLocale } from "@/lib/i18n";

export default async function InvitationNotFound() {
  const locale = await getLocale();
  return (
    <AccessShell
      title={locale === "ru" ? "Приглашение не найдено" : "Invitation not found"}
      subtitle={
        locale === "ru"
          ? "Проверьте ссылку или запросите новое приглашение у администратора."
          : "Check the link or ask an administrator for a new invitation."
      }
    >
      <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
        404
      </p>
    </AccessShell>
  );
}
