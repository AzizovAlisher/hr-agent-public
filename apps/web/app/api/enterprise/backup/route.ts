import { NextResponse } from "next/server";

import { getServerAccessToken } from "@/lib/api-client";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function GET(): Promise<Response> {
  const token = await getServerAccessToken();
  if (!token) return new NextResponse("Unauthorized", { status: 401 });
  const upstream = await fetch(`${API_BASE_URL}/enterprise/backup`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (!upstream.ok || !upstream.body) {
    return new NextResponse(await upstream.text(), { status: upstream.status });
  }
  const headers = new Headers({ "Cache-Control": "no-store" });
  for (const name of ["content-type", "content-disposition", "content-length"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new NextResponse(upstream.body, { status: 200, headers });
}
