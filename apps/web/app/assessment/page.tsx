import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import type { components } from "@/lib/generated/api";
import { PublicAssessment } from "@/components/assessments/public-assessment";
import { DocumentLanguage } from "@/components/i18n/document-language";
import { currentAssessmentToken } from "@/lib/public-assessment-session-request";
import { publicAssessmentFetch, safeApiError } from "@/lib/public-assessment-api";
import { getLocale } from "@/lib/i18n";

type PublicAssessmentLaunch = components["schemas"]["PublicAssessmentLaunch"];

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Assessment · Recruiter",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

const statusCopy = {
  en: {
    invalid: "This assessment link is invalid.",
    expired: "This assessment invitation has expired.",
    unavailable: "The assessment service is temporarily unavailable.",
    missing: "Open the private invitation link provided by the hiring team.",
    title: "Assessment unavailable",
    hint: "If you believe this is a mistake, request a new link from the hiring team.",
  },
  ru: {
    invalid: "Ссылка на оценивание недействительна.",
    expired: "Срок действия приглашения истёк.",
    unavailable: "Сервис оценивания временно недоступен.",
    missing: "Откройте персональную ссылку, которую прислала команда найма.",
    title: "Оценивание недоступно",
    hint: "Если это ошибка, запросите новую ссылку у команды найма.",
  },
} as const;

export default async function AssessmentPage({
  searchParams,
}: {
  searchParams: Promise<{ lang?: string; status?: string }>;
}) {
  const [query, locale] = await Promise.all([searchParams, getLocale()]);
  const requestedLanguage =
    query.lang === "en" || query.lang === "ru" ? query.lang : null;
  if (requestedLanguage && requestedLanguage !== locale) {
    const next = new URLSearchParams();
    if (
      query.status === "invalid" ||
      query.status === "expired" ||
      query.status === "unavailable"
    ) {
      next.set("status", query.status);
    }
    const suffix = next.size ? `?${next.toString()}` : "";
    redirect(`/assessment/language/${requestedLanguage}${suffix}`);
  }
  const language = requestedLanguage ?? locale;
  const copy = statusCopy[language];
  const token = await currentAssessmentToken();

  if (!token) {
    const status =
      query.status === "invalid" ||
      query.status === "expired" ||
      query.status === "unavailable"
        ? query.status
        : "missing";
    return (
      <>
        <DocumentLanguage language={language} />
        <Unavailable language={language} message={copy[status]} />
      </>
    );
  }

  const response = await publicAssessmentFetch(token);
  if (!response.ok) {
    const message =
      response.status === 410
        ? copy.expired
        : response.status === 404
          ? copy.invalid
          : await safeApiError(response);
    return (
      <>
        <DocumentLanguage language={language} />
        <Unavailable language={language} message={message} />
      </>
    );
  }

  const launch = (await response.json()) as PublicAssessmentLaunch;
  return (
    <>
      <DocumentLanguage language={language} />
      <PublicAssessment launch={launch} language={language} />
    </>
  );
}

function Unavailable({
  language,
  message,
}: {
  language: "en" | "ru";
  message: string;
}) {
  const copy = statusCopy[language];
  return (
    <main className="flex min-h-dvh items-center justify-center bg-background px-4 py-12">
      <section className="w-full max-w-lg rounded-2xl border border-border bg-bg-elevated p-6 shadow-xl sm:p-8">
        <div className="mb-5 flex items-center justify-between gap-4">
          <span className="text-sm font-semibold tracking-tight">Recruiter</span>
          <LanguageLinks language={language} />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">{copy.title}</h1>
        <p className="mt-3 text-base leading-7 text-text-muted">{message}</p>
        <p className="mt-2 text-sm leading-6 text-text-muted">{copy.hint}</p>
      </section>
    </main>
  );
}

function LanguageLinks({ language }: { language: "en" | "ru" }) {
  return (
    <nav aria-label="Language" className="flex rounded-lg border border-border p-0.5 text-sm">
      <Link
        href="/assessment/language/en"
        hrefLang="en"
        aria-current={language === "en" ? "page" : undefined}
        className={`min-h-9 rounded-md px-3 py-2 ${language === "en" ? "bg-bg-active text-text" : "text-text-muted"}`}
      >
        EN
      </Link>
      <Link
        href="/assessment/language/ru"
        hrefLang="ru"
        aria-current={language === "ru" ? "page" : undefined}
        className={`min-h-9 rounded-md px-3 py-2 ${language === "ru" ? "bg-bg-active text-text" : "text-text-muted"}`}
      >
        RU
      </Link>
    </nav>
  );
}
