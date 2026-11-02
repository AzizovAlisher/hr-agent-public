"""Technical-task submissions.

A recruiter records a task (brief) + the candidate's submission (repo link or
pasted content); POST .../evaluate runs the AI absorption pass that scores the
work and the result folds into the candidate's AI evidence.
"""

from datetime import UTC, datetime
from pathlib import PurePath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import user_with_permissions
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog
from app.models.position import Position
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.schemas.enrichment import TaskCreate, TaskOut, TaskUpdate
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.enterprise import require_field_access
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.structured_hiring import (
    resolve_application_for_write,
    resolve_linked_application,
)
from app.services.task_submission import TaskEvalError, evaluate_task_submission
from app.services.uploads import (
    cleanup_promoted_upload_after_db_failure,
    enqueue_or_delete_private_object,
    private_blob_response,
    resolve_resource_private_blob_reference,
    safe_download_filename,
    store_private_upload,
    validated_private_media_type,
)
from app.storage.registry import (
    enqueue_private_object_deletion,
    register_ready_private_object,
)

router = APIRouter(tags=["tasks"])

candidate_reader = user_with_permissions("candidates.read")
candidate_writer = user_with_permissions("candidates.write")
candidate_task_evaluator = user_with_permissions("candidates.write", "ai.use")
MAX_TASK_FILE_BYTES = 15 * 1024 * 1024
TASK_FILE_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".csv"}


async def _task_ai_evaluation_unavailable() -> None:
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "code": "task_ai_evaluation_unavailable",
            "detail": (
                "AI task evaluation is unavailable in the pilot until it runs "
                "as a durable, retryable job."
            ),
        },
    )


async def _ensure_candidate(
    candidate_id: UUID, user: User, db: AsyncSession
) -> Candidate:
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return c


