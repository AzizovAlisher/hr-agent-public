from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CandidateStage = Literal["new", "screening", "interview", "offer", "hired", "rejected"]


class CandidateSearchFilters(BaseModel):
    q: str | None = Field(default=None, max_length=200)
    score_min: int | None = Field(default=None, ge=0, le=100)
    score_max: int | None = Field(default=None, ge=0, le=100)
    sources: list[str] = Field(default_factory=list, max_length=20)
    stages: list[CandidateStage] = Field(default_factory=list, max_length=6)
    position_id: UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    favorites_only: bool = False
    new_only: bool = False
    unscored_only: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> "CandidateSearchFilters":
        if (
            self.score_min is not None
            and self.score_max is not None
            and self.score_min > self.score_max
        ):
            raise ValueError("score_min cannot exceed score_max")
        self.sources = sorted(
            {source.strip().lower() for source in self.sources if source.strip()}
        )
        self.tags = sorted({tag.strip() for tag in self.tags if tag.strip()})
        if any(len(source) > 32 for source in self.sources):
            raise ValueError("source values must be at most 32 characters")
        if any(len(tag) > 32 for tag in self.tags):
            raise ValueError("tag values must be at most 32 characters")
        self.q = (self.q or "").strip() or None
        return self


class CandidateSearchApplication(BaseModel):
    # Operational application rows remain visible when assessment-result
    # fields are denied; in that case the legacy score identifier is redacted.
    score_id: UUID | None = None
    position_id: UUID
    position_title: str
    stage: CandidateStage
    overall_score: int | None = None


class CandidateSearchItem(BaseModel):
    id: UUID
    source: str
    source_ref: str | None = None
    is_favorite: bool
    viewed_at: datetime | None = None
    created_at: datetime
    best_score: int | None = None
    best_position_id: UUID | None = None
    best_position_title: str | None = None
    score_count: int = 0
    stages: list[CandidateStage] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    applications: list[CandidateSearchApplication] = Field(default_factory=list)
    duplicate_count: int = 0


class CandidateSearchPage(BaseModel):
    items: list[CandidateSearchItem]
    total: int
    available_sources: list[str] = Field(default_factory=list)
    next_cursor: str | None = None


class CandidateSavedViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    filters: CandidateSearchFilters

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class CandidateSavedViewUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    filters: CandidateSearchFilters | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "CandidateSavedViewUpdate":
        if self.name is None and self.filters is None:
            raise ValueError("At least one field must be provided")
        return self


class CandidateSavedViewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    filters: CandidateSearchFilters
    created_at: datetime
    updated_at: datetime


# Keep duplicate review selections inside CandidateMergePreviewRequest's
# existing 20-candidate contract while preventing a single transitive cluster
# from expanding an otherwise bounded duplicate-group page without limit.
CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT = 20


class CandidateDuplicateMember(BaseModel):
    id: UUID
    source: str
    source_ref: str | None = None
    created_at: datetime
    best_score: int | None = None
    best_position_title: str | None = None


class CandidateDuplicateGroup(BaseModel):
    id: str
    match_types: list[Literal["phone", "email", "source_ref", "github", "cv"]]
    candidates: list[CandidateDuplicateMember] = Field(
        max_length=CANDIDATE_DUPLICATE_GROUP_MEMBER_LIMIT
    )
    candidate_count: int = Field(ge=2)
    has_more_candidates: bool


class CandidateDuplicatePage(BaseModel):
    items: list[CandidateDuplicateGroup]
    candidate_count: int
    next_cursor: str | None = None


class CandidateMergeScoreWinner(BaseModel):
    position_id: UUID
    winner_candidate_id: UUID


class CandidateMergeRequest(BaseModel):
    duplicate_ids: list[UUID] = Field(min_length=1, max_length=20)
    score_winners: list[CandidateMergeScoreWinner] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_winner_positions(self) -> "CandidateMergeRequest":
        positions = [winner.position_id for winner in self.score_winners]
        if len(set(positions)) != len(positions):
            raise ValueError("score_winners must contain each position once")
        return self


class CandidateMergeOut(BaseModel):
    batch_id: UUID
    primary_candidate_id: UUID
    merged_candidate_ids: list[UUID]


