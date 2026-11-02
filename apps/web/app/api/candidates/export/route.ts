import { NextRequest, NextResponse } from "next/server";

import { getServerAccessToken } from "@/lib/api-client";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export async function POST(request: NextRequest): Promise<Response> {
  const token = await getServerAccessToken();
  if (!token) return new NextResponse("Unauthorized", { status: 401 });

  let payload: {
    candidate_ids?: unknown;
    position_id?: unknown;
    include_contacts?: unknown;
  };
  try {
    payload = (await request.json()) as typeof payload;
  } catch {
    return new NextResponse("Invalid JSON", { status: 400 });
  }
  if (
    !Array.isArray(payload.candidate_ids) ||
    payload.candidate_ids.length < 1 ||
    payload.candidate_ids.length > 500 ||
    payload.candidate_ids.some((id) => typeof id !== "string")
  ) {
    return new NextResponse("Invalid candidate_ids", { status: 400 });
  }
  if (
    payload.position_id != null &&
    typeof payload.position_id !== "string"
  ) {
    return new NextResponse("Invalid position_id", { status: 400 });
  }
  if (
    payload.include_contacts != null &&
    typeof payload.include_contacts !== "boolean"
  ) {
    return new NextResponse("Invalid include_contacts", { status: 400 });
  }

  const exportRequest = {
    candidate_ids: payload.candidate_ids,
    position_id: payload.position_id ?? null,
    include_contacts: payload.include_contacts === true,
  };

  const upstream = await fetch(`${API_BASE_URL}/candidate-export`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(exportRequest),
    cache: "no-store",
  });
  if (!upstream.ok || !upstream.body) {
    return new NextResponse(await upstream.text(), { status: upstream.status });
  }

  const headers = new Headers();
  for (const name of ["content-type", "content-disposition", "content-length"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("Cache-Control", "private, no-store, max-age=0");
  headers.set("Referrer-Policy", "no-referrer");
  return new NextResponse(upstream.body, { status: 200, headers });
}
