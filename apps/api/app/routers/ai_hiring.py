"""Versioned API for the separate AI Hiring Assistant workspace."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.db import get_session as db_session
from app.models.user import User
from app.schemas.ai_hiring import (
    AiActionBundleDecision,
    AiActionBundleOut,
    AiActionBundlePage,
    AiAttachmentUploadOut,
    AiMessageCreate,
    AiOrganizationSettingsOut,
    AiOrganizationSettingsUpdate,
    AiSessionCreate,
    AiSessionDetailOut,
    AiSessionOut,
    AiSessionPage,
    AiStreamEvent,
    HiringMissionOut,
    HiringMissionPage,
    MissionCandidateAssessmentPage,
    MissionDetailOut,
    MissionEventPage,
    MissionReplanRequest,
)
from app.services.ai_hiring_attachments import (
    delete_ai_attachment,
    download_ai_attachment,
    import_ai_attachments,
    public_attachment_metadata,
    resolve_attachment_references,
    save_ai_attachment,
)
from app.services.ai_hiring_runtime import (
    archive_session,
    create_session,
    decide_bundle,
    get_ai_settings,
    get_mission,
    list_bundles,
    list_candidates,
    list_events,
    list_missions,
    list_sessions,
    put_ai_settings,
    replan_mission,
    require_ai_enabled,
    retry_mission,
    session_detail,
    stop_mission,
    submit_message,
)

router = APIRouter(prefix="/ai/v1", tags=["ai-hiring"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/settings", response_model=AiOrganizationSettingsOut)
async def read_ai_settings(
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    # Administrators must be able to enable the workspace while it is disabled.
    return await get_ai_settings(db, user)


@router.put("/settings", response_model=AiOrganizationSettingsOut)
async def update_ai_settings(
    body: AiOrganizationSettingsUpdate,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await put_ai_settings(
        db, user, body.model_dump(exclude_unset=True)
    )


@router.get("/sessions", response_model=AiSessionPage)
async def get_sessions(
    session_status: str | None = Query(default=None, alias="status"),
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    if session_status not in {None, "active", "archived"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid session status")
    return await list_sessions(
        db,
        user,
        session_status=session_status,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/sessions",
    response_model=AiSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_session(
    body: AiSessionCreate,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await create_session(db, user, title=body.title)


@router.get("/sessions/{session_id}", response_model=AiSessionDetailOut)
async def get_session(
    session_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await session_detail(db, user, session_id)


@router.post("/sessions/{session_id}/archive", response_model=AiSessionOut)
async def post_archive_session(
    session_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await archive_session(db, user, session_id)


@router.post(
    "/sessions/{session_id}/attachments",
    response_model=AiAttachmentUploadOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_attachment(
    session_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    await require_ai_enabled(db, user)
    return await save_ai_attachment(
        db,
        user,
        session_id,
        upload=file,
    )


@router.get("/sessions/{session_id}/attachments/{attachment_id}")
async def get_attachment(
    session_id: UUID,
    attachment_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> StreamingResponse:
    await require_ai_enabled(db, user)
    return await download_ai_attachment(
        db,
        user,
        session_id=session_id,
        attachment_id=attachment_id,
    )


@router.delete(
    "/sessions/{session_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_attachment(
    session_id: UUID,
    attachment_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> None:
    await require_ai_enabled(db, user)
    await delete_ai_attachment(
        db,
        user,
        session_id=session_id,
        attachment_id=attachment_id,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=AiStreamEvent,
    response_class=StreamingResponse,
    responses={
        status.HTTP_200_OK: {
            "description": "Typed AI workspace event stream",
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/AiStreamEvent"}
                }
            },
        }
    },
)
async def post_message(
    session_id: UUID,
    body: AiMessageCreate,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> StreamingResponse:
    await require_ai_enabled(db, user)
    resolved = await resolve_attachment_references(
        db,
        user=user,
        session_id=session_id,
        references=[item.model_dump(mode="json") for item in body.attachments],
    )
    imports = await import_ai_attachments(db, user, attachments=resolved) if resolved else {}
    result = await submit_message(
        db,
        user,
        session_id,
        content=body.content,
        attachments=public_attachment_metadata(resolved),
        attachment_imports=imports,
    )

    async def events() -> AsyncIterator[str]:
        # The local fallback completes one concise assistant message at once;
        # the same contract allows token deltas when a future model streams.
        yield _sse("message.completed", {"message": result["user_message"]})
        yield _sse("message.completed", {"message": result["assistant_message"]})
        yield _sse("mission.updated", {"mission": result["mission"]})
        yield _sse(
            "canvas.updated",
            {
                "name": "mission",
                "data": {
                    "mission_id": result["mission"]["id"],
                    "plan_version": result["mission"]["current_plan_version"],
                },
            },
        )
        yield _sse(
            "completed",
            {
                "session_id": str(session_id),
                "mission_id": result["mission"]["id"],
            },
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/missions", response_model=HiringMissionPage)
async def get_missions(
    mission_status: str | None = Query(default=None, alias="status"),
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> list[dict]:
    valid = {
        None,
        "active",
        "draft",
        "planning",
        "running",
        "awaiting_approval",
        "completed",
        "stopped",
        "failed",
    }
    if mission_status not in valid:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid mission status")
    return await list_missions(
        db,
        user,
        mission_status=mission_status,
        limit=limit,
        cursor=cursor,
    )


@router.get("/missions/{mission_id}", response_model=MissionDetailOut)
async def get_mission_detail(
    mission_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await get_mission(db, user, mission_id)


@router.post("/missions/{mission_id}/stop", response_model=HiringMissionOut)
async def post_stop_mission(
    mission_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await stop_mission(db, user, mission_id)


@router.post("/missions/{mission_id}/retry", response_model=HiringMissionOut)
async def post_retry_mission(
    mission_id: UUID,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await retry_mission(db, user, mission_id)


@router.post("/missions/{mission_id}/replan", response_model=HiringMissionOut)
async def post_replan_mission(
    mission_id: UUID,
    body: MissionReplanRequest | None = None,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await replan_mission(
        db, user, mission_id, request=body.request if body else None
    )


@router.get(
    "/missions/{mission_id}/candidates",
    response_model=MissionCandidateAssessmentPage,
)
async def get_mission_candidates(
    mission_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await list_candidates(
        db, user, mission_id, limit=limit, cursor=cursor
    )


@router.get("/missions/{mission_id}/events", response_model=MissionEventPage)
async def get_mission_events(
    mission_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await list_events(db, user, mission_id, limit=limit, cursor=cursor)


@router.get("/action-bundles", response_model=AiActionBundlePage)
async def get_action_bundles(
    mission_id: UUID | None = None,
    bundle_status: str | None = Query(default=None, alias="status"),
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    if bundle_status not in {
        None,
        "pending",
        "approved",
        "rejected",
        "executed",
        "failed",
        "stale",
    }:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid bundle status")
    return await list_bundles(
        db,
        user,
        mission_id=mission_id,
        bundle_status=bundle_status,
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/action-bundles/{bundle_id}/approve",
    response_model=AiActionBundleOut,
)
async def post_approve_bundle(
    bundle_id: UUID,
    body: AiActionBundleDecision | None = None,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await decide_bundle(
        db,
        user,
        bundle_id,
        decision="approve",
        idempotency_key=idempotency_key,
        comment=body.comment if body else None,
    )


@router.post(
    "/action-bundles/{bundle_id}/reject",
    response_model=AiActionBundleOut,
)
async def post_reject_bundle(
    bundle_id: UUID,
    body: AiActionBundleDecision | None = None,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(db_session),
    user: User = Depends(current_user),
) -> dict:
    return await decide_bundle(
        db,
        user,
        bundle_id,
        decision="reject",
        idempotency_key=idempotency_key,
        comment=body.comment if body else None,
    )
