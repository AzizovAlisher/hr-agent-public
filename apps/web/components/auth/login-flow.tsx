"use client";

import { Check, Copy, KeyRound, Loader2, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import { authFlowAction } from "@/app/(auth)/login/actions";
import { useI18n } from "@/components/i18n/locale-provider";
import type { AuthFlowError, AuthFlowState } from "@/lib/auth-flow-types";

const COPY = {
  en: {
    title: "Sign in",
    subtitle: "Enter your credentials to access the workspace.",
    email: "Email",
    password: "Password",
    forgotPassword: "Forgot password?",
    signIn: "Sign in",
    setupTitle: "Protect your admin account",
    setupIntro:
      "Administrators must use multi-factor authentication. Start setup, then add the account to your authenticator app.",
    beginSetup: "Start MFA setup",
    manualKey: "Manual setup key",
    openAuthenticator: "Open in authenticator app",
    setupOnce: "This setup key is shown only during this enrollment. Do not share it.",
    code: "6-digit authenticator code",
    confirmSetup: "Verify and finish setup",
    verifyTitle: "Multi-factor verification",
    verifySubtitle: "Enter a fresh code from your authenticator app.",
    recoverySubtitle: "Enter one unused recovery code. It will be consumed after sign-in.",
    useRecovery: "Use a recovery code",
    useAuthenticator: "Use an authenticator code",
    recoveryCode: "Recovery code",
    verify: "Verify and sign in",
    recoveryTitle: "Save your recovery codes now",
    recoveryIntro:
      "Each code works once. Store them somewhere secure; they will not be shown again.",
    copyAll: "Copy all codes",
    copied: "Copied",
    saved: "I saved these codes",
    restart: "Restart sign-in",
    working: "Please wait…",
    errors: {
      invalid_credentials: "The email or password is incorrect.",
      session_expired: "Your session expired. Sign in again to continue.",
      invalid_code: "That verification code is invalid or has already been used.",
      challenge_expired: "This verification session expired. Sign in again.",
      setup_already_started:
        "Setup was already started. Restart sign-in to receive a new setup key.",
      rate_limited: "Too many attempts. Wait before trying again.",
      service_unavailable: "Authentication is temporarily unavailable. Try again shortly.",
      invalid_request: "Check the highlighted value and try again.",
    } satisfies Record<AuthFlowError, string>,
  },
  ru: {
    title: "Войти",
    subtitle: "Введите данные для входа в воркспейс.",
    email: "Email",
    password: "Пароль",
    forgotPassword: "Забыли пароль?",
    signIn: "Войти",
    setupTitle: "Защитите аккаунт администратора",
    setupIntro:
      "Для администраторов обязательна многофакторная аутентификация. Начните настройку и добавьте аккаунт в приложение-аутентификатор.",
    beginSetup: "Начать настройку MFA",
    manualKey: "Ключ для ручной настройки",
    openAuthenticator: "Открыть в приложении-аутентификаторе",
    setupOnce: "Ключ показывается только при этой настройке. Никому его не передавайте.",
    code: "6-значный код из приложения",
    confirmSetup: "Проверить и завершить настройку",
    verifyTitle: "Многофакторная проверка",
    verifySubtitle: "Введите новый код из приложения-аутентификатора.",
    recoverySubtitle: "Введите неиспользованный резервный код. После входа он будет погашен.",
    useRecovery: "Использовать резервный код",
    useAuthenticator: "Использовать код из приложения",
    recoveryCode: "Резервный код",
    verify: "Проверить и войти",
    recoveryTitle: "Сохраните резервные коды сейчас",
    recoveryIntro:
      "Каждый код работает один раз. Храните их в безопасном месте — повторно они не показываются.",
    copyAll: "Копировать все коды",
    copied: "Скопировано",
    saved: "Я сохранил(а) коды",
    restart: "Начать вход заново",
    working: "Подождите…",
    errors: {
      invalid_credentials: "Неверный email или пароль.",
      session_expired: "Сессия истекла. Войдите снова, чтобы продолжить.",
      invalid_code: "Код неверен или уже был использован.",
      challenge_expired: "Сессия проверки истекла. Войдите заново.",
      setup_already_started:
        "Настройка уже начата. Начните вход заново, чтобы получить новый ключ.",
      rate_limited: "Слишком много попыток. Подождите и попробуйте снова.",
      service_unavailable: "Сервис входа временно недоступен. Попробуйте чуть позже.",
      invalid_request: "Проверьте введённое значение и повторите попытку.",
    } satisfies Record<AuthFlowError, string>,
  },
} as const;

function SubmitButton({ children }: { children: React.ReactNode }) {
  const { pending } = useFormStatus();
  const { locale } = useI18n();
  const t = COPY[locale];
  return (
    <button
      type="submit"
      disabled={pending}
      className="mt-2 flex min-h-11 items-center justify-center gap-2 rounded-[6px] bg-text px-4 py-2.5 text-[13px] font-medium text-bg outline-none transition-opacity hover:opacity-90 focus-visible:ring-4 focus-visible:ring-ring/40 disabled:cursor-wait disabled:opacity-60"
    >
      {pending ? (
        <>
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
          {t.working}
        </>
      ) : (
        children
      )}
    </button>
  );
}

function FlowError({ state, hidden = false }: { state: AuthFlowState; hidden?: boolean }) {
  const { locale } = useI18n();
  if (!state.error || hidden) return null;
  const t = COPY[locale];
  return (
    <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
      {t.errors[state.error]}
      {state.error === "rate_limited" && state.retryAfter
        ? ` (${state.retryAfter}s)`
        : ""}
    </p>
  );
}

function RestartButton() {
  const { locale } = useI18n();
  return (
    <button
      type="submit"
      name="intent"
      value="restart"
      className="min-h-11 rounded-md px-3 py-2 text-sm font-medium text-text-muted underline-offset-4 hover:text-text hover:underline focus-visible:ring-2 focus-visible:ring-ring"
    >
      {COPY[locale].restart}
    </button>
  );
}

export function LoginFlow({ initialState }: { initialState: AuthFlowState }) {
  const { locale } = useI18n();
  const t = COPY[locale];
  const [state, action] = useActionState(authFlowAction, initialState);
  const [recovery, setRecovery] = useState(false);
  const [dismissedErrorState, setDismissedErrorState] =
    useState<AuthFlowState | null>(null);
  const [copied, setCopied] = useState(false);

  if (state.step === "recovery" && state.recoveryCodes) {
    return (
      <section aria-labelledby="recovery-title">
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-primary/15 text-primary">
          <ShieldCheck className="h-5 w-5" aria-hidden />
        </div>
        <h1 id="recovery-title" className="text-xl font-semibold tracking-tight">
          {t.recoveryTitle}
        </h1>
        <p className="mt-2 text-sm leading-6 text-text-muted">{t.recoveryIntro}</p>
        <ol className="mt-5 grid grid-cols-1 gap-2 rounded-lg border border-border bg-bg p-4 sm:grid-cols-2">
          {state.recoveryCodes.map((code) => (
            <li key={code} className="select-all font-mono text-sm tracking-wide text-text">
              {code}
            </li>
          ))}
        </ol>
        <div className="mt-5 flex flex-col gap-2 sm:flex-row">
          <button
            type="button"
            onClick={async () => {
              await navigator.clipboard.writeText(state.recoveryCodes!.join("\n"));
              setCopied(true);
            }}
            className="flex min-h-11 flex-1 items-center justify-center gap-2 rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-bg-hover focus-visible:ring-2 focus-visible:ring-ring"
          >
            {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            {copied ? t.copied : t.copyAll}
          </button>
          <a
            href="/my-work"
            className="flex min-h-11 flex-1 items-center justify-center rounded-md bg-text px-4 py-2 text-sm font-medium text-bg focus-visible:ring-4 focus-visible:ring-ring/40"
          >
            {t.saved}
          </a>
        </div>
        <span aria-live="polite" className="sr-only">{copied ? t.copied : ""}</span>
      </section>
    );
  }

  if (state.step === "setup") {
    return (
      <section aria-labelledby="setup-title">
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-primary/15 text-primary">
          <ShieldCheck className="h-5 w-5" aria-hidden />
        </div>
        <h1 id="setup-title" className="text-xl font-semibold tracking-tight">{t.setupTitle}</h1>
        <p className="mt-2 text-sm leading-6 text-text-muted">{t.setupIntro}</p>
        <form action={action} className="mt-5 flex flex-col gap-4">
          <FlowError state={state} />
          {state.setup ? (
            <>
              <div className="rounded-lg border border-warning/30 bg-warning/10 p-4">
                <p className="text-sm font-medium text-warning">{t.setupOnce}</p>
                <p className="mt-3 text-xs font-medium uppercase tracking-wider text-text-muted">{t.manualKey}</p>
                <code className="mt-1 block break-all rounded bg-bg px-3 py-2 font-mono text-sm text-text">
                  {state.setup.secret}
                </code>
                <a
                  href={state.setup.provisioningUri}
                  className="mt-3 inline-flex min-h-10 items-center text-sm font-medium text-primary underline underline-offset-4 focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {t.openAuthenticator}
                </a>
              </div>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
                {t.code}
                <input
                  name="code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  pattern="[0-9]{6}"
                  minLength={6}
                  maxLength={6}
                  required
                  autoFocus
                  className="min-h-11 rounded-md border border-border bg-bg px-3 py-2.5 text-base tracking-[0.25em] text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30"
                />
              </label>
              <input type="hidden" name="intent" value="confirm" />
              <SubmitButton>{t.confirmSetup}</SubmitButton>
            </>
          ) : (
            <>
              <input type="hidden" name="intent" value="setup" />
              <SubmitButton>{t.beginSetup}</SubmitButton>
            </>
          )}
        </form>
        <form action={action} className="mt-2 text-center"><RestartButton /></form>
      </section>
    );
  }

  if (state.step === "verify") {
    return (
      <section aria-labelledby="verify-title">
        <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-primary/15 text-primary">
          <KeyRound className="h-5 w-5" aria-hidden />
        </div>
        <h1 id="verify-title" className="text-xl font-semibold tracking-tight">{t.verifyTitle}</h1>
        <p className="mt-2 text-sm leading-6 text-text-muted">
          {recovery ? t.recoverySubtitle : t.verifySubtitle}
        </p>
        <form action={action} className="mt-5 flex flex-col gap-4">
          <FlowError state={state} hidden={dismissedErrorState === state} />
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
            {recovery ? t.recoveryCode : t.code}
            <input
              key={recovery ? "recovery" : "authenticator"}
              name="verifier"
              type="text"
              inputMode={recovery ? "text" : "numeric"}
              autoComplete="one-time-code"
              pattern={recovery ? undefined : "[0-9]{6}"}
              minLength={recovery ? 1 : 6}
              maxLength={recovery ? 64 : 6}
              required
              autoFocus
              className="min-h-11 rounded-md border border-border bg-bg px-3 py-2.5 text-base text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30"
            />
          </label>
          <input type="hidden" name="method" value={recovery ? "recovery" : "code"} />
          <input type="hidden" name="intent" value="verify" />
          <button
            type="button"
            onClick={() => {
              setDismissedErrorState(state);
              setRecovery((value) => !value);
            }}
            className="min-h-10 self-start rounded-md px-1 text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring"
          >
            {recovery ? t.useAuthenticator : t.useRecovery}
          </button>
          <SubmitButton>{t.verify}</SubmitButton>
        </form>
        <form action={action} className="mt-2 text-center"><RestartButton /></form>
      </section>
    );
  }

  return (
    <section aria-labelledby="login-title">
      <h1 id="login-title" className="text-[18px] font-semibold tracking-tight">{t.title}</h1>
      <p className="mb-6 mt-1 text-[13px] text-text-muted">{t.subtitle}</p>
      <form action={action} className="flex flex-col gap-3.5">
        <FlowError state={state} />
        <label className="flex flex-col gap-1.5 text-[12px] font-medium text-text-muted">
          {t.email}
          <input
            name="email"
            type="email"
            required
            autoComplete="email"
            autoFocus
            placeholder="you@company.com"
            className="min-h-11 rounded-[6px] border border-border bg-bg px-3 py-2.5 text-base text-text outline-none transition-colors placeholder:text-text-faint focus:border-border-strong focus:bg-bg-elevated focus:ring-4 focus:ring-ring/30"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-[12px] font-medium text-text-muted">
          {t.password}
          <input
            name="password"
            type="password"
            required
            autoComplete="current-password"
            className="min-h-11 rounded-[6px] border border-border bg-bg px-3 py-2.5 text-base text-text outline-none transition-colors focus:border-border-strong focus:bg-bg-elevated focus:ring-4 focus:ring-ring/30"
          />
        </label>
        <Link
          href="/forgot-password"
          className="-mt-1 inline-flex min-h-6 self-end items-center rounded px-1 text-xs font-medium text-primary underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t.forgotPassword}
        </Link>
        <input type="hidden" name="intent" value="login" />
        <SubmitButton>{t.signIn}</SubmitButton>
      </form>
    </section>
  );
}
