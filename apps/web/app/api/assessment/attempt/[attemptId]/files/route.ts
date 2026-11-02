import { z } from "zod";

import { currentAssessmentToken } from "@/lib/public-assessment-session-request";
import {
  noStoreJson,
  publicAssessmentFetch,
  safeApiError,
  sameOriginRequest,
} from "@/lib/public-assessment-api";

const MAX_FILE_BYTES = 15 * 1024 * 1024;

export async function POST(
  request: Request,
  context: RouteContext<"/api/assessment/attempt/[attemptId]/files">,
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

  let incoming: FormData;
  try {
    incoming = await request.formData();
  } catch {
    return noStoreJson({ error: "Invalid file upload" }, 400);
  }
  const questionId = String(incoming.get("question_id") ?? "");
  const file = incoming.get("file");
  if (!z.uuid().safeParse(questionId).success || !(file instanceof File)) {
    return noStoreJson({ error: "Question and file are required" }, 422);
  }
  if (file.size <= 0 || file.size > MAX_FILE_BYTES) {
    return noStoreJson({ error: "File must be between 1 byte and 15 MiB" }, 413);
  }

  const body = new FormData();
  body.append("file", file, file.name);
  const upstream = await publicAssessmentFetch(
    token,
    `/attempts/${encodeURIComponent(attemptId)}/questions/${encodeURIComponent(questionId)}/files`,
    { method: "POST", body },
  );
  if (!upstream.ok) {
    return noStoreJson({ error: await safeApiError(upstream) }, upstream.status);
  }
  return noStoreJson(await upstream.json(), 201);
}