async def _validate_position(
    position_id: UUID,
    user: User,
    db: AsyncSession,
    *,
    candidate_id: UUID | None = None,
) -> None:
    p = (
        await db.execute(
            select(Position.id).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Position not found")
    if user.role in {"hiring_manager", "interviewer"} and candidate_id is not None:
        assigned_pair = (
            await db.execute(
                select(Application.id).where(
                    Application.candidate_id == candidate_id,
                    Application.position_id == position_id,
                )
            )
        ).scalar_one_or_none()
        if assigned_pair is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")


async def _position_title(position_id: UUID | None, db: AsyncSession) -> str | None:
    if position_id is None:
        return None
    return (
        await db.execute(select(Position.title).where(Position.id == position_id))
    ).scalar_one_or_none()


async def _get_task(
    task_id: UUID,
    user: User,
    db: AsyncSession,
    *,
    lock: bool = False,
) -> TaskSubmission:
    statement = select(TaskSubmission).where(
        TaskSubmission.id == task_id,
        TaskSubmission.organization_id == user.organization_id,
    )
    if lock:
        statement = statement.with_for_update()
    t = (await db.execute(statement)).scalar_one_or_none()
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task submission not found")
    candidate = await _ensure_candidate(t.candidate_id, user, db)
    t.candidate_id = candidate.id
    return t


@router.get("/candidates/{candidate_id}/tasks", response_model=CursorPage[TaskOut])
async def list_tasks(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[TaskOut]:
    candidate = await _ensure_candidate(candidate_id, user, db)
    namespace = "candidates.tasks"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(TaskSubmission).where(
        TaskSubmission.candidate_id == candidate.id,
        TaskSubmission.organization_id == user.organization_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid tasks cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid tasks cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid tasks cursor")
        stmt = stmt.where(
            or_(
                TaskSubmission.created_at < cursor_time,
                and_(TaskSubmission.created_at == cursor_time, TaskSubmission.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(TaskSubmission.created_at.desc(), TaskSubmission.id.desc()).limit(
                limit + 1
            )
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = list(rows[:limit])
    return CursorPage(
        items=[TaskOut.model_validate(row) for row in rows],
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


@router.post(
    "/candidates/{candidate_id}/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(
    candidate_id: UUID,
    body: TaskCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> TaskSubmission:
    candidate = await _ensure_candidate(candidate_id, user, db)
    if user.role == "interviewer" or (
        user.role == "hiring_manager" and body.position_id is None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    if body.position_id is not None:
        await _validate_position(
            body.position_id,
            user,
            db,
            candidate_id=candidate.id,
        )
        application = await resolve_application_for_write(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            position_id=body.position_id,
            application_id=body.application_id,
            source=candidate.source or "task",
            actor_user_id=user.id,
            actor_kind="human",
            reason="task_created",
        )
    else:
        if body.application_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "application_id requires position_id",
            )
        application = None
    # If the recruiter already pasted a submission / repo link, mark it
    # submitted; otherwise it's just the task we sent.
    has_submission = bool(body.repo_url or body.content)
    task = TaskSubmission(
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        position_id=body.position_id,
        application_id=application.id if application is not None else None,
        created_by=user.id,
        title=body.title.strip(),
        prompt=body.prompt,
        submission_type=body.submission_type,
        repo_url=body.repo_url,
        content=body.content,
        status="submitted" if has_submission else "sent",
    )
    db.add(task)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        application_id=task.application_id,
        position_id=task.position_id,
        event_type="task_added",
        subject_type="task",
        subject_id=task.id,
        payload={
            "title": task.title,
            "status": task.status,
            "submission_type": task.submission_type,
            "position_title": await _position_title(task.position_id, db),
        },
    )
    await db.commit()
    await db.refresh(task)
    return task


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> TaskSubmission:
    return await _get_task(task_id, user, db)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: UUID,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> TaskSubmission:
    task = await _get_task(task_id, user, db)
    old_status = task.status
    data = body.model_dump(exclude_unset=True)
    if (
        user.role in {"hiring_manager", "interviewer"}
        and "position_id" in data
        and (user.role == "interviewer" or data["position_id"] is None)
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    if data.get("position_id") is not None:
        await _validate_position(
            data["position_id"],
            user,
            db,
            candidate_id=task.candidate_id,
        )
    pair_change = bool({"position_id", "application_id"} & body.model_fields_set)
    target_position_id = data.get("position_id", task.position_id)
    target_application_id = data.get("application_id", task.application_id)
    if data.get("position_id") is None and "position_id" in body.model_fields_set:
        target_application_id = data.get("application_id")
    if task.application_id is not None and (
        target_application_id != task.application_id
        or target_position_id != task.position_id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Application-bound task evidence cannot be retargeted",
        )
    if target_position_id is None:
        if target_application_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "application_id requires position_id",
            )
        application = None
    elif task.application_id is not None:
        application = await resolve_linked_application(
            db,
            organization_id=user.organization_id,
            application_id=task.application_id,
            candidate_id=task.candidate_id,
            position_id=target_position_id,
            lock=True,
        )
    elif pair_change:
        application = await resolve_application_for_write(
            db,
            organization_id=user.organization_id,
            candidate_id=task.candidate_id,
            position_id=target_position_id,
            application_id=target_application_id,
            source="task",
            actor_user_id=user.id,
            actor_kind="human",
            reason="task_retargeted",
        )
    elif task.application_id is None:
        application = await resolve_linked_application(
            db,
            organization_id=user.organization_id,
            application_id=None,
            candidate_id=task.candidate_id,
            position_id=target_position_id,
            lock=True,
        )
    else:
        application = None
    data.pop("application_id", None)
    for key, value in data.items():
        setattr(task, key, value)
    task.application_id = application.id if application is not None else None
    if task.status != old_status:
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=task.candidate_id,
            actor_user_id=user.id,
            application_id=task.application_id,
            position_id=task.position_id,
            event_type="task_status_changed",
            subject_type="task",
            subject_id=task.id,
            payload={
                "title": task.title,
                "from_status": old_status,
                "to_status": task.status,
                "position_title": await _position_title(task.position_id, db),
            },
        )
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/tasks/{task_id}/evaluate", response_model=TaskOut)
async def evaluate_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_task_evaluator),
    _capability: None = Depends(_task_ai_evaluation_unavailable),
) -> TaskSubmission:
    task = await _get_task(task_id, user, db)
    try:
        task = await evaluate_task_submission(
            submission=task,
            db=db,
            organization_id=user.organization_id,
            actor_user_id=user.id,
            locale=user.locale,
        )
    except TaskEvalError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return task


@router.post("/tasks/{task_id}/file", response_model=TaskOut)
async def upload_task_file(
    task_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> TaskSubmission:
    """Replace a task attachment only after quarantine and security scanning."""

    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    await require_field_access(db, user, FieldClass.CONTACT)
    task = await _get_task(task_id, user, db, lock=True)
    organization_id = user.organization_id
    stored = await store_private_upload(
        file,
        organization_id=organization_id,
        category="tasks",
        allowed_suffixes=TASK_FILE_SUFFIXES,
        max_bytes=MAX_TASK_FILE_BYTES,
        label="Task file",
    )
    previous = task.file_path
    previous_private_object_id = task.private_object_id
    private_object_id: UUID | None = None
    try:
        private_object = await register_ready_private_object(
            db,
            organization_id=organization_id,
            resource_type="task_submission",
            resource_id=task.id,
            stored=stored,
        )
        private_object_id = private_object.id
        task.file_path = stored.key
        task.private_object_id = private_object.id
        task.submission_type = "file"
        task.status = "submitted"
        task.submitted_at = datetime.now(UTC)
        append_candidate_activity(
            db,
            organization_id=organization_id,
            candidate_id=task.candidate_id,
            actor_user_id=user.id,
            application_id=task.application_id,
            position_id=task.position_id,
            event_type="task_file_uploaded",
            subject_type="task",
            subject_id=task.id,
            payload={"size_bytes": stored.size_bytes, "replaced": bool(previous)},
        )
        if previous_private_object_id is not None:
            await enqueue_private_object_deletion(
                db,
                organization_id=organization_id,
                private_object_id=previous_private_object_id,
                reason="replacement",
            )
        elif previous and previous != stored.key:
            await enqueue_or_delete_private_object(
                db,
                organization_id=organization_id,
                category="tasks",
                resource_type="task_submission",
                resource_id=task.id,
                private_object_id=None,
                legacy_reference=previous,
                reason="replacement",
            )
        await db.commit()
    except Exception:
        await db.rollback()
        await cleanup_promoted_upload_after_db_failure(
            stored.key,
            organization_id=organization_id,
            category="tasks",
            private_object_id=private_object_id,
        )
        raise
    await db.refresh(task)
    return task


@router.get("/tasks/{task_id}/file")
async def download_task_file(
    task_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> StreamingResponse:
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    await require_field_access(db, user, FieldClass.CONTACT)
    task = await _get_task(task_id, user, db)
    if not task.file_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task file not found")
    key = await resolve_resource_private_blob_reference(
        db,
        organization_id=user.organization_id,
        category="tasks",
        resource_type="task_submission",
        resource_id=task.id,
        private_object_id=task.private_object_id,
        legacy_reference=task.file_path,
    )
    if key is None:
        raise HTTPException(status.HTTP_410_GONE, "Task file is unavailable")
    suffix = PurePath(key).suffix.lower() or ".bin"
    media_type = validated_private_media_type(
        key,
        allowed_suffixes=TASK_FILE_SUFFIXES,
    )
    response = await private_blob_response(
        key,
        filename=safe_download_filename(
            task.title,
            fallback=str(task.id),
            suffix=suffix,
        ),
        media_type=media_type,
    )
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="download_task_file",
            resource_type="task_submission",
            resource_id=task.id,
            path_template="/tasks/{task_id}/file",
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "candidate_id": str(task.candidate_id),
                "sensitive_read": True,
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return response


@router.delete("/tasks/{task_id}/file", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_file(
    task_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> None:
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    await require_field_access(db, user, FieldClass.CONTACT)
    task = await _get_task(task_id, user, db, lock=True)
    if not task.file_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task file not found")
    await enqueue_or_delete_private_object(
        db,
        organization_id=user.organization_id,
        category="tasks",
        resource_type="task_submission",
        resource_id=task.id,
        private_object_id=task.private_object_id,
        legacy_reference=task.file_path,
        reason="resource_deleted",
    )
    task.file_path = None
    task.private_object_id = None
    task.submission_type = "repo" if task.repo_url else "text"
    task.status = "submitted" if task.repo_url or task.content else "sent"
    task.ai_score = None
    task.ai_summary = None
    task.evaluation = None
    task.model_used = None
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=task.candidate_id,
        actor_user_id=user.id,
        application_id=task.application_id,
        position_id=task.position_id,
        event_type="task_file_deleted",
        subject_type="task",
        subject_id=task.id,
        payload={},
    )
    await db.commit()


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> None:
    task = await _get_task(task_id, user, db, lock=True)
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=task.candidate_id,
        actor_user_id=user.id,
        application_id=task.application_id,
        position_id=task.position_id,
        event_type="task_deleted",
        subject_type="task",
        subject_id=task.id,
        payload={
            "title": task.title,
            "status": task.status,
            "ai_score": task.ai_score,
            "position_title": await _position_title(task.position_id, db),
        },
    )
    if task.file_path:
        await enqueue_or_delete_private_object(
            db,
            organization_id=user.organization_id,
            category="tasks",
            resource_type="task_submission",
            resource_id=task.id,
            private_object_id=task.private_object_id,
            legacy_reference=task.file_path,
            reason="resource_deleted",
        )
    await db.delete(task)
    await db.commit()
