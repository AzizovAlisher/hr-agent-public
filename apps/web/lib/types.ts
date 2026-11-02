/**
 * Compatibility names for UI code, backed by the generated OpenAPI contract.
 *
 * Do not add handwritten API response/request objects here. Add or correct the
 * FastAPI schema, regenerate `lib/generated/api.ts`, and alias it below. The few
 * client refinements near the end exist only where OpenAPI intentionally exposes
 * opaque JSON payloads.
 */
import type {
  ApiOperationQuery,
  ApiResponseSchema,
  ApiSchema,
} from "@/lib/generated/contracts";

/** @ui-only Type utility for generated-schema presentation refinements. */
type Override<Base, Fields> = Omit<Base, keyof Fields> & Fields;

export type Notification = ApiResponseSchema<"NotificationOut">;
export type NotificationList = ApiResponseSchema<"NotificationListOut">;

export type PositionSensitivity = NonNullable<
  ApiSchema<"PositionUpdate">["sensitivity"]
>;
export type Position = Override<
  ApiResponseSchema<"PositionOut">,
  Record<"sensitivity", PositionSensitivity>
>;
export type Folder = ApiResponseSchema<"FolderOut"> &
  Partial<Pick<ApiResponseSchema<"FolderNode">, "children">>;

export type KanbanStage = ApiSchema<"StageUpdate">["stage"];
export type ApplicationDisposition = NonNullable<
  ApiSchema<"StageUpdate">["disposition_code"]
>;

export const KANBAN_STAGES: KanbanStage[] = [
  "new",
  "screening",
  "interview",
  "offer",
  "hired",
  "rejected",
];

export type RankedCandidate = Override<
  ApiResponseSchema<"RankedCandidate">,
  Record<"stage", KanbanStage>
>;
export type PositionDetail = Override<
  ApiResponseSchema<"PositionDetail">,
  Record<"sensitivity", PositionSensitivity>
>;
export type RankedCandidatePage = Override<
  ApiResponseSchema<"CursorPage_RankedCandidate_">,
  Record<"items", RankedCandidate[]>
>;
export type PositionMatch = ApiResponseSchema<"PositionMatch">;
export type CandidateProcessingJobReceipt = ApiResponseSchema<"CandidateProcessingJobOut">;
export type CandidateProcessingBatchReceipt = ApiResponseSchema<"CandidateProcessingBatchOut">;
export type Candidate = Override<
  ApiResponseSchema<"CandidateOut">,
  Record<"github_data", GitHubData | null>
>;
export type CandidateNote = ApiResponseSchema<"NoteOut">;
export type CandidateTag = ApiResponseSchema<"TagOut">;
export type Organization = ApiResponseSchema<"app__routers__organizations__OrganizationOut">;

export type AnalyticsBreakdown = ApiResponseSchema<"ApplicationAnalyticsBreakdown">;
export type AnalyticsStage = Override<
  ApiResponseSchema<"ApplicationStageAnalytics">,
  Record<"stage", KanbanStage>
>;
export type AnalyticsDrilldownDimension = NonNullable<
  NonNullable<
    ApiOperationQuery<"/analytics/pipeline", "get">
  >["drilldown_dimension"]
>;
export type ApplicationAnalytics = ApiResponseSchema<"ApplicationAnalyticsOut">;

export type ShortlistLink = ApiResponseSchema<"ShortlistOut">;
export type ShortlistLinkCreated = ApiResponseSchema<"ShortlistCreatedOut">;
export type PublicShortlist = ApiResponseSchema<"PublicShortlistOut">;
export type PublicShortlistCandidate = PublicShortlist["candidates"][number];

export type CandidateSummary = ApiResponseSchema<"CandidateSummary">;
export type CandidateSearchFilters = Override<
  ApiResponseSchema<"CandidateSearchFilters">,
  Record<"stages", KanbanStage[]>
>;
export type CandidateSearchApplication = Override<
  ApiResponseSchema<"CandidateSearchApplication">,
  Record<"stage", KanbanStage>
>;
export type CandidateSearchItem = Override<
  ApiResponseSchema<"CandidateSearchItem">,
  Record<"stages", KanbanStage[]> &
    Record<"applications", CandidateSearchApplication[]>
>;
export type CandidateSearchPage = Override<
  ApiResponseSchema<"CandidateSearchPage">,
  Record<"items", CandidateSearchItem[]>
>;
export type CandidateSavedView = Override<
  ApiResponseSchema<"CandidateSavedViewOut">,
  Record<"filters", CandidateSearchFilters>
