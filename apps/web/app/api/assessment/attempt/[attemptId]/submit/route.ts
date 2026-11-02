import { z } from "zod";

import type { components } from "@/lib/generated/api";
import { ASSESSMENT_SESSION_COOKIE } from "@/lib/assessment-session";
import { currentAssessmentToken } from "@/lib/public-assessment-session-request";
import {
  noStoreJson,
  publicAssessmentFetch,
  safeApiError,
  sameOriginRequest,
} from "@/lib/public-assessment-api";

type PublicAssessmentSubmissionPayload = {
  attempt: components["schemas"]["AssessmentAttemptOut"];
  result: components["schemas"]["PublicAssessmentResultOut"] & {
    needs_manual_review: boolean;
  };
};

export async function POST(
  request: Request,
  context: RouteContext<"/api/assessment/attempt/[attemptId]/submit">,
): Promise<Response> {
  if (!sameOriginRequest(request)) {
    return noStoreJson({ error: "Cross-origin assessment request rejected" }, 403);
  }
  const token = await currentAssessmentToken();
  if (!token) return noStoreJson({ error: "Assessment session expired" }, 401);
  const { attemptId } = await context.params;
  if (!z.uuid().safeParse(attemptId).success) {
    return noStoreJson({ error: "Invalid assessment attempt" }, 400);
  }

  const upstream = await publicAssessmentFetch(
    token,
    `/attempts/${encodeURIComponent(attemptId)}/submit`,
    { method: "POST" },
  );
  if (!upstream.ok) {
    return noStoreJson({ error: await safeApiError(upstream) }, upstream.status);
  }
  const submission = (await upstream.json()) as PublicAssessmentSubmissionPayload;
  const response = noStoreJson({
    attempt: submission.attempt,
    result: {
      attempt_id: submission.result.attempt_id,
      submitted_at: submission.result.submitted_at,
      percentage: submission.result.percentage,
      passed: submission.result.passed,
      needs_manual_review: submission.result.needs_manual_review,
    },
  });
  response.headers.append(
    "Set-Cookie",
    `${ASSESSMENT_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax${process.env.NODE_ENV === "production" ? "; Secure" : ""}`,
  );
  return response;
}
