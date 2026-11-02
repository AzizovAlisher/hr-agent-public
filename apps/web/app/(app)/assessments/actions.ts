"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";

import { auth } from "@/auth";
import { apiFetch, type CursorPage } from "@/lib/api-client";
import { apiProblemMessage } from "@/lib/api-problem";
import type {
  ApiResponseSchema,
  ApiSchema,
} from "@/lib/generated/contracts";

type AssessmentTemplateCreate = ApiSchema<"AssessmentTemplateCreate">;
type AssessmentTemplateUpdate = ApiSchema<"AssessmentTemplateUpdate">;
type AssessmentTemplate = ApiResponseSchema<"AssessmentTemplateOut">;
type AssessmentVersion = ApiResponseSchema<"AssessmentVersionOut">;
type AssessmentInvitationCreate = ApiSchema<"AssessmentInvitationCreate">;
type AssessmentInvitation = ApiResponseSchema<"AssessmentInvitationOut">;
type AssessmentInvitationCreated =
  ApiResponseSchema<"AssessmentInvitationCreated">;
type AssessmentSubmission = ApiResponseSchema<"AssessmentSubmissionOut">;
type AssessmentAttemptSummary =
  ApiResponseSchema<"AssessmentAttemptSummaryOut">;
type IntegrationConnectionPage =
  ApiResponseSchema<"IntegrationConnectionPage">;
type IntegrationJob = ApiResponseSchema<"IntegrationJobOut">;
type ProviderAssessmentEvidence =
  ApiResponseSchema<"ProviderAssessmentApplicationEvidenceOut">;

export type AssessmentApplicationPage = ApiResponseSchema<"ApplicationPage">;

const APPLICATION_PAGE_SIZE = 200;

/** @ui-only Serializable result returned by a Next.js server action. */
export type AssessmentActionResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string };

const templateCreateSchema = z.object({
  name: z.string().trim().min(1).max(160),
  description: z.string().trim().max(10_000).nullable().optional(),
});

const templateUpdateSchema = z
  .object({
    name: z.string().trim().min(1).max(160).nullable().optional(),
    description: z.string().trim().max(10_000).nullable().optional(),
    status: z.enum(["active", "archived"]).nullable().optional(),
  })
  .refine((body) => Object.values(body).some((value) => value !== undefined), {
    message: "At least one change is required",
  });

const optionSchema = z.object({
  value: z.string().trim().min(1).max(160),
  label: z.string().trim().min(1).max(500),
});

const questionSchema = z.object({
  key: z.string().trim().min(1).max(64),
  question_type: z.enum([
    "boolean",
    "single",
    "multi",
    "number",
    "short",
    "long",
    "file",
    "url",
  ]),
  prompt: z.string().trim().min(1).max(10_000),
  description: z.string().trim().max(10_000).nullable().optional(),
  required: z.boolean(),
  options: z.array(optionSchema).max(100).optional(),
  validation: z.record(z.string(), z.unknown()).optional(),
  condition: z.record(z.string(), z.unknown()).nullable().optional(),
  scoring: z.record(z.string(), z.unknown()).nullable().optional(),
  knockout: z.record(z.string(), z.unknown()).nullable().optional(),
  max_score: z.union([z.number().nonnegative().max(1_000_000), z.string()]),
});

const versionCreateSchema = z.object({
  title: z.string().trim().min(1).max(200),
  instructions: z.string().trim().max(20_000).nullable().optional(),
  passing_percentage: z.union([z.number().min(0).max(100), z.string()]),
  time_limit_minutes: z.number().int().min(1).max(10_080).nullable().optional(),
  sections: z
    .array(
      z.object({
        title: z.string().trim().min(1).max(200),
        description: z.string().trim().max(10_000).nullable().optional(),
        questions: z.array(questionSchema).min(1).max(200),
      }),
    )
    .min(1)
    .max(50),
});

const invitationCreateSchema = z.object({
  template_version_id: z.uuid(),
  expires_at: z.iso.datetime({ offset: true }),
  max_attempts: z.number().int().min(1).max(20),
  recipient_email: z.email().nullable().optional(),
  accommodations: z
    .object({
      extra_time_minutes: z.number().int().min(0).max(1_440),
      breaks_allowed: z.boolean(),
      screen_reader_optimized: z.boolean(),
    })
    .optional(),
});

