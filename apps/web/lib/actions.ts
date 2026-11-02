"use server";

import { revalidatePath } from "next/cache";

import { apiFetch, type CursorPage } from "@/lib/api-client";
import { apiProblemMessage } from "@/lib/api-problem";
import { candidateFiltersToSearchParams } from "@/lib/candidate-search";
import type {
  ApiOperationResponse,
  ApiResponseSchema,
  ApiSchema,
} from "@/lib/generated/contracts";
import {
  normalizeGitHubOut,
  normalizeInterviewRecording,
  normalizeTaskSubmission,
} from "@/lib/types";
import type {
  CandidateActivityPage,
  CandidateCommunicationPreview,
  CandidateContactPoint,
  CandidateBulkRequest,
  CandidateBulkResult,
  CandidateDuplicatePage,
  CandidateMergePreview,
  CandidateMergeReverseResult,
  CandidateMergeResult,
  CandidateMergeScoreWinner,
  CandidateNote,
  CandidateSavedView,
  CandidateProcessingBatchReceipt,
  CandidateProcessingJobReceipt,
  CandidateSearchFilters,
  CandidateSearchPage,
  CandidateTag,
  CandidateWork,
  ChatSessionDetail,
  ChatSessionListItem,
  CommunicationAccount,
  CommunicationAccountCreateResult,
  CommunicationCapabilities,
  CommunicationConnectionOption,
  CommunicationMessage,
  CommunicationMessagePage,
  CommunicationTemplate,
  CommunicationThread,
  CommunicationThreadPage,
  BulkCommunicationCreateResult,
  BulkCommunicationPreview,
  Availability,
  AvailabilityBlock,
  CalendarProvider,
  Folder,
  GitHubDetect,
  GitHubOut,
  Interview,
  InterviewDetail,
  InterviewCompleteRequest,
  InterviewKitVersionCreate,
  ManualInterviewCreate,
  InterviewQuestion,
  InterviewRecording,
  InterviewAppointment,
  InterviewStatus,
  MyWorkFilter,
  MyWorkPage,
  NotificationList,
  Organization,
  Position,
  PositionSensitivity,
  PositionMatch,
  RankedCandidatePage,
  SchedulingLink,
  SchedulingLinkCreate,
  ScorecardAssignment,
  ScorecardCriterionCreate,
  ScorecardPanel,
  ScorecardTemplate,
  HiringDecision,
  HiringRecommendation,
  ShortlistLink,
  ShortlistLinkCreated,
  TaskSubmission,
  WorkflowAction,
  WorkflowConditions,
  WorkflowDryRun,
  WorkflowProcessResult,
  WorkflowRule,
  WorkflowRun,
  WorkflowRunPage,
  WorkflowTrigger,
  CandidateHandoff,
  CandidateOffer,
  CandidateOfferSendResult,
  HandoffChecklistItem,
  OfferTemplate,
  Requisition,
  EffectivePermissions,
  UserAccessCreated,
} from "@/lib/types";

/** @ui-only Serializable result returned by Next.js server actions. */
export type ActionResult<T = unknown> =
  | { ok: true; data: T }
  | { ok: false; error: string };

const MAX_CANDIDATE_CV_BYTES = 15 * 1024 * 1024;

export async function listPositionApplicationsAction(
  positionId: string,
  cursor: string | null,
): Promise<ActionResult<RankedCandidatePage>> {
  const params = new URLSearchParams({ limit: "50" });
  if (cursor) params.set("cursor", cursor);
  const res = await apiFetch(
    `/positions/${encodeURIComponent(positionId)}/applications?${params.toString()}`,
  );
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: (await res.json()) as RankedCandidatePage };
}

async function _post<T>(path: string, body: unknown): Promise<ActionResult<T>> {
  const res = await apiFetch(path, {
    method: "POST",
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: (await res.json()) as T };
}

function withCollectionCursor(path: string, cursor: string | null): string {
  const [pathname, rawQuery = ""] = path.split("?", 2);
  const params = new URLSearchParams(rawQuery);
  if (!params.has("limit")) params.set("limit", "100");
  if (cursor) params.set("cursor", cursor);
  else params.delete("cursor");
  return `${pathname}?${params.toString()}`;
}

async function _getAll<T>(path: string): Promise<ActionResult<T[]>> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const res = await apiFetch(withCollectionCursor(path, cursor));
    if (!res.ok) {
      return { ok: false, error: await apiProblemMessage(res) };
    }
    const page = (await res.json()) as CursorPage<T>;
    items.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) return { ok: true, data: items };
    if (seen.has(cursor)) {
      return { ok: false, error: "API returned a repeated collection cursor" };
    }
    seen.add(cursor);
  }
  return { ok: false, error: "API collection exceeded the safe page traversal limit" };
}

async function _getAllPage<
  T,
  P extends CursorPage<T>,
>(path: string): Promise<ActionResult<P>> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let firstPage: P | null = null;
  for (let pageNumber = 0; pageNumber < 1_000; pageNumber += 1) {
    const res = await apiFetch(withCollectionCursor(path, cursor));
    if (!res.ok) {
      return { ok: false, error: await apiProblemMessage(res) };
    }
    const page = (await res.json()) as P;
    firstPage ??= page;
    items.push(...page.items);
    cursor = page.next_cursor;
    if (!cursor) {
      return {
        ok: true,
        data: { ...firstPage, items, next_cursor: null },
      };
    }
    if (seen.has(cursor)) {
      return { ok: false, error: "API returned a repeated collection cursor" };
    }
    seen.add(cursor);
  }
  return { ok: false, error: "API collection exceeded the safe page traversal limit" };
}

