"""Read-only candidate activity timeline."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import user_with_permissions
from app.db import get_session
from app.models.candidate_activity import CandidateActivity
from app.models.position import Position
from app.models.user import User
from app.schemas.activities import (
    ActivityActorOut,
    ActivityPositionOut,
    CandidateActivityOut,
    CandidateActivityPage,
)
from app.services.candidate_merge import (
    candidate_history_ids,
    resolve_canonical_candidate,
)
from app.services.enterprise import can_access_field
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

router = APIRouter(
    prefix="/candidates",
    tags=["candidate-activities"],
)
candidate_reader = user_with_permissions("candidates.read")


EVENT_CATEGORIES: dict[str, str] = {
    "candidate_added": "pipeline",
    "candidate_sourced": "pipeline",
    "candidate_deleted": "pipeline",
    "candidate_restored": "pipeline",
    "candidate_scored": "evaluation",
    "candidate_rescored": "evaluation",
    "ai_candidate_assessed": "evaluation",
    "ai_candidate_shortlisted": "evaluation",
    "stage_changed": "pipeline",
    "note_added": "collaboration",
    "note_deleted": "collaboration",
    "tag_added": "collaboration",
    "tag_removed": "collaboration",
    "github_attached": "evaluation",
    "github_detached": "evaluation",
    "interview_generated": "evaluation",
    "interview_status_changed": "evaluation",
    "interview_deleted": "evaluation",
    "recording_added": "evaluation",
    "recording_status_changed": "evaluation",
    "recording_deleted": "evaluation",
    "task_added": "evaluation",
    "task_status_changed": "evaluation",
    "task_evaluated": "evaluation",
    "task_deleted": "evaluation",
    "candidate_owner_changed": "collaboration",
    "next_action_due_changed": "collaboration",
    "candidate_merged": "collaboration",
    "candidate_merge_reversed": "collaboration",
    "candidate_exported": "collaboration",
    "contact_point_added": "collaboration",
    "contact_point_updated": "collaboration",
    "contact_point_opted_out": "collaboration",
    "message_scheduled": "collaboration",
    "message_sent": "collaboration",
    "message_failed": "collaboration",
    "message_cancelled": "collaboration",
    "message_received": "collaboration",
    "scorecard_assigned": "evaluation",
    "scorecard_submitted": "evaluation",
    "hiring_decision_created": "evaluation",
    "hiring_decision_updated": "evaluation",
    "hiring_decision_voted": "evaluation",
    "hiring_decision_finalized": "evaluation",
    "interview_scheduling_link_created": "collaboration",
    "interview_scheduling_link_revoked": "collaboration",
    "interview_scheduled": "collaboration",
    "interview_rescheduled": "collaboration",
    "interview_cancelled": "collaboration",
    "interview_reminder_sent": "collaboration",
    "talent_pool_added": "collaboration",
    "talent_pool_removed": "collaboration",
    "nurture_started": "collaboration",
    "workflow_completed": "collaboration",
    "workflow_failed": "collaboration",
    "offer_created": "pipeline",
    "offer_updated": "collaboration",
    "offer_approval_voted": "collaboration",
    "offer_status_changed": "pipeline",
    "offer_sent": "collaboration",
    "offer_accepted": "pipeline",
    "offer_declined": "pipeline",
    "handoff_updated": "collaboration",
}

CATEGORY_EVENTS: dict[str, set[str]] = {
    category: {
        event for event, mapped in EVENT_CATEGORIES.items() if mapped == category
    }
    for category in ("pipeline", "evaluation", "collaboration")
}

_CONTACT_PAYLOAD_KEYS = frozenset(
    {
        "applicant_email",
        "applicant_name",
        "applicant_phone",
        "contact_email",
        "contact_name",
        "contact_phone",
        "email",
        "email_normalized",
        "phone",
        "phone_normalized",
        "source_ref",
    }
)
_DOCUMENT_PAYLOAD_KEYS = frozenset(
    {
        "cv",
        "cv_text",
        "document",
        "document_name",
        "filename",
        "github_data",
        "github_fetched_at",
        "github_login",
        "parsed_cv",
        "raw_cv",
    }
)


def _redact_payload_keys(value: Any, forbidden: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_payload_keys(child, forbidden)
            for key, child in value.items()
            if str(key).lower() not in forbidden
        }
    if isinstance(value, list):
        return [_redact_payload_keys(child, forbidden) for child in value]
    return value


def encode_activity_cursor(
    occurred_at: datetime, activity_id: UUID, *, scope: str
) -> str:
    return encode_collection_cursor(
        "candidate-activities.timeline",
        scope=scope,
        boundary={"occurred_at": occurred_at.isoformat(), "id": str(activity_id)},
    )


def decode_activity_cursor(cursor: str, *, scope: str) -> tuple[datetime, UUID]:
    payload = decode_collection_cursor(
        cursor,
        namespace="candidate-activities.timeline",
        scope=scope,
        boundary_keys={"occurred_at", "id"},
        detail="Invalid activity cursor",
    )
    return (
        cursor_datetime(payload, "occurred_at", detail="Invalid activity cursor"),
        cursor_uuid(payload, detail="Invalid activity cursor"),
    )


@router.get("/{candidate_id}/activities", response_model=CandidateActivityPage)
async def list_candidate_activities(
    candidate_id: UUID,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    category: Literal["all", "pipeline", "evaluation", "collaboration"] = "all",
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateActivityPage:
    assessment_results_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    document_visible = await can_access_field(
        db, user, FieldClass.CANDIDATE_DOCUMENT
    )
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    history_ids = await candidate_history_ids(db, candidate.id, user.organization_id)
    cursor_scope = collection_cursor_scope(
        "candidate-activities.timeline",
        organization_id=user.organization_id,
        filters={
            "candidate_id": str(candidate.id),
            "category": category,
            "history_ids": ",".join(sorted(str(item) for item in history_ids)),
        },
    )

    stmt = (
        select(CandidateActivity, User, Position)
        .outerjoin(User, User.id == CandidateActivity.actor_user_id)
        .outerjoin(Position, Position.id == CandidateActivity.position_id)
        .where(
            CandidateActivity.candidate_id.in_(history_ids),
            CandidateActivity.organization_id == user.organization_id,
        )
    )
    if category != "all":
        stmt = stmt.where(CandidateActivity.event_type.in_(CATEGORY_EVENTS[category]))
    if cursor:
        cursor_time, cursor_id = decode_activity_cursor(cursor, scope=cursor_scope)
        stmt = stmt.where(
            or_(
                CandidateActivity.occurred_at < cursor_time,
                and_(
                    CandidateActivity.occurred_at == cursor_time,
                    CandidateActivity.id < cursor_id,
                ),
            )
        )

    rows = (
        await db.execute(
            stmt.order_by(
                CandidateActivity.occurred_at.desc(),
                CandidateActivity.id.desc(),
            ).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    items: list[CandidateActivityOut] = []
    for activity, actor, position in page_rows:
        mapped_category = EVENT_CATEGORIES.get(activity.event_type, "evaluation")
        activity_payload: Any = activity.payload or {}
        if mapped_category == "evaluation" and not assessment_results_visible:
            # Evaluation event payloads are unstructured and have historically
            # carried score, recommendation, evidence, vote, and status values.
            # Preserve the audit-safe occurrence while withholding the entire
            # result-bearing payload rather than relying on an incomplete key list.
            activity_payload = {}
        else:
            forbidden = set()
            if not contact_visible:
                forbidden.update(_CONTACT_PAYLOAD_KEYS)
            if not document_visible or not contact_visible:
                forbidden.update(_DOCUMENT_PAYLOAD_KEYS)
            if forbidden:
                activity_payload = _redact_payload_keys(
                    activity_payload, frozenset(forbidden)
                )
        items.append(
            CandidateActivityOut(
                id=activity.id,
                application_id=activity.application_id,
                event_type=activity.event_type,
                category=mapped_category,
                actor=(
                    ActivityActorOut(id=actor.id, email=actor.email) if actor else None
                ),
                actor_kind=activity.actor_kind,
                agent_run_id=activity.agent_run_id,
                position=(
                    ActivityPositionOut(id=position.id, title=position.title)
                    if position
                    else None
                ),
                subject_type=activity.subject_type,
                subject_id=activity.subject_id,
                payload=activity_payload,
                occurred_at=activity.occurred_at,
            )
        )

    next_cursor = None
    if has_more and page_rows:
        last_activity = page_rows[-1][0]
        next_cursor = encode_activity_cursor(
            last_activity.occurred_at.astimezone(UTC),
            last_activity.id,
            scope=cursor_scope,
        )
    return CandidateActivityPage(items=items, next_cursor=next_cursor)
