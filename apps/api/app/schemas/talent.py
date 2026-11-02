"""Talent CRM API contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name cannot be blank")
    return cleaned


class TalentPoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4_000)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)


class TalentPoolUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4_000)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return _clean_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "TalentPoolUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class TalentPoolOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    color: str | None
    member_count: int
    created_at: datetime
    updated_at: datetime


class TalentPoolPage(BaseModel):
    items: list[TalentPoolOut]
    next_cursor: str | None = None


class TalentPoolMembersRequest(BaseModel):
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("candidate_ids")
    @classmethod
    def unique_candidates(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class TalentCandidateOut(BaseModel):
    id: UUID
    source: str
    source_ref: str | None
    created_at: datetime
    best_score: int | None
    best_position_id: UUID | None
    best_position_title: str | None
    relevance: float | None = None
    match_reasons: list[str] = Field(default_factory=list)
    already_in_pipeline: bool = False
    pool_member_id: UUID | None = None


class TalentCandidatePage(BaseModel):
    items: list[TalentCandidateOut]
    total: int
    next_cursor: str | None = None


class TalentPoolMutationOut(BaseModel):
    added: int = 0
    removed: int = 0
    unchanged: int = 0


class TalentNurtureStepCreate(BaseModel):
    channel: Literal["email", "telegram", "whatsapp"]
    template_id: UUID
    delay_days: int = Field(ge=0, le=365)


class TalentNurtureSequenceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    pool_id: UUID | None = None
    steps: list[TalentNurtureStepCreate] = Field(min_length=1, max_length=20)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("steps")
    @classmethod
    def ordered_delays(
        cls, value: list[TalentNurtureStepCreate]
    ) -> list[TalentNurtureStepCreate]:
        delays = [step.delay_days for step in value]
        if delays != sorted(delays):
            raise ValueError("Nurture step delays must be ascending")
        if any(step.channel == "whatsapp" for step in value):
            raise ValueError("capability_unavailable: WhatsApp is outside pilot scope")
        return value


class TalentNurtureStepOut(BaseModel):
    id: UUID
    position: int
    channel: Literal["email", "telegram", "whatsapp"]
    template_id: UUID | None
    template_name: str
    delay_days: int


class TalentNurtureSequenceOut(BaseModel):
    id: UUID
    name: str
    pool_id: UUID | None
    is_active: bool
    steps: list[TalentNurtureStepOut]
    created_at: datetime
    updated_at: datetime


class TalentNurtureSequencePage(BaseModel):
    items: list[TalentNurtureSequenceOut]
    next_cursor: str | None = None


class TalentNurtureLaunchRequest(BaseModel):
    candidate_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    position_id: UUID | None = None

    @field_validator("candidate_ids")
    @classmethod
    def unique_candidates(cls, value: list[UUID] | None) -> list[UUID] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class TalentNurtureLaunchOut(BaseModel):
    enrolled: int
    already_enrolled: int
    skipped: int
    messages_scheduled: int
