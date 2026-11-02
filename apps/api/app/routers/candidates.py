from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import user_with_permissions
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_processing import CandidateProcessingJob
from app.models.enterprise import AuditLog
from app.models.folder import Folder
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.schemas.candidates import (
    CandidateDetail,
    CandidateApplicationOut,
    CandidateOut,
    CandidateProcessingBatchOut,
    CandidateProcessingJobOut,
    CandidateSummary,
    CandidateUpdate,
    HHJsonUploadRequest,
    PositionMatch,
    ScoreOut,
    ScoreRequest,
    TextUploadRequest,
)
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.candidate_processing import (
    CandidateProcessingError,
    enqueue_candidate_parse_job,
    enqueue_candidate_scoring_job,
    external_idempotency_key,
)
from app.services.enterprise import can_access_field, require_field_access
from app.services.entitlements import TECHNICAL_LIMITS, record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.uploads import (
    candidate_from_hh_json,
    candidate_from_text,
    candidate_from_upload,
    cleanup_promoted_upload_after_db_failure,
    private_blob_response,
    resolve_resource_private_blob_reference,
    safe_download_filename,
    validated_private_media_type,
)
from app.storage.registry import register_ready_private_object

router = APIRouter(prefix="/candidates", tags=["candidates"])

candidate_reader = user_with_permissions("candidates.read")
candidate_writer = user_with_permissions("candidates.write")
candidate_scorer = user_with_permissions("candidates.write", "ai.use")

MAX_CANDIDATE_CV_BYTES = TECHNICAL_LIMITS["candidate_document_bytes"]


async def _candidate_out(
    db: AsyncSession,
    user: User,
    candidate: Candidate,
) -> CandidateOut:
    """Apply candidate-document/contact policy before Pydantic serialization."""

    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    document_visible = contact_visible and await can_access_field(
        db, user, FieldClass.CANDIDATE_DOCUMENT
    )
    payload = CandidateOut.model_validate(candidate)
    if document_visible and contact_visible:
        return payload
    return payload.model_copy(
        update={
            "source_ref": candidate.source_ref if contact_visible else None,
            "raw_cv": None,
            "parsed_cv": None,
            "has_original_file": False,
            "github_login": None,
            "github_data": None,
            "github_fetched_at": None,
        }
    )


# ---------- list ----------


@router.get("", response_model=CursorPage[CandidateSummary])
async def list_candidates(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[CandidateSummary]:
    """All candidates in the caller's org, newest first, each enriched with
    their best score + the position it was scored against. Two queries — no N+1."""
    namespace = "candidates.list"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"merged": False},
    )
    stmt = select(Candidate).where(
        Candidate.organization_id == user.organization_id,
        Candidate.merged_at.is_(None),
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid candidates cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid candidates cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid candidates cursor")
        stmt = stmt.where(
            or_(
                Candidate.created_at < cursor_time,
                and_(Candidate.created_at == cursor_time, Candidate.id < cursor_id),
            )
        )
    candidates = (
        await db.execute(
            stmt.order_by(Candidate.created_at.desc(), Candidate.id.desc()).limit(
                limit + 1
            )
        )
    ).scalars().all()
    has_more = len(candidates) > limit
    candidates = candidates[:limit]

    # Best-match excludes positions in archived folders (and legacy '_'-prefixed
    # folder strings) so retired roles never surface as a candidate's best fit.
    candidate_ids = [candidate.id for candidate in candidates]
    scores_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    score_rows = []
    if scores_visible and candidate_ids:
        score_rows = (
            await db.execute(
                select(
                    Score.candidate_id,
                    Score.position_id,
                    Position.title,
                    Score.overall_score,
                )
                .join(Position, Position.id == Score.position_id)
                .outerjoin(Folder, Folder.id == Position.folder_id)
                .where(
                    Score.organization_id == user.organization_id,
                    Score.candidate_id.in_(candidate_ids),
                    or_(Position.folder_id.is_(None), Folder.is_archived.is_(False)),
                    or_(
                        Position.folder.is_(None),
                        Position.folder.not_like("\\_%", escape="\\"),
                    ),
                )
                .order_by(Score.candidate_id, Score.overall_score.desc().nulls_last())
            )
        ).all()

    best: dict[UUID, tuple[int | None, UUID, str]] = {}
    counts: dict[UUID, int] = {}
    for cand_id, pos_id, title, overall in score_rows:
        counts[cand_id] = counts.get(cand_id, 0) + 1
        if cand_id not in best:
            best[cand_id] = (overall, pos_id, title)

    out: list[CandidateSummary] = []
    for c in candidates:
        b = best.get(c.id)
        out.append(
            CandidateSummary(
                id=c.id,
                source=c.source,
                source_ref=c.source_ref if contact_visible else None,
                is_favorite=c.is_favorite,
                viewed_at=c.viewed_at,
                created_at=c.created_at,
                best_score=b[0] if b else None,
                best_position_id=b[1] if b else None,
                best_position_title=b[2] if b else None,
                score_count=counts.get(c.id, 0),
            )
        )
    return CursorPage(
        items=out,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": candidates[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(candidates[-1].id),
                },
            )
            if has_more and candidates
            else None
        ),
    )


