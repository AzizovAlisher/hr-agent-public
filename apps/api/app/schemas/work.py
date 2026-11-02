from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class WorkOwnerOut(BaseModel):
    id: UUID
    email: str


class CandidateWorkUpdate(BaseModel):
    # See StageUpdate: omission is accepted only for an unambiguous active
    # application cycle.
    application_id: UUID | None = None
    # The endpoint distinguishes omitted values from explicit nulls with
    # model_fields_set, allowing either field to be cleared independently.
    owner_user_id: UUID | None = None
    next_action_due_date: date | None = None
    # Optional compare-and-set values prevent a stale browser from silently
    # overwriting a teammate's newer edit to the same field.
    expected_owner_user_id: UUID | None = None
    expected_next_action_due_date: date | None = None


class CandidateWorkOut(BaseModel):
    candidate_id: UUID
    position_id: UUID
    application_id: UUID
    owner: WorkOwnerOut | None = None
    next_action_due_date: date | None = None
    stage: str


class MyWorkCandidateOut(BaseModel):
    id: UUID
    source: str
    source_ref: str | None = None


class MyWorkPositionOut(BaseModel):
    id: UUID
    title: str
    status: str


class MyWorkItemOut(BaseModel):
    score_id: UUID
    candidate: MyWorkCandidateOut
    position: MyWorkPositionOut
    stage: str
    overall_score: int | None = None
    owner: WorkOwnerOut
    next_action_due_date: date | None = None
    last_activity_at: datetime
    is_stale: bool
    is_missing_feedback: bool
    interview_starts_at: datetime | None = None


class MyWorkCountsOut(BaseModel):
    all: int
    overdue: int
    today: int
    upcoming: int
    undated: int
    interviews_today: int
    stale: int
    missing_feedback: int


class MyWorkPageOut(BaseModel):
    items: list[MyWorkItemOut]
    counts: MyWorkCountsOut
    today: date
    next_cursor: str | None = None
