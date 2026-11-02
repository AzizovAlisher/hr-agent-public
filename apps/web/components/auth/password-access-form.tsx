"use client";

import Link from "next/link";
import { CheckCircle2, Loader2 } from "lucide-react";
import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import type { PasswordAccessState } from "@/app/(auth)/access-actions";
import type { UserAccessPreview } from "@/lib/user-access";

/** @ui-only Password-access server-action adapter. */
type AccessAction = (
  previous: PasswordAccessState,
  formData: FormData,
) => Promise<PasswordAccessState>;

const COPY = {
  en: {
    email: "Account",
    organization: "Organization",
    expires: "Link expires",
    password: "New password",
    confirmation: "Confirm password",
    hint: "Use at least 8 characters. Long passphrases are recommended.",
    invitationSubmit: "Activate account",
    resetSubmit: "Set new password",
    working: "Saving…",
    invitationDone: "Your account is active. You can now sign in.",
    resetDone: "Your password was changed and all existing sessions were revoked.",
    signIn: "Continue to sign in",
    formInvalid: "Passwords must match and contain at least 8 characters.",
    invalid: "This private link is invalid.",
    gone: "This private link has expired, was revoked, or has already been used.",
    unavailable: "The account service is temporarily unavailable. Try again later.",
  },
  ru: {
    email: "Аккаунт",
    organization: "Организация",
    expires: "Ссылка действует до",
    password: "Новый пароль",
    confirmation: "Повторите пароль",
    hint: "Используйте не менее 8 символов. Рекомендуется длинная парольная фраза.",
    invitationSubmit: "Активировать аккаунт",
    resetSubmit: "Задать новый пароль",
    working: "Сохраняем…",
    invitationDone: "Аккаунт активирован. Теперь вы можете войти.",
    resetDone: "Пароль изменён, все прежние сессии завершены.",
    signIn: "Перейти ко входу",
    formInvalid: "Пароли должны совпадать и содержать не менее 8 символов.",
    invalid: "Эта персональная ссылка недействительна.",
    gone: "Ссылка истекла, была отозвана или уже использована.",
    unavailable: "Сервис аккаунтов временно недоступен. Попробуйте позже.",
  },
} as const;

export function PasswordAccessForm({
  mode,
  preview,
  locale,
  action,
}: {
  mode: "invitation" | "password_reset";
  preview: UserAccessPreview;
  locale: "en" | "ru";
  action: AccessAction;
}) {
  const copy = COPY[locale];
  const [state, formAction] = useActionState(action, { status: "idle" });

  if (state.status === "success") {
    return (
      <div role="status" className="rounded-xl border border-success/30 bg-success/10 p-5">
        <CheckCircle2 className="h-6 w-6 text-success" aria-hidden />
        <p className="mt-3 text-sm leading-6 text-text">
          {mode === "invitation" ? copy.invitationDone : copy.resetDone}
        </p>
        <Link
          href="/login"
          className="mt-5 inline-flex min-h-11 items-center justify-center rounded-lg bg-text px-4 py-2 text-sm font-semibold text-bg focus-visible:ring-4 focus-visible:ring-ring/40"
        >
          {copy.signIn}
        </Link>
      </div>
    );
  }

  const error =
    state.error === "form_invalid"
      ? copy.formInvalid
      : state.error === "invalid"
        ? copy.invalid
        : state.error === "gone"
          ? copy.gone
          : state.error === "unavailable"
            ? copy.unavailable
            : null;

  return (
    <>
      <dl className="mb-6 grid gap-2 rounded-xl border border-border bg-bg p-4 text-sm">
        <Info label={copy.email} value={preview.email} />
        <Info label={copy.organization} value={preview.organization_name} />
        <Info
          label={copy.expires}
          value={new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
            dateStyle: "medium",
            timeStyle: "short",
          }).format(new Date(preview.expires_at))}
        />
      </dl>
      <form action={formAction} className="flex flex-col gap-4">
        {error ? (
          <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}
        <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
          {copy.password}
          <input
            name="password"
            type="password"
            autoComplete="new-password"
            minLength={8}
            maxLength={512}
            required
            autoFocus
            className="min-h-11 rounded-lg border border-border bg-bg px-3 py-2.5 text-base text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
          {copy.confirmation}
          <input
            name="password_confirmation"
            type="password"
            autoComplete="new-password"
            minLength={8}
            maxLength={512}
            required
            className="min-h-11 rounded-lg border border-border bg-bg px-3 py-2.5 text-base text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30"
          />
        </label>
        <p className="text-xs leading-5 text-text-faint">{copy.hint}</p>
        <SubmitButton locale={locale}>
          {mode === "invitation" ? copy.invitationSubmit : copy.resetSubmit}
        </SubmitButton>
      </form>
    </>
  );
}

function SubmitButton({
  children,
  locale,
}: {
  children: React.ReactNode;
  locale: "en" | "ru";
}) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="flex min-h-11 items-center justify-center gap-2 rounded-lg bg-text px-4 py-2.5 text-sm font-semibold text-bg focus-visible:ring-4 focus-visible:ring-ring/40 disabled:opacity-60"
    >
      {pending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : null}
      {pending ? COPY[locale].working : children}
    </button>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1 sm:grid-cols-[110px_1fr]">
      <dt className="text-text-faint">{label}</dt>
      <dd className="break-words text-text">{value}</dd>
    </div>
  );
}
