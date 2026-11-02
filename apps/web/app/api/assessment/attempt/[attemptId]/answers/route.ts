import { z } from "zod";

import { currentAssessmentToken } from "@/lib/public-assessment-session-request";
import {
  noStoreJson,
  publicAssessmentFetch,
  safeApiError,
  sameOriginRequest,
} from "@/lib/public-assessment-api";

const answersSchema = z.object({
  answers: z
    .array(
      z.object({
        question_id: z.uuid(),
        value: z.unknown(),
      }),
    )
    .min(1)
    .max(200),
});

export async function PATCH(
  request: Request,
  context: RouteContext<"/api/assessment/attempt/[attemptId]/answers">,
): Promise<Response> {
  if (!sameOriginRequest(request)) {
    return noStoreJson({ error: "Cross-origin assessment request rejected" }, 403);
  }
  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(contentLength) && contentLength > 512_000) {
    return noStoreJson({ error: "Assessment answer payload is too large" }, 413);
  }
  const token = await currentAssessmentToken();
  if (!token) return noStoreJson({ error: "Assessment session expired" }, 401);

  const { attemptId } = await context.params;
  if (!z.uuid().safeParse(attemptId).success) {
    return noStoreJson({ error: "Invalid assessment attempt" }, 400);
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return noStoreJson({ error: "Invalid JSON request" }, 400);
  }
  const parsed = answersSchema.safeParse(body);
  if (!parsed.success) {
    return noStoreJson({ error: "Invalid assessment answers" }, 422);
  }

  const upstream = await publicAssessmentFetch(
    token,
    `/attempts/${encodeURIComponent(attemptId)}/answers`,
    { method: "PATCH", body: JSON.stringify(parsed.data) },
  );
  if (!upstream.ok) {
    return noStoreJson({ error: await safeApiError(upstream) }, upstream.status);
  }
  return noStoreJson(await upstream.json());
}
