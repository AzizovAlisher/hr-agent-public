import type {
  AiActionBundle,
  AiAttachment,
  AiAttachmentUpload,
  AiMessage,
  AiOrganizationSettings,
  AiSession,
  AiSessionDetail,
  AssessmentCandidate,
  EvidenceClaim,
  HiringBrief,
  HiringMission,
  HiringRubric,
  MissionCandidateAssessment,
  MissionStep,
} from "@/lib/ai/types";
import type { ApiResponseSchema } from "@/lib/generated/contracts";

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid ${label} response`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string") throw new Error(`Invalid ${label} response`);
  return value;
}

function nullableText(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return text(value, label);
}

function count(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid ${label} response`);
  }
  return value;
}

function texts(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function optionalCount(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function optionalRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function normalizeBrief(value: unknown): HiringBrief | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (typeof item.role_title !== "string") return null;
  const compensation = optionalRecord(item.compensation);
  const normalizedCompensation =
    typeof compensation.min === "number" &&
    typeof compensation.max === "number" &&
    typeof compensation.currency === "string"
      ? {
          min: compensation.min,
          max: compensation.max,
          currency: compensation.currency,
        }
      : null;
  return {
    role_title: item.role_title,
    headcount: optionalCount(item.headcount) ?? 1,
    responsibilities: texts(item.responsibilities),
    must_haves: texts(item.must_haves),
    preferences: texts(item.preferences),
    location: nullableText(item.location, "mission brief location"),
    work_format: nullableText(item.work_format, "mission brief work format"),
    compensation: normalizedCompensation,
    deadline: nullableText(item.deadline, "mission brief deadline"),
    shortlist_size: optionalCount(item.shortlist_size) ?? 5,
    assumptions: texts(item.assumptions),
    blockers: texts(item.blockers),
  };
}

function normalizeRubric(value: unknown): HiringRubric | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  const criteria = Array.isArray(item.criteria)
    ? item.criteria.flatMap((raw) => {
        const criterion = optionalRecord(raw);
        if (typeof criterion.key !== "string" || typeof criterion.weight !== "number") {
          return [];
        }
        return [{
          key: criterion.key,
          label: typeof criterion.label === "string" ? criterion.label : undefined,
          description:
            typeof criterion.description === "string" ? criterion.description : undefined,
          weight: criterion.weight,
          must_have:
            typeof criterion.must_have === "boolean" ? criterion.must_have : undefined,
        }];
      })
    : [];
  return {
    criteria,
    total_weight: optionalCount(item.total_weight),
  };
}

export function normalizeAiSession(value: unknown): AiSession {
  const item = record(value, "AI session");
  const status = text(item.status, "AI session status");
  if (status !== "active" && status !== "archived") {
    throw new Error("Invalid AI session status response");
  }
  const missionStatus = nullableText(item.mission_status, "AI mission status");
  const validMissionStatuses = new Set([
    "draft",
    "planning",
    "running",
    "awaiting_approval",
    "completed",
    "stopped",
    "failed",
  ]);
  if (missionStatus !== null && !validMissionStatuses.has(missionStatus)) {
    throw new Error("Invalid AI mission status response");
  }
  return {
    archived_at: nullableText(item.archived_at, "AI session archived_at"),
    created_at: text(item.created_at, "AI session created_at"),
    created_by_user_id: nullableText(item.created_by_user_id, "AI session creator"),
    creator_user_id: nullableText(
      item.creator_user_id ?? item.created_by_user_id,
      "AI session creator",
    ),
    id: text(item.id, "AI session id"),
    mission_id: nullableText(item.mission_id, "AI session mission_id"),
    mission_status: missionStatus as AiSession["mission_status"],
    status,
    title: nullableText(item.title, "AI session title"),
    updated_at: text(item.updated_at, "AI session updated_at"),
  };
}

