import { getToken, type JWT } from "next-auth/jwt";
import { type NextRequest, NextResponse } from "next/server";

import { AuthRefreshError, refreshApiSession } from "@/lib/auth-api";
import {
  API_SESSION_COOKIE,
  INTERNAL_ACCESS_HEADER,
  apiSessionCookieOptions,
  openApiSession,
  sealApiSession,
} from "@/lib/auth-session";
import { previewUserAccess } from "@/lib/user-access";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

type PublicErrorKind =
  | "apply_missing"
  | "referral_missing"
  | "referral_gone"
  | "shortlist_missing"
  | "shortlist_gone"
  | "schedule_missing"
  | "schedule_gone"
  | "schedule_manage_missing"
  | "schedule_manage_gone"
  | "offer_missing"
  | "offer_gone"
  | "unavailable";

type CapabilityPreview = {
  ok: boolean;
  status: number;
  code: string | null;
};

type PermissionPreview = "allowed" | "denied" | "unavailable";

const PUBLIC_ERROR_COPY = {
  en: {
    apply_missing: {
      title: "This position is no longer accepting applications",
      message:
        "It may have been closed or the link is outdated. Contact the recruiter if you are interested in similar roles.",
    },
    referral_missing: {
      title: "Referral link unavailable",
      message:
        "This private referral link is invalid, expired, or revoked. Ask the sender for a new link.",
    },
    referral_gone: {
      title: "Referral link unavailable",
      message:
        "This private referral link has expired or reached its usage limit. Ask the sender for a new link.",
    },
    shortlist_missing: {
      title: "Shortlist unavailable",
      message:
        "This private shortlist link is invalid or has been revoked. Ask the sender for a new link.",
    },
    shortlist_gone: {
      title: "Shortlist unavailable",
      message:
        "This private shortlist link has expired. Ask the sender for a new link.",
    },
    schedule_missing: {
      title: "Scheduling link unavailable",
      message:
        "This private scheduling link is invalid or has been revoked. Ask the hiring team for a new link.",
    },
    schedule_gone: {
      title: "Scheduling link unavailable",
      message:
        "This scheduling link has expired or has already been used. Ask the hiring team for a new link.",
    },
    schedule_manage_missing: {
      title: "Appointment link unavailable",
      message:
        "This private appointment link is invalid or has been revoked. Ask the hiring team for a new link.",
    },
    schedule_manage_gone: {
      title: "Appointment link unavailable",
      message:
        "This private appointment link has expired. Ask the hiring team for a new link.",
    },
    offer_missing: {
      title: "Offer unavailable",
      message:
        "This private offer link is invalid or the offer has been withdrawn. Contact the hiring team if you need help.",
    },
    offer_gone: {
      title: "Offer unavailable",
      message:
        "This private offer link has expired. Contact the hiring team if you need help.",
    },
    unavailable: {
      title: "Service temporarily unavailable",
      message: "Please try again in a few minutes.",
    },
    back: "Return to sign in",
  },
  ru: {
    apply_missing: {
      title: "Эта вакансия больше не принимает отклики",
      message:
        "Возможно, вакансия закрыта или ссылка устарела. Свяжитесь с рекрутером, если вас интересуют похожие роли.",
    },
    referral_missing: {
      title: "Реферальная ссылка недоступна",
      message:
        "Эта персональная ссылка недействительна, истекла или была отозвана. Запросите новую ссылку у отправителя.",
    },
    referral_gone: {
      title: "Реферальная ссылка недоступна",
      message:
        "Срок действия ссылки истёк или лимит использований исчерпан. Запросите новую ссылку у отправителя.",
    },
    shortlist_missing: {
      title: "Шорт-лист недоступен",
      message:
        "Эта персональная ссылка недействительна или была отозвана. Запросите новую ссылку у отправителя.",
    },
    shortlist_gone: {
      title: "Шорт-лист недоступен",
      message:
        "Срок действия этой персональной ссылки истёк. Запросите новую ссылку у отправителя.",
    },
    schedule_missing: {
      title: "Ссылка для записи недоступна",
      message:
        "Эта персональная ссылка недействительна или была отозвана. Запросите новую ссылку у команды найма.",
    },
    schedule_gone: {
      title: "Ссылка для записи недоступна",
      message:
        "Срок действия ссылки истёк или она уже была использована. Запросите новую ссылку у команды найма.",
    },
    schedule_manage_missing: {
      title: "Ссылка на встречу недоступна",
      message:
        "Эта персональная ссылка недействительна или была отозвана. Запросите новую ссылку у команды найма.",
    },
    schedule_manage_gone: {
      title: "Ссылка на встречу недоступна",
      message:
        "Срок действия этой персональной ссылки истёк. Запросите новую ссылку у команды найма.",
    },
    offer_missing: {
      title: "Оффер недоступен",
      message:
        "Эта персональная ссылка недействительна или оффер был отозван. Свяжитесь с командой найма, если вам нужна помощь.",
    },
    offer_gone: {
      title: "Оффер недоступен",
      message:
        "Срок действия этой персональной ссылки истёк. Свяжитесь с командой найма, если вам нужна помощь.",
    },
    unavailable: {
      title: "Сервис временно недоступен",
      message: "Повторите попытку через несколько минут.",
    },
    back: "Вернуться ко входу",
  },
} as const;

