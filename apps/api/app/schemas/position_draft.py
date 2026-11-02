from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class DraftMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class PositionDraft(BaseModel):
    title: str | None = None
    description: str | None = None
    requirements: dict[str, Any] | None = None
    seniority: str | None = None
    work_format: str | None = None
    location: str | None = None
    employment_type: str | None = None
    salary_range: str | None = None
    team_size: str | None = None


class QuickReply(BaseModel):
    """A single suggested answer chip rendered below an assistant message.

    `field` names the structured slot the chip will fill (e.g. "seniority").
    `value` is the canonical machine value; `label` is what the user sees.
    """

    field: str
    value: str
    label: str


class DraftConverseRequest(BaseModel):
    messages: list[DraftMessage] = Field(min_length=1)
    current_draft: PositionDraft | None = None


class DraftConverseResponse(BaseModel):
    assistant_message: str
    draft: PositionDraft
    is_complete: bool = False
    missing_required: list[str] = Field(default_factory=list)
    quick_replies: list[QuickReply] = Field(default_factory=list)
    model_used: str | None = None


class PositionDraftExtractTextResponse(BaseModel):
    filename: str
    char_count: int = Field(ge=0)
    truncated: bool
    text: str


class ProjectExtractResponse(BaseModel):
    """Result of parsing a project-hiring document into N position drafts."""

    project_name: str | None = None
    folder: str | None = None
    positions: list[PositionDraft] = Field(default_factory=list)
    source_filename: str | None = None
    source_chars: int = 0
    model_used: str | None = None


class ProjectCreateRequest(BaseModel):
    """Bulk-create N positions under a shared folder.

    `folder` is applied to every position so the dashboard groups them together;
    individual draft.folder values are ignored to keep the project cohesive.
    """

    folder: str | None = Field(default=None, max_length=64)
    positions: list[PositionDraft] = Field(min_length=1)


class ProjectCreateResponse(BaseModel):
    created: list[UUID] = Field(default_factory=list)
    folder_id: UUID
    folder: str | None = None