>;
export type CandidateDuplicateMatchType =
  ApiResponseSchema<"CandidateDuplicateGroup">["match_types"][number];
export type CandidateDuplicateMember = ApiResponseSchema<"CandidateDuplicateMember">;
export type CandidateDuplicateGroup = Override<
  ApiResponseSchema<"CandidateDuplicateGroup">,
  Record<"match_types", CandidateDuplicateMatchType[]>
>;
export type CandidateDuplicatePage = Override<
  ApiResponseSchema<"CandidateDuplicatePage">,
  Record<"items", CandidateDuplicateGroup[]>
>;
export type CandidateMergeConflict = ApiResponseSchema<"CandidateMergeConflict">;
export type CandidateMergePreview = ApiResponseSchema<"CandidateMergePreviewOut">;
export type CandidateMergeScoreWinner = ApiResponseSchema<"CandidateMergeScoreWinner">;
export type CandidateMergeResult = ApiResponseSchema<"CandidateMergeOut">;
export type CandidateMergeHistoryItem = ApiResponseSchema<"CandidateMergeHistoryItem">;
export type CandidateMergeHistoryPage = ApiResponseSchema<"CandidateMergeHistoryPage">;
export type CandidateMergeReverseResult = ApiResponseSchema<"CandidateMergeReverseOut">;
export type CandidateMergedSourceScore = ApiResponseSchema<"CandidateMergedSourceScore">;
export type CandidateMergedSource = ApiResponseSchema<"CandidateMergedSourceSummary">;
export type CandidateBulkRequest = ApiSchema<"CandidateBulkRequest">;
export type CandidateBulkResult = ApiResponseSchema<"CandidateBulkOut">;
export type Score = Override<
  ApiResponseSchema<"CandidateApplicationOut">,
  Record<"stage", KanbanStage>
>;
export type CandidateApplicationPage = Override<
  ApiResponseSchema<"CursorPage_CandidateApplicationOut_">,
  Record<"items", Score[]>
>;
export type CandidateDetail = Override<
  ApiResponseSchema<"CandidateDetail">,
  Record<"scores", Score[]> & Record<"github_data", GitHubData | null>
>;

export type CandidateActivityCategory = ApiResponseSchema<"CandidateActivityOut">["category"];
export type CandidateActivity = ApiResponseSchema<"CandidateActivityOut">;
export type CandidateActivityPage = ApiResponseSchema<"CandidateActivityPage">;

export type MyWorkFilter = NonNullable<
  NonNullable<ApiOperationQuery<"/my-work", "get">>["filter"]
>;
export type WorkOwner = ApiResponseSchema<"WorkOwnerOut">;
export type AssignableUser = ApiResponseSchema<"AssignableUserOut">;
export type CandidateWork = ApiResponseSchema<"CandidateWorkOut">;
export type MyWorkCounts = ApiResponseSchema<"MyWorkCountsOut">;
export type MyWorkItem = ApiResponseSchema<"MyWorkItemOut">;
export type MyWorkPage = ApiResponseSchema<"MyWorkPageOut">;

export type ChatSession = ApiResponseSchema<"ChatSessionOut">;
export type ChatSessionListItem = ApiResponseSchema<"ChatSessionListItem">;
export type ChatSessionDetail = Override<
  ApiResponseSchema<"ChatSessionDetail">,
  Record<"messages", ChatMessage[]>
>;
type GeneratedUser = ApiResponseSchema<"UserOut">;
export type AppUser = Pick<
  GeneratedUser,
  "id" | "email" | "role" | "locale" | "created_at"
> &
  Partial<
    Pick<GeneratedUser, "is_active" | "mfa_enabled" | "organization_id">
  >;
export type UserAccessCreated = ApiResponseSchema<"UserAccessCreatedOut">;
export type ChatMessage = Override<
  ApiResponseSchema<"ChatMessageOut">,
  Record<"role", "user" | "assistant" | "system">
>;

export type InterviewStatus = ApiResponseSchema<"InterviewOut">["status"];
export type InterviewQuestionCategory = ApiResponseSchema<"InterviewQuestionOut">["category"];
export type InterviewQuestion = ApiResponseSchema<"InterviewQuestionOut">;
export type Interview = ApiResponseSchema<"InterviewOut">;
export type InterviewDetail = ApiResponseSchema<"InterviewDetail">;
export type InterviewQuestionDraft = ApiSchema<"InterviewQuestionCreate">;
export type ManualInterviewCreate = ApiSchema<"ManualInterviewCreate">;
export type InterviewKitVersionCreate = ApiSchema<"InterviewKitVersionCreate">;
export type InterviewCompleteRequest = ApiSchema<"InterviewCompleteRequest">;