const invitationReissueSchema = z.object({
  expires_at: z.iso.datetime({ offset: true }),
});

const providerInvitationSchema = z.object({
  application_id: z.uuid(),
  template_version_id: z.uuid().nullable().optional(),
  external_template_ref: z.string().trim().min(1).max(255),
  expires_at: z.iso.datetime({ offset: true }),
  locale: z.enum(["ru", "en"]),
  accommodations: z.record(z.string(), z.union([z.boolean(), z.number().int(), z.string()])),
  idempotency_key: z.string().trim().min(1).max(160),
});

const providerPollSchema = z.object({
  provider_assessment_id: z.uuid(),
  idempotency_key: z.string().trim().min(1).max(160),
});

const manualGradeSchema = z.object({
  expected_graded_at: z.iso.datetime({ offset: true }),
  grades: z
    .array(
      z.object({
        question_id: z.uuid(),
        score: z.number().finite().nonnegative().max(1_000_000),
        evidence_comment: z.string().trim().min(1).max(4_000),
      }),
    )
    .max(200)
    .refine(
      (grades) => new Set(grades.map((grade) => grade.question_id)).size === grades.length,
      { message: "Each manual question may be graded only once" },
    )
    .default([]),
  knockout_review: z
    .object({
      outcome: z.enum(["confirmed", "cleared"]),
      reason: z.string().trim().min(1).max(4_000),
    })
    .nullish(),
}).refine(
  (value) => value.grades.length > 0 || value.knockout_review != null,
  { message: "Manual grades or a knockout review are required" },
);

async function requireSession(): Promise<AssessmentActionResult<true>> {
  const session = await auth();
  if (!session?.user) {
    return { ok: false, error: "Your session has expired. Sign in again." };
  }
  return { ok: true, data: true };
}

async function apiError(response: Response): Promise<string> {
  return apiProblemMessage(response);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<AssessmentActionResult<T>> {
  const session = await requireSession();
  if (!session.ok) return session;
  const response = await apiFetch(path, init);
  if (!response.ok) return { ok: false, error: await apiError(response) };
  return { ok: true, data: (await response.json()) as T };
}

async function requestAll<T>(path: string): Promise<AssessmentActionResult<T[]>> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const [pathname, rawQuery = ""] = path.split("?", 2);
    const params = new URLSearchParams(rawQuery);
    if (!params.has("limit")) params.set("limit", "100");
    if (cursor) params.set("cursor", cursor);
    const result = await request<CursorPage<T>>(
      `${pathname}?${params.toString()}`,
    );
    if (!result.ok) return result;
    items.push(...result.data.items);
    cursor = result.data.next_cursor;
    if (!cursor) return { ok: true, data: items };
    if (seen.has(cursor)) {
      return { ok: false, error: "The assessment collection returned a repeated page." };
    }
    seen.add(cursor);
  }
  return { ok: false, error: "The assessment collection exceeded the safe page limit." };
}

