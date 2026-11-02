"""Public contracts for the first-class Application aggregate."""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ApplicationStage = Literal["new", "screening", "interview", "offer", "hired", "rejected"]
ApplicationDisposition = Literal[
    "not_qualified",
    "withdrew",
    "compensation",
    "timing",
    "duplicate",
    "position_closed",
    "other",
]
ApplicationEvaluationKind = Literal[
    "ai_scoring", "deterministic_scoring", "manual"
]
ApplicationEvaluationStatus = Literal["completed", "reviewed", "failed", "voided"]


def _strip_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class ApplicationCreate(BaseModel):
    candidate_id: UUID
    position_id: UUID
    stage: Literal["new"] = "new"
    source: str = Field(default="manual", min_length=1, max_length=32)
    external_ref: str | None = Field(default=None, max_length=255)
    owner_user_id: UUID | None = None
    next_action_due_date: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _clean_external_ref = field_validator("external_ref")(_strip_optional)

    @field_validator("source")
    @classmethod
    def clean_source(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("source cannot be blank")
        return cleaned


class ApplicationUpdate(BaseModel):
    expected_version: datetime
    source: str | None = Field(default=None, min_length=1, max_length=32)
    external_ref: str | None = Field(default=None, max_length=255)
    owner_user_id: UUID | None = None
    next_action_due_date: date | None = None
    metadata: dict[str, Any] | None = None

    _clean_external_ref = field_validator("external_ref")(_strip_optional)

    @field_validator("expected_version")
    @classmethod
    def require_aware_version(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_version must include a timezone")
        return value

    @field_validator("source")
    @classmethod
    def clean_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("source cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def require_change(self) -> "ApplicationUpdate":
        if not (self.model_fields_set - {"expected_version"}):
            raise ValueError("At least one field must be provided")
        if "source" in self.model_fields_set and self.source is None:
            raise ValueError("source cannot be null")
        if "metadata" in self.model_fields_set and self.metadata is None:
            raise ValueError("metadata cannot be null")
        return self


class ApplicationStageTransition(BaseModel):
    stage: ApplicationStage
    expected_version: datetime
    reason: str | None = Field(default=None, max_length=2_000)
    disposition_code: ApplicationDisposition | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    _clean_reason = field_validator("reason")(_strip_optional)

    @field_validator("expected_version")
    @classmethod
    def require_aware_version(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_version must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_disposition(self) -> "ApplicationStageTransition":
        if self.stage == "rejected" and self.disposition_code is None:
            raise ValueError("rejected applications require a disposition_code")
        if self.stage == "rejected" and self.reason is None:
            raise ValueError("rejected applications require a reason")
        if self.stage != "rejected" and self.disposition_code is not None:
            raise ValueError("disposition_code is only valid when rejecting")
        return self


class ApplicationStageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_id: UUID
    from_stage: ApplicationStage | None
    to_stage: ApplicationStage
    actor_user_id: UUID | None
    actor_kind: Literal["human", "system", "public", "automation"]
    reason: str | None
    details: dict[str, Any]
    occurred_at: datetime


class ApplicationEvaluationCreate(BaseModel):
    kind: ApplicationEvaluationKind
    status: ApplicationEvaluationStatus = "completed"
    evidence: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime | None = None

    @field_validator("evaluated_at")
    @classmethod
    def aware_evaluated_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("evaluated_at must include a timezone")
        return value


class ApplicationEvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    application_id: UUID
    kind: ApplicationEvaluationKind
    status: ApplicationEvaluationStatus
    version: int
    supersedes_evaluation_id: UUID | None
    evidence: dict[str, Any]
    scores: dict[str, Any]
    provenance: dict[str, Any]
    actor_user_id: UUID | None
    actor_kind: Literal["human", "ai", "system", "automation"]
    evaluated_at: datetime
    created_at: datetime


class ApplicationOut(BaseModel):
    id: UUID
    organization_id: UUID
    candidate_id: UUID
    position_id: UUID
    legacy_score_id: UUID | None
    stage: ApplicationStage
    source: str
    external_ref: str | None
    owner_user_id: UUID | None
    next_action_due_date: date | None
    cycle_number: int
    is_active: bool
    metadata: dict[str, Any]
    applied_at: datetime
    closed_at: datetime | None
    archived_at: datetime | None
    archived_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime
    evaluation_count: int = 0
    stage_events: list[ApplicationStageEventOut] = Field(default_factory=list)


class ApplicationPage(BaseModel):
    items: list[ApplicationOut]
    next_cursor: str | None = None


class ApplicationAnalyticsBreakdown(BaseModel):
    key: str
    label: str | None = None
    count: int = Field(ge=0)


class ApplicationAnalyticsSourceQuality(ApplicationAnalyticsBreakdown):
    """Application-level outcomes for one immutable acquisition source."""

    applications: int = Field(ge=0)
    assessment_started: int = Field(ge=0)
    assessment_completed: int = Field(ge=0)
    assessment_dropped_off: int = Field(ge=0)
    interviews: int = Field(ge=0)
    offers: int = Field(ge=0)
    hires: int = Field(ge=0)
    assessment_completion_rate_percent: float | None = Field(
        default=None, ge=0, le=100
    )
    interview_rate_percent: float | None = Field(default=None, ge=0, le=100)
    offer_rate_percent: float | None = Field(default=None, ge=0, le=100)
    hire_rate_percent: float | None = Field(default=None, ge=0, le=100)
    scored_applications: int | None = Field(default=None, ge=0)
    average_quality_score: float | None = Field(default=None, ge=0, le=100)


class ApplicationAnalyticsDaily(BaseModel):
    date: date
    applications: int = Field(ge=0)
    transitions: int = Field(ge=0)
    hired: int = Field(ge=0)
    rejected: int = Field(ge=0)


class ApplicationStageAnalytics(BaseModel):
    stage: ApplicationStage
    entered: int = Field(ge=0)
    current: int = Field(ge=0)
    conversion_from_previous_percent: float | None = Field(
        default=None, ge=0, le=100
    )
    median_hours_from_application: float | None = Field(default=None, ge=0)
    median_hours_in_stage: float | None = Field(default=None, ge=0)


class ApplicationAnalyticsTiming(BaseModel):
    hired_applications: int = Field(ge=0)
    filled_positions: int = Field(ge=0)
    median_hours_to_hire: float | None = Field(default=None, ge=0)
    median_hours_to_fill: float | None = Field(default=None, ge=0)


class ApplicationAnalyticsOffers(BaseModel):
    sent: int = Field(ge=0)
    accepted: int = Field(ge=0)
    declined: int = Field(ge=0)
    response_rate_percent: float | None = Field(default=None, ge=0, le=100)
    acceptance_rate_percent: float | None = Field(default=None, ge=0, le=100)


class ApplicationAnalyticsAssessments(BaseModel):
    scope: Literal["native_and_provider"] = "native_and_provider"
    invitations: int = Field(ge=0)
    started: int = Field(ge=0)
    completed: int = Field(ge=0)
    dropped_off: int = Field(ge=0)
    completion_rate_percent: float | None = Field(default=None, ge=0, le=100)
    applications_invited: int = Field(ge=0)
    applications_started: int = Field(ge=0)
    applications_completed: int = Field(ge=0)
    applications_dropped_off: int = Field(ge=0)


class ApplicationAnalyticsAiDecisionProvenance(BaseModel):
    provider: str
    model: str
    prompt_version: str
    recommendations: int = Field(ge=0)
    human_reviews: int = Field(ge=0)
    human_overrides: int = Field(ge=0)
    review_rate_percent: float | None = Field(default=None, ge=0, le=100)
    override_rate_percent: float | None = Field(default=None, ge=0, le=100)


class ApplicationAnalyticsAiDecisions(BaseModel):
    """Cohort-bound AI recommendations and their recorded human review outcomes."""

    scope: Literal["application_cohort"] = "application_cohort"
    recommendations: int = Field(ge=0)
    human_reviews: int = Field(ge=0)
    human_overrides: int = Field(ge=0)
    review_rate_percent: float | None = Field(default=None, ge=0, le=100)
    override_rate_percent: float | None = Field(default=None, ge=0, le=100)
    by_provenance: list[ApplicationAnalyticsAiDecisionProvenance]


class ApplicationAnalyticsOwnerWorkload(BaseModel):
    owner_user_id: UUID | None
    label: str
    active: int = Field(ge=0)
    terminal: int = Field(ge=0)
    total: int = Field(ge=0)
    by_stage: dict[ApplicationStage, int]


class ApplicationAnalyticsIntegrationHealth(BaseModel):
    total: int = Field(ge=0)
    connected: int = Field(ge=0)
    attention_required: int = Field(ge=0)
    by_status: list[ApplicationAnalyticsBreakdown]
    by_provider: list[ApplicationAnalyticsBreakdown]


class ApplicationAnalyticsAiProviderUsage(BaseModel):
    provider: str
    calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    cost_microusd: int = Field(ge=0)


class ApplicationAnalyticsAiUsage(BaseModel):
    scope: Literal["organization_window"] = "organization_window"
    calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    cost_microusd: int = Field(ge=0)
    by_provider: list[ApplicationAnalyticsAiProviderUsage]


class ApplicationAnalyticsFilterOptions(BaseModel):
    sources: list[ApplicationAnalyticsBreakdown]
    owners: list[ApplicationAnalyticsBreakdown]
    positions: list[ApplicationAnalyticsBreakdown]


AnalyticsDrilldownDimension = Literal[
    "all",
    "stage_current",
    "stage_entered",
    "source",
    "owner",
    "disposition",
]


class ApplicationAnalyticsDrilldownRow(BaseModel):
    application_id: UUID
    candidate_id: UUID
    position_id: UUID
    applied_at: datetime
    current_stage: ApplicationStage
    current_stage_entered_at: datetime
    source: str
    owner_user_id: UUID | None
    owner_label: str
    disposition_code: str | None
    assessment_started: bool
    assessment_completed: bool
    assessment_dropped_off: bool
    interviewed: bool
    offer_sent: bool
    hired: bool
    quality_score: float | None = Field(default=None, ge=0, le=100)


class ApplicationAnalyticsDrilldownPage(BaseModel):
    dimension: AnalyticsDrilldownDimension
    key: str | None
    total: int = Field(ge=0)
    items: list[ApplicationAnalyticsDrilldownRow]
    next_cursor: str | None = None


class ApplicationAnalyticsReconciliation(BaseModel):
    applications_checked: int = Field(ge=0)
    applications_without_initial_event: int = Field(ge=0)
    duplicate_initial_events: int = Field(ge=0)
    stage_mismatches: int = Field(ge=0)


class ApplicationAnalyticsOut(BaseModel):
    start_at: datetime
    end_at: datetime
    timezone: str
    total_applications: int = Field(ge=0)
    unique_candidates: int = Field(ge=0)
    quality_score_authorized: bool = False
    by_source: list[ApplicationAnalyticsSourceQuality]
    by_owner: list[ApplicationAnalyticsBreakdown]
    by_position: list[ApplicationAnalyticsBreakdown]
    dispositions: list[ApplicationAnalyticsBreakdown]
    stages: list[ApplicationStageAnalytics]
    daily: list[ApplicationAnalyticsDaily]
    timing: ApplicationAnalyticsTiming
    offers: ApplicationAnalyticsOffers
    assessments: ApplicationAnalyticsAssessments
    owner_workload: list[ApplicationAnalyticsOwnerWorkload]
    integration_health: ApplicationAnalyticsIntegrationHealth | None = None
    ai_usage: ApplicationAnalyticsAiUsage | None = None
    ai_decisions: ApplicationAnalyticsAiDecisions | None = None
    filter_options: ApplicationAnalyticsFilterOptions
    drilldown: ApplicationAnalyticsDrilldownPage | None = None
    reconciliation: ApplicationAnalyticsReconciliation
