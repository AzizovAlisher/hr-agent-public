import "server-only";

import { cookies } from "next/headers";

import {
  ASSESSMENT_SESSION_COOKIE,
  openAssessmentSession,
} from "@/lib/assessment-session";

export async function currentAssessmentToken(): Promise<string | null> {
  const cookieStore = await cookies();
  const session = openAssessmentSession(
    cookieStore.get(ASSESSMENT_SESSION_COOKIE)?.value,
  );
  return session?.token ?? null;
}
