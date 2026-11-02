import json
import logging
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.db import SessionLocal, get_session, set_tenant_context
from app.models.candidate import Candidate
from app.models.chat_session import ChatSession
from app.models.folder import Folder
from app.models.message import Message
from app.models.position import Position
from app.models.user import User
from app.schemas.chat import (
    ChatCapabilityOut,
    ChatMessageOut,
    ChatMessageRequest,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionListItem,
    ChatSessionOut,
    ChatStreamEvent,
)
from app.schemas.pagination import CursorPage
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.chat import (
    ChatIdempotencyConflict,
    post_user_message_and_complete,
    stream_assistant_reply,
)
from app.services.enterprise import organization_llm_capability
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.structured_hiring import resolve_existing_active_application

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(current_user)])
logger = logging.getLogger(__name__)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _load_owned_session(
    session_id: UUID, user: User, db: AsyncSession
) -> ChatSession:
    # Scope by org_id AND user_id. Org_id is technically redundant here (a
    # user belongs to exactly one org in v1) but it's defense in depth: if
    # multi-org-per-user lands later, this query stays correct.
    s = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.user_id == user.id,
                ChatSession.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat session not found")
    if s.candidate_id is not None:
        candidate = await resolve_canonical_candidate(
            db, s.candidate_id, user.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat session not found")
        s.candidate_id = candidate.id
    return s


@router.get("/capability", response_model=ChatCapabilityOut)
async def get_chat_capability(
    position_id: UUID | None = Query(None),
    candidate_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ChatCapabilityOut:
    """Return provider-free readiness for the requested hiring context."""

    if candidate_id is not None:
        candidate = await resolve_canonical_candidate(
            db, candidate_id, user.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    if position_id is not None:
        position = (
            await db.execute(
                select(Position.id).where(
                    Position.id == position_id,
                    Position.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if position is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    return ChatCapabilityOut(
        **await organization_llm_capability(db, user.organization_id)
    )


@router.get("/sessions", response_model=CursorPage[ChatSessionListItem])
async def list_chat_sessions(
    scope: Literal["candidate", "position", "free"] | None = Query(None),
    position_id: UUID | None = Query(None),
    candidate_id: UUID | None = Query(None),
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[ChatSessionListItem]:
    """List the current user's chat sessions, optionally filtered. Returns
    lightweight previews (first user message + message count + linked entity)."""
    q = select(ChatSession).where(
        ChatSession.user_id == user.id,
        ChatSession.organization_id == user.organization_id,
    )
    if scope:
        q = q.where(ChatSession.scope == scope)
    if position_id:
        q = q.where(ChatSession.position_id == position_id)
    if candidate_id:
        candidate = await resolve_canonical_candidate(
            db, candidate_id, user.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
        q = q.where(ChatSession.candidate_id == candidate.id)
        candidate_id = candidate.id

    namespace = "chat.sessions"
    scope_claim = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "user_id": str(user.id),
            "scope": scope,
            "position_id": str(position_id) if position_id else None,
            "candidate_id": str(candidate_id) if candidate_id else None,
        },
    )

    # Hide conversations tied to an archived-folder position so retired roles
    # don't surface in the list (candidate/free sessions are unaffected).
    archived_pos = (
        select(Position.id)
        .outerjoin(Folder, Folder.id == Position.folder_id)
        .where(
            Position.organization_id == user.organization_id,
            or_(
                Folder.is_archived.is_(True),
                Position.folder.like("\\_%", escape="\\"),
            ),
        )
    )
    q = q.where(
        or_(
            ChatSession.position_id.is_(None),
            ChatSession.position_id.not_in(archived_pos),
        )
    )

    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope_claim,
            boundary_keys={"updated_at", "id"},
            detail="Invalid chat sessions cursor",
        )
        cursor_time = cursor_datetime(
            payload, "updated_at", detail="Invalid chat sessions cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid chat sessions cursor")
        q = q.where(
            or_(
                ChatSession.updated_at < cursor_time,
                and_(ChatSession.updated_at == cursor_time, ChatSession.id < cursor_id),
            )
        )

    sessions = (
        (
            await db.execute(
                q.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc()).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    has_more = len(sessions) > limit
    sessions = sessions[:limit]
    if not sessions:
        return CursorPage(items=[], next_cursor=None)
    sids = [s.id for s in sessions]

    # Bulk-load message counts + last-activity timestamps per session.
    msg_agg = (
        await db.execute(
            select(
                Message.session_id,
                func.count(Message.id).label("cnt"),
                func.max(Message.created_at).label("last"),
            )
            .where(Message.session_id.in_(sids), Message.purpose == "chat")
            .group_by(Message.session_id)
        )
    ).all()
    counts: dict[UUID, int] = {sid: cnt for sid, cnt, _ in msg_agg}
    last_act: dict[UUID, object] = {sid: last for sid, _, last in msg_agg}

    # First user message per session — for the preview.
    first_user = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id.in_(sids),
                    Message.purpose == "chat",
                    Message.role == "user",
                )
                .order_by(Message.session_id, Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    preview: dict[UUID, str] = {}
    for msg in first_user:
        if msg.session_id not in preview:
            preview[msg.session_id] = msg.content[:140]

    # Linked entity titles, fetched in bulk (one query each, max 2 queries).
    pos_ids = [s.position_id for s in sessions if s.position_id]
    cand_ids = [s.candidate_id for s in sessions if s.candidate_id]
    pos_titles: dict[UUID, str] = {}
    cand_info: dict[UUID, tuple[str | None, str]] = {}
    if pos_ids:
        rows = (
            await db.execute(
                select(Position.id, Position.title).where(
                    Position.id.in_(pos_ids),
                    Position.organization_id == user.organization_id,
                )
            )
        ).all()
        pos_titles = {pid: title for pid, title in rows}
    if cand_ids:
        rows = (
            await db.execute(
                select(Candidate.id, Candidate.source_ref, Candidate.source).where(
                    Candidate.id.in_(cand_ids),
                    Candidate.organization_id == user.organization_id,
                    Candidate.merged_at.is_(None),
                )
            )
        ).all()
        cand_info = {cid: (ref, src) for cid, ref, src in rows}

    out: list[ChatSessionListItem] = []
    for s in sessions:
        ref, src = (
            cand_info.get(s.candidate_id, (None, None))
            if s.candidate_id
            else (
                None,
                None,
            )
        )
        out.append(
            ChatSessionListItem(
                id=s.id,
                scope=s.scope,
                candidate_id=s.candidate_id,
                position_id=s.position_id,
                application_id=s.application_id,
                title=s.title,
                created_at=s.created_at,
                updated_at=s.updated_at,
                message_count=counts.get(s.id, 0),
                preview=preview.get(s.id),
                last_activity=last_act.get(s.id),  # type: ignore[arg-type]
                candidate_ref=ref,
                candidate_source=src,
                position_title=pos_titles.get(s.position_id) if s.position_id else None,
            )
        )
    return CursorPage(
        items=out,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope_claim,
                boundary={
                    "updated_at": sessions[-1].updated_at.astimezone(UTC).isoformat(),
                    "id": str(sessions[-1].id),
                },
            )
            if has_more
            else None
        ),
    )


@router.post(
    "/sessions", response_model=ChatSessionOut, status_code=status.HTTP_201_CREATED
)
async def create_session(
    body: ChatSessionCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ChatSessionOut:
    # Cross-org references are silently bounced as 404 — never reveal that an
    # entity exists in another org.
    canonical_candidate_id: UUID | None = None
    if body.candidate_id is not None:
        candidate = await resolve_canonical_candidate(
            db, body.candidate_id, user.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
        canonical_candidate_id = candidate.id
    if body.position_id is not None:
        ok = (
            await db.execute(
                select(Position.id).where(
                    Position.id == body.position_id,
                    Position.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if ok is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    if user.role == "interviewer" or (
        user.role == "hiring_manager"
        and (canonical_candidate_id is None or body.position_id is None)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")

    application_id: UUID | None = None
    if canonical_candidate_id is not None and body.position_id is not None:
        application = await resolve_existing_active_application(
            db,
            organization_id=user.organization_id,
            candidate_id=canonical_candidate_id,
            position_id=body.position_id,
            application_id=body.application_id,
        )
        application_id = application.id

    s = ChatSession(
        organization_id=user.organization_id,
        user_id=user.id,
        scope=body.scope,
        candidate_id=canonical_candidate_id,
        position_id=body.position_id,
        application_id=application_id,
        title=body.title,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return ChatSessionOut.model_validate(s)


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_session_detail(
    session_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ChatSessionDetail:
    s = await _load_owned_session(session_id, user, db)
    msgs = (
        (
            await db.execute(
                select(Message)
                .where(Message.session_id == s.id, Message.purpose == "chat")
                .order_by(Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return ChatSessionDetail(
        **ChatSessionOut.model_validate(s).model_dump(),
        messages=[ChatMessageOut.model_validate(m) for m in msgs],
    )


@router.post("/sessions/{session_id}/messages", response_model=ChatMessageOut)
async def post_message(
    session_id: UUID,
    body: ChatMessageRequest,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=160),
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ChatMessageOut:
    s = await _load_owned_session(session_id, user, db)
    try:
        assistant = await post_user_message_and_complete(
            db=db,
            session=s,
            content=body.content,
            idempotency_key=idempotency_key,
        )
    except ChatIdempotencyConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": exc.code,
                "detail": "Idempotency-Key was already used for different chat content",
            },
        ) from exc
    return ChatMessageOut.model_validate(assistant)


@router.post(
    "/sessions/{session_id}/messages/stream",
    response_model=ChatStreamEvent,
    response_class=StreamingResponse,
    responses={
        status.HTTP_200_OK: {
            "description": "Typed chat event stream",
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/ChatStreamEvent"}
                }
            },
        }
    },
)
async def post_message_stream(
    session_id: UUID,
    body: ChatMessageRequest,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=160),
    ] = None,
    user: User = Depends(current_user),
) -> StreamingResponse:
    """Stream typed JSON SSE events: ``delta``, ``usage``, ``done``, or ``error``.

    Uses its own DB session managed by the generator so the connection lives
    for the duration of the stream.
    """
    content = body.content
    turn_key = idempotency_key or uuid4().hex

    async def event_stream():
        async with SessionLocal() as db:
            try:
                await set_tenant_context(
                    db,
                    organization_id=user.organization_id,
                    actor_id=user.id,
                    identity_kind="user",
                    role=user.role,
                )
                s = await _load_owned_session(session_id, user, db)
                async for event in stream_assistant_reply(
                    db=db,
                    session=s,
                    content=content,
                    idempotency_key=turn_key,
                ):
                    if event.type == "delta":
                        yield _sse("delta", {"content": event.content})
                    elif event.type == "usage":
                        yield _sse(
                            "usage",
                            {
                                "model_used": event.model_used,
                                "tokens_in": event.tokens_in,
                                "tokens_out": event.tokens_out,
                                "provider_used": event.provider_used,
                                "cost_microusd": event.cost_microusd,
                            },
                        )
                    elif event.type == "done":
                        yield _sse(
                            "done",
                            {"message_id": str(event.message_id)},
                        )
            except ChatIdempotencyConflict:
                yield _sse(
                    "error",
                    {
                        "code": "idempotency_conflict",
                        "message": "This retry key belongs to a different message.",
                    },
                )
            except Exception:
                logger.exception(
                    "Chat streaming failed", extra={"session_id": str(session_id)}
                )
                yield _sse(
                    "error",
                    {
                        "code": "chat_provider_error",
                        "message": "The assistant is temporarily unavailable. Retry this message.",
                    },
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
            "Connection": "keep-alive",
        },
    )