# ---------- upload paths ----------


@router.post(
    "/upload/file", response_model=CandidateOut, status_code=status.HTTP_201_CREATED
)
async def upload_file(
    file: UploadFile = File(...),
    source_ref: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateOut:
    """Stream a PDF/DOCX through quarantine, validation, and malware scan."""
    organization_id = user.organization_id
    candidate_upload = await candidate_from_upload(
        file,
        organization_id=organization_id,
        source_ref=source_ref,
        max_bytes=MAX_CANDIDATE_CV_BYTES,
    )
    candidate = candidate_upload.candidate
    private_object_id: UUID | None = None
    try:
        await record_usage(
            db,
            organization_id=organization_id,
            metric="candidates",
            amount=1,
            kind="usage",
            idempotency_key=f"candidate:create:{candidate.id}",
            resource_type="candidate",
            resource_id=candidate.id,
        )
        db.add(candidate)
        private_object = await register_ready_private_object(
            db,
            organization_id=organization_id,
            resource_type="candidate",
            resource_id=candidate.id,
            stored=candidate_upload.stored,
        )
        private_object_id = private_object.id
        candidate.private_object_id = private_object.id
        await db.flush()
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            event_type="candidate_added",
            subject_type="candidate",
            subject_id=candidate.id,
            payload={"source": candidate.source},
        )
        await enqueue_candidate_parse_job(
            db,
            organization_id=organization_id,
            candidate_id=candidate.id,
            idempotency_key=f"manual-upload:{private_object.id}",
            actor_user_id=user.id,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        await cleanup_promoted_upload_after_db_failure(
            candidate.pdf_path,
            organization_id=organization_id,
            category="candidates",
            private_object_id=private_object_id,
        )
        raise
    await db.refresh(candidate)
    return await _candidate_out(db, user, candidate)


@router.post(
    "/upload/text", response_model=CandidateOut, status_code=status.HTTP_201_CREATED
)
async def upload_text(
    body: TextUploadRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateOut:
    candidate = candidate_from_text(body.raw_text, source_ref=body.source_ref)
    candidate.id = candidate.id or uuid4()
    candidate.organization_id = user.organization_id
    await record_usage(
        db,
        organization_id=user.organization_id,
        metric="candidates",
        amount=1,
        kind="usage",
        idempotency_key=f"candidate:create:{candidate.id}",
        resource_type="candidate",
        resource_id=candidate.id,
    )
    db.add(candidate)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="candidate_added",
        subject_type="candidate",
        subject_id=candidate.id,
        payload={"source": candidate.source},
    )
    await db.commit()
    await db.refresh(candidate)
    return await _candidate_out(db, user, candidate)


@router.post(
    "/upload/hh", response_model=CandidateOut, status_code=status.HTTP_201_CREATED
)
async def upload_hh_json(
    body: HHJsonUploadRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateOut:
    candidate = candidate_from_hh_json(body.hh_json)
    candidate.id = candidate.id or uuid4()
    candidate.organization_id = user.organization_id
    await record_usage(
        db,
        organization_id=user.organization_id,
        metric="candidates",
        amount=1,
        kind="usage",
        idempotency_key=f"candidate:create:{candidate.id}",
        resource_type="candidate",
        resource_id=candidate.id,
    )
    db.add(candidate)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="candidate_added",
        subject_type="candidate",
        subject_id=candidate.id,
        payload={"source": candidate.source},
    )
    await db.commit()
    await db.refresh(candidate)
    return await _candidate_out(db, user, candidate)


# ---------- detail + score ----------


@router.get("/{candidate_id}", response_model=CandidateDetail)
async def get_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateDetail:
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    candidate_payload = await _candidate_out(db, user, c)
    return CandidateDetail(**candidate_payload.model_dump())


@router.get(
    "/{candidate_id}/applications",
    response_model=CursorPage[CandidateApplicationOut],
)
async def list_candidate_applications(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[CandidateApplicationOut]:
    """Return the latest application cycle per position as a bounded page."""

    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")

    ranked_applications = (
        select(
            Application.id.label("application_id"),
            func.row_number()
            .over(
                partition_by=Application.position_id,
                order_by=(Application.cycle_number.desc(), Application.id.desc()),
            )
            .label("cycle_rank"),
        )
        .where(
            Application.organization_id == user.organization_id,
            Application.candidate_id == candidate.id,
        )
        .subquery()
    )
    stmt = (
        select(Application, Score)
        .join(
            ranked_applications,
            and_(
                ranked_applications.c.application_id == Application.id,
                ranked_applications.c.cycle_rank == 1,
            ),
        )
        .join(Position, Position.id == Application.position_id)
        .outerjoin(Folder, Folder.id == Position.folder_id)
        .outerjoin(Score, Score.id == Application.legacy_score_id)
        .where(
            Application.organization_id == user.organization_id,
            Application.candidate_id == candidate.id,
            or_(Position.folder_id.is_(None), Folder.is_archived.is_(False)),
            or_(
                Position.folder.is_(None),
                Position.folder.not_like("\\_%", escape="\\"),
            ),
        )
    )
    namespace = "candidates.applications"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id), "latest_per_position": True},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"applied_at", "id"},
            detail="Invalid candidate applications cursor",
        )
        cursor_time = cursor_datetime(
            payload,
            "applied_at",
            detail="Invalid candidate applications cursor",
        )
        cursor_id = cursor_uuid(
            payload,
            detail="Invalid candidate applications cursor",
        )
        stmt = stmt.where(
            or_(
                Application.applied_at < cursor_time,
                and_(
                    Application.applied_at == cursor_time,
                    Application.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(Application.applied_at.desc(), Application.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    scores_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    items: list[CandidateApplicationOut] = []
    for application, score in rows:
        score_values = (
            ScoreOut.model_validate(score).model_dump(
                exclude={
                    "candidate_id",
                    "position_id",
                    "stage",
                    "owner_user_id",
                    "next_action_due_date",
                }
            )
            if scores_visible and score is not None
            else {}
        )
        items.append(
            CandidateApplicationOut(
                **score_values,
                candidate_id=application.candidate_id,
                position_id=application.position_id,
                stage=application.stage,
                owner_user_id=application.owner_user_id,
                next_action_due_date=application.next_action_due_date,
                application_id=application.id,
                application_version=application.updated_at,
                cycle_number=application.cycle_number,
                is_active=(
                    application.closed_at is None
                    and application.archived_at is None
                ),
                applied_at=application.applied_at,
            )
        )
    last_application = rows[-1][0] if rows else None
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "applied_at": last_application.applied_at.astimezone(UTC).isoformat(),
                    "id": str(last_application.id),
                },
            )
            if has_more and last_application is not None
            else None
        ),
    )