class CandidateMergeHistoryItem(BaseModel):
    id: UUID
    primary_candidate_id: UUID
    member_count: int
    status: Literal["merged", "reversed"]
    version: int
    can_reverse: bool
    created_at: datetime
    reversed_at: datetime | None = None


class CandidateMergeHistoryPage(BaseModel):
    items: list[CandidateMergeHistoryItem]
    next_cursor: str | None = None


class CandidateMergeReverseRequest(BaseModel):
    expected_version: int = Field(ge=1)


class CandidateMergeReverseOut(BaseModel):
    batch_id: UUID
    primary_candidate_id: UUID
    restored_candidate_ids: list[UUID]
    version: int
    reversed_at: datetime


class CandidateMergePreviewRequest(BaseModel):
    candidate_ids: list[UUID] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def unique_candidates(self) -> "CandidateMergePreviewRequest":
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        return self


class CandidateMergeConflictOption(BaseModel):
    candidate_id: UUID
    overall_score: int | None = None
    stage: CandidateStage
    owner_user_id: UUID | None = None
    next_action_due_date: date | None = None
    scored_at: datetime


class CandidateMergeConflict(BaseModel):
    position_id: UUID
    position_title: str
    candidate_ids: list[UUID]
    options: list[CandidateMergeConflictOption]


class CandidateMergePreviewOut(BaseModel):
    candidates: list[CandidateDuplicateMember]
    score_conflicts: list[CandidateMergeConflict]
    shared_tags: list[str]


class CandidateMergedSourceScore(BaseModel):
    id: UUID
    position_id: UUID
    position_title: str
    overall_score: int | None = None
    stage: CandidateStage
    selected: bool


class CandidateMergedSourceSummary(BaseModel):
    """Safe metadata for a merged source record.

    The source evidence itself intentionally stays server-side.  This response is
    rendered by a client component, so it exposes presence flags instead of CV,
    contact, GitHub snapshot, or storage-path contents.
    """

    id: UUID
    is_primary: bool
    source: str
    source_ref: str | None = None
    has_cv: bool
    has_contact: bool
    github_login: str | None = None
    created_at: datetime
    merged_at: datetime | None = None
    scores: list[CandidateMergedSourceScore]


class CandidateMergedSourceEvidence(BaseModel):
    """Sensitive source evidence, returned only by the explicit detail route."""

    id: UUID
    source: str
    source_ref: str | None = None
    raw_cv: str | None = None
    parsed_cv: dict | None = None
    phone_normalized: str | None = None
    email_normalized: str | None = None
    github_login: str | None = None
    github_data: dict | None = None
    github_fetched_at: datetime | None = None
    has_original_file: bool


class CandidateBulkRequest(BaseModel):
    candidate_ids: list[UUID] = Field(min_length=1, max_length=100)
    action: Literal["tag_add", "tag_remove", "stage", "reject"]
    position_id: UUID | None = None
    stage: CandidateStage | None = None
    tag: str | None = Field(default=None, max_length=32)
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

    @model_validator(mode="after")
    def validate_action(self) -> "CandidateBulkRequest":
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        if self.action in {"tag_add", "tag_remove"}:
            self.tag = (self.tag or "").strip()
            if not self.tag:
                raise ValueError("tag is required for the tag action")
        if self.action in {"stage", "reject"} and self.position_id is None:
            raise ValueError("position_id is required for stage actions")
        if self.action == "stage" and self.stage is None:
            raise ValueError("stage is required for the stage action")
        if self.action == "stage" and self.stage == "rejected":
            raise ValueError("use the reject action when rejecting candidates")
        self.reason = (self.reason or "").strip() or None
        if self.action == "reject" and self.disposition_code is None:
            raise ValueError("disposition_code is required for rejection")
        if self.action == "reject" and self.reason is None:
            raise ValueError("reason is required for rejection")
        if self.action != "reject" and self.disposition_code is not None:
            raise ValueError("disposition_code is only valid for rejection")
        return self


class CandidateBulkOut(BaseModel):
    changed_candidate_ids: list[UUID]
    unchanged_candidate_ids: list[UUID]


class CandidateExportRequest(BaseModel):
    candidate_ids: list[UUID] = Field(min_length=1, max_length=500)
    position_id: UUID | None = None
    include_contacts: bool = False

    @model_validator(mode="after")
    def unique_ids(self) -> "CandidateExportRequest":
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        return self
