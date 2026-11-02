"use client";

import { KeyRound, Laptop, Loader2, LogOut, ShieldCheck, ShieldOff } from "lucide-react";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import {
  disableMfaAction,
  revokeSessionAction,
} from "@/app/(app)/settings/security-actions";
import { useI18n } from "@/components/i18n/locale-provider";
import type { components } from "@/lib/generated/api";
import { submitLocalLogout } from "@/lib/local-logout";
import { cn } from "@/lib/utils";

type RefreshSession = components["schemas"]["RefreshSessionOut"];

const COPY = {
  en: {
    title: "Account security",
    subtitle: "Review active sign-ins and protect access to your workspace.",
    sessions: "Active sessions",
    sessionsHint: "Revoking a session prevents its refresh token from being used again.",
    current: "This session",
    created: "Started",
    used: "Last used",
    expires: "Expires",
    never: "Not used yet",
    revoke: "Revoke",
    revoking: "Revoking…",
    revokeConfirm: "Revoke this session? It will need to sign in again.",
    logoutAll: "Sign out everywhere",
    logoutAllConfirm: "Sign out every active session, including this one?",
    empty: "No active sessions were returned.",
    loadFailed: "Security information could not be loaded. Try refreshing the page.",
    mfa: "Multi-factor authentication",
    enabled: "Enabled",
    required: "Required for administrators",
    mfaHint: "Reset MFA only if you need to enroll a different authenticator. You will be signed out everywhere and must enroll again at your next sign-in.",
    reset: "Reset MFA",
    password: "Current password",
    code: "6-digit authenticator code",
    recovery: "Recovery code",
    useRecovery: "Use a recovery code",
    useCode: "Use an authenticator code",
    resetConfirm: "Reset MFA and sign out every session?",
    updated: "Session revoked",
    failed: "The security change could not be completed.",
  },
  ru: {
    title: "Безопасность аккаунта",
    subtitle: "Проверьте активные входы и защитите доступ к воркспейсу.",
    sessions: "Активные сессии",
    sessionsHint: "После отзыва сессии её токен обновления больше нельзя использовать.",
    current: "Эта сессия",
    created: "Начало",
    used: "Последнее использование",
    expires: "Истекает",
    never: "Ещё не использовалась",
    revoke: "Отозвать",
    revoking: "Отзываем…",
    revokeConfirm: "Отозвать эту сессию? Потребуется войти заново.",
    logoutAll: "Выйти на всех устройствах",
    logoutAllConfirm: "Завершить все активные сессии, включая эту?",
    empty: "Активные сессии не найдены.",
    loadFailed: "Не удалось загрузить данные безопасности. Обновите страницу.",
    mfa: "Многофакторная аутентификация",
    enabled: "Включена",
    required: "Обязательна для администраторов",
    mfaHint: "Сбрасывайте MFA только для подключения другого приложения. Все сессии завершатся, а при следующем входе потребуется новая настройка.",
    reset: "Сбросить MFA",
    password: "Текущий пароль",
    code: "6-значный код из приложения",
    recovery: "Резервный код",
    useRecovery: "Использовать резервный код",
    useCode: "Использовать код из приложения",
    resetConfirm: "Сбросить MFA и завершить все сессии?",
    updated: "Сессия отозвана",
    failed: "Не удалось изменить настройки безопасности.",
  },
} as const;