// GitHub, diarization, and task-evaluation payloads are opaque JSON in OpenAPI.
// These client refinements are deliberately intersected with the generated
// fields so they cannot replace or drift from the declared wire contract.
/** @ui-only Render model for the intentionally opaque GitHub snapshot JSON. */
export type GitHubRepo = {
  name: string | null;
  full_name: string | null;
  html_url: string | null;
  description: string | null;
  language: string | null;
  stars: number;
  forks: number;
  topics: string[];
  pushed_at: string | null;
};
/** @ui-only Render model for the intentionally opaque GitHub snapshot JSON. */
export type GitHubLanguage = { language: string; repo_count: number };
/** @ui-only Render model for the intentionally opaque GitHub snapshot JSON. */
export type GitHubProfile = {
  login: string | null;
  name: string | null;
  html_url: string | null;
  avatar_url: string | null;
  bio: string | null;
  company: string | null;
  location: string | null;
  blog: string | null;
  followers: number;
  following: number;
  public_repos: number;
  created_at: string | null;
};
/** @ui-only Render model for `GitHubOut.data`, which is opaque JSON in OpenAPI. */
export type GitHubData = ApiResponseSchema<"GitHubOut">["data"] & {
  profile: GitHubProfile;
  repos: GitHubRepo[];
  languages: GitHubLanguage[];
  stats: { total_stars: number; non_fork_repos: number; shown_repos: number };
  ai_summary?: string | null;
  ai_summary_model?: string | null;
  fetched_at?: string | null;
};
export type GitHubOut = Override<
  ApiResponseSchema<"GitHubOut">,
  Record<"data", GitHubData>
>;
export type GitHubDetect = ApiResponseSchema<"GitHubDetectOut">;

function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function jsonString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function jsonNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function jsonStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

/** Convert the API's opaque GitHub JSON into the explicit render model. */
export function normalizeGitHubData(value: unknown): GitHubData {
  const data = jsonRecord(value);
  const profile = jsonRecord(data.profile);
  const stats = jsonRecord(data.stats);
  return {
    ...data,
    profile: {
      login: jsonString(profile.login),
      name: jsonString(profile.name),
      html_url: jsonString(profile.html_url),
      avatar_url: jsonString(profile.avatar_url),
      bio: jsonString(profile.bio),
      company: jsonString(profile.company),
      location: jsonString(profile.location),
      blog: jsonString(profile.blog),
      followers: jsonNumber(profile.followers),
      following: jsonNumber(profile.following),
      public_repos: jsonNumber(profile.public_repos),
      created_at: jsonString(profile.created_at),
    },
    repos: Array.isArray(data.repos)
      ? data.repos.map((value) => {
          const repo = jsonRecord(value);
          return {
            name: jsonString(repo.name),
            full_name: jsonString(repo.full_name),
            html_url: jsonString(repo.html_url),
            description: jsonString(repo.description),
            language: jsonString(repo.language),
            stars: jsonNumber(repo.stars),
            forks: jsonNumber(repo.forks),
            topics: jsonStrings(repo.topics),
            pushed_at: jsonString(repo.pushed_at),
          };
        })
      : [],
    languages: Array.isArray(data.languages)
      ? data.languages.flatMap((value) => {
          const language = jsonRecord(value);
          return typeof language.language === "string"
            ? [{ language: language.language, repo_count: jsonNumber(language.repo_count) }]
            : [];
        })
      : [],
    stats: {
      total_stars: jsonNumber(stats.total_stars),
      non_fork_repos: jsonNumber(stats.non_fork_repos),
      shown_repos: jsonNumber(stats.shown_repos),
    },
    ai_summary: jsonString(data.ai_summary),
    ai_summary_model: jsonString(data.ai_summary_model),
    fetched_at: jsonString(data.fetched_at),
  };
}

export function normalizeGitHubOut(value: ApiResponseSchema<"GitHubOut">): GitHubOut {
  return { ...value, data: normalizeGitHubData(value.data) };
}

export function normalizeCandidate(
  value: ApiResponseSchema<"CandidateOut">,
): Candidate {
  return {
    ...value,
    github_data: value.github_data ? normalizeGitHubData(value.github_data) : null,
  };
}