export async function createAssessmentTemplateAction(
  input: AssessmentTemplateCreate,
): Promise<AssessmentActionResult<AssessmentTemplate>> {
  const parsed = templateCreateSchema.safeParse(input);
  if (!parsed.success) return { ok: false, error: parsed.error.issues[0]?.message ?? "Invalid template" };
  const result = await request<AssessmentTemplate>("/assessment-templates", {
    method: "POST",
    body: JSON.stringify(parsed.data),
  });
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function updateAssessmentTemplateAction(
  templateId: string,
  input: AssessmentTemplateUpdate,
): Promise<AssessmentActionResult<AssessmentTemplate>> {
  if (!z.uuid().safeParse(templateId).success) return { ok: false, error: "Invalid template" };
  const parsed = templateUpdateSchema.safeParse(input);
  if (!parsed.success) return { ok: false, error: parsed.error.issues[0]?.message ?? "Invalid template update" };
  const result = await request<AssessmentTemplate>(
    `/assessment-templates/${encodeURIComponent(templateId)}`,
    { method: "PATCH", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function publishAssessmentVersionAction(
  templateId: string,
  input: unknown,
): Promise<AssessmentActionResult<AssessmentVersion>> {
  if (!z.uuid().safeParse(templateId).success) return { ok: false, error: "Invalid template" };
  const parsed = versionCreateSchema.safeParse(input);
  if (!parsed.success) {
    return {
      ok: false,
      error: parsed.error.issues.map((issue) => issue.message).slice(0, 3).join("; "),
    };
  }
  const result = await request<AssessmentVersion>(
    `/assessment-templates/${encodeURIComponent(templateId)}/versions`,
    { method: "POST", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function listAssessmentInvitationsAction(
  applicationId: string,
): Promise<AssessmentActionResult<AssessmentInvitation[]>> {
  if (!z.uuid().safeParse(applicationId).success) return { ok: false, error: "Invalid application" };
  return requestAll<AssessmentInvitation>(
    `/applications/${encodeURIComponent(applicationId)}/assessment-invitations`,
  );
}

export async function listAssessmentApplicationsAction(
  cursor: string | null,
): Promise<AssessmentActionResult<AssessmentApplicationPage>> {
  const parsedCursor = z.string().min(32).max(2_048).nullable().safeParse(cursor);
  if (!parsedCursor.success) return { ok: false, error: "Invalid application page" };
  const params = new URLSearchParams({ limit: String(APPLICATION_PAGE_SIZE) });
  if (parsedCursor.data) params.set("cursor", parsedCursor.data);
  return request<AssessmentApplicationPage>(`/applications?${params.toString()}`);
}

export async function createAssessmentInvitationAction(
  applicationId: string,
  input: AssessmentInvitationCreate,
): Promise<AssessmentActionResult<AssessmentInvitationCreated>> {
  if (!z.uuid().safeParse(applicationId).success) return { ok: false, error: "Invalid application" };
  const parsed = invitationCreateSchema.safeParse(input);
  if (!parsed.success) return { ok: false, error: parsed.error.issues[0]?.message ?? "Invalid invitation" };
  const result = await request<AssessmentInvitationCreated>(
    `/applications/${encodeURIComponent(applicationId)}/assessment-invitations`,
    { method: "POST", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function revokeAssessmentInvitationAction(
  invitationId: string,
): Promise<AssessmentActionResult<AssessmentInvitation>> {
  if (!z.uuid().safeParse(invitationId).success) return { ok: false, error: "Invalid invitation" };
  const result = await request<AssessmentInvitation>(
    `/assessment-invitations/${encodeURIComponent(invitationId)}/revoke`,
    { method: "POST" },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function reissueAssessmentInvitationAction(
  invitationId: string,
  expiresAt: string,
): Promise<AssessmentActionResult<AssessmentInvitationCreated>> {
  if (!z.uuid().safeParse(invitationId).success) {
    return { ok: false, error: "Invalid invitation" };
  }
  const parsed = invitationReissueSchema.safeParse({ expires_at: expiresAt });
  if (!parsed.success) {
    return { ok: false, error: parsed.error.issues[0]?.message ?? "Invalid expiry" };
  }
  const result = await request<AssessmentInvitationCreated>(
    `/assessment-invitations/${encodeURIComponent(invitationId)}/reissue`,
    { method: "POST", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function listAssessmentProviderConnectionsAction(
  cursor: string | null = null,
): Promise<AssessmentActionResult<IntegrationConnectionPage>> {
  const parsedCursor = z.string().min(32).max(2_048).nullable().safeParse(cursor);
  if (!parsedCursor.success) {
    return { ok: false, error: "Invalid provider connection page" };
  }
  const params = new URLSearchParams({ limit: "100" });
  if (parsedCursor.data) params.set("cursor", parsedCursor.data);
  return request<IntegrationConnectionPage>(
    `/integrations/assessment/connections?${params.toString()}`,
  );
}

export async function listProviderAssessmentEvidenceAction(
  applicationId: string,
): Promise<AssessmentActionResult<ProviderAssessmentEvidence>> {
  if (!z.uuid().safeParse(applicationId).success) {
    return { ok: false, error: "Invalid application" };
  }
  return request<ProviderAssessmentEvidence>(
    `/integrations/assessment/applications/${encodeURIComponent(applicationId)}/evidence`,
  );
}

export async function createProviderAssessmentInvitationAction(
  connectionId: string,
  input: z.input<typeof providerInvitationSchema>,
): Promise<AssessmentActionResult<IntegrationJob>> {
  if (!z.uuid().safeParse(connectionId).success) {
    return { ok: false, error: "Invalid provider connection" };
  }
  const parsed = providerInvitationSchema.safeParse(input);
  if (!parsed.success) {
    return {
      ok: false,
      error: parsed.error.issues.map((issue) => issue.message).slice(0, 3).join("; "),
    };
  }
  const result = await request<IntegrationJob>(
    `/integrations/${encodeURIComponent(connectionId)}/assessment/invitations`,
    { method: "POST", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

async function pollProviderAssessmentAction(
  connectionId: string,
  input: z.input<typeof providerPollSchema>,
  operation: "status" | "result",
): Promise<AssessmentActionResult<IntegrationJob>> {
  if (!z.uuid().safeParse(connectionId).success) {
    return { ok: false, error: "Invalid provider connection" };
  }
  const parsed = providerPollSchema.safeParse(input);
  if (!parsed.success) {
    return { ok: false, error: parsed.error.issues[0]?.message ?? "Invalid provider assessment" };
  }
  const result = await request<IntegrationJob>(
    `/integrations/${encodeURIComponent(connectionId)}/assessment/attempts/${operation}`,
    { method: "POST", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function pollProviderAssessmentStatusAction(
  connectionId: string,
  input: z.input<typeof providerPollSchema>,
) {
  return pollProviderAssessmentAction(connectionId, input, "status");
}

export async function pollProviderAssessmentResultAction(
  connectionId: string,
  input: z.input<typeof providerPollSchema>,
) {
  return pollProviderAssessmentAction(connectionId, input, "result");
}

export async function approveAssessmentRetakeAction(
  invitationId: string,
): Promise<AssessmentActionResult<AssessmentInvitation>> {
  if (!z.uuid().safeParse(invitationId).success) {
    return { ok: false, error: "Invalid invitation" };
  }
  const result = await request<AssessmentInvitation>(
    `/assessment-invitations/${encodeURIComponent(invitationId)}/retakes`,
    { method: "POST" },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}

export async function listAssessmentInvitationAttemptsAction(
  invitationId: string,
): Promise<AssessmentActionResult<AssessmentAttemptSummary[]>> {
  if (!z.uuid().safeParse(invitationId).success) {
    return { ok: false, error: "Invalid invitation" };
  }
  return requestAll<AssessmentAttemptSummary>(
    `/assessment-invitations/${encodeURIComponent(invitationId)}/attempts`,
  );
}

export async function getAssessmentResultAction(
  attemptId: string,
): Promise<AssessmentActionResult<AssessmentSubmission>> {
  if (!z.uuid().safeParse(attemptId).success) return { ok: false, error: "Invalid attempt" };
  return request<AssessmentSubmission>(
    `/assessment-attempts/${encodeURIComponent(attemptId)}`,
  );
}

export async function gradeAssessmentAttemptAction(
  attemptId: string,
  input: z.input<typeof manualGradeSchema>,
): Promise<AssessmentActionResult<AssessmentSubmission>> {
  if (!z.uuid().safeParse(attemptId).success) {
    return { ok: false, error: "Invalid attempt" };
  }
  const parsed = manualGradeSchema.safeParse(input);
  if (!parsed.success) {
    return {
      ok: false,
      error: parsed.error.issues.map((issue) => issue.message).slice(0, 3).join("; "),
    };
  }
  const result = await request<AssessmentSubmission>(
    `/assessment-attempts/${encodeURIComponent(attemptId)}/manual-grade`,
    { method: "POST", body: JSON.stringify(parsed.data) },
  );
  if (result.ok) revalidatePath("/assessments");
  return result;
}
