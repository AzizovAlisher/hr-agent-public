"use client";

import { Check, Loader2, ShieldCheck, X } from "lucide-react";
import { useState, useTransition } from "react";

import { useI18n } from "@/components/i18n/locale-provider";
import { respondToPublicOffer } from "@/lib/public-offers";
import type { PublicOffer } from "@/lib/types";

function dateLabel(value: string, locale: string): string {
  return new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "long",
    timeStyle: value.includes("T") ? "short" : undefined,
  }).format(new Date(value));
}

export function PublicOfferResponse({
  token,
  initialOffer,
}: {
  token: string;
  initialOffer: PublicOffer;
}) {
  const { locale, m } = useI18n();
  const copy = m.offers.public;
  const [offer, setOffer] = useState(initialOffer);
  const [signature, setSignature] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function respond(response: "accept" | "decline") {
    if (pending) return;
    if (response === "accept" && !signature.trim()) {
      setError(copy.signatureHint);
      return;
    }
    if (!window.confirm(response === "accept" ? copy.acceptConfirm : copy.declineConfirm)) {
      return;
    }
    setError(null);
    startTransition(async () => {
      const result = await respondToPublicOffer(
        token,
        response,
        response === "accept" ? signature : null,
      );
      if (!result.ok) {
        setError(copy.responseFailed);
        return;
      }
      setOffer(result.data);
    });
  }

  const final = offer.status === "accepted" || offer.status === "declined";
  const accepted = offer.status === "accepted";

  return (
    <section className="overflow-hidden rounded-xl border border-border bg-bg-elevated">
      <header className="border-b border-border px-6 py-6 sm:px-8">
        <span className="font-mono text-[10px] uppercase tracking-[0.09em] text-text-faint">
          {copy.pageTitle}
        </span>
        <h1 className="mt-2 text-[23px] font-semibold tracking-[-0.02em] text-text">
          {copy.offerFor(offer.position_title)}
        </h1>
        <p className="mt-1 text-[12.5px] text-text-muted">
          {offer.organization_name}
        </p>
      </header>

      <div className="space-y-6 px-6 py-6 sm:px-8">
        <div className="grid gap-3 sm:grid-cols-3">
          {offer.salary_amount != null ? (
            <Fact
              label={copy.salary}
              value={`${offer.salary_amount} ${offer.salary_currency}`}
            />
          ) : null}
          {offer.start_date ? (
            <Fact label={copy.startDate} value={dateLabel(offer.start_date, locale)} />
          ) : null}
          {offer.expires_at ? (
            <Fact label={copy.expiresAt} value={dateLabel(offer.expires_at, locale)} />
          ) : null}
        </div>

        <article className="whitespace-pre-wrap rounded-lg border border-border bg-bg px-5 py-5 text-[13px] leading-7 text-text-muted">
          <h2 className="mb-3 text-[15px] font-semibold text-text">{offer.subject}</h2>
          {offer.body}
        </article>

        {final ? (
          <div
            className={`rounded-lg border px-5 py-6 text-center ${
              accepted
                ? "border-emerald-500/30 bg-emerald-500/10"
                : "border-border bg-bg"
            }`}
          >
            <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-bg-elevated">
              {accepted ? <Check className="h-5 w-5 text-emerald-500" /> : <X className="h-5 w-5 text-text-muted" />}
            </span>
            <h2 className="mt-3 text-[16px] font-semibold">
              {accepted ? copy.acceptedTitle : copy.declinedTitle}
            </h2>
            <p className="mx-auto mt-1 max-w-md text-[12.5px] leading-5 text-text-muted">
              {accepted ? copy.acceptedDesc : copy.declinedDesc}
            </p>
          </div>
        ) : (
          <div className="space-y-4 rounded-lg border border-border bg-bg px-5 py-5">
            <label className="block">
              <span className="text-[11px] font-medium text-text-muted">{copy.signature}</span>
              <input
                value={signature}
                onChange={(event) => setSignature(event.target.value)}
                autoComplete="name"
                maxLength={255}
                placeholder={copy.signaturePlaceholder}
                className="mt-1.5 block w-full rounded-[6px] border border-border bg-bg-elevated px-3 py-2.5 text-[13px] outline-none placeholder:text-text-faint focus:border-border-strong"
              />
              <span className="mt-1.5 block text-[10.5px] leading-4 text-text-faint">
                {copy.signatureHint}
              </span>
            </label>
            {error ? <p role="alert" className="text-[12px] text-red-500">{error}</p> : null}
            <div className="flex flex-col gap-2 sm:flex-row">
              <button
                type="button"
                disabled={pending || !signature.trim()}
                onClick={() => respond("accept")}
                className="inline-flex min-h-10 flex-1 items-center justify-center gap-2 rounded-[6px] bg-primary px-4 text-[12.5px] font-semibold text-primary-foreground disabled:opacity-50"
              >
                {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                {copy.accept}
              </button>
              <button
                type="button"
                disabled={pending}
                onClick={() => respond("decline")}
                className="inline-flex min-h-10 flex-1 items-center justify-center gap-2 rounded-[6px] border border-border px-4 text-[12.5px] font-medium text-text-muted hover:bg-bg-hover disabled:opacity-50"
              >
                <X className="h-4 w-4" />
                {copy.decline}
              </button>
            </div>
          </div>
        )}

        <p className="flex items-start gap-2 text-[10.5px] leading-4 text-text-faint">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {copy.legal}
        </p>
      </div>
    </section>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg px-3.5 py-3">
      <div className="text-[9.5px] font-medium uppercase tracking-[0.07em] text-text-faint">{label}</div>
      <div className="mt-1 text-[12px] font-medium text-text">{value}</div>
    </div>
  );
}