@router.get("/{candidate_id}/cv")
async def download_candidate_cv(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> StreamingResponse:
    """Stream the original CV file (PDF or DOCX) back to the recruiter.

    Falls back to 404 when the candidate was created via paste-text or
    HH JSON — there's no original file to send. The frontend should hide
    the download button in those cases, but we still 404 defensively.
    """
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    if not c.pdf_path:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No original file for this candidate."
        )
    await require_field_access(db, user, FieldClass.CANDIDATE_DOCUMENT)
    await require_field_access(db, user, FieldClass.CONTACT)
    key = await resolve_resource_private_blob_reference(
        db,
        organization_id=user.organization_id,
        category="candidates",
        resource_type="candidate",
        resource_id=c.id,
        private_object_id=c.private_object_id,
        legacy_reference=c.pdf_path,
    )
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored CV file is unavailable.")
    # Use the candidate ref as the download filename so the recruiter sees a
    # human-readable name, not a UUID. Keep the original extension intact.
    ext = Path(key).suffix.lower() or ".bin"
    media_type = validated_private_media_type(
        key,
        allowed_suffixes={".pdf", ".docx"},
    )
    filename = safe_download_filename(
        c.source_ref,
        fallback=str(c.id),
        suffix=ext,
    )
    response = await private_blob_response(
        key,
        filename=filename,
        media_type=media_type,
    )
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="download_candidate_cv",
            resource_type="candidate",
            resource_id=c.id,
            path_template="/candidates/{candidate_id}/cv",
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={"sensitive_read": True},
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return response


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_candidate(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> None:
    """Soft-delete a candidate; retention cleanup performs the eventual purge."""
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == candidate_id,
                Candidate.organization_id == user.organization_id,
                Candidate.merged_at.is_(None),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    deleted_at = datetime.now(UTC)
    await record_usage(
        db,
        organization_id=user.organization_id,
        metric="candidates",
        amount=-1,
        kind="refund",
        idempotency_key=f"candidate:delete:{candidate.id}:{deleted_at.isoformat()}",
        resource_type="candidate",
        resource_id=candidate.id,
        enforce_limit=False,
    )
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="candidate_deleted",
        subject_type="candidate",
        subject_id=candidate.id,
        payload={"source": candidate.source},
    )
    candidate.deleted_at = deleted_at
    candidate.deleted_by_user_id = user.id
    await db.commit()