export async function createPositionAction(
  formData: FormData,
): Promise<ActionResult<ApiResponseSchema<"PositionOut">>> {
  const title = String(formData.get("title") ?? "").trim();
  const description = String(formData.get("description") ?? "").trim() || null;
  const requirementsRaw = String(formData.get("requirements") ?? "").trim();

  let requirements: Record<string, unknown> | null = null;
  if (requirementsRaw) {
    try {
      requirements = JSON.parse(requirementsRaw);
    } catch {
      return { ok: false, error: "Requirements must be valid JSON." };
    }
  }
  if (!title) return { ok: false, error: "Title is required." };

  const result = await _post<ApiResponseSchema<"PositionOut">>("/positions", {
    title,
    description,
    requirements,
    status: "open",
  });
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

export async function uploadCandidateFileAction(
  formData: FormData,
): Promise<ActionResult<ApiResponseSchema<"CandidateOut">>> {
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return { ok: false, error: "No file provided." };
  }
  if (file.size > MAX_CANDIDATE_CV_BYTES) {
    return {
      ok: false,
      error: "CV file exceeds the 15 MiB upload limit.",
    };
  }
  const proxyForm = new FormData();
  proxyForm.append("file", file);
  const res = await apiFetch("/candidates/upload/file", {
    method: "POST",
    body: proxyForm,
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return {
    ok: true,
    data: (await res.json()) as ApiResponseSchema<"CandidateOut">,
  };
}

export async function uploadCandidateTextAction(
  raw_text: string,
): Promise<ActionResult<ApiResponseSchema<"CandidateOut">>> {
  return _post<ApiResponseSchema<"CandidateOut">>("/candidates/upload/text", {
    raw_text,
  });
}

export async function uploadCandidateHHJsonAction(
  hhJsonRaw: string,
): Promise<ActionResult<ApiResponseSchema<"CandidateOut">>> {
  let hh_json: unknown;
  try {
    hh_json = JSON.parse(hhJsonRaw);
  } catch {
    return { ok: false, error: "Not valid JSON." };
  }
  return _post<ApiResponseSchema<"CandidateOut">>("/candidates/upload/hh", {
    hh_json,
  });
}

export async function scoreCandidateAction(
  candidate_id: string,
  position_id: string,
): Promise<ActionResult<CandidateProcessingJobReceipt>> {
  return _post<CandidateProcessingJobReceipt>(`/candidates/${candidate_id}/score`, {
    position_id,
  });
}

export async function getCandidateProcessingJobAction(
  candidateId: string,
  jobId: string,
): Promise<ActionResult<CandidateProcessingJobReceipt>> {
  const res = await apiFetch(
    `/candidates/${encodeURIComponent(candidateId)}/processing-jobs/${encodeURIComponent(jobId)}`,
  );
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return {
    ok: true,
    data: (await res.json()) as CandidateProcessingJobReceipt,
  };
}

export async function updateCandidateStageAction(
  positionId: string,
  candidateId: string,
  stage: string,
  expectedVersion: string | null,
  dispositionCode?: string,
  reason?: string,
): Promise<ActionResult<ApiResponseSchema<"RankedCandidate">>> {
  const result = await _patch<ApiResponseSchema<"RankedCandidate">>(
    `/positions/${positionId}/candidates/${candidateId}/stage`,
    {
      stage,
      expected_version: expectedVersion,
      disposition_code: dispositionCode,
      reason,
    },
  );
  if (result.ok) {
    revalidatePath(`/positions/${positionId}`);
  }
  return result;
}

export async function uploadAndScoreAction(
  formData: FormData,
  positionId: string,
): Promise<ActionResult<CandidateProcessingJobReceipt>> {
  const kind = String(formData.get("kind") ?? "");
  let candidate: ActionResult<ApiResponseSchema<"CandidateOut">>;

  if (kind === "file") {
    candidate = await uploadCandidateFileAction(formData);
  } else if (kind === "text") {
    candidate = await uploadCandidateTextAction(String(formData.get("raw_text") ?? ""));
  } else if (kind === "hh") {
    candidate = await uploadCandidateHHJsonAction(String(formData.get("hh_json") ?? ""));
  } else {
    return { ok: false, error: `Unknown upload kind: ${kind}` };
  }

  if (!candidate.ok) return candidate;
  return scoreCandidateAction(candidate.data.id, positionId);
}

export async function createChatSessionAction(args: {
  scope: "candidate" | "position" | "free";
  candidate_id?: string;
  position_id?: string;
}): Promise<ActionResult<ApiResponseSchema<"ChatSessionOut">>> {
  return _post<ApiResponseSchema<"ChatSessionOut">>("/chat/sessions", args);
}

async function _patch<T>(path: string, body: unknown): Promise<ActionResult<T>> {
  const res = await apiFetch(path, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: (await res.json()) as T };
}

async function _put<T>(path: string, body: unknown): Promise<ActionResult<T>> {
  const res = await apiFetch(path, {
    method: "PUT",
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: (await res.json()) as T };
}

async function _delete<T = void>(path: string): Promise<ActionResult<T>> {
  const res = await apiFetch(path, { method: "DELETE" });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  // 204 No Content has an empty body — calling res.json() would throw.
  if (res.status === 204) return { ok: true, data: undefined as T };
  return { ok: true, data: (await res.json()) as T };
}

export async function setFavoriteAction(
  candidateId: string,
  isFavorite: boolean,
  pathToRevalidate?: string,
): Promise<ActionResult> {
  const result = await _patch<ApiResponseSchema<"CandidateOut">>(
    `/candidates/${candidateId}`,
    { is_favorite: isFavorite },
  );
  if (result.ok) {
    revalidatePath(`/candidates/${candidateId}`);
    if (pathToRevalidate) revalidatePath(pathToRevalidate);
  }
  return result;
}

export async function listCandidateActivitiesAction(
  candidateId: string,
  options?: {
    cursor?: string | null;
    category?: "all" | "pipeline" | "evaluation" | "collaboration";
    limit?: number;
  },
): Promise<ActionResult<CandidateActivityPage>> {
  const params = new URLSearchParams();
  params.set("limit", String(options?.limit ?? 30));
  if (options?.cursor) params.set("cursor", options.cursor);
  if (options?.category && options.category !== "all") {
    params.set("category", options.category);
  }
  const res = await apiFetch(
    `/candidates/${candidateId}/activities?${params.toString()}`,
  );
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as CandidateActivityPage };
}

export async function listCandidateSearchAction(
  filters: CandidateSearchFilters,
  cursor?: string | null,
): Promise<ActionResult<CandidateSearchPage>> {
  const params = candidateFiltersToSearchParams(filters, {
    cursor,
    limit: 50,
  });
  const res = await apiFetch(`/candidate-search?${params.toString()}`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as CandidateSearchPage };
}

export async function listCandidateViewsAction(): Promise<
  ActionResult<CandidateSavedView[]>
> {
  return _getAll<CandidateSavedView>("/candidate-views");
}

export async function createCandidateViewAction(
  name: string,
  filters: CandidateSearchFilters,
): Promise<ActionResult<CandidateSavedView>> {
  const result = await _post<CandidateSavedView>("/candidate-views", {
    name,
    filters,
  });
  if (result.ok) revalidatePath("/candidates");
  return result;
}

export async function updateCandidateViewAction(
  viewId: string,
  updates: { name?: string; filters?: CandidateSearchFilters },
): Promise<ActionResult<CandidateSavedView>> {
  const result = await _patch<CandidateSavedView>(
    `/candidate-views/${viewId}`,
    updates,
  );
  if (result.ok) revalidatePath("/candidates");
  return result;
}

export async function deleteCandidateViewAction(
  viewId: string,
): Promise<ActionResult<void>> {
  const result = await _delete<void>(`/candidate-views/${viewId}`);
  if (result.ok) revalidatePath("/candidates");
  return result;
}

export async function listCandidateDuplicatesAction(): Promise<
  ActionResult<CandidateDuplicatePage>
> {
  return _getAllPage<CandidateDuplicatePage["items"][number], CandidateDuplicatePage>(
    "/candidate-duplicates",
  );
}

export async function previewCandidateMergeAction(
  candidateIds: string[],
): Promise<ActionResult<CandidateMergePreview>> {
  return _post<CandidateMergePreview>("/candidate-merges/preview", {
    candidate_ids: candidateIds,
  });
}

export async function mergeCandidatesAction(
  primaryCandidateId: string,
  duplicateIds: string[],
  scoreWinners: CandidateMergeScoreWinner[],
): Promise<ActionResult<CandidateMergeResult>> {
  const result = await _post<CandidateMergeResult>(
    `/candidates/${primaryCandidateId}/merge`,
    { duplicate_ids: duplicateIds, score_winners: scoreWinners },
  );
  if (result.ok) {
    revalidatePath("/candidates");
    revalidatePath(`/candidates/${primaryCandidateId}`);
    revalidatePath("/my-work");
    revalidatePath("/dashboard");
  }
  return result;
}

export async function reverseCandidateMergeAction(
  batchId: string,
  expectedVersion: number,
  primaryCandidateId: string,
): Promise<ActionResult<CandidateMergeReverseResult>> {
  const result = await _post<CandidateMergeReverseResult>(
    `/candidate-merges/${encodeURIComponent(batchId)}/reverse`,
    { expected_version: expectedVersion },
  );
  if (result.ok) {
    revalidatePath("/candidates");
    revalidatePath(`/candidates/${primaryCandidateId}`);
    revalidatePath("/my-work");
    revalidatePath("/dashboard");
  }
  return result;
}

export async function applyCandidateBulkAction(
  body: CandidateBulkRequest,
): Promise<ActionResult<CandidateBulkResult>> {
  const result = await _post<CandidateBulkResult>("/candidate-bulk", body);
  if (result.ok) {
    revalidatePath("/candidates");
    revalidatePath("/my-work");
    if (body.position_id) revalidatePath(`/positions/${body.position_id}`);
  }
  return result;
}

export async function listMyWorkAction(options?: {
  cursor?: string | null;
  limit?: number;
  staleDays?: number;
  filter?: MyWorkFilter;
}): Promise<ActionResult<MyWorkPage>> {
  const params = new URLSearchParams();
  params.set("limit", String(options?.limit ?? 50));
  params.set("stale_days", String(options?.staleDays ?? 7));
  if (options?.cursor) params.set("cursor", options.cursor);
  if (options?.filter && options.filter !== "all") {
    params.set("filter", options.filter);
  }
  const res = await apiFetch(`/my-work?${params.toString()}`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as MyWorkPage };
}

export async function updateCandidateWorkAction(
  positionId: string,
  candidateId: string,
  updates: {
    owner_user_id?: string | null;
    next_action_due_date?: string | null;
    expected_owner_user_id?: string | null;
    expected_next_action_due_date?: string | null;
  },
): Promise<ActionResult<CandidateWork>> {
  const result = await _patch<CandidateWork>(
    `/positions/${positionId}/candidates/${candidateId}/work`,
    updates,
  );
  if (result.ok) {
    revalidatePath("/my-work");
    revalidatePath(`/positions/${positionId}`);
    revalidatePath(`/candidates/${candidateId}`);
  }
  return result;
}

export async function markViewedAction(
  candidateId: string,
): Promise<ActionResult> {
  const result = await _post<ApiResponseSchema<"CandidateOut">>(
    `/candidates/${candidateId}/view`,
    {},
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function updatePositionAction(
  positionId: string,
  updates: {
    title?: string;
    description?: string;
    internal_notes?: string;
    status?: string;
    public_apply_enabled?: boolean;
    sensitivity?: PositionSensitivity;
    candidate_profile?: string;
    public_description?: string;
    // NOTE: PATCH replaces `requirements` wholesale rather than merging, so a
    // caller editing one key must send the whole object back.
    requirements?: Record<string, unknown>;
  },
): Promise<ActionResult> {
  const result = await _patch<ApiResponseSchema<"PositionOut">>(
    `/positions/${positionId}`,
    updates,
  );
  if (result.ok) {
    revalidatePath(`/positions/${positionId}`);
    revalidatePath("/dashboard");
  }
  return result;
}

/** (Re)generate the ideal-candidate brief, and the redacted public
 *  description when the role is confidential. Overwrites both. */
export async function generatePositionProfileAction(
  positionId: string,
): Promise<ActionResult<ApiResponseSchema<"GenerateProfileResponse">>> {
  const r = await _post<ApiResponseSchema<"GenerateProfileResponse">>(
    `/positions/${positionId}/generate-profile`,
    {},
  );
  if (r.ok) revalidatePath(`/positions/${positionId}`);
  return r;
}

export async function rescoreAllAction(
  positionId: string,
): Promise<ActionResult<ApiResponseSchema<"RescoreAllResponse">>> {
  const r = await _post<ApiResponseSchema<"RescoreAllResponse">>(
    `/positions/${positionId}/rescore`,
    {},
  );
  if (r.ok) revalidatePath(`/positions/${positionId}`);
  return r;
}

export async function deletePositionAction(
  positionId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/positions/${positionId}`, { method: "DELETE" });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  revalidatePath("/dashboard");
  return { ok: true, data: null };
}

export async function deleteCandidateAction(
  candidateId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/candidates/${candidateId}`, { method: "DELETE" });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  revalidatePath("/candidates");
  return { ok: true, data: null };
}

export async function changePasswordAction(
  current: string,
  next: string,
): Promise<ActionResult> {
  const res = await apiFetch("/users/me/password", {
    method: "POST",
    body: JSON.stringify({ current_password: current, new_password: next }),
  });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: null };
}

export async function createUserInvitationAction(
  body: {
    email: string;
    role: string;
    locale: string;
    expires_in_days: number;
  },
): Promise<ActionResult<UserAccessCreated>> {
  const r = await _post<UserAccessCreated>("/users/invitations", body);
  if (r.ok) revalidatePath("/settings");
  return r;
}

export async function createUserPasswordResetAction(
  userId: string,
): Promise<ActionResult<UserAccessCreated>> {
  return _post<UserAccessCreated>(
    `/users/${encodeURIComponent(userId)}/password-reset`,
    {},
  );
}

export async function revokeUserAccessTokenAction(
  tokenId: string,
): Promise<ActionResult<null>> {
  const res = await apiFetch(
    `/users/access-tokens/${encodeURIComponent(tokenId)}/revoke`,
    { method: "POST" },
  );
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: null };
}

export async function deleteUserAction(
  userId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/users/${userId}`, { method: "DELETE" });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  revalidatePath("/settings");
  return { ok: true, data: null };
}

export async function listChatSessionsAction(filters?: {
  scope?: "candidate" | "position" | "free";
  position_id?: string;
  candidate_id?: string;
}): Promise<ActionResult<ChatSessionListItem[]>> {
  const params = new URLSearchParams();
  if (filters?.scope) params.set("scope", filters.scope);
  if (filters?.position_id) params.set("position_id", filters.position_id);
  if (filters?.candidate_id) params.set("candidate_id", filters.candidate_id);
  const qs = params.toString();
  return _getAll<ChatSessionListItem>(
    `/chat/sessions${qs ? `?${qs}` : ""}`,
  );
}

export async function loadChatSessionAction(
  sessionId: string,
): Promise<ActionResult<ChatSessionDetail>> {
  const res = await apiFetch(`/chat/sessions/${sessionId}`);
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  return { ok: true, data: (await res.json()) as ChatSessionDetail };
}

export type DraftMessage = ApiSchema<"DraftMessage">;
/** @ui-only Fully initialized editor state backed by the generated draft schema. */
export type PositionDraft = ApiResponseSchema<"PositionDraft"> & {
  folder: string | null;
};
export type QuickReply = ApiSchema<"QuickReply">;
export type DraftConverseResult = Omit<
  ApiResponseSchema<"DraftConverseResponse">,
  "draft"
> & { draft: PositionDraft };

export async function draftPositionAction(
  messages: DraftMessage[],
  current_draft: PositionDraft | null,
): Promise<ActionResult<DraftConverseResult>> {
  return _post<DraftConverseResult>("/positions/draft", {
    messages,
    current_draft,
  });
}

/** @ui-only Validated view of `/positions/draft/extract-text`'s untyped response. */
export type ExtractedFile = ApiOperationResponse<
  "/positions/draft/extract-text",
  "post"
> & {
  filename: string;
  char_count: number;
  truncated: boolean;
  text: string;
};

function normalizeExtractedFile(value: unknown): ExtractedFile | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    typeof item.filename !== "string" ||
    typeof item.char_count !== "number" ||
    typeof item.truncated !== "boolean" ||
    typeof item.text !== "string"
  ) {
    return null;
  }
  return {
    ...item,
    filename: item.filename,
    char_count: item.char_count,
    truncated: item.truncated,
    text: item.text,
  };
}

export async function extractFileTextAction(
  formData: FormData,
): Promise<ActionResult<ExtractedFile>> {
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return { ok: false, error: "No file provided." };
  }
  const proxyForm = new FormData();
  proxyForm.append("file", file);
  const res = await apiFetch("/positions/draft/extract-text", {
    method: "POST",
    body: proxyForm,
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  const extracted = normalizeExtractedFile(await res.json());
  return extracted
    ? { ok: true, data: extracted }
    : { ok: false, error: "The extraction service returned an invalid response." };
}

export async function createPositionFromDraftAction(
  draft: PositionDraft,
): Promise<ActionResult<ApiResponseSchema<"PositionOut">>> {
  // Merge soft fields (work_format, salary_range, etc.) into requirements jsonb
  // so they are visible on the position detail. Skip empty entries.
  const reqs: Record<string, unknown> = { ...(draft.requirements ?? {}) };
  const soft: Array<[string, string | null]> = [
    ["seniority", draft.seniority],
    ["work_format", draft.work_format],
    ["location", draft.location],
    ["employment_type", draft.employment_type],
    ["salary_range", draft.salary_range],
    ["team_size", draft.team_size],
  ];
  for (const [k, v] of soft) {
    if (v && String(v).trim() && reqs[k] == null) reqs[k] = v;
  }

  const body = {
    title: draft.title ?? "",
    description: draft.description ?? null,
    requirements: Object.keys(reqs).length > 0 ? reqs : null,
    status: "open" as const,
    folder: draft.folder?.trim() || null,
  };
  const result = await _post<ApiResponseSchema<"PositionOut">>("/positions", body);
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

// ---------- Project (multi-position) flow ----------

export type ProjectExtractResult = Omit<
  ApiResponseSchema<"ProjectExtractResponse">,
  "positions"
> & { positions: PositionDraft[] };

export async function extractProjectAction(
  formData: FormData,
): Promise<ActionResult<ProjectExtractResult>> {
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return { ok: false, error: "No file provided." };
  }
  const proxy = new FormData();
  proxy.append("file", file);
  const res = await apiFetch("/positions/project/extract", {
    method: "POST",
    body: proxy,
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as ProjectExtractResult };
}

export type ProjectCreateResult = ApiResponseSchema<"ProjectCreateResponse"> & {
  folder_id: string | null;
};

export async function createPositionsFromProjectAction(
  positions: PositionDraft[],
  folder: string | null,
): Promise<ActionResult<ProjectCreateResult>> {
  // Mirror the single-position flow: merge soft fields into requirements so the
  // detail view shows seniority/work_format/etc. The server also does this as
  // a safety net, but doing it client-side keeps the wire payload stable.
  const cleaned = positions
    .filter((p) => (p.title ?? "").trim().length > 0)
    .map((p) => {
      const reqs: Record<string, unknown> = { ...(p.requirements ?? {}) };
      const soft: Array<[string, string | null]> = [
        ["seniority", p.seniority],
        ["work_format", p.work_format],
        ["location", p.location],
        ["employment_type", p.employment_type],
        ["salary_range", p.salary_range],
        ["team_size", p.team_size],
      ];
      for (const [k, v] of soft) {
        if (v && String(v).trim() && reqs[k] == null) reqs[k] = v;
      }
      return {
        title: (p.title ?? "").trim(),
        description: p.description ?? null,
        requirements: Object.keys(reqs).length > 0 ? reqs : null,
        seniority: p.seniority,
        work_format: p.work_format,
        location: p.location,
        employment_type: p.employment_type,
        salary_range: p.salary_range,
        team_size: p.team_size,
        folder: null,
      };
    });

  if (cleaned.length === 0) {
    return { ok: false, error: "Add at least one position with a title." };
  }

  const r = await _post<ProjectCreateResult>("/positions/project", {
    folder: folder?.trim() || null,
    positions: cleaned,
  });
  if (r.ok) revalidatePath("/dashboard");
  return r;
}

// ---------- Interview-prep ----------

export async function listInterviewsAction(
  candidateId: string,
): Promise<ActionResult<Interview[]>> {
  return _getAll<Interview>(`/candidates/${candidateId}/interviews`);
}

export async function generateInterviewAction(
  candidateId: string,
  body: { position_id: string; count?: number },
): Promise<ActionResult<InterviewDetail>> {
  const res = await apiFetch(`/candidates/${candidateId}/interviews/generate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: (await res.json()) as InterviewDetail };
}

