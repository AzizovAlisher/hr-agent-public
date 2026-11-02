import { type NextRequest, NextResponse } from "next/server";

import { unauthenticatedAuthFetch } from "@/lib/auth-api";
import {
  API_SESSION_COOKIE,
  MFA_CHALLENGE_COOKIE,
  apiSessionCookieOptions,
  currentApiSession,
  mfaChallengeCookieOptions,
} from "@/lib/auth-session";

export const dynamic = "force-dynamic";

function forwardedValues(request: Request, name: string): string[] {
  return (request.headers.get(name) ?? "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
}

function isSameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  const fetchSite = request.headers.get("sec-fetch-site")?.toLowerCase();
  const fetchMode = request.headers.get("sec-fetch-mode")?.toLowerCase();
  // Some privacy-preserving browsers serialize same-origin form navigation as
  // Origin: null. Fetch Metadata is browser-controlled and still distinguishes
  // the legitimate navigation from a cross-site logout form.
  if (!origin || origin === "null") {
    return fetchSite === "same-origin" && fetchMode === "navigate";
  }
  try {
    const originUrl = new URL(origin);
    const requestUrl = new URL(request.url);
    const allowedHosts = new Set([
      requestUrl.host.toLowerCase(),
      request.headers.get("host")?.toLowerCase() ?? "",
      ...forwardedValues(request, "x-forwarded-host"),
    ]);
    const allowedProtocols = new Set([
      requestUrl.protocol.slice(0, -1).toLowerCase(),
      ...forwardedValues(request, "x-forwarded-proto"),
    ]);
    const originMatches =
      (originUrl.protocol === "https:" || originUrl.protocol === "http:") &&
      allowedHosts.has(originUrl.host.toLowerCase()) &&
      allowedProtocols.has(originUrl.protocol.slice(0, -1).toLowerCase());
    return (
      originMatches && fetchSite === "same-origin" && fetchMode === "navigate"
    );
  } catch {
    return false;
  }
}

function rejected(status: 400 | 403): Response {
  return Response.json(
    {
      error: {
        code: status === 403 ? "logout_origin_rejected" : "logout_scope_invalid",
        message: "The sign-out request was rejected.",
      },
    },
    {
      status,
      headers: {
        "Cache-Control": "private, no-store, max-age=0",
        Pragma: "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
      },
    },
  );
}

const AUTHJS_SESSION_COOKIE =
  /^(?:__Secure-|__Host-)?authjs\.session-token(?:\.\d+)?$/;

function expireCookie(
  response: NextResponse,
  name: string,
  options: {
    httpOnly: boolean;
    secure: boolean;
    sameSite: "lax" | "strict";
    path: string;
  },
): void {
  response.cookies.set(name, "", {
    ...options,
    maxAge: 0,
    expires: new Date(0),
  });
}

function signedOutResponse(
  request: NextRequest,
  revoked: boolean,
  scope: "current" | "all",
): NextResponse {
  const lang = request.cookies.get("locale")?.value === "ru" ? "ru" : "en";
  const copy =
    lang === "ru"
      ? {
          title:
            scope === "all" && revoked
              ? "Вы вышли на всех устройствах"
              : "Этот браузер вышел из системы",
          message: revoked
            ? "Локальные данные сеанса удалены. Теперь можно безопасно закрыть эту страницу."
            : "Локальные данные сеанса удалены, но сервер не подтвердил отзыв сеанса. Перед повторным входом при необходимости обратитесь к администратору.",
          action: "Вернуться ко входу",
        }
      : {
          title:
            scope === "all" && revoked
              ? "Signed out on every device"
              : "This browser is signed out",
          message: revoked
            ? "Local session data has been removed. You can safely close this page."
            : "Local session data has been removed, but the server could not confirm revocation. Contact your administrator if needed before signing in again.",
          action: "Return to sign in",
        };
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
      .brand { margin: 0 0 28px; color: #59616c; font-size: 14px; font-weight: 700; letter-spacing: .03em; }
      h1 { margin: 0; font-size: clamp(24px, 7vw, 32px); line-height: 1.15; }
      p { margin: 14px 0 0; color: #59616c; font-size: 15px; line-height: 1.6; }
      .status { display: inline-flex; align-items: center; gap: 8px; margin-top: 24px; color: #176b45; font-size: 13px; font-weight: 750; }
      .status::before { content: ""; width: 9px; height: 9px; border-radius: 999px; background: #23855b; }
      a { display: inline-flex; min-height: 44px; align-items: center; margin-top: 24px; color: #174ea6; font-weight: 650; text-underline-offset: 3px; }
      a:focus-visible { outline: 3px solid #1a73e8; outline-offset: 4px; border-radius: 4px; }
      @media (prefers-color-scheme: dark) {
        body { background: #111315; color: #f4f6f8; }
        main { background: #191c20; border-color: #343a42; box-shadow: none; }
        .brand, p { color: #b1b8c2; }
        .status { color: #83ddb7; }
        .status::before { background: #64d6a1; }
        a { color: #9fc4ff; }
      }
    </style>
  </head>
  <body>
    <main id="main">
      <p class="brand">Recruiter</p>
      <h1>${copy.title}</h1>
      <p>${copy.message}</p>
      <div class="status" role="status">${copy.title}</div><br>
      <a href="/login">${copy.action}</a>
    </main>
  </body>
</html>`;
  const response = new NextResponse(html, {
    status: 200,
    headers: {
      "Cache-Control": "private, no-store, max-age=0",
      "Clear-Site-Data": '"cache", "cookies", "storage"',
      "Content-Language": lang,
      "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
      "Content-Type": "text/html; charset=utf-8",
      "Cross-Origin-Opener-Policy": "same-origin",
      Pragma: "no-cache",
      "Referrer-Policy": "no-referrer",
      Vary: "Cookie",
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "X-Robots-Tag": "noindex, nofollow, noarchive",
    },
  });

  // This route is deliberately excluded from the Auth.js middleware wrapper.
  // Otherwise a concurrent RSC request can roll the JWT cookie after logout.
  // Clear-Site-Data is the browser-wide fence; explicit expirations also cover
  // clients that do not implement that response header.
  expireCookie(response, API_SESSION_COOKIE, apiSessionCookieOptions());
  expireCookie(response, MFA_CHALLENGE_COOKIE, mfaChallengeCookieOptions());
  for (const cookie of request.cookies.getAll()) {
    if (!AUTHJS_SESSION_COOKIE.test(cookie.name)) continue;
    expireCookie(response, cookie.name, {
      httpOnly: true,
      secure:
        process.env.NODE_ENV === "production" ||
        cookie.name.startsWith("__Secure-") ||
        cookie.name.startsWith("__Host-"),
      sameSite: "lax",
      path: "/",
    });
  }
  return response;
}

export async function POST(request: NextRequest): Promise<Response> {
  if (!isSameOrigin(request)) return rejected(403);

  const scope = new URL(request.url).searchParams.get("scope") ?? "current";
  if (scope !== "current" && scope !== "all") return rejected(400);

  const session = await currentApiSession();
  let revoked = false;
  if (session) {
    try {
      const response = await unauthenticatedAuthFetch(
        scope === "all" ? "/auth/logout-all" : "/auth/logout",
        {
          method: "POST",
          headers: { Authorization: `Bearer ${session.accessToken}` },
          signal: AbortSignal.timeout(3_000),
        },
      );
      revoked = response.ok;
      await response.body?.cancel();
    } catch {
      revoked = false;
    }
  }

  if (session && !revoked) {
    console.warn("Backend revocation was unavailable during local logout");
  }
  return signedOutResponse(request, revoked, scope);
}
