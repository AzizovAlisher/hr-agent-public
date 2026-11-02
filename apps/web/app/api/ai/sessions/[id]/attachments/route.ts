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
  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    return privateAiError(400, "ai_attachment_invalid_request");
  }
  let upstream: Response;
  try {
    upstream = await apiFetch(
      `/ai/v1/sessions/${encodeURIComponent(id)}/attachments`,
      {
        method: "POST",
        body: formData,
        signal: request.signal,
      },
    );
  } catch (error) {
    unstable_rethrow(error);
    return privateAiError(502, "ai_attachment_gateway_unavailable");
  }
  if (!upstream.ok) {
    return privateAiError(
      upstream.status,
      `ai_attachment_upstream_${upstream.status}`,
    );
  }
  if (
    !upstream.body ||
    !upstream.headers.get("content-type")?.toLowerCase().includes("application/json")
  ) {
    return privateAiError(502, "ai_attachment_invalid_upstream");
  }
  return new Response(upstream.body, {
    status: 200,
    headers: privateAiHeaders({ contentType: "application/json; charset=utf-8" }),
  });
}
