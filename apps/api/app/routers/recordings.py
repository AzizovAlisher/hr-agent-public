"""Interview recordings.

We store the recording row and expose a PATCH write-hook the external
record→diarize→transcribe→score pipeline calls to fill in transcript,
diarization, and the interview score. Whatever the pipeline writes (transcript
or interview_score) folds into the candidate's AI evidence. We do not transcribe
or score audio here.
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
from app.auth.dependencies import current_user
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog
from app.models.interview_recording import InterviewRecording
from app.models.position import Position
from app.models.user import User
from app.schemas.enrichment import RecordingCreate, RecordingOut, RecordingUpdate
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
from app.services.uploads import (
    cleanup_promoted_upload_after_db_failure,
    enqueue_or_delete_private_object,
    private_blob_response,
    resolve_resource_private_blob_reference,
    safe_download_filename,
    store_private_upload,
    validated_private_media_type,
)
from app.storage import (
    enqueue_private_object_deletion,
    register_ready_private_object,
)

router = APIRouter(tags=["recordings"])

MAX_RECORDING_BYTES = 15 * 1024 * 1024
RECORDING_FILE_SUFFIXES = {".m4a", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}


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


async def _get_recording(
    recording_id: UUID,
    user: User,
    db: AsyncSession,
    *,
    lock: bool = False,
) -> InterviewRecording:
    statement = select(InterviewRecording).where(
        InterviewRecording.id == recording_id,
        InterviewRecording.organization_id == user.organization_id,
    )
    if lock:
        statement = statement.with_for_update()
    r = (await db.execute(statement)).scalar_one_or_none()
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    candidate = await _ensure_candidate(r.candidate_id, user, db)
    r.candidate_id = candidate.id
    return r


@router.get(
    "/candidates/{candidate_id}/recordings",
    response_model=CursorPage[RecordingOut],
)
async def list_recordings(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[RecordingOut]:
    candidate = await _ensure_candidate(candidate_id, user, db)
    namespace = "candidates.recordings"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(InterviewRecording).where(
        InterviewRecording.candidate_id == candidate.id,
        InterviewRecording.organization_id == user.organization_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid recordings cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid recordings cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid recordings cursor")
        stmt = stmt.where(
            or_(
                InterviewRecording.created_at < cursor_time,
                and_(
                    InterviewRecording.created_at == cursor_time,
                    InterviewRecording.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(
                InterviewRecording.created_at.desc(), InterviewRecording.id.desc()
            ).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = list(rows[:limit])
    return CursorPage(
        items=[RecordingOut.model_validate(row) for row in rows],
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
    "/candidates/{candidate_id}/recordings",
    response_model=RecordingOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_recording(
    candidate_id: UUID,
    body: RecordingCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewRecording:
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
            source=candidate.source or "recording",
            actor_user_id=user.id,
            actor_kind="human",
            reason="recording_created",
            occurred_at=body.recorded_at,
        )
    else:
        if body.application_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "application_id requires position_id",
            )
        application = None
    rec = InterviewRecording(
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        position_id=body.position_id,
        application_id=application.id if application is not None else None,
        created_by=user.id,
        title=body.title.strip(),
        status=body.status or "pending",
        media_url=body.media_url,
        language=body.language,
        recorded_at=body.recorded_at,
    )
    db.add(rec)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        application_id=rec.application_id,
        position_id=rec.position_id,
        event_type="recording_added",
        subject_type="recording",
        subject_id=rec.id,
        payload={
            "title": rec.title,
            "status": rec.status,
            "position_title": await _position_title(rec.position_id, db),
        },
    )
    await db.commit()
    await db.refresh(rec)
    return rec


@router.get("/recordings/{recording_id}", response_model=RecordingOut)
async def get_recording(
    recording_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewRecording:
    return await _get_recording(recording_id, user, db)


@router.patch("/recordings/{recording_id}", response_model=RecordingOut)
async def update_recording(
    recording_id: UUID,
    body: RecordingUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewRecording:
    """Partial update + the pipeline write-hook (transcript / diarization /
    interview_score / summary)."""
    rec = await _get_recording(recording_id, user, db)
    old_status = rec.status
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
            candidate_id=rec.candidate_id,
        )
    pair_change = bool({"position_id", "application_id"} & body.model_fields_set)
    target_position_id = data.get("position_id", rec.position_id)
    target_application_id = data.get("application_id", rec.application_id)
    if data.get("position_id") is None and "position_id" in body.model_fields_set:
        target_application_id = data.get("application_id")
    if rec.application_id is not None and (
        target_application_id != rec.application_id
        or target_position_id != rec.position_id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Application-bound recording evidence cannot be retargeted",
        )
    if target_position_id is None:
        if target_application_id is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "application_id requires position_id",
            )
        application = None
    elif rec.application_id is not None:
        application = await resolve_linked_application(
            db,
            organization_id=user.organization_id,
            application_id=rec.application_id,
            candidate_id=rec.candidate_id,
            position_id=target_position_id,
            lock=True,
        )
    elif pair_change:
        application = await resolve_application_for_write(
            db,
            organization_id=user.organization_id,
            candidate_id=rec.candidate_id,
            position_id=target_position_id,
            application_id=target_application_id,
            source="recording",
            actor_user_id=user.id,
            actor_kind="human",
            reason="recording_retargeted",
            occurred_at=rec.recorded_at,
        )
    elif rec.application_id is None:
        application = await resolve_linked_application(
            db,
            organization_id=user.organization_id,
            application_id=None,
            candidate_id=rec.candidate_id,
            position_id=target_position_id,
            lock=True,
        )
    else:
        application = None
    data.pop("application_id", None)
    for key, value in data.items():
        setattr(rec, key, value)
    rec.application_id = application.id if application is not None else None
    if rec.status != old_status:
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=rec.candidate_id,
            actor_user_id=user.id,
            application_id=rec.application_id,
            position_id=rec.position_id,
            event_type="recording_status_changed",
            subject_type="recording",
            subject_id=rec.id,
            payload={
                "title": rec.title,
                "from_status": old_status,
                "to_status": rec.status,
                "interview_score": rec.interview_score,
                "position_title": await _position_title(rec.position_id, db),
            },
        )
    await db.commit()
    await db.refresh(rec)
    return rec


@router.post("/recordings/{recording_id}/file", response_model=RecordingOut)
async def upload_recording_file(
    recording_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewRecording:
    """Replace owned media only after bounded validation and malware scanning."""

    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    rec = await _get_recording(recording_id, user, db, lock=True)
    organization_id = user.organization_id
    stored = await store_private_upload(
        file,
        organization_id=organization_id,
        category="recordings",
        allowed_suffixes=RECORDING_FILE_SUFFIXES,
        max_bytes=MAX_RECORDING_BYTES,
        label="Recording file",
    )
    previous = rec.media_path
    previous_private_object_id = rec.private_object_id
    private_object_id: UUID | None = None
    try:
        private_object = await register_ready_private_object(
            db,
            organization_id=organization_id,
            resource_type="interview_recording",
            resource_id=rec.id,
            stored=stored,
        )
        private_object_id = private_object.id
        rec.media_path = stored.key
        rec.private_object_id = private_object.id
        rec.status = "uploaded"
        rec.duration_seconds = None
        rec.transcript = None
        rec.diarization = None
        rec.interview_score = None
        rec.interview_summary = None
        rec.evaluation = None
        append_candidate_activity(
            db,
            organization_id=organization_id,
            candidate_id=rec.candidate_id,
            actor_user_id=user.id,
            application_id=rec.application_id,
            position_id=rec.position_id,
            event_type="recording_file_uploaded",
            subject_type="recording",
            subject_id=rec.id,
            payload={
                "size_bytes": stored.size_bytes,
                "replaced": bool(previous),
            },
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
                category="recordings",
                resource_type="interview_recording",
                resource_id=rec.id,
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
            category="recordings",
            private_object_id=private_object_id,
        )
        raise
    await db.refresh(rec)
    return rec


@router.get("/recordings/{recording_id}/file")
async def download_recording_file(
    recording_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> StreamingResponse:
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    rec = await _get_recording(recording_id, user, db)
    if not rec.media_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file not found")
    key = await resolve_resource_private_blob_reference(
        db,
        organization_id=user.organization_id,
        category="recordings",
        resource_type="interview_recording",
        resource_id=rec.id,
        private_object_id=rec.private_object_id,
        legacy_reference=rec.media_path,
    )
    if key is None:
        raise HTTPException(status.HTTP_410_GONE, "Recording file is unavailable")
    suffix = PurePath(key).suffix.casefold()
    response = await private_blob_response(
        key,
        filename=safe_download_filename(
            rec.title,
            fallback=str(rec.id),
            suffix=suffix,
        ),
        media_type=validated_private_media_type(
            key,
            allowed_suffixes=RECORDING_FILE_SUFFIXES,
        ),
    )
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="download_recording_file",
            resource_type="interview_recording",
            resource_id=rec.id,
            path_template="/recordings/{recording_id}/file",
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "candidate_id": str(rec.candidate_id),
                "sensitive_read": True,
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return response


@router.delete(
    "/recordings/{recording_id}/file",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_recording_file(
    recording_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    rec = await _get_recording(recording_id, user, db, lock=True)
    if not rec.media_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file not found")
    await enqueue_or_delete_private_object(
        db,
        organization_id=user.organization_id,
        category="recordings",
        resource_type="interview_recording",
        resource_id=rec.id,
        private_object_id=rec.private_object_id,
        legacy_reference=rec.media_path,
        reason="resource_deleted",
    )
    rec.media_path = None
    rec.private_object_id = None
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=rec.candidate_id,
        actor_user_id=user.id,
        application_id=rec.application_id,
        position_id=rec.position_id,
        event_type="recording_file_deleted",
        subject_type="recording",
        subject_id=rec.id,
        payload={},
    )
    await db.commit()


@router.delete("/recordings/{recording_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recording(
    recording_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    rec = await _get_recording(recording_id, user, db, lock=True)
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=rec.candidate_id,
        actor_user_id=user.id,
        application_id=rec.application_id,
        position_id=rec.position_id,
        event_type="recording_deleted",
        subject_type="recording",
        subject_id=rec.id,
        payload={
            "title": rec.title,
            "status": rec.status,
            "interview_score": rec.interview_score,
            "position_title": await _position_title(rec.position_id, db),
        },
    )
    if rec.media_path:
        await enqueue_or_delete_private_object(
            db,
            organization_id=user.organization_id,
            category="recordings",
            resource_type="interview_recording",
            resource_id=rec.id,
            private_object_id=rec.private_object_id,
            legacy_reference=rec.media_path,
            reason="resource_deleted",
        )
    await db.delete(rec)
    await db.commit()