@router.patch("/{candidate_id}", response_model=CandidateOut)
async def update_candidate(
    candidate_id: UUID,
    body: CandidateUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_writer),
) -> CandidateOut:
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    if body.is_favorite is not None:
        c.is_favorite = body.is_favorite
    await db.commit()
    await db.refresh(c)
    return await _candidate_out(db, user, c)


@router.post("/{candidate_id}/view", response_model=CandidateOut)
async def mark_candidate_viewed(
    candidate_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CandidateOut:
    c = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    c.viewed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(c)
    return await _candidate_out(db, user, c)


@router.post(
    "/{candidate_id}/score",
    response_model=CandidateProcessingJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def score_candidate_endpoint(
    candidate_id: UUID,
    body: ScoreRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=8,
        max_length=512,
    ),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_scorer),
) -> CandidateProcessingJobOut:
    if user.role == "interviewer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    # Belt-and-suspenders: refuse to even try scoring if candidate or position
    # don't belong to the caller's org. The scoring service insert would also
    # blow up via FK, but we catch it cleanly here.
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    cand = candidate.id if candidate else None
    if cand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    candidate_id = cand
    pos = (
        await db.execute(
            select(Position.id).where(
                Position.id == body.position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if pos is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    if len((candidate.raw_cv or "").strip()) < 50 and not candidate.pdf_path:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "CV text is too short to score.",
        )
    try:
        stable_key = external_idempotency_key(
            "manual-score",
            idempotency_key or str(uuid4()),
        )
        job, _created = await enqueue_candidate_scoring_job(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate_id,
            position_id=body.position_id,
            application_id=body.application_id,
            actor_user_id=user.id,
            idempotency_key=stable_key,
        )
        await db.commit()
        await db.refresh(job)
    except CandidateProcessingError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Scoring request conflicts with an existing idempotency key.",
        ) from exc
    return CandidateProcessingJobOut.model_validate(job)


@router.get(
    "/{candidate_id}/processing-jobs/{job_id}",
    response_model=CandidateProcessingJobOut,
)
async def get_candidate_processing_job(
    candidate_id: UUID,
    job_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_scorer),
) -> CandidateProcessingJobOut:
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    job = (
        await db.execute(
            select(CandidateProcessingJob).where(
                CandidateProcessingJob.id == job_id,
                CandidateProcessingJob.organization_id == user.organization_id,
                CandidateProcessingJob.candidate_id == candidate.id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Processing job not found")
    return CandidateProcessingJobOut.model_validate(job)


# ---------- reverse matching: which open positions suit this candidate ----------


@router.get(
    "/{candidate_id}/best-positions",
    response_model=CursorPage[PositionMatch],
)
async def best_positions_for_candidate(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_reader),
) -> CursorPage[PositionMatch]:
    """Reverse-matching: pull every open position in the org and join existing
    scores. Ranks scored positions first (by overall_score desc), then unscored
    positions in updated_at order so the recruiter knows what's still to score.
    """
    # This operation is score-ranked by definition. Returning a differently
    # ordered, all-null list would still leak the score boundary through
    # cursor/order behavior and would misrepresent the endpoint's semantics.
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    cand = candidate.id if candidate else None
    if cand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    candidate_id = cand

    namespace = "candidates.best-positions"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate_id), "status": "open"},
    )
    rank = func.coalesce(Score.overall_score, -1)
    stmt = (
        select(Position, Score)
        .outerjoin(
            Score,
            (Score.position_id == Position.id)
            & (Score.candidate_id == candidate_id),
        )
        .where(
            Position.organization_id == user.organization_id,
            Position.status == "open",
        )
    )
    if user.role in {"hiring_manager", "interviewer"}:
        stmt = stmt.where(
            select(Application.id)
            .where(
                Application.candidate_id == candidate_id,
                Application.position_id == Position.id,
            )
            .exists()
        )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"rank", "updated_at", "id"},
            detail="Invalid position matches cursor",
        )
        try:
            cursor_rank = int(payload["rank"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid position matches cursor"},
            ) from exc
        cursor_time = cursor_datetime(
            payload, "updated_at", detail="Invalid position matches cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid position matches cursor")
        stmt = stmt.where(
            or_(
                rank < cursor_rank,
                and_(rank == cursor_rank, Position.updated_at < cursor_time),
                and_(
                    rank == cursor_rank,
                    Position.updated_at == cursor_time,
                    Position.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(rank.desc(), Position.updated_at.desc(), Position.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        PositionMatch(
            position_id=p.id,
            position_title=p.title,
            position_status=p.status,
            overall_score=s.overall_score if s else None,
            score_technical_skills=s.score_technical_skills if s else None,
            score_quality_of_projects=s.score_quality_of_projects if s else None,
            score_previous_experience=s.score_previous_experience if s else None,
            score_impact=s.score_impact if s else None,
            scored_at=s.scored_at if s else None,
            model_used=s.model_used if s else None,
        )
        for p, s in rows
    ]
    last_position, last_score = rows[-1] if rows else (None, None)
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "rank": last_score.overall_score if last_score else -1,
                    "updated_at": last_position.updated_at.astimezone(UTC).isoformat(),
                    "id": str(last_position.id),
                },
            )
            if has_more and last_position is not None
            else None
        ),
    )


