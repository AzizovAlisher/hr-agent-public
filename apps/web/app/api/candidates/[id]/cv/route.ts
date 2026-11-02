import { NextRequest, NextResponse } from "next/server";

import { getServerAccessToken } from "@/lib/api-client";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

/**
 * Authenticated CV download proxy.
 *
 * The browser can't send a bearer token via a plain <a download> click, and
 * we don't want to ship the bearer to the client. So this route runs
 * server-side: it pulls the session, calls the API with `Authorization`,
 * and streams the file back. Same auth boundary, no token leakage.
 */
export async function GET(
  _req: NextRequest,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await context.params;
  const token = await getServerAccessToken();
  if (!token) {
    return new NextResponse("Unauthorized", { status: 401 });
  }
  const upstream = await fetch(`${API_BASE_URL}/candidates/${encodeURIComponent(id)}/cv`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!upstream.ok || !upstream.body) {
    return new NextResponse(await upstream.text(), { status: upstream.status });
  }
  // Pass through the file body and the headers that matter for download.
  const headers = new Headers();
  for (const name of ["content-type", "content-disposition", "content-length"]) {
    const v = upstream.headers.get(name);
    if (v) headers.set(name, v);
  }
  return new NextResponse(upstream.body, { status: 200, headers });
}
