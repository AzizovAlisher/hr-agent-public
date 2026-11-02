import type {
  ApiResponseSchema,
  ApiSchema,
} from "@/lib/generated/contracts";

/** @ui-only Type utility for generated-schema presentation refinements. */
type Override<Base, Fields> = Omit<Base, keyof Fields> & Fields;

export type AiSessionStatus = ApiResponseSchema<"AiSessionOut">["status"];
export type HiringMissionStatus =
  ApiResponseSchema<"HiringMissionOut">["status"];
export type AssessmentConfidence =
  ApiResponseSchema<"MissionCandidateAssessmentOut">["confidence"];
export type AssessmentRecommendation =
  ApiResponseSchema<"MissionCandidateAssessmentOut">["recommendation"];
export type AiActionBundleStatus =
  ApiResponseSchema<"AiActionBundleOut">["status"];
export type AiActorKind = ApiResponseSchema<"MissionEventOut">["actor_kind"];

/** @ui-only Normalized view of a mission brief stored as opaque JSON by the API. */
export type CompensationRange = {
  min: number;
  max: number;
  currency: string;
};

/** @ui-only Normalized view of a mission brief stored as opaque JSON by the API. */
export type HiringBrief = {
  role_title: string;
  headcount: number;
  responsibilities: string[];
  must_haves: string[];
  preferences: string[];
  location: string | null;
  work_format: string | null;
  compensation: CompensationRange | null;
  deadline: string | null;
  shortlist_size: number;
  assumptions: string[];
  blockers: string[];
};

/** @ui-only Normalized rubric criterion read from opaque mission JSON. */
export type HiringRubricCriterion = {
  key: string;
  label?: string;
  description?: string;
  weight: number;
  must_have?: boolean;
};

/** @ui-only Normalized rubric read from opaque mission JSON. */
export type HiringRubric = {
  criteria: HiringRubricCriterion[];
  total_weight?: number;
};

/** @ui-only Validated attachment metadata for the untyped AI attachment response. */
export type AiAttachment = {
  id: string;
  filename: string;
  media_type?: string | null;
  size_bytes?: number | null;
  status?: "uploaded" | "processing" | "ready" | "failed";
};

/** @ui-only Validated upload view returned by the AI attachment BFF. */
export type AiAttachmentUpload = AiAttachment & {
  sha256: string;
  upload_token: string;
};

/** @ui-only Validated message view from the untyped AI session/SSE responses. */
export type AiMessage = {
  id: string;
  session_id: string;
  sequence: number;
  role: "user" | "assistant" | "system" | "tool";
  status: "streaming" | "completed" | "failed";
  content: string;
  structured_content: Record<string, unknown> | null;
  attachments: AiAttachment[];
  redaction_metadata?: Record<string, unknown> | null;
  actor_user_id?: string | null;
  created_at: string;
  completed_at?: string | null;
};

export type AiSession = ApiResponseSchema<"AiSessionOut">;

/** @ui-only Validated plan view from the untyped mission-detail response. */
export type MissionPlanVersion = {
  id: string;
  mission_id: string;
  version: number;
  status: "active" | "superseded" | "completed" | "cancelled";
  summary?: string | null;
  steps?: MissionStep[];
  created_at: string;
};

/** @ui-only Validated step view from the untyped mission-detail response. */
export type MissionStep = {
  id: string;
  mission_id: string;
  plan_version?: number;
  plan_version_id?: string;
  sequence?: number;
  ordinal?: number;
  key?: string;
  step_type: string;
  title?: string;
  description?: string | null;
  status:
    | "pending"
    | "ready"
    | "running"
    | "completed"
    | "failed"
    | "blocked"
    | "cancelled"
    | "skipped";
  depends_on?: string[];
  dependencies?: string[];
  result?: Record<string, unknown> | null;
  safe_result?: Record<string, unknown> | null;
  error_code?: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
};

/** @ui-only Status projection from the normalized mission-step view. */
export type MissionStepStatus = MissionStep["status"];

/**
 * @ui-only Normalized mission view. The list fields come from HiringMissionOut;
 * plan/steps/counts are added only after validating the untyped detail payload.
 */
export type HiringMission = Override<
  ApiResponseSchema<"HiringMissionOut">,
  {
    brief: HiringBrief | null;
    rubric: HiringRubric | null;
  }
> & {
  model_budget_cents: number | null;
  plan?: MissionPlanVersion | null;
  steps?: MissionStep[];
  candidate_count?: number;
  shortlisted_count?: number;
  pending_approval_count?: number;
};

export type MissionAssessmentCriterion =
  ApiResponseSchema<"CriterionAssessment">;

/** @ui-only Validated evidence view from the assessment's opaque evidence JSON. */
export type EvidenceClaim = {
  id: string;
  mission_id: string;
  candidate_id: string;
  assessment_id?: string | null;
  claim: string;
  source_type: string;
  source_id?: string | null;
  source_label?: string | null;
  excerpt?: string | null;
  created_at: string;
};

/** @ui-only Validated candidate summary from opaque assessment JSON. */
export type AssessmentCandidate = {
  id: string;
  display_name?: string | null;
  name?: string | null;
  source?: string | null;
  current_title?: string | null;
};

/** @ui-only Generated assessment plus normalized opaque candidate/evidence views. */
export type MissionCandidateAssessment = Override<
  ApiResponseSchema<"MissionCandidateAssessmentOut">,
  {
    candidate: AssessmentCandidate | null;
    criteria: MissionAssessmentCriterion[];
    evidence: EvidenceClaim[];
  }
>;

export type AiActionBundle = ApiResponseSchema<"AiActionBundleOut">;
export type MissionEvent = ApiResponseSchema<"MissionEventOut">;

/** @ui-only Validated settings view; the current settings endpoints return untyped JSON. */
type AiSettingsUpdate = ApiSchema<"AiOrganizationSettingsUpdate">;
export type AiOrganizationSettings = {
  feature_enabled: NonNullable<AiSettingsUpdate["feature_enabled"]>;
  default_headcount: NonNullable<AiSettingsUpdate["default_headcount"]>;
  default_shortlist_size: NonNullable<AiSettingsUpdate["default_shortlist_size"]>;
  default_model_budget_cents: number | null;
  hiring_defaults: NonNullable<AiSettingsUpdate["hiring_defaults"]>;
  autonomy_policy: NonNullable<AiSettingsUpdate["autonomy_policy"]>;
  updated_at?: string | null;
};

/** @ui-only Compatibility page view used while accepting legacy array payloads. */
export type AiListPage<T> = {
  items: T[];
  next_cursor: string | null;
};

/** @ui-only Validated view from the untyped AI session-detail response. */
export type AiSessionDetail = AiSession & {
  messages: AiMessage[];
  mission: HiringMission | null;
};

/** @ui-only Normalized discriminated events emitted by the AI SSE parser. */
export type AiMessageStreamEvent =
  | { type: "session"; session: AiSession }
  | { type: "message"; message: AiMessage }
  | { type: "message_delta"; message_id?: string; delta: string }
  | { type: "mission"; mission: HiringMission }
  | { type: "canvas"; name: string; data: Record<string, unknown> }
  | { type: "done"; session_id?: string; mission_id?: string }
  | { type: "error"; message: string };
