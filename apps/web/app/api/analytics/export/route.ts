import { NextRequest, NextResponse } from "next/server";

import { getServerAccessToken } from "@/lib/api-client";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const ALLOWED_QUERY_KEYS = new Set([
  "start_at",
  "end_at",
  "start_date",
  "end_date",
  "timezone",
  "position_id",
  "source",
  "owner",
  "drilldown_dimension",
  "drilldown_key",
]);

export async function GET(request: NextRequest): Promise<Response> {
  const token = await getServerAccessToken();
  if (!token) return new NextResponse("Unauthorized", { status: 401 });

  const query = new URLSearchParams();
  for (const [key, value] of request.nextUrl.searchParams) {
    if (!ALLOWED_QUERY_KEYS.has(key)) continue;
    if (value.length > 512) {
      return new NextResponse("Invalid analytics export query", { status: 400 });
    }
    query.set(key, value);
  }
  query.set("format", "csv");
  const upstream = await fetch(
    `${API_BASE_URL}/analytics/pipeline?${query.toString()}`,
    {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    },
  );
  if (!upstream.ok || !upstream.body) {
    return new NextResponse(await upstream.text(), { status: upstream.status });
  }

  const headers = new Headers({
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
  });
  for (const name of ["content-type", "content-disposition", "content-length"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new NextResponse(upstream.body, { status: 200, headers });
}
