"use client";

import Link from "next/link";
import { Loader2 } from "lucide-react";
import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import {
  requestPasswordResetAction,
  type ResetRequestState,
} from "@/app/(auth)/access-actions";

const COPY = {
  en: {
    email: "Work email",
    submit: "Request reset link",
    working: "Requesting…",
    accepted: "If an active account matches that address, a reset email has been queued. Check your inbox and spam folder.",
    error: "The request could not be completed. Check the address and try again.",
    back: "Back to sign in",
  },
  ru: {
    email: "Рабочий email",
    submit: "Запросить ссылку",
    working: "Отправляем…",
    accepted: "Если активный аккаунт с таким адресом существует, письмо поставлено в очередь. Проверьте входящие и папку «Спам».",
    error: "Не удалось выполнить запрос. Проверьте адрес и попробуйте снова.",
    back: "Вернуться ко входу",
  },
} as const;

export function ForgotPasswordForm({ locale }: { locale: "en" | "ru" }) {
  const copy = COPY[locale];
  const [state, action] = useActionState<ResetRequestState, FormData>(
    requestPasswordResetAction,
    { status: "idle" },
  );

  return (
    <div className="space-y-5">
      {state.status === "accepted" ? (
        <p role="status" className="rounded-lg border border-success/30 bg-success/10 p-4 text-sm leading-6 text-text">
          {copy.accepted}
        </p>
      ) : (
        <form action={action} className="flex flex-col gap-4">
          {state.status === "error" ? (
            <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
              {copy.error}
            </p>
          ) : null}
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text-muted">
            {copy.email}
            <input
              name="email"
              type="email"
              autoComplete="email"
              required
              autoFocus
              className="min-h-11 rounded-lg border border-border bg-bg px-3 py-2.5 text-base text-text outline-none focus:border-ring focus:ring-4 focus:ring-ring/30"
            />
          </label>
          <RequestButton locale={locale} />
        </form>
      )}
      <Link href="/login" className="inline-flex min-h-10 items-center rounded-md text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring">
        {copy.back}
      </Link>
    </div>
  );
}

function RequestButton({ locale }: { locale: "en" | "ru" }) {
  const { pending } = useFormStatus();
  const copy = COPY[locale];
  return (
    <button type="submit" disabled={pending} className="flex min-h-11 items-center justify-center gap-2 rounded-lg bg-text px-4 py-2.5 text-sm font-semibold text-bg disabled:opacity-60">
      {pending ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> : null}
      {pending ? copy.working : copy.submit}
    </button>
  );
}