export function normalizeCandidateDetail(
  value: ApiResponseSchema<"CandidateDetail">,
  scores: ApiResponseSchema<"CandidateApplicationOut">[] = [],
): CandidateDetail {
  return {
    ...value,
    scores: scores as Score[],
    github_data: value.github_data ? normalizeGitHubData(value.github_data) : null,
  };
}

/** @ui-only Render model for `RecordingOut.diarization` (`unknown[]` in OpenAPI). */
export type DiarizationTurn = {
  speaker?: string;
  start?: number;
  end?: number;
  text?: string;
};
export type InterviewRecording = Override<
  ApiResponseSchema<"RecordingOut">,
  Record<"diarization", DiarizationTurn[] | null | undefined>
>;

export function normalizeInterviewRecording(
  value: ApiResponseSchema<"RecordingOut">,
): InterviewRecording {
  return {
    ...value,
    diarization: Array.isArray(value.diarization)
      ? value.diarization.map((raw) => {
          const turn = jsonRecord(raw);
          return {
            speaker: typeof turn.speaker === "string" ? turn.speaker : undefined,
            start: typeof turn.start === "number" ? turn.start : undefined,
            end: typeof turn.end === "number" ? turn.end : undefined,
            text: typeof turn.text === "string" ? turn.text : undefined,
          };
        })
      : null,
  };
}

/** @ui-only Render model for `TaskOut.evaluation`, which is opaque JSON in OpenAPI. */
export type TaskEvaluation = {
  score?: number | null;
  summary?: string | null;
  strengths?: string[];
  concerns?: string[];
  recommendation?: string | null;
};
export type TaskSubmission = Override<
  ApiResponseSchema<"TaskOut">,
  Record<"evaluation", TaskEvaluation | null | undefined>
>;

export function normalizeTaskSubmission(
  value: ApiResponseSchema<"TaskOut">,
): TaskSubmission {
  const evaluation = value.evaluation ? jsonRecord(value.evaluation) : null;
  return {
    ...value,
    evaluation: evaluation
      ? {
          score:
            typeof evaluation.score === "number" ? evaluation.score : null,
          summary: jsonString(evaluation.summary),
          strengths: jsonStrings(evaluation.strengths),
          concerns: jsonStrings(evaluation.concerns),
          recommendation: jsonString(evaluation.recommendation),
        }
      : null,
  };
}

export type ContactChannel = ApiResponseSchema<"CandidateContactPointOut">["channel"];
export type MessageChannel = ApiResponseSchema<"CommunicationTemplateOut">["channel"];
export type CreatableContactChannel = Exclude<ContactChannel, "whatsapp">;
export type OutboundMessageChannel = Exclude<MessageChannel, "whatsapp">;
export type CommunicationProvider = ApiResponseSchema<"CommunicationAccountOut">["provider"];
export type CandidateContactPoint = ApiResponseSchema<"CandidateContactPointOut">;
export type CommunicationAccount = ApiResponseSchema<"CommunicationAccountOut">;
export type CommunicationAccountCreateResult = ApiResponseSchema<"CommunicationAccountCreateOut">;
export type CommunicationCapabilities = ApiResponseSchema<"CommunicationCapabilitiesOut">;
type CommunicationConnectionProvider = Exclude<
  ApiResponseSchema<"CommunicationAccountOut">["provider"],
  "sandbox" | "smtp" | "whatsapp"
>;
export type CommunicationConnectionOption = Override<
  Pick<
    ApiResponseSchema<"IntegrationConnectionOut">,
    "id" | "provider" | "name" | "status" | "capabilities"
  >,
  Record<"provider", CommunicationConnectionProvider>
>;
export type CommunicationTemplate = ApiResponseSchema<"CommunicationTemplateOut">;
export type CommunicationTarget = ApiResponseSchema<"CommunicationTargetOut">;
export type CandidateCommunicationPreview = ApiResponseSchema<"CandidateCommunicationPreviewOut">;
export type CommunicationMessageStatus = ApiResponseSchema<"CommunicationMessageOut">["status"];
export type CommunicationMessage = ApiResponseSchema<"CommunicationMessageOut">;
export type CommunicationThread = ApiResponseSchema<"CommunicationThreadOut"> &
  Partial<
    Record<"candidate_source" | "candidate_source_ref", string | null>
  >;
