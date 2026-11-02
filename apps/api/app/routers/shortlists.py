"""Shortlist link endpoints — recruiter-facing.

A shortlist link is a revokable, read-only public URL that exposes the top-N
candidates of a position to a hiring manager (or external stakeholder) without
giving them an org account. Tokens are random, opaque strings.

The public-side view of a shortlist lives in `routers/public.py` so all
unauthenticated traffic is colocated.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import user_with_permissions
from app.db import get_session
from app.models.position import Position
from app.models.shortlist import ShortlistLink
from app.models.user import User
from app.schemas.pagination import CursorPage
from app.services.enterprise import require_field_access
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

router = APIRouter(tags=["shortlists"])
shortlist_user = user_with_permissions("candidates.export")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> tuple[str, str]:
    token = "sl_" + secrets.token_urlsafe(32)
    return token, _token_digest(token)


# ---------- schemas ----------


class ShortlistCreate(BaseModel):
    top_n: int = Field(default=5, ge=1, le=50)
    include_contact: bool = False
    expires_in_days: int = Field(default=14, ge=1, le=90)


class ShortlistOut(BaseModel):
    id: UUID
    position_id: UUID
    top_n: int
    include_contact: bool
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class ShortlistCreatedOut(ShortlistOut):
    """One-time capability response; the raw token is never listable."""

    token: str


def _to_out(s: ShortlistLink) -> ShortlistOut:
    return ShortlistOut(
        id=s.id,
        position_id=s.position_id,
        top_n=s.top_n,
        include_contact=s.include_contact,
        created_at=s.created_at,
        expires_at=s.expires_at,
        revoked_at=s.revoked_at,
    )


# ---------- endpoints ----------


@router.post(
    "/positions/{position_id}/shortlists",
    response_model=ShortlistCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_shortlist(
    position_id: UUID,
    body: ShortlistCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(shortlist_user),
) -> ShortlistCreatedOut:
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    if body.include_contact:
        await require_field_access(db, user, FieldClass.CONTACT)
    p = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")

    token, token_digest = _new_token()
    link = ShortlistLink(
        organization_id=user.organization_id,
        position_id=position_id,
        created_by=user.id,
        token_digest=token_digest,
        top_n=body.top_n,
        include_contact=body.include_contact,
        expires_at=datetime.now(UTC) + timedelta(days=body.expires_in_days),
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return ShortlistCreatedOut(**_to_out(link).model_dump(), token=token)


@router.get(
    "/positions/{position_id}/shortlists",
    response_model=CursorPage[ShortlistOut],
)
async def list_shortlists(
    position_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(shortlist_user),
) -> CursorPage[ShortlistOut]:
    namespace = "shortlists.position"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"position_id": str(position_id)},
    )
    stmt = select(ShortlistLink).where(
        ShortlistLink.position_id == position_id,
        ShortlistLink.organization_id == user.organization_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid shortlists cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid shortlists cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid shortlists cursor")
        stmt = stmt.where(
            or_(
                ShortlistLink.created_at < cursor_time,
                and_(ShortlistLink.created_at == cursor_time, ShortlistLink.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(ShortlistLink.created_at.desc(), ShortlistLink.id.desc()).limit(
                limit + 1
            )
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[_to_out(row) for row in rows],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": rows[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(rows[-1].id),
                },
            )
            if has_more and rows
            else None
        ),
    )


@router.post("/shortlists/{shortlist_id}/revoke", response_model=ShortlistOut)
async def revoke_shortlist(
    shortlist_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(shortlist_user),
) -> ShortlistOut:
    s = (
        await db.execute(
            select(ShortlistLink).where(
                ShortlistLink.id == shortlist_id,
                ShortlistLink.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shortlist not found")
    if s.revoked_at is None:
        s.revoked_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(s)
    return _to_out(s)


@router.post(
    "/shortlists/{shortlist_id}/rotate",
    response_model=ShortlistCreatedOut,
)
async def rotate_shortlist(
    shortlist_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(shortlist_user),
) -> ShortlistCreatedOut:
    link = (
        await db.execute(
            select(ShortlistLink)
            .where(
                ShortlistLink.id == shortlist_id,
                ShortlistLink.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shortlist not found")
    if link.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Shortlist is revoked")
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    if link.include_contact:
        await require_field_access(db, user, FieldClass.CONTACT)
    token, token_digest = _new_token()
    link.token_digest = token_digest
    link.expires_at = datetime.now(UTC) + timedelta(days=14)
    await db.commit()
    await db.refresh(link)
    return ShortlistCreatedOut(**_to_out(link).model_dump(), token=token)
