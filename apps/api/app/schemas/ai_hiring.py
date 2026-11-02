"""Public contracts for the AI Hiring Assistant workspace."""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

AiSessionStatus = Literal["active", "archived"]
AiMessageRole = Literal["user", "assistant", "system", "tool"]
AiMessageStatus = Literal["streaming", "completed", "failed"]
HiringMissionStatus = Literal[
    "draft",
    "planning",
    "running",
    "awaiting_approval",
    "completed",
    "stopped",
    "failed",
]
MissionPlanStatus = Literal["active", "superseded", "completed", "cancelled"]
MissionStepStatus = Literal[
    "pending",
    "ready",
    "running",
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "skipped",
]
MissionStepAttemptStatus = Literal["running", "completed", "failed", "cancelled"]
PolicyOutcome = Literal["auto", "auto_notify", "approval_required", "forbidden"]
AssessmentConfidence = Literal["low", "medium", "high"]
AssessmentRecommendation = Literal[
    "strong_fit",
    "fit",
    "possible_fit",
    "insufficient_evidence",
    "not_recommended",
]
EvidenceStance = Literal["supports", "contradicts", "context"]
AiActionBundleStatus = Literal[
    "pending", "approved", "rejected", "executed", "failed", "stale"
]
AiAgentRunStatus = Literal[
    "queued", "running", "completed", "failed", "cancelled", "budget_exhausted"
]
ActorKind = Literal["human", "ai", "system", "public"]


class CompensationRange(BaseModel):
    min: float = Field(ge=0)
    max: float = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a three-letter ISO code")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "CompensationRange":
        if self.min > self.max:
            raise ValueError("compensation min cannot exceed max")
        return self