export type CommunicationThreadPage = ApiResponseSchema<"CommunicationThreadPage">;
export type CommunicationMessagePage = ApiResponseSchema<"CommunicationMessagePage">;
export type BulkCommunicationPreviewItem = ApiResponseSchema<"BulkCommunicationPreviewItem">;
export type BulkCommunicationPreview = ApiResponseSchema<"BulkCommunicationPreviewOut">;
export type CommunicationBatch = ApiResponseSchema<"CommunicationBatchOut">;
export type BulkCommunicationCreateResult = ApiResponseSchema<"BulkCommunicationCreateOut">;

export type SchedulingLocationMode = NonNullable<
  ApiSchema<"SchedulingLinkCreate">["location_mode"]
>;
export type AppointmentStatus = ApiResponseSchema<"AppointmentOut">["status"];
export type AvailabilityInterval = ApiResponseSchema<"AvailabilityInterval">;
export type Availability = ApiResponseSchema<"AvailabilityOut">;
export type AvailabilityBlock = ApiResponseSchema<"AvailabilityBlockOut">;
export type CalendarProvider = ApiResponseSchema<"CalendarProviderOut">;
export type SchedulingLink = ApiResponseSchema<"SchedulingLinkOut">;
export type SchedulingLinkCreate = ApiSchema<"SchedulingLinkCreate">;
export type InterviewAppointment = ApiResponseSchema<"AppointmentOut">;
export type PublicAppointment = ApiResponseSchema<"PublicAppointmentOut">;
export type SchedulingSlot = ApiResponseSchema<"SchedulingSlotOut">;
export type SchedulingSlotPage = ApiResponseSchema<"SchedulingSlotPage">;
export type PublicSchedulingDetails = Override<
  ApiResponseSchema<"PublicSchedulingDetails">,
  Record<"organization_name", string>
>;
export type PublicAppointmentDetails = Override<
  ApiResponseSchema<"PublicAppointmentDetails">,
  Record<"organization_name", string>
>;

export type ScorecardCriterion = ApiResponseSchema<"ScorecardCriterionOut">;
export type ScorecardCriterionCreate = ApiSchema<"ScorecardCriterionCreate">;
export type ScorecardTemplate = ApiResponseSchema<"ScorecardTemplateOut">;
export type ScorecardRating = ApiResponseSchema<"ScorecardRatingOut">;
export type ScorecardAssignment = ApiResponseSchema<"ScorecardAssignmentOut">;
export type HiringRecommendation = ApiResponseSchema<"HiringDecisionOut">["recommendation"];
export type HiringDecisionApproval = ApiResponseSchema<"HiringDecisionApprovalOut">;
export type HiringDecision = ApiResponseSchema<"HiringDecisionOut">;
export type ScorecardPanel = ApiResponseSchema<"ScorecardPanelOut">;

export type WorkflowTrigger = ApiResponseSchema<"WorkflowRuleOut">["trigger"];
export type WorkflowConditions = ApiSchema<"WorkflowConditions">;
export type WorkflowAction = ApiSchema<"WorkflowRuleOut">["actions"][number];
export type WorkflowRule = ApiResponseSchema<"WorkflowRuleOut">;
export type WorkflowRun = ApiResponseSchema<"WorkflowRunOut">;
export type WorkflowRunPage = ApiResponseSchema<"WorkflowRunPage">;
export type WorkflowProcessResult = ApiResponseSchema<"WorkflowProcessOut">;
export type WorkflowDryRun = ApiResponseSchema<"WorkflowDryRunOut">;

export type EffectivePermissions = ApiResponseSchema<"EffectivePermissionsOut">;
export type EnterpriseFieldVisibility = Pick<
  ApiResponseSchema<"EnterprisePolicyOut">,
  "field_visibility"
>;
export type ApprovalRecord = ApiResponseSchema<"ApprovalOut">;
export type RequisitionStatus = ApiResponseSchema<"RequisitionOut">["status"];
export type Requisition = ApiResponseSchema<"RequisitionOut">;
export type OfferTemplate = ApiResponseSchema<"OfferTemplateOut">;
export type CandidateOfferStatus = ApiResponseSchema<"CandidateOfferOut">["status"];
export type CandidateOffer = ApiResponseSchema<"CandidateOfferOut">;
export type CandidateOfferSendResult = ApiResponseSchema<"CandidateOfferSendOut">;
export type PublicOffer = ApiResponseSchema<"PublicOfferOut">;
export type HandoffChecklistItem = keyof ApiResponseSchema<"CandidateHandoffOut">["checklist"];
export type CandidateHandoff = ApiResponseSchema<"CandidateHandoffOut">;
