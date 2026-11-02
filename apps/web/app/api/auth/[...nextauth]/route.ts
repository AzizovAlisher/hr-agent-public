import type { NextRequest } from "next/server";

import { handlers } from "@/auth";

async function handleWithoutRollingSession(
  request: NextRequest,
  handler: (request: NextRequest) => Promise<Response>,
): Promise<Response> {
  const response = await handler(request);
  if (new URL(request.url).pathname !== "/api/auth/session") return response;

  // Session reads/updates are not used as an authorization boundary. Keep the
  // endpoint compatible for diagnostics, but never let a stale tab refresh the
  // identity cookie after local logout. The separately encrypted API session
  // remains the authoritative, revocable browser credential.
  const headers = new Headers(response.headers);
  headers.delete("set-cookie");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export function GET(request: NextRequest): Promise<Response> {
  return handleWithoutRollingSession(request, handlers.GET);
}

export function POST(request: NextRequest): Promise<Response> {
  return handleWithoutRollingSession(request, handlers.POST);
}