export async function createManualInterviewAction(
  candidateId: string,
  body: ManualInterviewCreate,
): Promise<ActionResult<InterviewDetail>> {
  const result = await _post<InterviewDetail>(
    `/candidates/${candidateId}/interviews`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function createInterviewKitVersionAction(
  interviewId: string,
  body: InterviewKitVersionCreate,
): Promise<ActionResult<InterviewDetail>> {
  return _post<InterviewDetail>(
    `/interviews/${interviewId}/kit-versions`,
    body,
  );
}

export async function getInterviewAction(
  interviewId: string,
): Promise<ActionResult<InterviewDetail>> {
  const res = await apiFetch(`/interviews/${interviewId}`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as InterviewDetail };
}

export async function scoreQuestionAction(
  interviewId: string,
  questionId: string,
  body: { score?: number | null; notes?: string },
): Promise<ActionResult<InterviewQuestion>> {
  const res = await apiFetch(
    `/interviews/${interviewId}/questions/${questionId}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as InterviewQuestion };
}

export async function completeInterviewAction(
  interviewId: string,
  body: InterviewCompleteRequest,
): Promise<ActionResult<Interview>> {
  return _post<Interview>(`/interviews/${interviewId}/complete`, body);
}

export async function updateInterviewAction(
  interviewId: string,
  body: { status?: InterviewStatus; notes?: string },
): Promise<ActionResult<Interview>> {
  const res = await apiFetch(`/interviews/${interviewId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as Interview };
}

export async function deleteInterviewAction(
  interviewId: string,
  candidateId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/interviews/${interviewId}`, { method: "DELETE" });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: null };
}

// ---------- Reverse matching: best positions for a candidate ----------

export async function getBestPositionsAction(
  candidateId: string,
): Promise<ActionResult<PositionMatch[]>> {
  return _getAll<PositionMatch>(`/candidates/${candidateId}/best-positions`);
}

export async function scoreAllOpenAction(
  candidateId: string,
  folder?: string | null,
  folderId?: string | null,
): Promise<ActionResult<CandidateProcessingBatchReceipt>> {
  // `folder_id` (the folders-tree filter) wins when provided; otherwise the
  // legacy `folder` string: omit → all open in org, empty string → Unsorted,
  // otherwise → exact folder match.
  let qs = "";
  if (folderId !== undefined && folderId !== null) {
    qs = `?folder_id=${encodeURIComponent(folderId)}`;
  } else if (folder !== undefined && folder !== null) {
    qs = `?folder=${encodeURIComponent(folder)}`;
  }
  const res = await apiFetch(
    `/candidates/${candidateId}/score-all-open${qs}`,
    { method: "POST", body: "{}" },
  );
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return {
    ok: true,
    data: (await res.json()) as CandidateProcessingBatchReceipt,
  };
}

// ---------- Folders ----------

export async function getFolderTreeAction(): Promise<ActionResult<Folder[]>> {
  return _getAll<Folder>("/folders");
}

export async function createFolderAction(input: {
  name: string;
  parent_id?: string | null;
  color?: string | null;
  icon?: string | null;
}): Promise<ActionResult<Folder>> {
  const result = await _post<Folder>("/folders", input);
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

export async function updateFolderAction(
  id: string,
  patch: {
    name?: string;
    parent_id?: string | null;
    color?: string | null;
    icon?: string | null;
    is_archived?: boolean;
  },
): Promise<ActionResult<Folder>> {
  const result = await _patch<Folder>(`/folders/${id}`, patch);
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

export async function deleteFolderAction(
  id: string,
  opts?: { reassignTo?: string | null; force?: boolean },
): Promise<ActionResult<void>> {
  const params = new URLSearchParams();
  if (opts?.reassignTo) params.set("reassign_to", opts.reassignTo);
  if (opts?.force) params.set("force", "true");
  const qs = params.toString();
  const result = await _delete<void>(`/folders/${id}${qs ? `?${qs}` : ""}`);
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

export async function reorderFoldersAction(
  parentId: string | null,
  orderedIds: string[],
): Promise<ActionResult<Folder[]>> {
  const result = await _patch<Folder[]>("/folders/reorder", {
    parent_id: parentId,
    ordered_ids: orderedIds,
  });
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

export async function movePositionsAction(
  positionIds: string[],
  folderId: string | null,
): Promise<ActionResult<Position[]>> {
  const result = await _patch<Position[]>("/positions/move", {
    position_ids: positionIds,
    folder_id: folderId,
  });
  if (result.ok) revalidatePath("/dashboard");
  return result;
}

// ---------- Notifications ----------

export async function listNotificationsAction(): Promise<
  ActionResult<NotificationList>
> {
  const res = await apiFetch(`/notifications`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as NotificationList };
}

export async function markNotificationReadAction(
  notificationId: string,
): Promise<ActionResult<void>> {
  const res = await apiFetch(`/notifications/${notificationId}/read`, {
    method: "POST",
    body: "{}",
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: undefined };
}

// ---------- Candidate notes & tags ----------

export async function listCandidateNotesAction(
  candidateId: string,
): Promise<ActionResult<CandidateNote[]>> {
  return _getAll<CandidateNote>(`/candidates/${candidateId}/notes`);
}

export async function addCandidateNoteAction(
  candidateId: string,
  content: string,
): Promise<ActionResult<CandidateNote>> {
  const r = await _post<CandidateNote>(`/candidates/${candidateId}/notes`, {
    content,
  });
  if (r.ok) revalidatePath(`/candidates/${candidateId}`);
  return r;
}

export async function deleteCandidateNoteAction(
  candidateId: string,
  noteId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/candidates/${candidateId}/notes/${noteId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: null };
}

export async function listCandidateTagsAction(
  candidateId: string,
): Promise<ActionResult<CandidateTag[]>> {
  return _getAll<CandidateTag>(`/candidates/${candidateId}/tags`);
}

export async function addCandidateTagAction(
  candidateId: string,
  label: string,
): Promise<ActionResult<CandidateTag>> {
  const r = await _post<CandidateTag>(`/candidates/${candidateId}/tags`, {
    label,
  });
  if (r.ok) revalidatePath(`/candidates/${candidateId}`);
  return r;
}

export async function deleteCandidateTagAction(
  candidateId: string,
  tagId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/candidates/${candidateId}/tags/${tagId}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    return { ok: false, error: await apiProblemMessage(res) };
  }
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: null };
}

// ---------- Organization branding ----------

export async function getMyOrganizationAction(): Promise<
  ActionResult<Organization>
> {
  const res = await apiFetch("/organizations/me");
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as Organization };
}

export async function updateMyOrganizationAction(
  body: { name?: string; accent_color?: string | null },
): Promise<ActionResult<Organization>> {
  const r = await _post<Organization>("/organizations/me", body);
  if (r.ok) revalidatePath("/", "layout");
  return r;
}

export async function uploadMyOrganizationLogoAction(
  formData: FormData,
): Promise<ActionResult<Organization>> {
  const file = formData.get("file");
  if (!(file instanceof File)) {
    return { ok: false, error: "No file provided." };
  }
  const proxy = new FormData();
  proxy.append("file", file);
  const res = await apiFetch("/organizations/me/logo", {
    method: "POST",
    body: proxy,
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath("/", "layout");
  return { ok: true, data: (await res.json()) as Organization };
}

export async function deleteMyOrganizationLogoAction(): Promise<
  ActionResult<Organization>
> {
  const res = await apiFetch("/organizations/me/logo", { method: "DELETE" });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath("/", "layout");
  return { ok: true, data: (await res.json()) as Organization };
}

// ---------- Shortlist links ----------

export async function listShortlistsAction(
  positionId: string,
): Promise<ActionResult<ShortlistLink[]>> {
  return _getAll<ShortlistLink>(`/positions/${positionId}/shortlists`);
}

export async function createShortlistAction(
  positionId: string,
  body: { top_n: number; include_contact: boolean; expires_in_days?: number },
): Promise<ActionResult<ShortlistLinkCreated>> {
  const r = await _post<ShortlistLinkCreated>(
    `/positions/${positionId}/shortlists`,
    body,
  );
  if (r.ok) revalidatePath(`/positions/${positionId}`);
  return r;
}

export async function rotateShortlistAction(
  shortlistId: string,
  positionId: string,
): Promise<ActionResult<ShortlistLinkCreated>> {
  const r = await _post<ShortlistLinkCreated>(
    `/shortlists/${shortlistId}/rotate`,
    {},
  );
  if (r.ok) revalidatePath(`/positions/${positionId}`);
  return r;
}

export async function revokeShortlistAction(
  shortlistId: string,
  positionId: string,
): Promise<ActionResult<ShortlistLink>> {
  const r = await _post<ShortlistLink>(
    `/shortlists/${shortlistId}/revoke`,
    {},
  );
  if (r.ok) revalidatePath(`/positions/${positionId}`);
  return r;
}

export async function markAllNotificationsReadAction(): Promise<
  ActionResult<void>
> {
  const res = await apiFetch(`/notifications/read-all`, {
    method: "POST",
    body: "{}",
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: undefined };
}

// ---------- GitHub enrichment ----------

export async function attachGithubAction(
  candidateId: string,
  login: string,
): Promise<ActionResult<GitHubOut>> {
  const res = await apiFetch(`/candidates/${candidateId}/github`, {
    method: "PUT",
    body: JSON.stringify({ login }),
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return {
    ok: true,
    data: normalizeGitHubOut(
      (await res.json()) as ApiResponseSchema<"GitHubOut">,
    ),
  };
}

export async function detectGithubAction(
  candidateId: string,
): Promise<ActionResult<GitHubDetect>> {
  const res = await apiFetch(`/candidates/${candidateId}/github/detect`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as GitHubDetect };
}

export async function detachGithubAction(
  candidateId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/candidates/${candidateId}/github`, {
    method: "DELETE",
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: null };
}

// ---------- Interview recordings ----------

export async function listRecordingsAction(
  candidateId: string,
): Promise<ActionResult<InterviewRecording[]>> {
  const result = await _getAll<ApiResponseSchema<"RecordingOut">>(
    `/candidates/${candidateId}/recordings`,
  );
  return result.ok
    ? { ok: true, data: result.data.map(normalizeInterviewRecording) }
    : result;
}

export async function createRecordingAction(
  candidateId: string,
  body: { title: string; position_id?: string | null; media_url?: string | null },
): Promise<ActionResult<InterviewRecording>> {
  const r = await _post<ApiResponseSchema<"RecordingOut">>(
    `/candidates/${candidateId}/recordings`,
    body,
  );
  if (r.ok) revalidatePath(`/candidates/${candidateId}`);
  return r.ok ? { ok: true, data: normalizeInterviewRecording(r.data) } : r;
}

export async function deleteRecordingAction(
  recordingId: string,
  candidateId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/recordings/${recordingId}`, { method: "DELETE" });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: null };
}

// ---------- Technical task submissions ----------

export async function listTasksAction(
  candidateId: string,
): Promise<ActionResult<TaskSubmission[]>> {
  const result = await _getAll<ApiResponseSchema<"TaskOut">>(
    `/candidates/${candidateId}/tasks`,
  );
  return result.ok
    ? { ok: true, data: result.data.map(normalizeTaskSubmission) }
    : result;
}

export async function createTaskAction(
  candidateId: string,
  body: {
    title: string;
    prompt?: string | null;
    submission_type: "repo" | "file" | "text";
    repo_url?: string | null;
    content?: string | null;
    position_id?: string | null;
  },
): Promise<ActionResult<TaskSubmission>> {
  const r = await _post<ApiResponseSchema<"TaskOut">>(
    `/candidates/${candidateId}/tasks`,
    body,
  );
  if (r.ok) revalidatePath(`/candidates/${candidateId}`);
  return r.ok ? { ok: true, data: normalizeTaskSubmission(r.data) } : r;
}

export async function evaluateTaskAction(
  taskId: string,
  candidateId: string,
): Promise<ActionResult<TaskSubmission>> {
  const res = await apiFetch(`/tasks/${taskId}/evaluate`, {
    method: "POST",
    body: "{}",
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return {
    ok: true,
    data: normalizeTaskSubmission(
      (await res.json()) as ApiResponseSchema<"TaskOut">,
    ),
  };
}

export async function deleteTaskAction(
  taskId: string,
  candidateId: string,
): Promise<ActionResult> {
  const res = await apiFetch(`/tasks/${taskId}`, { method: "DELETE" });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath(`/candidates/${candidateId}`);
  return { ok: true, data: null };
}

// ---------- Candidate communications ----------

export type CandidateCommunicationComposeInput =
  ApiSchema<"CandidateCommunicationCompose">;

export async function listCandidateContactPointsAction(
  candidateId: string,
): Promise<ActionResult<CandidateContactPoint[]>> {
  return _getAll<CandidateContactPoint>(
    `/candidates/${candidateId}/contact-points`,
  );
}

export async function createCandidateContactPointAction(
  candidateId: string,
  body: {
    channel: "email" | "phone" | "telegram";
    value: string;
    is_primary?: boolean;
  },
): Promise<ActionResult<CandidateContactPoint>> {
  const result = await _post<CandidateContactPoint>(
    `/candidates/${candidateId}/contact-points`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function updateCandidateContactPointAction(
  candidateId: string,
  contactPointId: string,
  body: {
    value?: string;
    is_primary?: boolean;
    is_opted_out?: boolean;
  },
): Promise<ActionResult<CandidateContactPoint>> {
  const result = await _patch<CandidateContactPoint>(
    `/candidates/${candidateId}/contact-points/${contactPointId}`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function listCommunicationAccountsAction(): Promise<
  ActionResult<CommunicationAccount[]>
> {
  return _getAll<CommunicationAccount>("/communications/accounts");
}

export async function getCommunicationCapabilitiesAction(): Promise<
  ActionResult<CommunicationCapabilities>
> {
  const res = await apiFetch("/communications/capabilities");
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as CommunicationCapabilities };
}

export async function listCommunicationConnectionsAction(): Promise<
  ActionResult<CommunicationConnectionOption[]>
> {
  const page = await _getAll<CommunicationConnectionOption>("/integrations");
  if (!page.ok) return page;
  return {
    ok: true,
    data: page.data.filter((item) =>
      ["google_workspace", "microsoft_365", "telegram"].includes(item.provider),
    ),
  };
}

export async function createCommunicationAccountAction(body: {
  channel: "email" | "telegram";
  provider: "sandbox" | "google_workspace" | "microsoft_365" | "telegram";
  integration_connection_id?: string | null;
  name: string;
  sender_label?: string | null;
  configuration?: Record<string, unknown>;
}): Promise<ActionResult<CommunicationAccountCreateResult>> {
  const result = await _post<CommunicationAccountCreateResult>(
    "/communications/accounts",
    body,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function rotateCommunicationWebhookSecretAction(
  accountId: string,
): Promise<ActionResult<ApiResponseSchema<"WebhookSecretOut">>> {
  return _post<ApiResponseSchema<"WebhookSecretOut">>(
    `/communications/accounts/${accountId}/rotate-webhook-secret`,
    {},
  );
}

export async function listCommunicationTemplatesAction(options?: {
  includeInactive?: boolean;
}): Promise<ActionResult<CommunicationTemplate[]>> {
  const params = new URLSearchParams();
  if (options?.includeInactive) params.set("include_inactive", "true");
  const query = params.size ? `?${params.toString()}` : "";
  return _getAll<CommunicationTemplate>(`/communications/templates${query}`);
}

export async function createCommunicationTemplateAction(body: {
  name: string;
  channel: "email" | "telegram";
  language: string;
  subject_template?: string | null;
  body_template: string;
  is_active?: boolean;
}): Promise<ActionResult<CommunicationTemplate>> {
  const result = await _post<CommunicationTemplate>(
    "/communications/templates",
    body,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function updateCommunicationTemplateAction(
  templateId: string,
  body: {
    name?: string;
    language?: string;
    subject_template?: string | null;
    body_template?: string;
    is_active?: boolean;
  },
): Promise<ActionResult<CommunicationTemplate>> {
  const result = await _patch<CommunicationTemplate>(
    `/communications/templates/${templateId}`,
    body,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function previewCandidateCommunicationAction(
  candidateId: string,
  body: CandidateCommunicationComposeInput,
): Promise<ActionResult<CandidateCommunicationPreview>> {
  return _post<CandidateCommunicationPreview>(
    `/candidates/${candidateId}/communications/preview`,
    body,
  );
}

export async function sendCandidateCommunicationAction(
  candidateId: string,
  body: CandidateCommunicationComposeInput & {
    idempotency_key: string;
    scheduled_for?: string | null;
  },
): Promise<ActionResult<CommunicationMessage>> {
  const result = await _post<CommunicationMessage>(
    `/candidates/${candidateId}/communications/send`,
    body,
  );
  if (result.ok) {
    revalidatePath(`/candidates/${candidateId}`);
    revalidatePath("/conversations");
  }
  return result;
}

export async function listCandidateCommunicationThreadsAction(
  candidateId: string,
): Promise<ActionResult<CommunicationThread[]>> {
  return _getAll<CommunicationThread>(
    `/candidates/${candidateId}/communication-threads`,
  );
}

export async function listCommunicationThreadsAction(options?: {
  cursor?: string | null;
  channel?: "email" | "telegram" | "whatsapp" | null;
  unread?: boolean | null;
}): Promise<ActionResult<CommunicationThreadPage>> {
  const params = new URLSearchParams({ limit: "30" });
  if (options?.cursor) params.set("cursor", options.cursor);
  if (options?.channel) params.set("channel", options.channel);
  if (options?.unread != null) params.set("unread", String(options.unread));
  const res = await apiFetch(`/communications/threads?${params.toString()}`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as CommunicationThreadPage };
}

export async function markCommunicationThreadReadAction(
  threadId: string,
): Promise<ActionResult<CommunicationThread>> {
  const result = await _post<CommunicationThread>(
    `/communications/threads/${threadId}/read`,
    {},
  );
  if (result.ok) revalidatePath("/conversations");
  return result;
}

export async function listCommunicationMessagesAction(
  threadId: string,
  cursor?: string | null,
): Promise<ActionResult<CommunicationMessagePage>> {
  const params = new URLSearchParams({ limit: "50" });
  if (cursor) params.set("cursor", cursor);
  const res = await apiFetch(
    `/communications/threads/${threadId}/messages?${params.toString()}`,
  );
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as CommunicationMessagePage };
}

export async function cancelCommunicationMessageAction(
  messageId: string,
  candidateId?: string,
): Promise<ActionResult<CommunicationMessage>> {
  const result = await _post<CommunicationMessage>(
    `/communications/messages/${messageId}/cancel`,
    {},
  );
  if (result.ok) {
    if (candidateId) revalidatePath(`/candidates/${candidateId}`);
    revalidatePath("/conversations");
  }
  return result;
}

export async function previewBulkCommunicationAction(body: {
  candidate_ids: string[];
  channel: "email" | "telegram";
  contact_point_id?: string | null;
  account_id?: string | null;
  template_id?: string | null;
  position_id?: string | null;
  subject?: string | null;
  body?: string | null;
}): Promise<ActionResult<BulkCommunicationPreview>> {
  return _post<BulkCommunicationPreview>("/communications/bulk/preview", body);
}

export async function createBulkCommunicationAction(
  body: {
    candidate_ids: string[];
    channel: "email" | "telegram";
    contact_point_id?: string | null;
    account_id?: string | null;
    template_id?: string | null;
    position_id?: string | null;
    subject?: string | null;
    body?: string | null;
    scheduled_for?: string | null;
    idempotency_key: string;
  },
): Promise<ActionResult<BulkCommunicationCreateResult>> {
  const result = await _post<BulkCommunicationCreateResult>(
    "/communications/bulk",
    body,
  );
  if (result.ok) {
    revalidatePath("/candidates");
    revalidatePath("/conversations");
  }
  return result;
}

// ---------- Interview scheduling ----------

export async function listCandidateSchedulingLinksAction(
  candidateId: string,
): Promise<ActionResult<SchedulingLink[]>> {
  return _getAll<SchedulingLink>(
    `/candidates/${candidateId}/scheduling-links`,
  );
}

export async function createCandidateSchedulingLinkAction(
  candidateId: string,
  body: SchedulingLinkCreate,
): Promise<ActionResult<SchedulingLink>> {
  const result = await _post<SchedulingLink>(
    `/candidates/${candidateId}/scheduling-links`,
    body,
  );
  return result;
}

export async function rotateSchedulingLinkAction(
  linkId: string,
): Promise<ActionResult<SchedulingLink>> {
  const result = await _post<SchedulingLink>(
    `/scheduling-links/${linkId}/rotate`,
    {},
  );
  return result;
}

export async function getSchedulingPermissionsAction(): Promise<
  ActionResult<EffectivePermissions>
> {
  const res = await apiFetch("/enterprise/permissions/me");
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as EffectivePermissions };
}

export async function revokeSchedulingLinkAction(
  linkId: string,
  candidateId: string,
): Promise<ActionResult<void>> {
  const result = await _delete<void>(`/scheduling-links/${linkId}`);
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function listCandidateAppointmentsAction(
  candidateId: string,
): Promise<ActionResult<InterviewAppointment[]>> {
  return _getAll<InterviewAppointment>(
    `/candidates/${candidateId}/appointments`,
  );
}

export async function getMyAvailabilityAction(): Promise<
  ActionResult<Availability>
> {
  const res = await apiFetch("/calendar/availability/me");
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as Availability };
}

export async function updateMyAvailabilityAction(
  availability: Availability,
): Promise<ActionResult<Availability>> {
  const result = await _put<Availability>(
    "/calendar/availability/me",
    availability,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function listAvailabilityBlocksAction(): Promise<
  ActionResult<AvailabilityBlock[]>
> {
  return _getAll<AvailabilityBlock>("/calendar/availability/blocks");
}

export async function createAvailabilityBlockAction(body: {
  starts_at: string;
  ends_at: string;
}): Promise<ActionResult<AvailabilityBlock>> {
  const result = await _post<AvailabilityBlock>(
    "/calendar/availability/blocks",
    body,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function deleteAvailabilityBlockAction(
  blockId: string,
): Promise<ActionResult<void>> {
  const result = await _delete<void>(`/calendar/availability/blocks/${blockId}`);
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function listCalendarProvidersAction(): Promise<
  ActionResult<CalendarProvider[]>
> {
  return _getAll<CalendarProvider>("/calendar/providers");
}

// ---------- Hiring-team scorecards ----------

export async function listScorecardTemplatesAction(options?: {
  positionId?: string | null;
  includeInactive?: boolean;
}): Promise<ActionResult<ScorecardTemplate[]>> {
  const params = new URLSearchParams();
  if (options?.positionId) params.set("position_id", options.positionId);
  if (options?.includeInactive) params.set("include_inactive", "true");
  const suffix = params.size ? `?${params.toString()}` : "";
  return _getAll<ScorecardTemplate>(`/scorecard-templates${suffix}`);
}

export async function createScorecardTemplateAction(body: {
  name: string;
  description?: string | null;
  position_id?: string | null;
  criteria: ScorecardCriterionCreate[];
}): Promise<ActionResult<ScorecardTemplate>> {
  const result = await _post<ScorecardTemplate>("/scorecard-templates", body);
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function updateScorecardTemplateAction(
  templateId: string,
  body: { name?: string; description?: string | null; is_active?: boolean },
): Promise<ActionResult<ScorecardTemplate>> {
  const result = await _patch<ScorecardTemplate>(
    `/scorecard-templates/${templateId}`,
    body,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function getCandidateScorecardsAction(
  candidateId: string,
  positionId?: string | null,
): Promise<ActionResult<ScorecardPanel>> {
  const params = new URLSearchParams();
  if (positionId) params.set("position_id", positionId);
  const suffix = params.size ? `?${params.toString()}` : "";
  const res = await apiFetch(`/candidates/${candidateId}/scorecards${suffix}`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as ScorecardPanel };
}

export async function assignCandidateScorecardsAction(
  candidateId: string,
  body: {
    position_id: string;
    template_id: string;
    user_ids: string[];
    due_at?: string | null;
  },
): Promise<ActionResult<ScorecardPanel>> {
  const result = await _post<ScorecardPanel>(
    `/candidates/${candidateId}/scorecards/assign`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function updateScorecardDraftAction(
  assignmentId: string,
  ratings: Array<{
    criterion_id: string;
    rating: number;
    comment?: string | null;
  }>,
): Promise<ActionResult<ScorecardAssignment>> {
  return _patch<ScorecardAssignment>(
    `/scorecard-assignments/${assignmentId}`,
    { ratings },
  );
}

export async function submitScorecardAction(
  assignmentId: string,
  candidateId: string,
): Promise<ActionResult<ScorecardPanel>> {
  const result = await _post<ScorecardPanel>(
    `/scorecard-assignments/${assignmentId}/submit`,
    {},
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function saveHiringDecisionAction(
  candidateId: string,
  body: {
    position_id: string;
    recommendation: HiringRecommendation;
    summary?: string | null;
    required_approvals: number;
  },
): Promise<ActionResult<HiringDecision>> {
  const result = await _post<HiringDecision>(
    `/candidates/${candidateId}/decisions`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function voteHiringDecisionAction(
  decisionId: string,
  candidateId: string,
  body: { vote: "approve" | "reject"; comment?: string | null },
): Promise<ActionResult<HiringDecision>> {
  const result = await _post<HiringDecision>(
    `/hiring-decisions/${decisionId}/vote`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

// ---------- Workflow automation ----------

export async function listWorkflowRulesAction(options?: {
  includeInactive?: boolean;
}): Promise<ActionResult<WorkflowRule[]>> {
  const params = new URLSearchParams();
  if (options?.includeInactive) params.set("include_inactive", "true");
  const suffix = params.size ? `?${params.toString()}` : "";
  return _getAll<WorkflowRule>(`/workflows/rules${suffix}`);
}

export async function createWorkflowRuleAction(body: {
  name: string;
  trigger: WorkflowTrigger;
  conditions: WorkflowConditions;
  actions: WorkflowAction[];
  is_active: boolean;
}): Promise<ActionResult<WorkflowRule>> {
  const result = await _post<WorkflowRule>("/workflows/rules", body);
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function updateWorkflowRuleAction(
  ruleId: string,
  body: {
    name?: string;
    conditions?: WorkflowConditions;
    actions?: WorkflowAction[];
    is_active?: boolean;
  },
): Promise<ActionResult<WorkflowRule>> {
  const result = await _patch<WorkflowRule>(`/workflows/rules/${ruleId}`, body);
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function disableWorkflowRuleAction(
  ruleId: string,
): Promise<ActionResult<void>> {
  const result = await _delete<void>(`/workflows/rules/${ruleId}`);
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function pauseWorkflowRuleAction(
  ruleId: string,
): Promise<ActionResult<WorkflowRule>> {
  const result = await _post<WorkflowRule>(
    `/workflows/rules/${ruleId}/pause`,
    {},
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function resumeWorkflowRuleAction(
  ruleId: string,
): Promise<ActionResult<WorkflowRule>> {
  const result = await _post<WorkflowRule>(
    `/workflows/rules/${ruleId}/resume`,
    {},
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function previewWorkflowRuleAction(
  ruleId: string,
  body: {
    candidate_id?: string | null;
    position_id?: string | null;
    source_activity_id?: string | null;
    limit?: number;
  } = {},
): Promise<ActionResult<WorkflowDryRun>> {
  return _post<WorkflowDryRun>(`/workflows/rules/${ruleId}/dry-run`, body);
}

export async function replayWorkflowRunAction(
  runId: string,
): Promise<ActionResult<WorkflowRun>> {
  const result = await _post<WorkflowRun>(
    `/workflows/runs/${runId}/replay`,
    {},
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function cancelWorkflowRunAction(
  runId: string,
): Promise<ActionResult<WorkflowRun>> {
  const result = await _post<WorkflowRun>(
    `/workflows/runs/${runId}/cancel`,
    {},
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function processWorkflowsAction(
  ruleId?: string | null,
): Promise<ActionResult<WorkflowProcessResult>> {
  const params = new URLSearchParams({ limit: "50" });
  if (ruleId) params.set("rule_id", ruleId);
  const res = await apiFetch(`/workflows/process?${params.toString()}`, {
    method: "POST",
  });
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  revalidatePath("/settings");
  return { ok: true, data: (await res.json()) as WorkflowProcessResult };
}

export async function listWorkflowRunsAction(options?: {
  cursor?: string | null;
  ruleId?: string | null;
  status?:
    | "queued"
    | "running"
    | "retrying"
    | "completed"
    | "skipped"
    | "failed"
    | "dead_letter"
    | "cancelled"
    | null;
}): Promise<ActionResult<WorkflowRunPage>> {
  const params = new URLSearchParams({ limit: "30" });
  if (options?.cursor) params.set("cursor", options.cursor);
  if (options?.ruleId) params.set("rule_id", options.ruleId);
  if (options?.status) params.set("status", options.status);
  const res = await apiFetch(`/workflows/runs?${params.toString()}`);
  if (!res.ok) return { ok: false, error: await apiProblemMessage(res) };
  return { ok: true, data: (await res.json()) as WorkflowRunPage };
}

// ---------- Requisitions, offers, and handoff ----------

export async function listRequisitionsAction(): Promise<
  ActionResult<Requisition[]>
> {
  return _getAll<Requisition>("/requisitions");
}

export async function createRequisitionAction(body: {
  title: string;
  department?: string | null;
  business_justification: string;
  headcount: number;
  hiring_manager_user_id?: string | null;
  salary_min?: number | null;
  salary_max?: number | null;
  salary_currency?: string;
  salary_confidential?: boolean;
  required_approvals: number;
}): Promise<ActionResult<Requisition>> {
  const result = await _post<Requisition>("/requisitions", body);
  if (result.ok) revalidatePath("/requisitions");
  return result;
}

export async function updateRequisitionAction(
  requisitionId: string,
  body: {
    title?: string;
    department?: string | null;
    business_justification?: string;
    headcount?: number;
    hiring_manager_user_id?: string | null;
    salary_min?: number | null;
    salary_max?: number | null;
    salary_currency?: string;
    salary_confidential?: boolean;
    required_approvals?: number;
  },
): Promise<ActionResult<Requisition>> {
  const result = await _patch<Requisition>(
    `/requisitions/${requisitionId}`,
    body,
  );
  if (result.ok) revalidatePath("/requisitions");
  return result;
}

export async function submitRequisitionAction(
  requisitionId: string,
): Promise<ActionResult<Requisition>> {
  const result = await _post<Requisition>(
    `/requisitions/${requisitionId}/submit`,
    {},
  );
  if (result.ok) revalidatePath("/requisitions");
  return result;
}

export async function voteRequisitionAction(
  requisitionId: string,
  body: { vote: "approve" | "reject"; comment?: string | null },
): Promise<ActionResult<Requisition>> {
  const result = await _post<Requisition>(
    `/requisitions/${requisitionId}/vote`,
    body,
  );
  if (result.ok) revalidatePath("/requisitions");
  return result;
}

export async function openRequisitionAction(
  requisitionId: string,
  body: {
    description?: string | null;
    requirements?: Record<string, unknown> | null;
    public_apply_enabled: boolean;
  },
): Promise<ActionResult<Requisition>> {
  const result = await _post<Requisition>(
    `/requisitions/${requisitionId}/open`,
    body,
  );
  if (result.ok) {
    revalidatePath("/requisitions");
    revalidatePath("/dashboard");
  }
  return result;
}

export async function cancelRequisitionAction(
  requisitionId: string,
): Promise<ActionResult<Requisition>> {
  const result = await _post<Requisition>(
    `/requisitions/${requisitionId}/cancel`,
    {},
  );
  if (result.ok) revalidatePath("/requisitions");
  return result;
}

export async function listOfferTemplatesAction(): Promise<
  ActionResult<OfferTemplate[]>
> {
  return _getAll<OfferTemplate>("/offer-templates");
}

export async function createOfferTemplateAction(body: {
  name: string;
  subject_template: string;
  body_template: string;
}): Promise<ActionResult<OfferTemplate>> {
  const result = await _post<OfferTemplate>("/offer-templates", body);
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function updateOfferTemplateAction(
  templateId: string,
  body: {
    name?: string;
    subject_template?: string;
    body_template?: string;
    is_active?: boolean;
  },
): Promise<ActionResult<OfferTemplate>> {
  const result = await _patch<OfferTemplate>(
    `/offer-templates/${templateId}`,
    body,
  );
  if (result.ok) revalidatePath("/settings");
  return result;
}

export async function listCandidateOffersAction(
  candidateId: string,
): Promise<ActionResult<CandidateOffer[]>> {
  return _getAll<CandidateOffer>(`/candidates/${candidateId}/offers`);
}

export async function createCandidateOfferAction(
  candidateId: string,
  body: {
    position_id: string;
    template_id?: string | null;
    subject?: string | null;
    body?: string | null;
    salary_amount?: number | null;
    salary_currency: string;
    start_date?: string | null;
    expires_at?: string | null;
    required_approvals: number;
  },
): Promise<ActionResult<CandidateOffer>> {
  const result = await _post<CandidateOffer>(
    `/candidates/${candidateId}/offers`,
    body,
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function updateCandidateOfferAction(
  candidateId: string,
  offerId: string,
  body: {
    subject?: string;
    body?: string;
    salary_amount?: number | null;
    salary_currency?: string;
    start_date?: string | null;
    expires_at?: string | null;
    required_approvals?: number;
  },
): Promise<ActionResult<CandidateOffer>> {
  const result = await _patch<CandidateOffer>(`/offers/${offerId}`, body);
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function submitCandidateOfferAction(
  candidateId: string,
  offerId: string,
): Promise<ActionResult<CandidateOffer>> {
  const result = await _post<CandidateOffer>(`/offers/${offerId}/submit`, {});
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function voteCandidateOfferAction(
  candidateId: string,
  offerId: string,
  body: { vote: "approve" | "reject"; comment?: string | null },
): Promise<ActionResult<CandidateOffer>> {
  const result = await _post<CandidateOffer>(`/offers/${offerId}/vote`, body);
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function sendCandidateOfferAction(
  candidateId: string,
  offerId: string,
): Promise<ActionResult<CandidateOfferSendResult>> {
  const result = await _post<CandidateOfferSendResult>(
    `/offers/${offerId}/send`,
    {},
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function rotateCandidateOfferLinkAction(
  candidateId: string,
  offerId: string,
): Promise<ActionResult<CandidateOfferSendResult>> {
  const result = await _post<CandidateOfferSendResult>(
    `/offers/${offerId}/rotate`,
    { expires_in_days: 14 },
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function withdrawCandidateOfferAction(
  candidateId: string,
  offerId: string,
): Promise<ActionResult<CandidateOffer>> {
  const result = await _post<CandidateOffer>(
    `/offers/${offerId}/withdraw`,
    {},
  );
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}

export async function listCandidateHandoffsAction(
  candidateId: string,
): Promise<ActionResult<CandidateHandoff[]>> {
  return _getAll<CandidateHandoff>(`/candidates/${candidateId}/handoffs`);
}

export async function updateCandidateHandoffAction(
  candidateId: string,
  handoffId: string,
  checklist: Partial<Record<HandoffChecklistItem, boolean>>,
): Promise<ActionResult<CandidateHandoff>> {
  const result = await _patch<CandidateHandoff>(`/handoffs/${handoffId}`, {
    checklist,
  });
  if (result.ok) revalidatePath(`/candidates/${candidateId}`);
  return result;
}