class HiringBrief(BaseModel):
    role_title: str = Field(min_length=1, max_length=255)
    headcount: int = Field(default=1, ge=1, le=100)
    responsibilities: list[str] = Field(default_factory=list, max_length=50)
    must_haves: list[str] = Field(default_factory=list, max_length=20)
    preferences: list[str] = Field(default_factory=list, max_length=20)
    location: str | None = Field(default=None, max_length=255)
    work_format: str | None = Field(default=None, max_length=64)
    compensation: CompensationRange | None = None
    deadline: date | None = None
    shortlist_size: int = Field(default=5, ge=1, le=20)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    blockers: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("role_title")
    @classmethod
    def clean_role_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("role_title cannot be blank")
        return value

    @field_validator(
        "responsibilities", "must_haves", "preferences", "assumptions", "blockers"
    )
    @classmethod
    def clean_string_lists(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            item = raw.strip()
            if item and item not in cleaned:
                if len(item) > 1_000:
                    raise ValueError("brief list items cannot exceed 1000 characters")
                cleaned.append(item)
        return cleaned

    @field_validator("location", "work_format")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def apply_shortlist_default(self) -> "HiringBrief":
        if "shortlist_size" not in self.model_fields_set:
            self.shortlist_size = min(20, max(5, self.headcount * 3))
        return self


class HiringRubricCriterion(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1_000)
    weight: int = Field(ge=1, le=100)
    must_have: bool = False

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("criterion label cannot be blank")
        return value


class HiringRubric(BaseModel):
    criteria: list[HiringRubricCriterion] = Field(min_length=3, max_length=10)

    @model_validator(mode="after")
    def validate_criteria(self) -> "HiringRubric":
        keys = [criterion.key for criterion in self.criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("rubric criterion keys must be unique")
        if sum(criterion.weight for criterion in self.criteria) != 100:
            raise ValueError("rubric criterion weights must total 100")
        return self


class HiringBriefOut(BaseModel):
    """Stored mission brief, including compatibility for pre-pilot drafts."""

    role_title: str | None = None
    headcount: int = Field(default=1, ge=1, le=100)
    responsibilities: list[str] = Field(default_factory=list)
    must_haves: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    location: str | None = None
    work_format: str | None = None
    compensation: CompensationRange | None = None
    deadline: date | None = None
    shortlist_size: int = Field(default=5, ge=1, le=20)
    assumptions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class HiringRubricCriterionOut(BaseModel):
    key: str
    label: str
    description: str | None = None
    weight: int = Field(ge=0, le=100)
    must_have: bool = False
    keywords: list[str] = Field(default_factory=list)


class HiringRubricOut(BaseModel):
    version: int | None = Field(default=None, ge=1)
    criteria: list[HiringRubricCriterionOut] = Field(default_factory=list)
    weight_total: int | None = Field(default=None, ge=0, le=100)


class AiAttachmentOut(BaseModel):
    id: UUID
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AiAttachmentReference(BaseModel):
    """Opaque reference returned by the upload endpoint.

    The server validates the signed token and reconstructs trusted attachment
    metadata; clients never submit or receive a filesystem/storage key.
    """

    id: UUID
    upload_token: str = Field(min_length=20, max_length=2_048)


class AiAttachmentUploadOut(AiAttachmentOut):
    upload_token: str


class AiSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class AiSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_by_user_id: UUID | None
    creator_user_id: UUID | None = None
    mission_id: UUID | None = None
    mission_status: HiringMissionStatus | None = None
    title: str | None
    status: AiSessionStatus
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AiSessionPage(BaseModel):
    items: list[AiSessionOut]
    next_cursor: str | None = None


class AiMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    attachments: list[AiAttachmentReference] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content cannot be blank")
        return value


class AiMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    actor_user_id: UUID | None
    agent_run_id: UUID | None
    role: AiMessageRole
    content: str
    structured_content: dict[str, Any]
    attachments: list[AiAttachmentOut]
    redaction_metadata: dict[str, Any]
    sequence: int
    status: AiMessageStatus
    completed_at: datetime | None
    created_at: datetime


class HiringMissionCreate(BaseModel):
    session_id: UUID
    original_request: str = Field(min_length=1, max_length=50_000)
    title: str | None = Field(default=None, max_length=255)

    @field_validator("original_request")
    @classmethod
    def clean_request(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("original_request cannot be blank")
        return value


class HiringMissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    created_by_user_id: UUID | None
    position_id: UUID | None
    title: str
    original_request: str
    brief: HiringBriefOut
    rubric: HiringRubricOut
    assumptions: list[str]
    blockers: list[str]
    headcount: int
    shortlist_size: int
    model_budget_cents: int | None
    status: HiringMissionStatus
    current_plan_version: int
    correlation_id: UUID
    last_error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    stopped_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AiSessionDetailOut(AiSessionOut):
    messages: list[AiMessageOut]
    mission_id: UUID | None = None
    mission: HiringMissionOut | None = None


class HiringMissionPage(BaseModel):
    items: list[HiringMissionOut]
    next_cursor: str | None = None


class MissionReplanRequest(BaseModel):
    request: str = Field(min_length=1, max_length=50_000)

    @field_validator("request")
    @classmethod
    def clean_request(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request cannot be blank")
        return value


class MissionPlanVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    mission_id: UUID
    version: int
    status: MissionPlanStatus
    brief_snapshot: dict[str, Any]
    rubric_snapshot: dict[str, Any]
    plan: dict[str, Any]
    input_hash: str
    created_at: datetime


class MissionStepOut(BaseModel):
    id: UUID
    key: str
    step_type: str
    ordinal: int
    sequence: int
    title: str
    dependencies: list[str]
    depends_on: list[str]
    status: MissionStepStatus
    safe_result: dict[str, Any]
    result: dict[str, Any]
    policy_outcome: PolicyOutcome | None
    last_error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None


class MissionStepAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    step_id: UUID
    agent_run_id: UUID | None
    attempt_number: int
    status: MissionStepAttemptStatus
    safe_result: dict[str, Any]
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class MissionDetailOut(HiringMissionOut):
    steps: list[MissionStepOut]


class CriterionAssessment(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    score: float = Field(ge=0, le=100)
    evidence_claim_ids: list[UUID] = Field(default_factory=list, max_length=100)


class MissionAssessmentCandidateOut(BaseModel):
    id: UUID
    source: str
    source_ref: str | None = None


class MissionEvidenceSourceLocatorOut(BaseModel):
    kind: str | None = None


class MissionAssessmentEvidenceOut(BaseModel):
    id: UUID
    criterion_key: str
    claim: str
    source_type: str
    source_id: UUID | None
    source_locator: MissionEvidenceSourceLocatorOut
    stance: EvidenceStance
    confidence: float = Field(ge=0, le=1)


class MissionCandidateAssessmentOut(BaseModel):
    id: UUID
    plan_version: int
    candidate_id: UUID
    candidate: MissionAssessmentCandidateOut | None = None
    version: int
    overall_score: float
    confidence: AssessmentConfidence
    recommendation: AssessmentRecommendation
    criteria: list[CriterionAssessment]
    concerns: list[str]
    missing_evidence: list[str]
    evidence_coverage: float
    concise_rationale: str | None
    evidence: list[MissionAssessmentEvidenceOut] = Field(default_factory=list)
    evidence_claims: list[MissionAssessmentEvidenceOut] = Field(default_factory=list)
    created_at: datetime


class MissionCandidateAssessmentPage(BaseModel):
    items: list[MissionCandidateAssessmentOut]
    next_cursor: str | None = None


class EvidenceClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    mission_id: UUID
    plan_version_id: UUID
    assessment_id: UUID
    candidate_id: UUID
    criterion_key: str
    claim: str
    source_type: str
    source_id: UUID | None
    source_locator: dict[str, Any]
    stance: EvidenceStance
    confidence: float
    content_hash: str
    created_at: datetime


class AiActionBundleOut(BaseModel):
    id: UUID
    mission_id: UUID
    plan_version: int
    action_type: str
    summary: str
    preview: dict[str, Any]
    required_permission: str
    status: AiActionBundleStatus
    approved_by_user_id: UUID | None
    decision_comment: str | None
    execution_result: dict[str, Any]
    result: dict[str, Any]
    error_code: str | None
    error: str | None
    decided_at: datetime | None
    executed_at: datetime | None
    created_at: datetime


class AiActionBundlePage(BaseModel):
    items: list[AiActionBundleOut]
    next_cursor: str | None = None


class AiActionBundleDecision(BaseModel):
    comment: str | None = Field(default=None, max_length=2_000)

    @field_validator("comment")
    @classmethod
    def clean_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class AiAgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID | None
    mission_id: UUID
    step_id: UUID | None
    initiated_by_user_id: UUID | None
    agent_type: str
    status: AiAgentRunStatus
    model_provider: str | None
    model: str | None
    prompt_version: str | None
    tool_name: str | None
    validation_outcome: Literal["pending", "valid", "invalid", "not_applicable"]
    tokens_in: int | None
    tokens_out: int | None
    cost_cents: int | None
    latency_ms: int | None
    correlation_id: UUID
    causation_id: UUID | None
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class MissionEventOut(BaseModel):
    id: UUID
    sequence: int
    event_type: str
    actor_kind: ActorKind
    actor_user_id: UUID | None
    agent_run_id: UUID | None
    subject_type: str | None
    subject_id: UUID | None
    payload: dict[str, Any]
    occurred_at: datetime


class MissionEventPage(BaseModel):
    items: list[MissionEventOut]
    next_cursor: str | None = None


class AiOrganizationSettingsUpdate(BaseModel):
    feature_enabled: bool | None = None
    default_headcount: int | None = Field(default=None, ge=1, le=100)
    default_shortlist_size: int | None = Field(default=None, ge=1, le=20)
    default_model_budget_cents: int | None = Field(default=None, ge=0)
    hiring_defaults: dict[str, Any] | None = None
    autonomy_policy: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_change(self) -> "AiOrganizationSettingsUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class AiOrganizationSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feature_enabled: bool
    default_headcount: int
    default_shortlist_size: int
    default_model_budget_cents: int | None
    hiring_defaults: dict[str, Any]
    autonomy_policy: dict[str, Any]
    updated_at: datetime | None


class AiMessageDeltaEventData(BaseModel):
    delta: str
    message_id: UUID | None = None


class AiMessageCompletedEventData(BaseModel):
    message: AiMessageOut


class AiMissionUpdatedEventData(BaseModel):
    mission: HiringMissionOut


class AiMissionCanvasData(BaseModel):
    mission_id: UUID
    plan_version: int


class AiCanvasUpdatedEventData(BaseModel):
    name: Literal["mission"]
    data: AiMissionCanvasData


class AiCompletedEventData(BaseModel):
    session_id: UUID
    mission_id: UUID


class AiErrorEventData(BaseModel):
    code: str
    message: str


class AiMessageDeltaStreamEvent(BaseModel):
    event: Literal["message.delta"]
    data: AiMessageDeltaEventData


class AiMessageCompletedStreamEvent(BaseModel):
    event: Literal["message.completed"]
    data: AiMessageCompletedEventData


class AiMissionUpdatedStreamEvent(BaseModel):
    event: Literal["mission.updated"]
    data: AiMissionUpdatedEventData


class AiCanvasUpdatedStreamEvent(BaseModel):
    event: Literal["canvas.updated"]
    data: AiCanvasUpdatedEventData


class AiCompletedStreamEvent(BaseModel):
    event: Literal["completed"]
    data: AiCompletedEventData


class AiErrorStreamEvent(BaseModel):
    event: Literal["error"]
    data: AiErrorEventData


AiStreamEventVariant = Annotated[
    AiMessageDeltaStreamEvent
    | AiMessageCompletedStreamEvent
    | AiMissionUpdatedStreamEvent
    | AiCanvasUpdatedStreamEvent
    | AiCompletedStreamEvent
    | AiErrorStreamEvent,
    Field(discriminator="event"),
]


class AiStreamEvent(RootModel[AiStreamEventVariant]):
    """One typed JSON event carried in the AI workspace SSE stream."""
