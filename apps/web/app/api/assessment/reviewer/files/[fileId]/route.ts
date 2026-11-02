import { z } from "zod";

import { apiFetch } from "@/lib/api-client";
import { getServerAccessToken } from "@/lib/api-client";

const BASE_HEADERS = {
  "Cache-Control": "private, no-store, max-age=0",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

export async function GET(
  _request: Request,
  context: RouteContext<"/api/assessment/reviewer/files/[fileId]">,
): Promise<Response> {
  if (!(await getServerAccessToken())) {
    return Response.json(
      { error: "Authentication required" },
      { status: 401, headers: BASE_HEADERS },
    );
  }
  const { fileId } = await context.params;
  if (!z.uuid().safeParse(fileId).success) {
    return Response.json(
      { error: "Invalid assessment file" },
      { status: 400, headers: BASE_HEADERS },
    );
  }
  const upstream = await apiFetch(
    `/assessment-files/${encodeURIComponent(fileId)}/download`,
  );
  if (!upstream.ok || !upstream.body) {
    return Response.json(
      {
        error:
          upstream.status === 403
            ? "You do not have permission to download this file"
            : "Assessment file is unavailable",
      },
      { status: upstream.status, headers: BASE_HEADERS },
    );
  }

  const headers = new Headers(BASE_HEADERS);
  for (const name of ["content-type", "content-disposition", "content-length"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(upstream.body, { status: 200, headers });
}
