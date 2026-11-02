import { currentAssessmentToken } from "@/lib/public-assessment-session-request";
import {
  noStoreJson,
  publicAssessmentFetch,
  safeApiError,
  sameOriginRequest,
} from "@/lib/public-assessment-api";

export async function POST(request: Request): Promise<Response> {
  if (!sameOriginRequest(request)) {
    return noStoreJson({ error: "Cross-origin assessment request rejected" }, 403);
  }
  const token = await currentAssessmentToken();
  if (!token) return noStoreJson({ error: "Assessment session expired" }, 401);

  const upstream = await publicAssessmentFetch(token, "/attempts", {
    method: "POST",
  });
  if (!upstream.ok) {
    return noStoreJson({ error: await safeApiError(upstream) }, upstream.status);
  }
  return noStoreJson(await upstream.json());
}
