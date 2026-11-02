"use client";

import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { finishIntegrationOAuthAction } from "@/app/(app)/integrations/actions";
import { useI18n } from "@/components/i18n/locale-provider";

export function OAuthCallbackCompleter() {
  const { locale } = useI18n();
  const router = useRouter();
  const search = useSearchParams();
  const started = useRef(false);
  const [state, setState] = useState<"working" | "failed">("working");

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const oauthState = search.get("state") ?? "";
    const code = search.get("code") ?? "";
    // Authorization codes and one-time state values must not remain in browser
    // history, screenshots, referrers, or copied URLs while the exchange runs.
    window.history.replaceState(
      window.history.state,
      "",
      "/integrations/oauth/callback",
    );
    if (oauthState.length < 32 || oauthState.length > 512 || !code || code.length > 8192) {
      setState("failed");
      return;
    }
    void finishIntegrationOAuthAction(oauthState, code)
      .then((result) => {
        if (!result.ok) {
          setState("failed");
          return;
        }
        router.replace("/integrations?oauth=connected");
        router.refresh();
      })
      .catch(() => {
        setState("failed");
      });
  }, [router, search]);

  const ru = locale === "ru";
  return (
    <section className="w-full max-w-md rounded-xl border border-border bg-bg-elevated p-7 text-center">
      {state === "working" ? (
        <>
          <Loader2 className="mx-auto h-7 w-7 animate-spin text-accent" />
          <h1 className="mt-4 text-lg font-semibold">
            {ru ? "Завершаем подключение" : "Completing connection"}
          </h1>
          <p role="status" className="mt-2 text-sm text-text-muted">
            {ru ? "Проверяем одноразовый ответ провайдера…" : "Verifying the provider’s one-time response…"}
          </p>
        </>
      ) : (
        <>
          <XCircle className="mx-auto h-7 w-7 text-danger" />
          <h1 className="mt-4 text-lg font-semibold">
            {ru ? "Подключение не завершено" : "Connection was not completed"}
          </h1>
          <p role="alert" className="mt-2 text-sm text-text-muted">
            {ru
              ? "Повторите подключение со страницы интеграций. Если ошибка повторится, обратитесь к администратору."
              : "Try connecting again from Integrations. If the problem continues, contact your administrator."}
          </p>
          <button type="button" onClick={() => router.replace("/integrations")} className="mt-5 min-h-11 rounded-lg border border-border px-4 text-sm font-medium">
            {ru ? "Вернуться к интеграциям" : "Return to integrations"}
          </button>
        </>
      )}
      {state === "working" ? <CheckCircle2 className="sr-only" /> : null}
    </section>
  );
}
