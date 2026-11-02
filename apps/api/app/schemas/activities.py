from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


ActivityCategory = Literal["pipeline", "evaluation", "collaboration"]


class ActivityActorOut(BaseModel):
    id: UUID
    email: str


class ActivityPositionOut(BaseModel):
    id: UUID
    title: str


class CandidateActivityOut(BaseModel):
    id: UUID
    application_id: UUID | None = None
    event_type: str
    category: ActivityCategory
    actor: ActivityActorOut | None = None
    actor_kind: Literal["human", "ai", "system", "public"] = "system"
    agent_run_id: UUID | None = None
    position: ActivityPositionOut | None = None
    subject_type: str | None = None
    subject_id: UUID | None = None
    payload: dict[str, Any]
    occurred_at: datetime


class CandidateActivityPage(BaseModel):
    items: list[CandidateActivityOut]
    next_cursor: str | None = None
