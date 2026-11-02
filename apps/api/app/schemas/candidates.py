from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    computed_field,
)


def _is_present(value: object) -> bool:
    return bool(value)


FilePresence = Annotated[bool, BeforeValidator(_is_present)]


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    source_ref: str | None = None
    raw_cv: str | None = None
    parsed_cv: dict | None = None
    # The ORM's legacy ``pdf_path`` attribute is accepted only as a presence
    # signal. The filesystem value itself is never retained or serialized by
    # this DTO. ``pdf_path`` below remains as a compatibility alias for the web
    # client, but now points to the authenticated API endpoint.
    has_original_file: FilePresence = Field(
        default=False,
        validation_alias=AliasChoices("has_original_file", "pdf_path"),
    )
    github_login: str | None = None
    github_data: dict | None = None
    github_fetched_at: datetime | None = None
    is_favorite: bool = False
    viewed_at: datetime | None = None
    created_at: datetime

    @computed_field
    @property
    def pdf_path(self) -> str | None:
        """Backward-compatible, authorization-preserving CV URL."""

        return f"/candidates/{self.id}/cv" if self.has_original_file else None


class CandidateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    source_ref: str | None = None
    is_favorite: bool = False
    viewed_at: datetime | None = None
    created_at: datetime
    # Aggregated info (populated by the list endpoint, not stored on the model).
    best_score: int | None = None
    best_position_id: UUID | None = None
    best_position_title: str | None = None
    score_count: int = 0


class CandidateUpdate(BaseModel):
    is_favorite: bool | None = None


class TextUploadRequest(BaseModel):
    raw_text: str = Field(min_length=50, description="Raw CV text (plain).")
    source_ref: str | None = None


class HHJsonUploadRequest(BaseModel):
    hh_json: dict[str, Any] = Field(
        description="A single HeadHunter resume JSON object."
    )


class ScoreRequest(BaseModel):
    position_id: UUID
    application_id: UUID | None = None


class CandidateProcessingJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    position_id: UUID | None = None
    application_id: UUID | None = None
    kind: str
    status: str
    failure_code: str | None = None
    failure_detail: str | None = None
    result_summary: dict = Field(default_factory=dict)
    attempt_count: int
    max_attempts: int
    created_at: datetime
    completed_at: datetime | None = None


class CandidateProcessingBatchOut(BaseModel):
    queued: int
    message: str


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: UUID
    position_id: UUID
    score_technical_skills: int | None = None
    score_quality_of_projects: int | None = None
    score_previous_experience: int | None = None
    score_impact: int | None = None
    comment_technical_skills: str | None = None
    comment_quality_of_projects: str | None = None
    comment_previous_experience: str | None = None
    comment_impact: str | None = None
    overall_score: int | None = None
    model_used: str | None = None
    stage: str = "new"
    owner_user_id: UUID | None = None
    next_action_due_date: date | None = None


class CandidateDetail(CandidateOut):
    """Bounded candidate metadata.

    Application/score associations are exposed through the cursor-paginated
    ``/candidates/{candidate_id}/applications`` collection.
    """


class CandidateApplicationOut(ScoreOut):
    application_id: UUID
    application_version: datetime
    cycle_number: int = Field(ge=1)
    is_active: bool
    applied_at: datetime


class PositionMatch(BaseModel):
    """A single (position, score-or-null) row in the reverse-match results.

    `overall_score` is None until the candidate has been scored against this
    specific position; the UI then offers a 'score this' action.
    """

    position_id: UUID
    position_title: str
    position_status: str
    overall_score: int | None = None
    score_technical_skills: int | None = None
    score_quality_of_projects: int | None = None
    score_previous_experience: int | None = None
    score_impact: int | None = None
    scored_at: datetime | None = None
    model_used: str | None = None
