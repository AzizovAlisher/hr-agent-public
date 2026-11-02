"""Candidate notes (chronological feed) and tags (free-text labels).

Both live under /candidates/{id}/... rather than top-level /notes /tags so
auth checks can validate org-scoping in one place. Notes are
author-or-admin deletable; tags are anyone-on-the-team deletable to keep
collaboration friction-free.
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import user_with_permissions
from app.db import get_session
from app.models.candidate import Candidate
from app.models.candidate_note import CandidateNote, CandidateTag
from app.models.user import User
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

router = APIRouter(
    prefix="/candidates",
    tags=["candidate-notes"],
)
candidate_reader = user_with_permissions("candidates.read")
candidate_writer = user_with_permissions("candidates.write")


# ---------- schemas ----------


class NoteCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class NoteOut(BaseModel):
    id: UUID
    candidate_id: UUID
    author_user_id: UUID | None
    author_email: str | None
    content: str
    created_at: datetime
    updated_at: datetime


class TagCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=32)


class TagOut(BaseModel):
    id: UUID
    candidate_id: UUID
    label: str
    created_by: UUID | None
    created_at: datetime


# ---------- helpers ----------


async def _ensure_candidate(
    candidate_id: UUID, user: User, db: AsyncSession
) -> Candidate:
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return c


# ---------- notes ----------


@router.get("/{candidate_id}/notes", response_model=CursorPage[NoteOut])
async def list_notes(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[NoteOut]:
    candidate = await _ensure_candidate(candidate_id, user, db)
    namespace = "candidates.notes"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = (
        select(CandidateNote, User.email)
        .outerjoin(User, User.id == CandidateNote.author_user_id)
        .where(
            CandidateNote.candidate_id == candidate.id,
            CandidateNote.organization_id == user.organization_id,
        )
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid candidate notes cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid candidate notes cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid candidate notes cursor")
        stmt = stmt.where(
            or_(
                CandidateNote.created_at < cursor_time,
                and_(CandidateNote.created_at == cursor_time, CandidateNote.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(CandidateNote.created_at.desc(), CandidateNote.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        NoteOut(
            id=n.id,
            candidate_id=n.candidate_id,
            author_user_id=n.author_user_id,
            author_email=email,
            content=n.content,
            created_at=n.created_at,
            updated_at=n.updated_at,
        )
        for n, email in rows
    ]
    last = rows[-1][0] if rows else None
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"created_at": last.created_at.astimezone(UTC).isoformat(), "id": str(last.id)},
            )
            if has_more and last is not None
            else None
        ),
    )


@router.post(
    "/{candidate_id}/notes",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_note(
    candidate_id: UUID,
    body: NoteCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> NoteOut:
    candidate = await _ensure_candidate(candidate_id, user, db)
    n = CandidateNote(
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        author_user_id=user.id,
        content=body.content.strip(),
    )
    db.add(n)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="note_added",
        subject_type="note",
        subject_id=n.id,
    )
    await db.commit()
    await db.refresh(n)
    return NoteOut(
        id=n.id,
        candidate_id=n.candidate_id,
        author_user_id=n.author_user_id,
        author_email=user.email,
        content=n.content,
        created_at=n.created_at,
        updated_at=n.updated_at,
    )


@router.delete(
    "/{candidate_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_note(
    candidate_id: UUID,
    note_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> None:
    candidate = await _ensure_candidate(candidate_id, user, db)
    n = (
        await db.execute(
            select(CandidateNote).where(
                CandidateNote.id == note_id,
                CandidateNote.candidate_id == candidate.id,
                CandidateNote.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    # Author or admin can delete; everyone else gets 403.
    if n.author_user_id != user.id and user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only the author or an admin can delete this note.",
        )
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="note_deleted",
        subject_type="note",
        subject_id=n.id,
    )
    await db.delete(n)
    await db.commit()


# ---------- tags ----------


@router.get("/{candidate_id}/tags", response_model=CursorPage[TagOut])
async def list_tags(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[TagOut]:
    candidate = await _ensure_candidate(candidate_id, user, db)
    namespace = "candidates.tags"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(CandidateTag).where(
        CandidateTag.candidate_id == candidate.id,
        CandidateTag.organization_id == user.organization_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid candidate tags cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid candidate tags cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid candidate tags cursor")
        stmt = stmt.where(
            or_(
                CandidateTag.created_at > cursor_time,
                and_(CandidateTag.created_at == cursor_time, CandidateTag.id > cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(CandidateTag.created_at.asc(), CandidateTag.id.asc()).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        TagOut(
            id=t.id,
            candidate_id=t.candidate_id,
            label=t.label,
            created_by=t.created_by,
            created_at=t.created_at,
        )
        for t in rows
    ]
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"created_at": rows[-1].created_at.astimezone(UTC).isoformat(), "id": str(rows[-1].id)},
            )
            if has_more and rows
            else None
        ),
    )


@router.post(
    "/{candidate_id}/tags",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_tag(
    candidate_id: UUID,
    body: TagCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> TagOut:
    candidate = await _ensure_candidate(candidate_id, user, db)
    label = body.label.strip()
    if not label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty tag label.")
    t = CandidateTag(
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        label=label,
        created_by=user.id,
    )
    db.add(t)
    try:
        await db.flush()
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            event_type="tag_added",
            subject_type="tag",
            subject_id=t.id,
            payload={"label": label},
        )
        await db.commit()
    except IntegrityError:
        # Hit the (candidate_id, label) unique constraint — surface as 409.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Candidate already has this tag.",
        )
    await db.refresh(t)
    return TagOut(
        id=t.id,
        candidate_id=t.candidate_id,
        label=t.label,
        created_by=t.created_by,
        created_at=t.created_at,
    )


@router.delete(
    "/{candidate_id}/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_tag(
    candidate_id: UUID,
    tag_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> None:
    candidate = await _ensure_candidate(candidate_id, user, db)
    t = (
        await db.execute(
            select(CandidateTag).where(
                CandidateTag.id == tag_id,
                CandidateTag.candidate_id == candidate.id,
                CandidateTag.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tag not found")
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="tag_removed",
        subject_type="tag",
        subject_id=t.id,
        payload={"label": t.label},
    )
    await db.delete(t)
    await db.commit()
