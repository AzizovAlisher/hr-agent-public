from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PositionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    full_description: str | None = None
    requirements: dict[str, Any] | None = None
    internal_notes: str | None = None
    status: Literal["open", "paused", "closed"] = "open"
    folder: str | None = Field(default=None, max_length=64)
    folder_id: UUID | None = None
    sensitivity: Literal["public", "nda", "restricted"] = "public"


class PositionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    full_description: str | None = None
    requirements: dict[str, Any] | None = None
    internal_notes: str | None = None
    status: Literal["open", "paused", "closed"] | None = None
    public_apply_enabled: bool | None = None
    folder: str | None = Field(default=None, max_length=64)
    folder_id: UUID | None = None
    sensitivity: Literal["public", "nda", "restricted"] | None = None
    candidate_profile: str | None = None
    public_description: str | None = None


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None = None
    full_description: str | None = None
    requirements: dict | None = None
    internal_notes: str | None = None
    status: str
    folder: str | None = None
    folder_id: UUID | None = None
    public_apply_enabled: bool = True
    sensitivity: str = "public"
    candidate_profile: str | None = None
    public_description: str | None = None
    created_by: UUID | None = None
    candidate_count: int = 0
    chat_session_count: int = 0
    created_at: datetime
    updated_at: datetime

class RankedCandidate(BaseModel):
    candidate_id: UUID
    source: str
    source_ref: str | None = None
    display_name: str | None = None
    overall_score: int | None = None
    score_technical_skills: int | None = None
    score_quality_of_projects: int | None = None
    score_previous_experience: int | None = None
    score_impact: int | None = None
    comment_technical_skills: str | None = None
    comment_quality_of_projects: str | None = None
    comment_previous_experience: str | None = None
    comment_impact: str | None = None
    model_used: str | None = None
    is_favorite: bool = False
    viewed_at: datetime | None = None
    stage: str = "new"
    owner_user_id: UUID | None = None
    next_action_due_date: date | None = None
    application_id: UUID | None = None
    application_version: datetime | None = None


class StageUpdate(BaseModel):
    # One-release compatibility: callers may omit the cycle only while the
    # candidate/position pair has exactly one active Application.  New clients
    # should always send the identifier returned by the pipeline collection.
    application_id: UUID | None = None
    stage: Literal["new", "screening", "interview", "offer", "hired", "rejected"]
    expected_version: datetime | None = None
    disposition_code: Literal[
        "not_qualified",
        "withdrew",
        "compensation",
        "timing",
        "duplicate",
        "position_closed",
        "other",
    ] | None = None
    reason: str | None = Field(default=None, max_length=2_000)

    @field_validator("expected_version")
    @classmethod
    def aware_version(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expected_version must include a timezone")
        return value

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value is not None else None
        return cleaned or None

    @model_validator(mode="after")
    def rejection_metadata(self) -> "StageUpdate":
        if self.stage == "rejected" and self.disposition_code is None:
            raise ValueError("rejected applications require a disposition_code")
        if self.stage == "rejected" and self.reason is None:
            raise ValueError("rejected applications require a reason")
        if self.stage != "rejected" and self.disposition_code is not None:
            raise ValueError("disposition_code is only valid when rejecting")
        return self


class PositionDetail(PositionOut):
    """Bounded position metadata.

    The active pipeline is exposed separately through the cursor-paginated
    ``/positions/{position_id}/applications`` collection.
    """
