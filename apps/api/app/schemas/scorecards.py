"""API contracts for structured hiring-team scorecards."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _clean(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be blank")
    return cleaned


class ScorecardCriterionCreate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2_000)
    weight: int = Field(default=1, ge=1, le=100)
    is_required: bool = True

    _clean_label = field_validator("label")(_clean)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        if not normalized:
            raise ValueError("criterion key must contain a letter or number")
        return normalized


class ScorecardTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4_000)
    position_id: UUID | None = None
    criteria: list[ScorecardCriterionCreate] = Field(min_length=1, max_length=30)

    _clean_name = field_validator("name")(_clean)

    @model_validator(mode="after")
    def unique_criteria(self) -> "ScorecardTemplateCreate":
        keys = [criterion.key for criterion in self.criteria]
        if len(set(keys)) != len(keys):
            raise ValueError("criterion keys must be unique")
        return self


class ScorecardTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4_000)
    is_active: bool | None = None
    criteria: list[ScorecardCriterionCreate] | None = Field(
        default=None, min_length=1, max_length=30
    )

    _clean_name = field_validator("name")(_clean)

    @model_validator(mode="after")
    def require_change(self) -> "ScorecardTemplateUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self

    @model_validator(mode="after")
    def unique_criteria(self) -> "ScorecardTemplateUpdate":
        if self.criteria is not None:
            keys = [criterion.key for criterion in self.criteria]
            if len(set(keys)) != len(keys):
                raise ValueError("criterion keys must be unique")
        return self


class ScorecardCriterionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    key: str
    label: str
    description: str | None
    weight: int
    position: int
    is_required: bool


class ScorecardTemplateOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    position_id: UUID | None
    is_active: bool
    current_version: int
    criteria: list[ScorecardCriterionOut]
    created_at: datetime
    updated_at: datetime


class ScorecardAssignRequest(BaseModel):
    position_id: UUID
    application_id: UUID | None = None
    template_id: UUID
    user_ids: list[UUID] = Field(min_length=1, max_length=20)
    due_at: datetime | None = None

    @field_validator("due_at")
    @classmethod
    def aware_due_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_at must include a timezone")
        return value

    @field_validator("user_ids")
    @classmethod
    def unique_users(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class ScorecardRatingInput(BaseModel):
    criterion_id: UUID
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=4_000)


class ScorecardDraftUpdate(BaseModel):
    ratings: list[ScorecardRatingInput] = Field(min_length=1, max_length=30)

    @field_validator("ratings")
    @classmethod
    def unique_ratings(
        cls, value: list[ScorecardRatingInput]
    ) -> list[ScorecardRatingInput]:
        ids = [entry.criterion_id for entry in value]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion ratings must be unique")
        return value


class ScorecardRatingOut(BaseModel):
    id: UUID
    criterion_id: UUID | None
    criterion_key: str
    criterion_label: str
    weight: int
    rating: int
    comment: str | None


class ScorecardAssignmentOut(BaseModel):
    id: UUID
    application_id: UUID | None
    candidate_id: UUID
    position_id: UUID | None
    template_id: UUID | None
    template_name: str
    template_version: int
    criteria: list[ScorecardCriterionOut]
    position_title: str
    assigned_to_user_id: UUID | None
    assignee_email: str
    is_mine: bool
    status: Literal["draft", "submitted"]
    due_at: datetime | None
    submitted_at: datetime | None
    ratings: list[ScorecardRatingOut] | None
    weighted_average: float | None
    created_at: datetime
    updated_at: datetime


class ScorecardPanelOut(BaseModel):
    assignments: list[ScorecardAssignmentOut]
    revealed: bool
    aggregate_average: float | None
    completion_count: int
    assignment_count: int
    decision: "HiringDecisionOut | None" = None


class HiringDecisionCreate(BaseModel):
    position_id: UUID
    application_id: UUID | None = None
    recommendation: Literal["advance", "hold", "reject", "hire"]
    summary: str | None = Field(default=None, max_length=8_000)
    required_approvals: int = Field(default=1, ge=1, le=20)

    @model_validator(mode="after")
    def terminal_decision_reason(self) -> "HiringDecisionCreate":
        if self.recommendation in {"reject", "hire"} and not (
            self.summary or ""
        ).strip():
            raise ValueError("reject and hire recommendations require a summary")
        return self


class HiringDecisionVoteRequest(BaseModel):
    vote: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def rejection_reason(self) -> "HiringDecisionVoteRequest":
        if self.vote == "reject" and not (self.comment or "").strip():
            raise ValueError("A rejection reason is required")
        return self


class HiringDecisionApprovalOut(BaseModel):
    id: UUID
    user_id: UUID | None
    user_email: str
    decision_version: int
    approval_cycle: int
    vote: Literal["approve", "reject"]
    comment: str | None
    created_at: datetime
    updated_at: datetime


class HiringDecisionOut(BaseModel):
    id: UUID
    application_id: UUID | None
    candidate_id: UUID
    position_id: UUID | None
    position_title: str
    created_by_user_id: UUID | None
    created_by_email: str
    recommendation: Literal["advance", "hold", "reject", "hire"]
    summary: str | None
    version: int
    approval_cycle: int
    required_approvals: int
    status: Literal["pending", "approved", "rejected"]
    decided_at: datetime | None
    approvals: list[HiringDecisionApprovalOut]
    created_at: datetime
    updated_at: datetime


ScorecardPanelOut.model_rebuild()
