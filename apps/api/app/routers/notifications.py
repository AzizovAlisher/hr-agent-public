from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.db import get_session
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notifications import NotificationListOut, NotificationOut
from app.services.enterprise import can_access_field
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
    dependencies=[Depends(current_user)],
)

_CONTACT_NOTIFICATION_KEYS = frozenset(
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


def _redact_contact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_contact_payload(child)
            for key, child in value.items()
            if str(key).lower() not in _CONTACT_NOTIFICATION_KEYS
        }
    if isinstance(value, list):
        return [_redact_contact_payload(child) for child in value]
    return value


def _notification_out(notification: Notification, *, contact_visible: bool) -> NotificationOut:
    output = NotificationOut.model_validate(notification)
    if contact_visible or output.payload is None:
        return output
    return output.model_copy(update={"payload": _redact_contact_payload(output.payload)})


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> NotificationListOut:
    """Latest notifications for the caller, plus the unread count.

    The frontend uses this single call for both the bell badge (unread_count)
    and the dropdown content (items). 20 most recent items is plenty for an
    in-app feed; older ones quietly age out of the popover.
    """
    namespace = "notifications.list"
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"user_id": str(user.id)},
    )
    stmt = select(Notification).where(Notification.user_id == user.id)
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid notifications cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid notifications cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid notifications cursor")
        stmt = stmt.where(
            or_(
                Notification.created_at < cursor_time,
                and_(Notification.created_at == cursor_time, Notification.id < cursor_id),
            )
        )
    items = (
        await db.execute(
            stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(
                limit + 1
            )
        )
    ).scalars().all()
    has_more = len(items) > limit
    items = items[:limit]
    unread_count = (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()
    return NotificationListOut(
        items=[
            _notification_out(item, contact_visible=contact_visible) for item in items
        ],
        unread_count=int(unread_count),
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": items[-1].created_at.astimezone(timezone.utc).isoformat(),
                    "id": str(items[-1].id),
                },
            )
            if has_more and items
            else None
        ),
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> NotificationOut:
    n = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(n)
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    return _notification_out(n, contact_visible=contact_visible)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await db.execute(
        update(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    await db.commit()
