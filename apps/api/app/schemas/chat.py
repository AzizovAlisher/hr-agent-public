from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

ChatScope = Literal["candidate", "position", "free"]


class ChatCapabilityOut(BaseModel):
    """Sanitized, tenant-specific readiness used to gate AI controls."""

    available: bool
    route: Literal["managed", "private"] | None = None
    reason: (
        Literal[
            "ai_managed_processing_opt_in_required",
            "ai_managed_provider_unavailable",
            "ai_private_provider_unavailable",
            "ai_provider_unavailable",
            "ai_budget_exceeded",
        ]
        | None
    ) = None


class ChatSessionCreate(BaseModel):
    scope: ChatScope
    candidate_id: UUID | None = None
    position_id: UUID | None = None
    application_id: UUID | None = None
    title: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _check_scope_target(self) -> "ChatSessionCreate":
        if self.scope == "candidate" and self.candidate_id is None:
            raise ValueError("candidate_id is required when scope=candidate")
        if self.scope == "position" and self.position_id is None:
            raise ValueError("position_id is required when scope=position")
        if self.application_id is not None and (
            self.candidate_id is None or self.position_id is None
        ):
            raise ValueError(
                "application_id requires both candidate_id and position_id"
            )
        return self


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    model_used: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    created_at: datetime


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: str
    candidate_id: UUID | None = None
    position_id: UUID | None = None
    application_id: UUID | None = None
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatSessionDetail(ChatSessionOut):
    messages: list[ChatMessageOut] = Field(default_factory=list)


class ChatSessionListItem(ChatSessionOut):
    """Lightweight session item for the list view — no full message history."""

    message_count: int = 0
    preview: str | None = None
    last_activity: datetime | None = None
    # Display labels for the linked entity (resolved server-side so the client
    # doesn't have to make N extra calls to render them).
    candidate_ref: str | None = None
    candidate_source: str | None = None
    position_title: str | None = None


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class ChatDeltaEventData(BaseModel):
    content: str


class ChatUsageEventData(BaseModel):
    model_used: str | None = None
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    provider_used: str | None = None
    cost_microusd: int | None = Field(default=None, ge=0)


class ChatDoneEventData(BaseModel):
    message_id: UUID


class ChatErrorEventData(BaseModel):
    code: str
    message: str


class ChatDeltaStreamEvent(BaseModel):
    event: Literal["delta"]
    data: ChatDeltaEventData


class ChatUsageStreamEvent(BaseModel):
    event: Literal["usage"]
    data: ChatUsageEventData


class ChatDoneStreamEvent(BaseModel):
    event: Literal["done"]
    data: ChatDoneEventData


class ChatErrorStreamEvent(BaseModel):
    event: Literal["error"]
    data: ChatErrorEventData


ChatStreamEventVariant = Annotated[
    ChatDeltaStreamEvent
    | ChatUsageStreamEvent
    | ChatDoneStreamEvent
    | ChatErrorStreamEvent,
    Field(discriminator="event"),
]


class ChatStreamEvent(RootModel[ChatStreamEventVariant]):
    """One typed JSON event carried in the general chat SSE stream."""
