import "server-only";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function publicAssessmentFetch(
  token: string,
  suffix = "",
  init: RequestInit = {},
): Promise<Response> {
  if (!/^asm_[A-Za-z0-9_-]{20,220}$/.test(token)) {
    return Response.json({ detail: "Assessment invitation not found" }, { status: 404 });
  }
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  return fetch(
    `${API_BASE_URL.replace(/\/$/, "")}/public/assessments/${encodeURIComponent(token)}${suffix}`,
    {
      ...init,
      headers,
      cache: "no-store",
    },
  );
}

export async function safeApiError(response: Response): Promise<string> {
  if (response.status === 401) return "Assessment session expired";
  if (response.status === 403) return "Assessment action is not allowed";
  if (response.status === 404) return "Assessment invitation was not found";
  if (response.status === 409) return "Assessment state changed; reload and try again";
  if (response.status === 410) return "Assessment invitation has expired";
  if (response.status === 413) return "Assessment upload exceeds the allowed limit";
  if (response.status === 422) return "Assessment answers or file are invalid";
  if (response.status === 429) return "Too many requests; wait and try again";
  return "Assessment service is temporarily unavailable";
}

export function noStoreJson(body: unknown, status = 200): Response {
  return Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store, max-age=0",
      Pragma: "no-cache",
      "Referrer-Policy": "no-referrer",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

export function sameOriginRequest(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return true;
  try {
    const originUrl = new URL(origin);
    const configuredOrigin =
      process.env.PUBLIC_APP_ORIGIN ??
      process.env.AUTH_URL ??
      process.env.NEXTAUTH_URL;
    const expectedUrl = new URL(configuredOrigin ?? request.url);
    return originUrl.origin === expectedUrl.origin;
  } catch {
    return false;
  }
}
