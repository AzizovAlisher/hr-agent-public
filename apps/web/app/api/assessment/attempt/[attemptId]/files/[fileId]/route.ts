import { z } from "zod";

import { currentAssessmentToken } from "@/lib/public-assessment-session-request";
import {
  publicAssessmentFetch,
  safeApiError,
  sameOriginRequest,
} from "@/lib/public-assessment-api";

const NO_STORE_HEADERS = {
  "Cache-Control": "no-store, max-age=0",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

export async function DELETE(
  request: Request,
  context: RouteContext<"/api/assessment/attempt/[attemptId]/files/[fileId]">,
): Promise<Response> {
  if (!sameOriginRequest(request)) {
    return Response.json(
      { error: "Cross-origin assessment request rejected" },
      { status: 403, headers: NO_STORE_HEADERS },
    );
  }
  const token = await currentAssessmentToken();
  if (!token) {
    return Response.json(
      { error: "Assessment session expired" },
      { status: 401, headers: NO_STORE_HEADERS },
    );
  }
  const { attemptId, fileId } = await context.params;
  if (!z.uuid().safeParse(attemptId).success || !z.uuid().safeParse(fileId).success) {
    return Response.json(
      { error: "Invalid assessment file" },
      { status: 400, headers: NO_STORE_HEADERS },
    );
  }

  const upstream = await publicAssessmentFetch(
    token,
    `/attempts/${encodeURIComponent(attemptId)}/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  );
  if (!upstream.ok) {
    return Response.json(
      { error: await safeApiError(upstream) },
      { status: upstream.status, headers: NO_STORE_HEADERS },
    );
  }
  return new Response(null, { status: 204, headers: NO_STORE_HEADERS });
}
