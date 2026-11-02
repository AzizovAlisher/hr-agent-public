"""Schemas for candidate enrichment: GitHub, interview recordings, technical
task submissions."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field


def _is_present(value: object) -> bool:
    return bool(value)


FilePresence = Annotated[bool, BeforeValidator(_is_present)]


# ---------- GitHub ----------


class GitHubAttachRequest(BaseModel):
    # Accepts a bare username, @handle, or full github.com URL — normalized
    # server-side.
    login: str = Field(..., min_length=1, max_length=200)


class GitHubDetectOut(BaseModel):
    suggested_login: str | None = None


class GitHubProfileOut(BaseModel):
    login: str | None = None
    name: str | None = None
    html_url: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    company: str | None = None
    location: str | None = None
    blog: str | None = None
    followers: int = Field(default=0, ge=0)
    following: int = Field(default=0, ge=0)
    public_repos: int = Field(default=0, ge=0)
    created_at: datetime | None = None


class GitHubRepositoryOut(BaseModel):
    name: str | None = None
    full_name: str | None = None
    html_url: str | None = None
    description: str | None = None
    language: str | None = None
    stars: int = Field(default=0, ge=0)
    forks: int = Field(default=0, ge=0)
    topics: list[str] = Field(default_factory=list)
    pushed_at: datetime | None = None


class GitHubLanguageOut(BaseModel):
    language: str
    repo_count: int = Field(ge=0)


class GitHubStatsOut(BaseModel):
    total_stars: int = Field(default=0, ge=0)
    non_fork_repos: int = Field(default=0, ge=0)
    shown_repos: int = Field(default=0, ge=0)


class GitHubDataOut(BaseModel):
    profile: GitHubProfileOut
    repos: list[GitHubRepositoryOut] = Field(
        default_factory=list,
        validation_alias=AliasChoices("repos", "repositories"),
    )
    languages: list[GitHubLanguageOut] = Field(default_factory=list)
    stats: GitHubStatsOut = Field(default_factory=GitHubStatsOut)
    ai_summary: str | None = None
    ai_summary_model: str | None = None
    fetched_at: datetime | None = None


class GitHubOut(BaseModel):
    login: str
    data: GitHubDataOut
    fetched_at: datetime | None = None


# ---------- Interview recordings ----------


class RecordingCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    position_id: UUID | None = None
    application_id: UUID | None = None
    media_url: str | None = Field(default=None, max_length=1024)
    status: str | None = Field(default=None, max_length=24)
    language: str | None = Field(default=None, max_length=16)
    recorded_at: datetime | None = None


class RecordingUpdate(BaseModel):
    """Partial update — also the write-hook the external transcription /
    diarization / scoring pipeline calls to fill results in."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    position_id: UUID | None = None
    application_id: UUID | None = None
    status: str | None = Field(default=None, max_length=24)
    media_url: str | None = Field(default=None, max_length=1024)
    language: str | None = Field(default=None, max_length=16)
    duration_seconds: int | None = None
    transcript: str | None = None
    diarization: list[Any] | None = None
    interview_score: int | None = Field(default=None, ge=0, le=100)
    interview_summary: str | None = None
    evaluation: dict[str, Any] | None = None
    recorded_at: datetime | None = None


class DiarizationTurnOut(BaseModel):
    speaker: str | None = None
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, ge=0)
    text: str | None = None


class RecordingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    position_id: UUID | None = None
    application_id: UUID | None = None
    title: str
    status: str
    has_media_file: FilePresence = Field(
        default=False,
        validation_alias=AliasChoices("has_media_file", "media_path"),
    )
    media_url: str | None = None
    language: str | None = None
    duration_seconds: int | None = None
    transcript: str | None = None
    diarization: list[DiarizationTurnOut] | None = None
    interview_score: int | None = None
    interview_summary: str | None = None
    evaluation: dict[str, Any] | None = None
    recorded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ---------- Technical task submissions ----------


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    prompt: str | None = None
    submission_type: Literal["repo", "file", "text"] = "text"
    repo_url: str | None = Field(default=None, max_length=1024)
    content: str | None = None
    position_id: UUID | None = None
    application_id: UUID | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    prompt: str | None = None
    submission_type: Literal["repo", "file", "text"] | None = None
    repo_url: str | None = Field(default=None, max_length=1024)
    content: str | None = None
    status: str | None = Field(default=None, max_length=16)
    position_id: UUID | None = None
    application_id: UUID | None = None


class TaskEvaluationOut(BaseModel):
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str | None = None
    strengths: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    recommendation: Literal["strong_yes", "yes", "maybe", "no"] | None = None
    output_locale: Literal["en", "ru"] | None = None
    decision_authority: Literal["human"] | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    position_id: UUID | None = None
    application_id: UUID | None = None
    title: str
    prompt: str | None = None
    submission_type: str
    repo_url: str | None = None
    has_submission_file: FilePresence = Field(
        default=False,
        validation_alias=AliasChoices("has_submission_file", "file_path"),
    )
    content: str | None = None
    status: str
    ai_score: int | None = None
    ai_summary: str | None = None
    evaluation: TaskEvaluationOut | None = None
    model_used: str | None = None
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
