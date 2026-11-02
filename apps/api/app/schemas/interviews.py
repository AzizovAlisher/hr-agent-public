from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

InterviewStatus = Literal["draft", "in_progress", "scored", "archived"]
QuestionCategory = Literal["technical", "behavioral", "situational", "culture"]


class InterviewQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    interview_id: UUID
    position: int
    category: str
    question_text: str
    rationale: str | None = None
    expected_signals: str | None = None
    is_required: bool = True
    score: int | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class InterviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_id: UUID | None = None
    candidate_id: UUID
    position_id: UUID
    status: InterviewStatus
    notes: str | None = None
    overall_score: float | None = None
    model_used: str | None = None
    kit_version: int = 1
    question_count: int = 0
    created_at: datetime
    updated_at: datetime


class InterviewDetail(InterviewOut):
    questions: list[InterviewQuestionOut] = Field(default_factory=list)
    interviewer_user_ids: list[UUID] = Field(default_factory=list)


class InterviewQuestionCreate(BaseModel):
    category: QuestionCategory
    question_text: str = Field(min_length=1, max_length=10_000)
    rationale: str | None = Field(default=None, max_length=4_000)
    expected_signals: str | None = Field(default=None, max_length=4_000)
    is_required: bool = True

    @field_validator("question_text")
    @classmethod
    def clean_question_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question_text cannot be blank")
        return cleaned

    @field_validator("rationale", "expected_signals")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ManualInterviewCreate(BaseModel):
    """Create one immutable manual kit for an exact active application cycle."""

    application_id: UUID
    position_id: UUID
    questions: list[InterviewQuestionCreate] = Field(min_length=1, max_length=50)
    interviewer_user_ids: list[UUID] = Field(min_length=1, max_length=20)
    notes: str | None = Field(default=None, max_length=20_000)

    @field_validator("interviewer_user_ids")
    @classmethod
    def unique_interviewers(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def unique_questions(self) -> "ManualInterviewCreate":
        normalized = [question.question_text.casefold() for question in self.questions]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Interview question text must be unique within a kit")
        return self


class InterviewKitVersionCreate(BaseModel):
    """Replace a still-draft live kit while preserving prior snapshots."""

    expected_version: int = Field(ge=1)
    questions: list[InterviewQuestionCreate] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_questions(self) -> "InterviewKitVersionCreate":
        normalized = [question.question_text.casefold() for question in self.questions]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Interview question text must be unique within a kit")
        return self


class InterviewCompleteRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=20_000)

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class GenerateRequest(BaseModel):
    position_id: UUID
    application_id: UUID | None = None
    count: int = Field(default=10, ge=4, le=20)


class InterviewUpdate(BaseModel):
    status: InterviewStatus | None = None
    notes: str | None = None


class QuestionScoreRequest(BaseModel):
    score: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None
