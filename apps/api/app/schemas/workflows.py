"""Strict workflow rule contracts; arbitrary executable payloads are forbidden."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

WorkflowTrigger = Literal[
    "candidate_added",
    "candidate_scored",
    "candidate_rescored",
    "stage_changed",
    "scorecard_submitted",
    "message_received",
    "interview_scheduled",
    "candidate_inactive",
    "feedback_overdue",
]
WorkflowActionType = Literal[
    "move_stage",
    "assign_owner",
    "set_due_date",
    "add_tag",
    "create_task",
    "send_template",
    "notify_owner",
]
PipelineStage = Literal["new", "screening", "interview", "offer", "hired", "rejected"]
AI_SCORE_TRIGGERS = frozenset({"candidate_scored", "candidate_rescored"})
TERMINAL_HIRING_STAGES = frozenset({"hired", "rejected"})


class WorkflowConditions(BaseModel):
    position_id: UUID | None = None
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)
    stage_in: list[PipelineStage] | None = Field(
        default=None, min_length=1, max_length=6
    )
    source_in: list[str] | None = Field(default=None, min_length=1, max_length=20)
    inactivity_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("source_in")
    @classmethod
    def clean_sources(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = list(
            dict.fromkeys(item.strip().lower() for item in value if item.strip())
        )
        if not cleaned:
            raise ValueError("source_in cannot be empty")
        if any(len(item) > 64 for item in cleaned):
            raise ValueError("source values cannot exceed 64 characters")
        return cleaned

    @model_validator(mode="after")
    def valid_range(self) -> "WorkflowConditions":
        if (
            self.min_score is not None
            and self.max_score is not None
            and self.min_score > self.max_score
        ):
            raise ValueError("min_score cannot exceed max_score")
        return self


class MoveStageAction(BaseModel):
    type: Literal["move_stage"]
    stage: PipelineStage
    disposition_code: (
        Literal[
            "not_qualified",
            "withdrew",
            "compensation",
            "timing",
            "duplicate",
            "position_closed",
            "other",
        ]
        | None
    ) = None
    reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value is not None else None
        return cleaned or None

    @model_validator(mode="after")
    def rejection_metadata(self) -> "MoveStageAction":
        if self.stage == "rejected" and self.disposition_code is None:
            raise ValueError("rejected applications require a disposition_code")
        if self.stage == "rejected" and self.reason is None:
            raise ValueError("rejected applications require a reason")
        if self.stage != "rejected" and self.disposition_code is not None:
            raise ValueError("disposition_code is only valid when rejecting")
        return self


class AssignOwnerAction(BaseModel):
    type: Literal["assign_owner"]
    user_id: UUID


class SetDueDateAction(BaseModel):
    type: Literal["set_due_date"]
    days_from_now: int = Field(ge=0, le=365)


class AddTagAction(BaseModel):
    type: Literal["add_tag"]
    label: str = Field(min_length=1, max_length=32)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("label cannot be blank")
        return cleaned


class CreateTaskAction(BaseModel):
    type: Literal["create_task"]
    title: str = Field(min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=20_000)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title cannot be blank")
        return cleaned


class SendTemplateAction(BaseModel):
    type: Literal["send_template"]
    channel: Literal["email", "telegram", "whatsapp"]
    template_id: UUID


class NotifyOwnerAction(BaseModel):
    type: Literal["notify_owner"]
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title cannot be blank")
        return cleaned


WorkflowAction = Annotated[
    MoveStageAction
    | AssignOwnerAction
    | SetDueDateAction
    | AddTagAction
    | CreateTaskAction
    | SendTemplateAction
    | NotifyOwnerAction,
    Field(discriminator="type"),
]


class WorkflowRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    trigger: WorkflowTrigger
    conditions: WorkflowConditions = Field(default_factory=WorkflowConditions)
    actions: list[WorkflowAction] = Field(min_length=1, max_length=10)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_trigger_conditions(self) -> "WorkflowRuleCreate":
        if (
            self.trigger == "candidate_inactive"
            and self.conditions.inactivity_days is None
        ):
            raise ValueError("candidate_inactive requires inactivity_days")
        if (
            self.trigger != "candidate_inactive"
            and self.conditions.inactivity_days is not None
        ):
            raise ValueError("inactivity_days is only valid for candidate_inactive")
        if any(
            isinstance(action, SendTemplateAction) and action.channel == "whatsapp"
            for action in self.actions
        ):
            raise ValueError("capability_unavailable: WhatsApp is outside pilot scope")
        delivery_types = [
            action.type
            for action in self.actions
            if action.type in {"send_template", "notify_owner"}
        ]
        if len(delivery_types) != len(set(delivery_types)):
            raise ValueError(
                "notification_amplification: each delivery action type may appear "
                "only once per workflow"
            )
        if self.trigger in AI_SCORE_TRIGGERS and any(
            isinstance(action, MoveStageAction)
            and action.stage in TERMINAL_HIRING_STAGES
            for action in self.actions
        ):
            raise ValueError(
                "human_decision_required: score-triggered workflows cannot "
                "hire or reject a candidate"
            )
        return self


class WorkflowRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    conditions: WorkflowConditions | None = None
    actions: list[WorkflowAction] | None = Field(
        default=None, min_length=1, max_length=10
    )
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def require_change(self) -> "WorkflowRuleUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        if self.actions is not None and any(
            isinstance(action, SendTemplateAction) and action.channel == "whatsapp"
            for action in self.actions
        ):
            raise ValueError("capability_unavailable: WhatsApp is outside pilot scope")
        return self


class WorkflowRuleOut(BaseModel):
    id: UUID
    version: int
    name: str
    trigger: WorkflowTrigger
    conditions: WorkflowConditions
    actions: list[WorkflowAction]
    required_permissions: list[str]
    required_fields: list[str]
    is_active: bool
    starts_at: datetime
    last_processed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkflowRunOut(BaseModel):
    id: UUID
    rule_id: UUID | None
    rule_id_snapshot: UUID
    rule_version: int
    replay_of_run_id: UUID | None
    rule_name: str
    trigger: str
    candidate_id: UUID | None
    position_id: UUID | None
    application_id: UUID | None
    source_activity_id: UUID | None
    status: Literal[
        "queued",
        "running",
        "retrying",
        "completed",
        "skipped",
        "failed",
        "dead_letter",
        "cancelled",
    ]
    action_results: list[dict]
    required_permissions: list[str]
    required_fields: list[str]
    error_code: str | None
    available_at: datetime
    attempt_count: int
    max_attempts: int
    cancellation_requested: bool
    cancelled_at: datetime | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class WorkflowRunPage(BaseModel):
    items: list[WorkflowRunOut]
    next_cursor: str | None = None


class WorkflowProcessOut(BaseModel):
    rules: int
    queued: int


class WorkflowDryRunRequest(BaseModel):
    candidate_id: UUID | None = None
    position_id: UUID | None = None
    application_id: UUID | None = None
    source_activity_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def target_is_coherent(self) -> "WorkflowDryRunRequest":
        if self.candidate_id is None and (
            self.position_id is not None
            or self.application_id is not None
            or self.source_activity_id is not None
        ):
            raise ValueError(
                "candidate_id is required with position_id or source_activity_id"
            )
        if self.application_id is not None and self.position_id is None:
            raise ValueError("application_id requires position_id")
        return self


class WorkflowDryRunActionOut(BaseModel):
    index: int
    type: WorkflowActionType
    outcome: Literal["would_execute"] = "would_execute"


class WorkflowDryRunItemOut(BaseModel):
    candidate_id: UUID
    position_id: UUID | None
    application_id: UUID | None = None
    source_activity_id: UUID | None
    matched: bool
    actions: list[WorkflowDryRunActionOut]


class WorkflowDryRunOut(BaseModel):
    rule_id: UUID
    rule_version: int
    evaluated: int
    matched: int
    items: list[WorkflowDryRunItemOut]
