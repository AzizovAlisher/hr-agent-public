import { ApplyLocaleSwitch } from "@/components/public/apply-locale-switch";
import { getLocale } from "@/lib/i18n";

const COPY = {
  en: {
    title: "Referral link unavailable",
    body: "This private referral link is invalid, expired, or has been revoked. Ask the person who shared it for a new link.",
    footer: "Candidate referrals · Recruiter",
  },
  ru: {
    title: "Реферальная ссылка недоступна",
    body: "Эта персональная ссылка недействительна, истекла или была отозвана. Запросите новую ссылку у отправителя.",
    footer: "Реферальная программа · Recruiter",
  },
} as const;

export default async function ReferralNotFound() {
  const locale = await getLocale();
  const copy = COPY[locale];

  return (
    <main className="min-h-dvh bg-bg">
      <div className="mx-auto flex max-w-[640px] flex-col gap-7 px-5 py-10 sm:py-14">
        <header className="flex items-center justify-between gap-3">
          <span
            aria-label="Recruiter"
            className="relative flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-text text-[12px] font-bold tracking-tight text-bg"
          >
            R
            <span className="absolute -right-[3px] -top-[3px] h-1.5 w-1.5 rounded-full bg-primary" />
          </span>
          <ApplyLocaleSwitch />
        </header>

        <section
          aria-labelledby="referral-not-found-title"
          className="flex flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-7 text-center sm:p-8"
        >
          <h1
            id="referral-not-found-title"
            className="text-[20px] font-semibold tracking-tight"
          >
            {copy.title}
          </h1>
          <p className="mx-auto max-w-md text-[14px] leading-6 text-text-muted">
            {copy.body}
          </p>
        </section>

        <footer className="text-center font-mono text-[10px] tracking-[0.04em] text-text-faint">
          {copy.footer}
        </footer>
      </div>
    </main>
  );
}