function publicErrorResponse(
  req: Pick<NextRequest, "cookies">,
  kind: PublicErrorKind,
  statusCode: 404 | 410 | 503,
): NextResponse {
  const lang = req.cookies.get("locale")?.value === "ru" ? "ru" : "en";
  const copy = PUBLIC_ERROR_COPY[lang];
  const error = copy[kind];
  const html = `<!doctype html>
<html lang="${lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive">
    <meta name="referrer" content="no-referrer">
    <title>${error.title} · Recruiter</title>
    <style>
      :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
      * { box-sizing: border-box; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f6f7f9; color: #17191c; }
      main { width: min(100%, 520px); padding: 32px; border: 1px solid #dfe3e8; border-radius: 16px; background: #fff; box-shadow: 0 16px 48px rgba(20, 24, 31, .08); }
      .brand { margin: 0 0 28px; color: #59616c; font-size: 14px; font-weight: 700; letter-spacing: .03em; }
      h1 { margin: 0; font-size: clamp(24px, 7vw, 32px); line-height: 1.15; }
      p { margin: 14px 0 0; color: #59616c; font-size: 15px; line-height: 1.6; }
      .status { display: inline-block; margin-top: 24px; padding: 6px 10px; border-radius: 999px; background: #fff0f0; color: #a61b1b; font-size: 13px; font-weight: 700; }
      a { display: inline-flex; min-height: 44px; align-items: center; margin-top: 24px; color: #174ea6; font-weight: 650; text-underline-offset: 3px; }
      a:focus-visible { outline: 3px solid #1a73e8; outline-offset: 4px; border-radius: 4px; }
      @media (prefers-color-scheme: dark) {
        body { background: #111315; color: #f4f6f8; }
        main { background: #191c20; border-color: #343a42; box-shadow: none; }
        .brand, p { color: #b1b8c2; }
        .status { background: #441c1c; color: #ffb4b4; }
        a { color: #9fc4ff; }
      }
    </style>
  </head>
  <body>
    <main id="main">
      <p class="brand">Recruiter</p>
      <h1>${error.title}</h1>
      <p>${error.message}</p>
      <span class="status" role="status">${statusCode}</span><br>
      <a href="/login">${copy.back}</a>
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
  if (statusCode === 503) response.headers.set("Retry-After", "30");
  return response;
}

function protectedRouteErrorResponse(
  req: Pick<NextRequest, "cookies">,
  statusCode: 404 | 503,
): NextResponse {
  const lang = req.cookies.get("locale")?.value === "ru" ? "ru" : "en";
  const unavailable = statusCode === 503;
  const title = unavailable
    ? lang === "ru"
      ? "Сервис временно недоступен"
      : "Service temporarily unavailable"
    : lang === "ru"
      ? "Страница не найдена"
      : "Page not found";
  const message = unavailable
    ? lang === "ru"
      ? "Не удалось проверить доступ. Повторите попытку через несколько минут."
      : "Access could not be verified. Please try again in a few minutes."
    : lang === "ru"
      ? "Страница не существует или недоступна для вашей учётной записи."
      : "This page does not exist or is unavailable to your account.";
  const back = lang === "ru" ? "Вернуться к позициям" : "Return to positions";
  const html = `<!doctype html><html lang="${lang}"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">
    <title>${statusCode} · Recruiter</title><style>
      :root{color-scheme:light dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif}*{box-sizing:border-box}
      body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#f6f7f9;color:#17191c}
      main{width:min(100%,520px);padding:32px;border:1px solid #dfe3e8;border-radius:16px;background:#fff}
      .status{font-size:14px;font-weight:750;color:#59616c}h1{margin:12px 0 0;font-size:clamp(24px,7vw,32px);line-height:1.15}
      p{margin:14px 0 0;color:#59616c;font-size:15px;line-height:1.6}a{display:inline-flex;min-height:44px;align-items:center;margin-top:24px;color:#174ea6;font-weight:650;text-underline-offset:3px}
      a:focus-visible{outline:3px solid #1a73e8;outline-offset:4px;border-radius:4px}@media(prefers-color-scheme:dark){body{background:#111315;color:#f4f6f8}main{background:#191c20;border-color:#343a42}.status,p{color:#b1b8c2}a{color:#9fc4ff}}
    </style></head><body><main id="main"><div class="status" role="status">${statusCode}</div><h1>${title}</h1><p>${message}</p><a href="/dashboard">${back}</a></main></body></html>`;
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
  if (unavailable) response.headers.set("Retry-After", "30");
  return response;
}

async function previewPublicPosition(positionId: string): Promise<Response> {
  return fetch(
    `${API_BASE_URL}/public/positions/${encodeURIComponent(positionId)}`,
    {
      method: "HEAD",
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(2_500),
    },
  );
}

async function resolveReferral(token: string): Promise<Response> {
  return fetch(
    `${API_BASE_URL}/public/referrals/${encodeURIComponent(token)}`,
    {
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(2_500),
    },
  );
}

async function upstreamProblemCode(response: Response): Promise<string | null> {
  const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.includes("json")) return null;
  try {
    const payload = (await response.json()) as {
      code?: unknown;
      detail?: { code?: unknown } | unknown;
    };
    if (typeof payload.code === "string") return payload.code;
    if (
      payload.detail &&
      typeof payload.detail === "object" &&
      "code" in payload.detail &&
      typeof payload.detail.code === "string"
    ) {
      return payload.detail.code;
    }
  } catch {
    // An unparseable upstream error is handled as unavailable by the caller.
  }
  return null;
}

async function previewPublicCapability(apiPath: string): Promise<CapabilityPreview> {
  const response = await fetch(`${API_BASE_URL}${apiPath}`, {
    method: "GET",
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(2_500),
  });
  const code = response.ok ? null : await upstreamProblemCode(response);
  try {
    await response.body?.cancel();
  } catch {
    // The JSON error reader may already have consumed and closed the body.
  }
  return { ok: response.ok, status: response.status, code };
}

async function previewPermissions(
  accessToken: string,
  required: readonly string[],
): Promise<PermissionPreview> {
  try {
    const response = await fetch(`${API_BASE_URL}/enterprise/permissions/me`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(2_500),
    });
    if (response.status === 401 || response.status === 403) return "denied";
    if (!response.ok) return "unavailable";
    const payload = (await response.json()) as {
      permissions?: Record<string, unknown>;
    };
    if (!payload.permissions || typeof payload.permissions !== "object") {
      return "unavailable";
    }
    return required.every(
      (permission) => payload.permissions?.[permission] === true,
    )
      ? "allowed"
      : "denied";
  } catch {
    return "unavailable";
  }
}

function accessGoneResponse(
  req: Pick<NextRequest, "cookies">,
  purpose: "invitation" | "password_reset",
): NextResponse {
  const ru = req.cookies.get("locale")?.value === "ru";
  const invitation = purpose === "invitation";
  const lang = ru ? "ru" : "en";
  const title = invitation
    ? ru
      ? "Приглашение недоступно"
      : "Invitation unavailable"
    : ru
      ? "Сброс пароля недоступен"
      : "Password reset unavailable";
  const message = invitation
    ? ru
      ? "Ссылка истекла, была отозвана или уже использована. Запросите новое приглашение у администратора."
      : "This link expired, was revoked, or was already used. Ask an administrator for a new invitation."
    : ru
      ? "Ссылка истекла, была отозвана или уже использована. Запросите новую ссылку."
      : "This link expired, was revoked, or was already used. Request a new reset link.";
  const back = ru ? "Вернуться ко входу" : "Return to sign in";

  // A Next.js rewrite renders the intended page but replaces a custom 410
  // with 200. Returning a complete response here preserves the capability
  // token's HTTP semantics for browsers, crawlers, and intermediary caches.
  const html = `<!doctype html>
<html lang="${lang}">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive">
    <meta name="referrer" content="no-referrer">
    <title>${title} · Recruiter</title>
    <style>
      :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
      * { box-sizing: border-box; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; background: #f6f7f9; color: #17191c; }
      main { width: min(100%, 440px); padding: 32px; border: 1px solid #dfe3e8; border-radius: 16px; background: #fff; box-shadow: 0 16px 48px rgba(20, 24, 31, .08); }
      .brand { margin: 0 0 28px; color: #59616c; font-size: 14px; font-weight: 700; letter-spacing: .03em; }
      h1 { margin: 0; font-size: clamp(24px, 7vw, 32px); line-height: 1.15; }
      p { margin: 14px 0 0; color: #59616c; font-size: 15px; line-height: 1.6; }
      .status { display: inline-block; margin-top: 24px; padding: 6px 10px; border-radius: 999px; background: #fff0f0; color: #a61b1b; font-size: 13px; font-weight: 700; }
      a { display: inline-flex; min-height: 44px; align-items: center; margin-top: 28px; color: #174ea6; font-weight: 650; text-underline-offset: 3px; }
      a:focus-visible { outline: 3px solid #1a73e8; outline-offset: 4px; border-radius: 4px; }
      @media (prefers-color-scheme: dark) {
        body { background: #111315; color: #f4f6f8; }
        main { background: #191c20; border-color: #343a42; box-shadow: none; }
        .brand, p { color: #b1b8c2; }
        .status { background: #441c1c; color: #ffb4b4; }
        a { color: #9fc4ff; }
      }
    </style>
  </head>
  <body>
    <main id="main">
      <p class="brand">Recruiter</p>
      <h1>${title}</h1>
      <p>${message}</p>
      <span class="status" role="status">410</span><br>
      <a href="/login">${back}</a>
    </main>
  </body>
</html>`;

  return new NextResponse(html, {
    status: 410,
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
}

function isPublicPath(path: string): boolean {
  return (
    path === "/login" ||
    path === "/forgot-password" ||
    path === "/access-gone" ||
    path.startsWith("/invite/") ||
    path.startsWith("/reset-password/") ||
    path.startsWith("/api/auth") ||
    path.startsWith("/api/assessment/") ||
    path.startsWith("/api/public") ||
    path === "/assessment" ||
    path.startsWith("/assessment/") ||
    path.startsWith("/apply/") ||
    path.startsWith("/r/") ||
    path.startsWith("/shortlist/") ||
    path.startsWith("/schedule/") ||
    path.startsWith("/offer/") ||
    path.startsWith("/_next") ||
    path === "/favicon.ico"
  );
}

function isTypedApiPath(path: string): boolean {
  return path.startsWith("/api/chat/") || path.startsWith("/api/assessment/");
}

type BrowserIdentity = {
  id: string;
  organizationId: string;
};

function validUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function browserIdentity(token: JWT | null): BrowserIdentity | null {
  if (!token || !validUuid(token.sub)) return null;
  const organization = token.organization;
  if (
    !organization ||
    typeof organization !== "object" ||
    !("id" in organization) ||
    !validUuid(organization.id)
  ) {
    return null;
  }
  return { id: token.sub, organizationId: organization.id };
}

async function readBrowserIdentity(
  req: NextRequest,
): Promise<BrowserIdentity | null> {
  const secret = process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET;
  if (!secret) return null;
  const cookieNames = new Set(req.cookies.getAll().map((cookie) => cookie.name));
  const headers = new Headers(req.headers);
  // Auth.js identity is cookie-only. Never reinterpret the FastAPI bearer
  // token or a browser-supplied Authorization header as a web identity.
  headers.delete("authorization");
  for (const cookieName of [
    "__Secure-authjs.session-token",
    "authjs.session-token",
  ]) {
    if (
      ![...cookieNames].some(
        (name) => name === cookieName || name.startsWith(`${cookieName}.`),
      )
    ) {
      continue;
    }
    const token = await getToken({
      req: { headers },
      secret,
      cookieName,
      secureCookie: cookieName.startsWith("__Secure-"),
    });
    const identity = browserIdentity(token);
    if (identity) return identity;
  }
  return null;
}

export default async function proxy(req: NextRequest) {
  // Decode the encrypted identity without calling Auth.js' session action.
  // The Auth.js middleware wrapper re-signs its JWT on every request, which
  // lets an already-running RSC response recreate that cookie after logout.
  const identity = await readBrowserIdentity(req);
  const isAuthed = !!identity;
  const path = req.nextUrl.pathname;
  const forwardedHeaders = new Headers(req.headers);
  // A browser-supplied internal header must never survive the proxy boundary.
  forwardedHeaders.delete(INTERNAL_ACCESS_HEADER);

  const accessMatch = path.match(/^\/(invite|reset-password)\/([^/]+)$/);
  if (accessMatch) {
    const purpose = accessMatch[1] === "invite" ? "invitation" : "password_reset";
    const result = await previewUserAccess(purpose, accessMatch[2]);
    if (result.status === "gone") {
      return accessGoneResponse(req, purpose);
    }
  }
  const incomingApiCookie = req.cookies.get(API_SESSION_COOKIE)?.value;
  const hadApiCookie = !!incomingApiCookie;
  let apiSession = isAuthed ? openApiSession(incomingApiCookie) : null;
  let refreshed = false;
  let refreshUnavailable = false;

  if (
    apiSession &&
    identity &&
    (apiSession.id !== identity.id ||
      apiSession.organization.id !== identity.organizationId)
  ) {
    apiSession = null;
  }

  if (apiSession) {
    try {
      const nextSession = await refreshApiSession(apiSession);
      refreshed = nextSession.refreshToken !== apiSession.refreshToken;
      apiSession = nextSession;
      forwardedHeaders.set(INTERNAL_ACCESS_HEADER, apiSession.accessToken);
    } catch (error) {
      if (error instanceof AuthRefreshError && !error.terminal) {
        refreshUnavailable = true;
      } else {
        apiSession = null;
      }
    }
  }

  const nextResponse = () =>
    NextResponse.next({ request: { headers: forwardedHeaders } });
  const finalize = (response: NextResponse): NextResponse => {
    if (refreshed && apiSession) {
      response.cookies.set(
        API_SESSION_COOKIE,
        sealApiSession(apiSession),
        apiSessionCookieOptions(),
      );
    } else if (hadApiCookie && !apiSession && !refreshUnavailable) {
      response.cookies.set(API_SESSION_COOKIE, "", {
        ...apiSessionCookieOptions(),
        maxAge: 0,
        expires: new Date(0),
      });
    }
    return response;
  };

  // The chat gateway owns its authentication check so browser fetches receive
  // a typed 401 envelope instead of following an HTML login redirect.
  if (isTypedApiPath(path)) return finalize(nextResponse());

  const applyMatch = req.method === "GET" && path.match(/^\/apply\/([^/]+)$/);
  if (applyMatch) {
    try {
      const preview = await previewPublicPosition(applyMatch[1]);
      if (preview.status === 404 || preview.status === 422) {
        return finalize(publicErrorResponse(req, "apply_missing", 404));
      }
      if (!preview.ok) {
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
    } catch {
      return finalize(publicErrorResponse(req, "unavailable", 503));
    }
  }

  const referralMatch = req.method === "GET" && path.match(/^\/r\/([^/]+)$/);
  if (referralMatch) {
    try {
      const result = await resolveReferral(referralMatch[1]);
      if (result.status === 404) {
        return finalize(publicErrorResponse(req, "referral_missing", 404));
      }
      if (result.status === 410) {
        const code = await upstreamProblemCode(result);
        if (code === "referral_revoked") {
          return finalize(publicErrorResponse(req, "referral_missing", 404));
        }
        return finalize(publicErrorResponse(req, "referral_gone", 410));
      }
      if (!result.ok) {
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
      const payload = (await result.json()) as { apply_url?: unknown };
      if (typeof payload.apply_url !== "string") {
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
      const destination = new URL(payload.apply_url, req.url);
      if (
        destination.origin !== req.nextUrl.origin ||
        !destination.pathname.startsWith("/apply/")
      ) {
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
      return finalize(NextResponse.redirect(destination, 307));
    } catch {
      return finalize(publicErrorResponse(req, "unavailable", 503));
    }
  }

  const shortlistMatch =
    req.method === "GET" && path.match(/^\/shortlist\/([^/]+)$/);
  if (shortlistMatch) {
    try {
      const preview = await previewPublicCapability(
        `/public/shortlists/${encodeURIComponent(shortlistMatch[1])}`,
      );
      if (!preview.ok) {
        if (preview.status === 404) {
          return finalize(publicErrorResponse(req, "shortlist_missing", 404));
        }
        if (
          preview.status === 410 &&
          preview.code === "shortlist_expired"
        ) {
          return finalize(publicErrorResponse(req, "shortlist_gone", 410));
        }
        if (preview.status === 410) {
          return finalize(publicErrorResponse(req, "shortlist_missing", 404));
        }
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
    } catch {
      return finalize(publicErrorResponse(req, "unavailable", 503));
    }
  }

  const scheduleManageMatch =
    req.method === "GET" && path.match(/^\/schedule\/manage\/([^/]+)$/);
  if (scheduleManageMatch) {
    try {
      const preview = await previewPublicCapability(
        `/public/scheduling/manage/${encodeURIComponent(scheduleManageMatch[1])}`,
      );
      if (!preview.ok) {
        if (
          preview.status === 410 &&
          preview.code === "link_expired"
        ) {
          return finalize(
            publicErrorResponse(req, "schedule_manage_gone", 410),
          );
        }
        if (preview.status === 404 || preview.status === 410) {
          return finalize(
            publicErrorResponse(req, "schedule_manage_missing", 404),
          );
        }
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
    } catch {
      return finalize(publicErrorResponse(req, "unavailable", 503));
    }
  }

  const scheduleMatch =
    req.method === "GET" && path.match(/^\/schedule\/([^/]+)$/);
  if (scheduleMatch) {
    try {
      const preview = await previewPublicCapability(
        `/public/scheduling/${encodeURIComponent(scheduleMatch[1])}`,
      );
      if (!preview.ok) {
        if (
          (preview.status === 410 && preview.code === "link_expired") ||
          (preview.status === 409 && preview.code === "link_booked")
        ) {
          return finalize(publicErrorResponse(req, "schedule_gone", 410));
        }
        if (preview.status === 404 || preview.status === 410) {
          return finalize(publicErrorResponse(req, "schedule_missing", 404));
        }
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
    } catch {
      return finalize(publicErrorResponse(req, "unavailable", 503));
    }
  }

  const offerMatch = req.method === "GET" && path.match(/^\/offer\/([^/]+)$/);
  if (offerMatch) {
    try {
      const preview = await previewPublicCapability(
        `/public/offers/${encodeURIComponent(offerMatch[1])}`,
      );
      if (!preview.ok) {
        if (preview.status === 404) {
          return finalize(publicErrorResponse(req, "offer_missing", 404));
        }
        if (preview.status === 410) {
          return finalize(publicErrorResponse(req, "offer_gone", 410));
        }
        return finalize(publicErrorResponse(req, "unavailable", 503));
      }
    } catch {
      return finalize(publicErrorResponse(req, "unavailable", 503));
    }
  }

  // Public routes — anyone can hit these without an account.
  if (isPublicPath(path)) return finalize(nextResponse());

  if (!isAuthed || !apiSession || refreshUnavailable) {
    const loginUrl = new URL("/login", req.nextUrl);
    if (isAuthed && refreshUnavailable) loginUrl.searchParams.set("unavailable", "1");
    else if (isAuthed) loginUrl.searchParams.set("expired", "1");
    return finalize(NextResponse.redirect(loginUrl));
  }

  if (
    req.method === "GET" &&
    (path === "/positions/new" || path === "/positions/project")
  ) {
    const permission = await previewPermissions(apiSession.accessToken, [
      "candidates.write",
      "ai.use",
    ]);
    if (permission === "denied") {
      return finalize(protectedRouteErrorResponse(req, 404));
    }
    if (permission === "unavailable") {
      return finalize(protectedRouteErrorResponse(req, 503));
    }
  }

  return finalize(nextResponse());
}

export const config = {
  // Local logout must not pass through Auth.js' rolling-session middleware:
  // doing so can append a fresh identity cookie to the logout response.
  matcher: [
    "/((?!api/auth/local-logout(?:/|$)|_next/static|_next/image|favicon.ico).*)",
  ],
};