export function normalizeHiringMission(value: unknown): HiringMission {
  const item = record(value, "hiring mission");
  const status = text(item.status, "hiring mission status");
  const validStatuses = new Set([
    "draft",
    "planning",
    "running",
    "awaiting_approval",
    "completed",
    "stopped",
    "failed",
  ]);
  if (!validStatuses.has(status)) throw new Error("Invalid hiring mission status response");
  const steps = Array.isArray(item.steps)
    ? item.steps.map((raw): MissionStep => {
        const step = record(raw, "mission step");
        return {
          id: text(step.id, "mission step id"),
          mission_id:
            typeof step.mission_id === "string" ? step.mission_id : text(item.id, "mission id"),
          plan_version: optionalCount(step.plan_version),
          plan_version_id:
            typeof step.plan_version_id === "string" ? step.plan_version_id : undefined,
          sequence: optionalCount(step.sequence),
          ordinal: optionalCount(step.ordinal),
          key: typeof step.key === "string" ? step.key : undefined,
          step_type: text(step.step_type, "mission step type"),
          title: typeof step.title === "string" ? step.title : undefined,
          description: nullableText(step.description, "mission step description"),
          status: text(step.status, "mission step status") as MissionStep["status"],
          depends_on: texts(step.depends_on),
          dependencies: texts(step.dependencies),
          result: optionalRecord(step.result),
          safe_result: optionalRecord(step.safe_result),
          error_code: nullableText(step.error_code, "mission step error"),
          created_at:
            typeof step.created_at === "string"
              ? step.created_at
              : text(item.created_at, "mission created_at"),
          started_at: nullableText(step.started_at, "mission step started_at"),
          completed_at: nullableText(step.completed_at, "mission step completed_at"),
        };
      })
    : undefined;
  return {
    assumptions: texts(item.assumptions),
    blockers: texts(item.blockers),
    brief: normalizeBrief(item.brief),
    completed_at: nullableText(item.completed_at, "mission completed_at"),
    correlation_id: text(item.correlation_id, "mission correlation_id"),
    created_at: text(item.created_at, "mission created_at"),
    created_by_user_id: nullableText(item.created_by_user_id, "mission creator"),
    current_plan_version: count(item.current_plan_version, "mission plan version"),
    headcount: count(item.headcount, "mission headcount"),
    id: text(item.id, "mission id"),
    last_error_code: nullableText(item.last_error_code, "mission error"),
    model_budget_cents:
      item.model_budget_cents === null
        ? null
        : count(item.model_budget_cents, "mission model budget"),
    original_request: text(item.original_request, "mission request"),
    position_id: nullableText(item.position_id, "mission position_id"),
    rubric: normalizeRubric(item.rubric),
    session_id: text(item.session_id, "mission session_id"),
    shortlist_size: count(item.shortlist_size, "mission shortlist size"),
    started_at: nullableText(item.started_at, "mission started_at"),
    status: status as HiringMission["status"],
    stopped_at: nullableText(item.stopped_at, "mission stopped_at"),
    title: text(item.title, "mission title"),
    updated_at: text(item.updated_at, "mission updated_at"),
    steps,
    candidate_count: optionalCount(item.candidate_count),
    shortlisted_count: optionalCount(item.shortlisted_count),
    pending_approval_count: optionalCount(item.pending_approval_count),
  };
}

function normalizeAttachment(value: unknown): AiAttachment {
  const item = record(value, "AI attachment");
  return {
    id: text(item.id, "AI attachment id"),
    filename: text(item.filename, "AI attachment filename"),
    media_type:
      item.media_type === undefined
        ? undefined
        : nullableText(item.media_type, "AI attachment media type"),
    size_bytes: item.size_bytes === undefined ? undefined : count(item.size_bytes, "AI attachment size"),
    status:
      item.status === "uploaded" ||
      item.status === "processing" ||
      item.status === "ready" ||
      item.status === "failed"
        ? item.status
        : undefined,
  };
}

export function normalizeAiAttachmentUpload(value: unknown): AiAttachmentUpload {
  const item = record(value, "AI attachment upload");
  return {
    ...normalizeAttachment(item),
    sha256: text(item.sha256, "AI attachment sha256"),
    upload_token: text(item.upload_token, "AI attachment upload token"),
  };
}

export function normalizeAiMessage(value: unknown): AiMessage {
  const item = record(value, "AI message");
  const role = text(item.role, "AI message role");
  const status = text(item.status, "AI message status");
  if (!["user", "assistant", "system", "tool"].includes(role)) {
    throw new Error("Invalid AI message role response");
  }
  if (!["streaming", "completed", "failed"].includes(status)) {
    throw new Error("Invalid AI message status response");
  }
  return {
    id: text(item.id, "AI message id"),
    session_id: text(item.session_id, "AI message session_id"),
    sequence: count(item.sequence, "AI message sequence"),
    role: role as AiMessage["role"],
    status: status as AiMessage["status"],
    content: text(item.content, "AI message content"),
    structured_content: optionalRecord(item.structured_content),
    attachments: Array.isArray(item.attachments)
      ? item.attachments.map(normalizeAttachment)
      : [],
    redaction_metadata: optionalRecord(item.redaction_metadata),
    actor_user_id: nullableText(item.actor_user_id, "AI message actor"),
    created_at: text(item.created_at, "AI message created_at"),
    completed_at: nullableText(item.completed_at, "AI message completed_at"),
  };
}