@router.post(
    "/{candidate_id}/score-all-open",
    response_model=CandidateProcessingBatchOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def score_all_open(
    candidate_id: UUID,
    folder: str | None = None,
    folder_id: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(candidate_scorer),
) -> CandidateProcessingBatchOut:
    """Score the candidate against every open, unscored position in the org.

    When `folder` is supplied, scoping is limited to positions in that folder.
    The API transaction only records work; dedicated replicas lease and score
    each pair with retry and dead-letter history.
    """
    if user.role == "interviewer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    cand = candidate.id if candidate else None
    if cand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    candidate_id = cand
    if len((candidate.raw_cv or "").strip()) < 50 and not candidate.pdf_path:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "CV text is too short to score.",
        )

    # Find every open position with no score for this candidate yet, optionally
    # constrained to a single folder.
    stmt = (
        select(Position.id)
        .outerjoin(
            Score,
            (Score.position_id == Position.id) & (Score.candidate_id == candidate_id),
        )
        .where(
            Position.organization_id == user.organization_id,
            Position.status == "open",
            or_(
                Score.id.is_(None),
                and_(Score.model_used.is_(None), Score.overall_score.is_(None)),
            ),
        )
    )
    if user.role in {"hiring_manager", "interviewer"}:
        stmt = stmt.where(
            select(Application.id)
            .where(
                Application.candidate_id == candidate_id,
                Application.position_id == Position.id,
            )
            .exists()
        )
    # folder_id (the folders-tree filter) takes precedence; the legacy folder
    # string branch is kept unchanged for existing callers.
    if folder_id is not None:
        if folder_id == "__none__":
            stmt = stmt.where(Position.folder_id.is_(None))
        else:
            try:
                fid = UUID(folder_id)
            except ValueError:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid folder_id"
                )
            # Score against the folder AND its children (2-level tree), so
            # picking a parent like «Демо Холдинг» scores the whole subtree.
            subtree = (
                select(Folder.id)
                .where(
                    Folder.organization_id == user.organization_id,
                    or_(Folder.id == fid, Folder.parent_id == fid),
                )
                .subquery()
            )
            stmt = stmt.where(Position.folder_id.in_(select(subtree.c.id)))
    elif folder is not None:
        f = folder.strip()
        # Empty string = "score against the Unsorted bucket (no folder set)";
        # any other value = exact folder match. Matches the dashboard's filter.
        if f == "":
            stmt = stmt.where(Position.folder.is_(None))
        else:
            stmt = stmt.where(Position.folder == f)

    rows = (await db.execute(stmt)).all()
    pending_position_ids = [r[0] for r in rows]

    request_id = uuid4()
    accepted_job_ids: set[UUID] = set()
    for position_id in pending_position_ids:
        job, _created = await enqueue_candidate_scoring_job(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate_id,
            position_id=position_id,
            actor_user_id=user.id,
            idempotency_key=f"score-all:{request_id}:{position_id}",
        )
        accepted_job_ids.add(job.id)
    await db.commit()
    queued = len(accepted_job_ids)
    return CandidateProcessingBatchOut(
        queued=queued,
        message=f"Queued scoring for {queued} position(s).",
    )
