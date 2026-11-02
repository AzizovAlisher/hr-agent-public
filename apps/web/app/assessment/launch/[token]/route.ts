import { NextResponse, type NextRequest } from "next/server";

import type { components } from "@/lib/generated/api";
import {
  ASSESSMENT_SESSION_COOKIE,
  sealAssessmentSession,
} from "@/lib/assessment-session";
import { publicAssessmentFetch } from "@/lib/public-assessment-api";

type PublicAssessmentLaunch = components["schemas"]["PublicAssessmentLaunch"];

const UNAVAILABLE_COPY = {
  en: {
    title: "Assessment unavailable",
    invalid: "This assessment link is invalid.",
    expired: "This assessment invitation has expired.",
    unavailable: "The assessment service is temporarily unavailable.",
    hint: "If you believe this is a mistake, request a new link from the hiring team.",
  },
  ru: {
    title: "Оценивание недоступно",
    invalid: "Ссылка на оценивание недействительна.",
    expired: "Срок действия приглашения истёк.",
    unavailable: "Сервис оценивания временно недоступен.",
    hint: "Если это ошибка, запросите новую ссылку у команды найма.",
  },
} as const;

function setLanguageCookie(response: NextResponse, lang: "en" | "ru") {
  response.cookies.set("locale", lang, {
    path: "/",
    maxAge: 60 * 60 * 24 * 365,
    sameSite: "lax",
  });
}

function unavailableResponse(
  token: string,
  lang: "en" | "ru",
  reason: "invalid" | "expired" | "unavailable",
  statusCode: 404 | 410 | 503,
): NextResponse {
  const copy = UNAVAILABLE_COPY[lang];
  const otherLanguage = lang === "en" ? "ru" : "en";
  const otherLabel = otherLanguage.toUpperCase();
  const retryPath = `/assessment/launch/${encodeURIComponent(token)}?lang=${otherLanguage}`;
  const html = `<!doctype html>
<html lang="${lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive">
    <meta name="referrer" content="no-referrer">
    <title>${copy.title} · Recruiter</title>
    <style>
      :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
      * { box-sizing: border-box; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f6f7f9; color: #17191c; }
      main { width: min(100%, 520px); padding: 32px; border: 1px solid #dfe3e8; border-radius: 16px; background: #fff; box-shadow: 0 16px 48px rgba(20, 24, 31, .08); }
      header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 24px; }
      .brand { font-size: 14px; font-weight: 700; letter-spacing: .03em; }
      h1 { margin: 0; font-size: clamp(24px, 7vw, 32px); line-height: 1.15; }
      p { margin: 14px 0 0; color: #59616c; font-size: 15px; line-height: 1.6; }
      a { display: inline-flex; min-height: 44px; align-items: center; border: 1px solid #cfd5dc; border-radius: 8px; padding: 8px 12px; color: #174ea6; font-weight: 700; }
      a:focus-visible { outline: 3px solid #1a73e8; outline-offset: 4px; }
      @media (prefers-color-scheme: dark) {
        body { background: #111315; color: #f4f6f8; }
        main { background: #191c20; border-color: #343a42; box-shadow: none; }
        p { color: #b1b8c2; }
        a { border-color: #4b535e; color: #9fc4ff; }
      }
    </style>
  </head>
  <body>
    <main>
      <header><span class="brand">Recruiter</span><a href="${retryPath}" hreflang="${otherLanguage}">${otherLabel}</a></header>
      <h1>${copy.title}</h1>
      <p>${copy[reason]}</p>
      <p>${copy.hint}</p>
    </main>
  </body>
</html>`;
  const response = new NextResponse(html, {
    status: statusCode,
    headers: {
      "Cache-Control": "private, no-store, max-age=0",
      "Content-Language": lang,
      "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
      "Content-Type": "text/html; charset=utf-8",
      "Referrer-Policy": "no-referrer",
      Vary: "Cookie",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "X-Robots-Tag": "noindex, nofollow, noarchive",
    },
  });
  response.cookies.delete(ASSESSMENT_SESSION_COOKIE);
  setLanguageCookie(response, lang);
  return response;
}

export async function GET(
  request: NextRequest,
  context: RouteContext<"/assessment/launch/[token]">,
): Promise<Response> {
  const { token } = await context.params;
  const requestedLanguage = request.nextUrl.searchParams.get("lang");
  const lang = requestedLanguage === "ru" ? "ru" : "en";
  const destination = new URL("/assessment", request.url);
  destination.searchParams.set("lang", lang);

  const upstream = await publicAssessmentFetch(token);
  if (!upstream.ok) {
    return upstream.status === 410
      ? unavailableResponse(token, lang, "expired", 410)
      : upstream.status === 404
        ? unavailableResponse(token, lang, "invalid", 404)
        : unavailableResponse(token, lang, "unavailable", 503);
  }

  const launch = (await upstream.json()) as PublicAssessmentLaunch;
  const expiresAt = new Date(launch.expires_at);
  if (!Number.isFinite(expiresAt.getTime()) || expiresAt.getTime() <= Date.now()) {
    return unavailableResponse(token, lang, "expired", 410);
  }

  const response = NextResponse.redirect(destination, 303);
  response.cookies.set({
    name: ASSESSMENT_SESSION_COOKIE,
    value: sealAssessmentSession(token, expiresAt),
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    expires: expiresAt,
  });
  setLanguageCookie(response, lang);
  response.headers.set("Cache-Control", "no-store, max-age=0");
  response.headers.set("Pragma", "no-cache");
  response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}