export function normalizeAiSessionDetail(value: unknown): AiSessionDetail {
  const item = record(value, "AI session detail");
  return {
    ...normalizeAiSession(item),
    messages: Array.isArray(item.messages) ? item.messages.map(normalizeAiMessage) : [],
    mission: item.mission ? normalizeHiringMission(item.mission) : null,
  };
}

function normalizeCandidate(value: unknown): AssessmentCandidate | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (typeof item.id !== "string") return null;
  return {
    id: item.id,
    display_name: nullableText(item.display_name, "assessment candidate name"),
    name: nullableText(item.name, "assessment candidate name"),
    source: nullableText(item.source, "assessment candidate source"),
    current_title: nullableText(item.current_title, "assessment candidate title"),
  };
}

function normalizeEvidence(value: unknown): EvidenceClaim | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    typeof item.id !== "string" ||
    typeof item.mission_id !== "string" ||
    typeof item.candidate_id !== "string" ||
    typeof item.claim !== "string" ||
    typeof item.source_type !== "string" ||
    typeof item.created_at !== "string"
  ) {
    return null;
  }
  return {
    id: item.id,
    mission_id: item.mission_id,
    candidate_id: item.candidate_id,
    assessment_id: nullableText(item.assessment_id, "evidence assessment_id"),
    claim: item.claim,
    source_type: item.source_type,
    source_id: nullableText(item.source_id, "evidence source_id"),
    source_label: nullableText(item.source_label, "evidence source label"),
    excerpt: nullableText(item.excerpt, "evidence excerpt"),
    created_at: item.created_at,
  };
}

export function normalizeMissionAssessment(
  value: ApiResponseSchema<"MissionCandidateAssessmentOut">,
): MissionCandidateAssessment {
  return {
    ...value,
    candidate: normalizeCandidate(value.candidate),
    criteria: value.criteria,
    evidence: value.evidence.flatMap((item) => {
      const normalized = normalizeEvidence(item);
      return normalized ? [normalized] : [];
    }),
  };
}

export function normalizeAiActionBundle(value: unknown): AiActionBundle {
  const item = record(value, "AI action bundle");
  const status = text(item.status, "AI action bundle status");
  if (!["pending", "approved", "rejected", "executed", "failed", "stale"].includes(status)) {
    throw new Error("Invalid AI action bundle status response");
  }
  return {
    action_type: text(item.action_type, "AI action type"),
    approved_by_user_id: nullableText(item.approved_by_user_id, "AI approver"),
    created_at: text(item.created_at, "AI action bundle created_at"),
    decided_at: nullableText(item.decided_at, "AI decision time"),
    decision_comment: nullableText(item.decision_comment, "AI decision comment"),
    error: nullableText(item.error, "AI action error detail"),
    error_code: nullableText(item.error_code, "AI action error"),
    executed_at: nullableText(item.executed_at, "AI execution time"),
    execution_result: optionalRecord(item.execution_result ?? item.result),
    id: text(item.id, "AI action id"),
    mission_id: text(item.mission_id, "AI action mission_id"),
    plan_version: count(item.plan_version, "AI action plan version"),
    preview: optionalRecord(item.preview),
    required_permission: text(item.required_permission, "AI action permission"),
    result: optionalRecord(item.result ?? item.execution_result),
    status: status as AiActionBundle["status"],
    summary: text(item.summary, "AI action summary"),
  };
}

export function normalizeAiSettings(value: unknown): AiOrganizationSettings {
  const item = record(value, "AI settings");
  if (typeof item.feature_enabled !== "boolean") {
    throw new Error("Invalid AI settings response");
  }
  return {
    feature_enabled: item.feature_enabled,
    default_headcount: count(item.default_headcount, "AI default headcount"),
    default_shortlist_size: count(
      item.default_shortlist_size,
      "AI default shortlist size",
    ),
    default_model_budget_cents:
      item.default_model_budget_cents === null
        ? null
        : count(item.default_model_budget_cents, "AI budget"),
    hiring_defaults: optionalRecord(item.hiring_defaults),
    autonomy_policy: optionalRecord(item.autonomy_policy),
    updated_at: nullableText(item.updated_at, "AI settings updated_at"),
  };
}
