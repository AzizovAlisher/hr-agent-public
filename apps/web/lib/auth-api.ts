import "server-only";

import { createHash } from "node:crypto";

import type { components } from "@/lib/generated/api";
import {
  apiSessionFromLogin,
  type ApiSession,
} from "@/lib/auth-session";

type LoginResponse = components["schemas"]["LoginResponse"];

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const REFRESH_EARLY_MS = 45_000;

const globalRefreshFlights = globalThis as typeof globalThis & {
  __hrAuthRefreshFlights?: Map<string, Promise<ApiSession>>;
};
const refreshFlights =
  globalRefreshFlights.__hrAuthRefreshFlights ??
  new Map<string, Promise<ApiSession>>();
globalRefreshFlights.__hrAuthRefreshFlights = refreshFlights;

export class AuthRefreshError extends Error {
  readonly terminal: boolean;

  constructor(terminal = false) {
    super("Authentication session could not be refreshed");
    this.name = "AuthRefreshError";
    this.terminal = terminal;
  }
}

export function apiSessionNeedsRefresh(session: ApiSession): boolean {
  return session.accessExpiresAt <= Date.now() + REFRESH_EARLY_MS;
}

function refreshKey(refreshToken: string): string {
  return createHash("sha256").update(refreshToken).digest("base64url");
}

async function performRefresh(session: ApiSession): Promise<ApiSession> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL.replace(/\/$/, "")}/auth/refresh`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ refresh_token: session.refreshToken }),
      cache: "no-store",
    });
  } catch {
    throw new AuthRefreshError();
  }
  if (!response.ok) throw new AuthRefreshError(response.status === 401);
  const data = (await response.json()) as LoginResponse;
  const refreshed = apiSessionFromLogin(data, session.email);
  if (
    !refreshed ||
    refreshed.id !== session.id ||
    refreshed.organization.id !== session.organization.id
  ) {
    throw new AuthRefreshError(true);
  }
  return refreshed;
}

export async function refreshApiSession(session: ApiSession): Promise<ApiSession> {
  if (!apiSessionNeedsRefresh(session)) return session;
  const key = refreshKey(session.refreshToken);
  const existing = refreshFlights.get(key);
  if (existing) return existing;

  // Coalesce only requests that are genuinely concurrent. The cleanup runs
  // before the wrapped promise settles for callers, so replaying an old,
  // already-rotated cookie can never receive refreshed credentials from a
  // completed in-process cache entry.
  let flight: Promise<ApiSession>;
  flight = performRefresh(session).finally(() => {
    if (refreshFlights.get(key) === flight) refreshFlights.delete(key);
  });
  refreshFlights.set(key, flight);
  return flight;
}

export async function unauthenticatedAuthFetch(
  path: string,
  init: RequestInit,
): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${API_BASE_URL.replace(/\/$/, "")}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
}