function dateTime(value: string, locale: "en" | "ru"): string {
  return new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function SecuritySettingsTab({
  sessions,
  loadFailed,
  adminMfaRequired,
  mfaEnabled,
}: {
  sessions: RefreshSession[];
  loadFailed: boolean;
  adminMfaRequired: boolean;
  mfaEnabled: boolean;
}) {
  const { locale } = useI18n();
  const t = COPY[locale];
  const [items, setItems] = useState(sessions);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [recovery, setRecovery] = useState(false);

  function revoke(session: RefreshSession) {
    if (!window.confirm(t.revokeConfirm)) return;
    setPendingId(session.id);
    startTransition(async () => {
      const result = await revokeSessionAction(session.id, session.current);
      if (result.ok) {
        setItems((current) => current.filter((item) => item.id !== session.id));
        toast.success(t.updated);
      } else {
        toast.error(t.failed);
      }
      setPendingId(null);
    });
  }

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <header>
        <h2 className="text-xl font-semibold tracking-tight">{t.title}</h2>
        <p className="mt-1 text-sm leading-6 text-text-muted">{t.subtitle}</p>
      </header>

      {loadFailed ? (
        <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
          {t.loadFailed}
        </p>
      ) : null}

      <section aria-labelledby="security-sessions" className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
        <div className="flex items-start gap-3">
          <Laptop className="mt-0.5 h-5 w-5 text-primary" aria-hidden />
          <div>
            <h3 id="security-sessions" className="font-semibold">{t.sessions}</h3>
            <p className="mt-1 text-sm leading-6 text-text-muted">{t.sessionsHint}</p>
          </div>
        </div>
        {items.length ? (
          <ul className="mt-5 divide-y divide-border rounded-lg border border-border">
            {items.map((session) => (
              <li key={session.id} className="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs text-text-muted">{session.id.slice(0, 8)}</span>
                    {session.current ? (
                      <span className="rounded-full bg-primary/15 px-2 py-0.5 text-xs font-medium text-primary">{t.current}</span>
                    ) : null}
                  </div>
                  <dl className="mt-2 grid gap-x-5 gap-y-1 text-xs text-text-muted sm:grid-cols-3">
                    <div><dt className="sr-only">{t.created}</dt><dd>{t.created}: {dateTime(session.created_at, locale)}</dd></div>
                    <div><dt className="sr-only">{t.used}</dt><dd>{t.used}: {session.last_used_at ? dateTime(session.last_used_at, locale) : t.never}</dd></div>
                    <div><dt className="sr-only">{t.expires}</dt><dd>{t.expires}: {dateTime(session.expires_at, locale)}</dd></div>
                  </dl>
                </div>
                <button
                  type="button"
                  onClick={() => revoke(session)}
                  disabled={pending}
                  className="flex min-h-10 shrink-0 items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-sm font-medium text-text-muted hover:bg-bg-hover hover:text-text focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
                >
                  {pendingId === session.id ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <LogOut className="h-4 w-4" />}
                  {pendingId === session.id ? t.revoking : t.revoke}
                </button>
              </li>
            ))}
          </ul>
        ) : !loadFailed ? (
          <p className="mt-5 rounded-lg border border-dashed border-border p-4 text-sm text-text-muted">{t.empty}</p>
        ) : null}
        <button
          type="button"
          onClick={() => {
            if (!window.confirm(t.logoutAllConfirm)) return;
            submitLocalLogout("all");
          }}
          disabled={pending}
          className="mt-4 flex min-h-10 items-center gap-2 rounded-md px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/10 focus-visible:ring-2 focus-visible:ring-ring"
        >
          <LogOut className="h-4 w-4" aria-hidden />
          {t.logoutAll}
        </button>
      </section>

      {adminMfaRequired ? (
        <section aria-labelledby="security-mfa" className="rounded-xl border border-border bg-bg-elevated p-5 sm:p-6">
          <div className="flex items-start gap-3">
            {mfaEnabled ? <ShieldCheck className="mt-0.5 h-5 w-5 text-primary" aria-hidden /> : <ShieldOff className="mt-0.5 h-5 w-5 text-warning" aria-hidden />}
            <div>
              <h3 id="security-mfa" className="font-semibold">{t.mfa}</h3>
              <p className={cn("mt-1 text-sm font-medium", mfaEnabled ? "text-primary" : "text-warning")}>{mfaEnabled ? t.enabled : t.required}</p>
            </div>
          </div>
          {mfaEnabled ? (
            <form
              className="mt-5 max-w-lg space-y-4"
              onSubmit={(event) => {
                event.preventDefault();
                if (!window.confirm(t.resetConfirm)) return;
                const data = new FormData(event.currentTarget);
                startTransition(async () => {
                  const result = await disableMfaAction(
                    String(data.get("password") ?? ""),
                    recovery ? "recovery" : "code",
                    String(data.get("verifier") ?? ""),
                  );
                  if (!result.ok) toast.error(t.failed);
                });
              }}
            >
              <p className="text-sm leading-6 text-text-muted">{t.mfaHint}</p>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
                {t.password}
                <input name="password" type="password" autoComplete="current-password" required className="min-h-11 rounded-md border border-border bg-bg px-3 py-2.5 text-base text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30" />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
                {recovery ? t.recovery : t.code}
                <input name="verifier" type="text" inputMode={recovery ? "text" : "numeric"} autoComplete="one-time-code" pattern={recovery ? undefined : "[0-9]{6}"} minLength={recovery ? 1 : 6} maxLength={recovery ? 64 : 6} required className="min-h-11 rounded-md border border-border bg-bg px-3 py-2.5 text-base text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30" />
              </label>
              <button type="button" onClick={() => setRecovery((value) => !value)} className="min-h-10 rounded-md text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring">{recovery ? t.useCode : t.useRecovery}</button>
              <button type="submit" disabled={pending} className="flex min-h-11 items-center justify-center gap-2 rounded-md border border-destructive/40 px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/10 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60">
                {pending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : <KeyRound className="h-4 w-4" />}
                {t.reset}
              </button>
            </form>
          ) : (
            <p className="mt-4 text-sm leading-6 text-text-muted">{t.mfaHint}</p>
          )}
        </section>
      ) : null}
    </div>
  );
}
