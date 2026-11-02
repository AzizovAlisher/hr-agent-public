import { unstable_rethrow } from "next/navigation";

import { apiFetch } from "@/lib/api-client";
import {
  privateAiError,
  privateAiHeaders,
} from "@/lib/ai/private-response";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  let body: string;
  try {
    body = await request.text();
  } catch {
    return privateAiError(400, "ai_invalid_request");
  }
  let upstream: Response;
  try {
    upstream = await apiFetch(
      `/ai/v1/sessions/${encodeURIComponent(id)}/messages`,
      {
        method: "POST",
        body,
        signal: request.signal,
      },
    );
  } catch (error) {
    unstable_rethrow(error);
    return privateAiError(502, "ai_gateway_unavailable");
  }

  if (!upstream.ok) {
    // Do not proxy provider/API bodies. Even a nominal problem response may
    // contain prompt fragments, filenames, or internal routing details.
    return privateAiError(upstream.status, `ai_upstream_${upstream.status}`);
  }

  const contentType = upstream.headers.get("content-type")?.toLowerCase() ?? "";
  const isStream = contentType.includes("text/event-stream");
  const isJson = contentType.includes("application/json");
  if (!upstream.body || (!isStream && !isJson)) {
    return privateAiError(502, "ai_invalid_upstream");
  }

  return new Response(upstream.body, {
    status: 200,
    headers: privateAiHeaders({
      contentType: isStream
        ? "text/event-stream; charset=utf-8"
        : "application/json; charset=utf-8",
      stream: isStream,
    }),
  });
}
