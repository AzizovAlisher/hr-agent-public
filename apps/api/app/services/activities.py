"""Helpers for recording candidate activity in the caller's transaction."""

from datetime import datetime
import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate_activity import CandidateActivity


# Activity payloads are rendered in durable history. Keep sensitive or large
# source material in its owning table, never in this append-only stream.
_FORBIDDEN_PAYLOAD_KEYS = {
    "body",
    "contact",
    "contacts",
    "content",
    "cv",
    "email",
    "message",
    "messages",
    "phone",
    "prompt",
    "raw_cv",
    "resume",
    "task_content",
    "text",
    "transcript",
}


def _assert_safe_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            key_parts = set(filter(None, re.split(r"[^a-z0-9]+", normalized)))
            if (
                normalized in _FORBIDDEN_PAYLOAD_KEYS
                or key_parts & _FORBIDDEN_PAYLOAD_KEYS
            ):
                raise ValueError(f"Sensitive activity payload key is not allowed: {path}.{key}")
            _assert_safe_payload(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_safe_payload(child, f"{path}[{index}]")


def append_candidate_activity(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_id: UUID,
    event_type: str,
    actor_user_id: UUID | None = None,
    actor_kind: str | None = None,
    agent_run_id: UUID | None = None,
    application_id: UUID | None = None,
    position_id: UUID | None = None,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> CandidateActivity:
    """Add an activity without flushing or committing.

    The owning router/service controls the transaction so an activity can never
    commit independently from the action it describes.
    """

    safe_payload = payload or {}
    _assert_safe_payload(safe_payload)
    activity = CandidateActivity(
        organization_id=organization_id,
        candidate_id=candidate_id,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind or ("human" if actor_user_id is not None else "system"),
        agent_run_id=agent_run_id,
        application_id=application_id,
        position_id=position_id,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=safe_payload,
    )
    if occurred_at is not None:
        activity.occurred_at = occurred_at
    db.add(activity)
    return activity
