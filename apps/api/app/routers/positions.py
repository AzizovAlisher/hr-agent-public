import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user, user_with_permissions
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.chat_session import ChatSession
from app.models.folder import Folder
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.schemas.applications import ApplicationStageTransition
from app.schemas.chat import ChatCapabilityOut
from app.schemas.folders import MovePositionsRequest
from app.schemas.pagination import CursorPage
from app.schemas.position_draft import (
    DraftConverseRequest,
    DraftConverseResponse,
    PositionDraftExtractTextResponse,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectExtractResponse,
)
from app.schemas.positions import (
    PositionCreate,
    PositionDetail,
    PositionOut,
    PositionUpdate,
    RankedCandidate,
    StageUpdate,
)
from app.services.applications import apply_application_stage_transition
from app.services.bounded_document_parser import (
    BoundedDocumentParseError,
    BoundedDocumentParseTimeout,
    extract_document_text,
)
from app.services.candidate_processing import enqueue_candidate_scoring_job
from app.services.candidate_profile import (
    generate_candidate_profile,
    generate_public_description,
)
from app.services.enterprise import can_access_field, organization_llm_capability
from app.services.entitlements import record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.position_draft import (
    converse as draft_converse,
)
from app.services.position_draft import (
    extract_project,
)
from app.services.public_positions import SENSITIVITY_PUBLIC, normalize_sensitivity
from app.services.structured_hiring import resolve_application_for_write
from app.storage.validation import UploadValidationError, validate_upload_path_bounded

router = APIRouter(prefix="/positions", tags=["positions"])

position_reader = user_with_permissions("candidates.read")
position_writer = user_with_permissions("candidates.write")
position_ai_user = user_with_permissions("ai.use")
position_ai_writer = user_with_permissions("candidates.write", "ai.use")

MAX_POSITION_UPLOAD_BYTES = 10 * 1024 * 1024

#: How restrictive each tier is, for detecting an escalation on PATCH.
_SENSITIVITY_RANK = {"public": 0, "nda": 1, "restricted": 2}


async def _position_profile_generation_unavailable() -> None:
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "code": "ai_position_profile_generation_unavailable",
            "detail": (
                "AI position-profile generation is unavailable in the pilot "
                "until it runs as a durable, retryable job."
            ),
        },
    )


async def _can_view_position_compensation(db: AsyncSession, user: User) -> bool:
    return await can_access_field(
        db,
        user,
        FieldClass.COMPENSATION,
        required_permission="requisitions.compensation",
    )


async def _require_position_compensation(db: AsyncSession, user: User) -> None:
    if not await _can_view_position_compensation(db, user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Compensation access is restricted by organization policy",
        )


def _to_out(
    position: Position,
    candidate_count: int = 0,
    chat_session_count: int = 0,
    *,
    compensation_visible: bool,
) -> PositionOut:
    # Position text and requirements are legacy unstructured stores.  A pay
    # phrase can be written under any JSON key or in any language, so pattern
    # redaction cannot prove confidentiality.  Compensation-restricted readers
    # therefore receive only structural/non-text position metadata; this is the
    # fail-closed compatibility contract until compensation has a dedicated
    # typed column/store and legacy content has been classified.
    restricted_title = "Restricted position"
    return PositionOut(
        id=position.id,
        title=position.title if compensation_visible else restricted_title,
        description=position.description if compensation_visible else None,
        full_description=position.full_description if compensation_visible else None,
        requirements=position.requirements if compensation_visible else None,
        internal_notes=position.internal_notes if compensation_visible else None,
        status=position.status,
        folder=position.folder if compensation_visible else None,
        folder_id=position.folder_id,
        public_apply_enabled=position.public_apply_enabled,
        sensitivity=position.sensitivity,
        candidate_profile=position.candidate_profile if compensation_visible else None,
        public_description=position.public_description
        if compensation_visible
        else None,
        created_by=position.created_by,
        candidate_count=candidate_count,
        chat_session_count=chat_session_count,
        created_at=position.created_at,
        updated_at=position.updated_at,
    )


def _ranked_candidate_out(
    score: Score | None,
    candidate: Candidate,
    application: Application,
    *,
    contact_visible: bool,
) -> RankedCandidate:
    display_name = None
    if contact_visible:
        parsed_cv = candidate.parsed_cv if isinstance(candidate.parsed_cv, dict) else {}
        parsed_name = parsed_cv.get("applicant_name")
        if isinstance(parsed_name, str):
            display_name = " ".join(parsed_name.split()) or None
        if display_name is None and candidate.source_ref:
            display_name = " ".join(candidate.source_ref.split()) or None
        if display_name is not None:
            display_name = display_name[:255]

    return RankedCandidate(
        candidate_id=candidate.id,
        source=candidate.source,
        source_ref=candidate.source_ref if contact_visible else None,
        display_name=display_name,
        overall_score=score.overall_score if score is not None else None,
        score_technical_skills=(
            score.score_technical_skills if score is not None else None
        ),
        score_quality_of_projects=(
            score.score_quality_of_projects if score is not None else None
        ),
        score_previous_experience=(
            score.score_previous_experience if score is not None else None
        ),
        score_impact=score.score_impact if score is not None else None,
        comment_technical_skills=(
            score.comment_technical_skills if score is not None else None
        ),
        comment_quality_of_projects=(
            score.comment_quality_of_projects if score is not None else None
        ),
        comment_previous_experience=(
            score.comment_previous_experience if score is not None else None
        ),
        comment_impact=score.comment_impact if score is not None else None,
        model_used=score.model_used if score is not None else None,
        is_favorite=candidate.is_favorite,
        viewed_at=candidate.viewed_at,
        stage=application.stage,
        owner_user_id=application.owner_user_id,
        next_action_due_date=application.next_action_due_date,
        application_id=application.id,
        application_version=application.updated_at,
    )


async def _resolve_folder(db: AsyncSession, user: User, folder_id: UUID) -> Folder:
    """Fetch a folder, scoped to the caller's org. 404 on cross-org / missing.

    The FK alone does not block cross-org references, so every folder_id from
    a request body is validated here before use."""
    folder = (
        await db.execute(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    return folder


async def _resolve_or_create_project_folder(
    db: AsyncSession,
    *,
    organization_id: UUID,
    requested_name: str | None,
    project_id: UUID,
) -> Folder:
    """Race-safely resolve a case-insensitive tenant root project folder."""

    name = (requested_name or "").strip()
    if not name:
        name = f"Project {project_id.hex[:8]}"
    # The database's legacy uniqueness index is case-sensitive. Serialize the
    # case-folded tenant/name namespace so two concurrent requests cannot create
    # visually duplicate roots that differ only by case.
    lock_key = f"project-folder:{organization_id}:{name.casefold()}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )
    roots = list(
        (
            await db.execute(
                select(Folder).where(
                    Folder.organization_id == organization_id,
                    Folder.parent_id.is_(None),
                )
            )
        ).scalars()
    )
    existing = next(
        (folder for folder in roots if folder.name.casefold() == name.casefold()),
        None,
    )
    if existing is not None:
        if existing.is_archived:
            existing.is_archived = False
        return existing
    folder = Folder(
        organization_id=organization_id,
        parent_id=None,
        name=name,
    )
    db.add(folder)
    await db.flush()
    return folder


@router.post("/draft", response_model=DraftConverseResponse)
async def position_draft_converse(
    body: DraftConverseRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_ai_user),
) -> DraftConverseResponse:
    """AI-guided position drafting.

    Stateless on the server side — the client passes the full conversation
    each turn. Returns the next assistant message + the updated structured
    draft (extracted from everything said so far)."""
    return await draft_converse(
        body.messages,
        body.current_draft,
        db=db,
        organization_id=user.organization_id,
    )


async def _extract_uploaded_text(
    file: UploadFile, *, max_chars: int
) -> tuple[str, bool]:
    """Read a job-description / project upload and return (text, truncated).

    Supports PDF / DOCX / TXT / MD. Raises HTTPException on unsupported type
    or unreadable content. `max_chars` is the cap before we trim with a
    "[…trimmed…]" marker so the prompt stays bounded."""
    content = await file.read(MAX_POSITION_UPLOAD_BYTES + 1)
    if len(content) > MAX_POSITION_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "Position document exceeds the 10 MB upload limit.",
        )
    text = ""
    try:
        suffix_hint = Path(file.filename or "upload").suffix.casefold() or ".upload"
        with tempfile.NamedTemporaryFile(suffix=suffix_hint, delete=True) as tmp:
            tmp.write(content)
            tmp.flush()
            path = Path(tmp.name)
            suffix = await validate_upload_path_bounded(
                path,
                filename=file.filename or "",
                media_type=file.content_type or "",
                allowed_suffixes={".pdf", ".docx", ".txt", ".md"},
            )
            if suffix in {".pdf", ".docx"}:
                text = await extract_document_text(path, suffix)
            else:
                text = content.decode("utf-8-sig")
    except HTTPException:
        raise
    except UploadValidationError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except BoundedDocumentParseTimeout as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "document_parse_timeout"},
        ) from exc
    except BoundedDocumentParseError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Uploaded document could not be parsed.",
        ) from exc

    text = (text or "").strip()
    if not text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Could not extract any readable text from the file.",
        )

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n\n[…trimmed…]"
    return text, truncated


@router.post("/draft/extract-text", response_model=PositionDraftExtractTextResponse)
async def position_draft_extract_text(
    file: UploadFile = File(...),
    _user: User = Depends(position_writer),
) -> dict:
    """Extract plain text from an uploaded job description (PDF / DOCX / TXT / MD).

    Returns just the text + filename + char count. The frontend then injects
    this into the conversation as a user message and calls /positions/draft
    as normal — keeps extraction and drafting as separate concerns."""
    text, truncated = await _extract_uploaded_text(file, max_chars=20000)
    return {
        "filename": file.filename or "uploaded",
        "char_count": len(text),
        "truncated": truncated,
        "text": text,
    }


@router.post("/project/extract", response_model=ProjectExtractResponse)
async def position_project_extract(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_ai_user),
) -> ProjectExtractResponse:
    """Parse a multi-position hiring document into N position drafts.

    Use this when a recruiter has one document describing an entire project
    (e.g. "Mobile Banking launch — backend + iOS + Android + QA + designer")
    and wants to spawn all the position cards at once. The frontend then shows
    the drafts in a review screen, the user tweaks them, and commits via
    /positions/project/create."""
    text, _truncated = await _extract_uploaded_text(file, max_chars=40000)
    return await extract_project(
        text=text,
        source_filename=file.filename,
        db=db,
        organization_id=user.organization_id,
    )


@router.post(
    "/project",
    response_model=ProjectCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def position_project_create(
    body: ProjectCreateRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_writer),
) -> ProjectCreateResponse:
    """Create every position from a reviewed project draft in one go.

    All positions inherit the shared `folder` so the dashboard groups them.
    Soft fields (seniority, work_format, salary_range, etc.) get merged into
    the requirements JSON the same way /lib/actions.ts does for the single
    position flow — keeps the position detail view consistent regardless of
    which entry point created the row."""
    # Project drafts are entirely unstructured (title, description,
    # requirements, and folder).  Their compensation semantics cannot be
    # classified completely, so compensation-restricted callers may not write
    # them at all until the domain has a dedicated typed compensation store.
    await _require_position_compensation(db, user)
    created_ids: list[UUID] = []
    valid_count = sum(1 for draft in body.positions if (draft.title or "").strip())
    project_id = uuid4()
    project_folder = await _resolve_or_create_project_folder(
        db,
        organization_id=user.organization_id,
        requested_name=body.folder,
        project_id=project_id,
    )
    folder = project_folder.name
    if valid_count:
        await record_usage(
            db,
            organization_id=user.organization_id,
            metric="active_jobs",
            amount=valid_count,
            kind="usage",
            idempotency_key=f"active-job:project:{project_id}",
            resource_type="position_project",
            resource_id=project_id,
        )

    for draft in body.positions:
        title = (draft.title or "").strip()
        if not title:
            # Skip blank entries silently — the recruiter may have cleared a
            # row in the review UI but kept it in the list.
            continue

        reqs: dict = dict(draft.requirements or {})
        for key, value in (
            ("seniority", draft.seniority),
            ("work_format", draft.work_format),
            ("location", draft.location),
            ("employment_type", draft.employment_type),
            ("salary_range", draft.salary_range),
            ("team_size", draft.team_size),
        ):
            if value and str(value).strip() and key not in reqs:
                reqs[key] = value

        p = Position(
            id=uuid4(),
            organization_id=user.organization_id,
            title=title,
            description=(draft.description or "").strip() or None,
            requirements=reqs or None,
            status="open",
            folder_id=project_folder.id,
            folder=folder,
            created_by=user.id,
        )
        db.add(p)
        await db.flush()
        created_ids.append(p.id)

    if not created_ids:
        # Empty input is treated as a client error rather than a no-op — the UI
        # should not call this endpoint with zero valid drafts.
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No valid positions to create. Each position must have a title.",
        )

    await db.commit()
    return ProjectCreateResponse(
        created=created_ids,
        folder_id=project_folder.id,
        folder=folder,
    )


@router.get("", response_model=CursorPage[PositionOut])
async def list_positions(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_reader),
    folder_id: str | None = None,
    parent_id: UUID | None = None,
    exclude_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
) -> CursorPage[PositionOut]:
    """List positions for the caller's org.

    All filter params are OPT-IN — with none of them set the response is
    identical to the original behaviour (six existing consumers depend on this).
    - folder_id=<uuid>  exact folder; folder_id=__none__  Unsorted.
    - parent_id=<uuid>  positions in that parent folder OR any of its children.
    - exclude_archived  hide positions in archived folders (dashboard opts in),
      plus the legacy '_'-prefixed string net for re-imported archived rows.
    """
    compensation_visible = await _can_view_position_compensation(db, user)
    candidate_subq = (
        select(
            Application.position_id,
            func.count(func.distinct(Application.candidate_id)).label("c"),
        )
        .where(
            Application.organization_id == user.organization_id,
            Application.closed_at.is_(None),
            Application.archived_at.is_(None),
        )
        .group_by(Application.position_id)
        .subquery()
    )
    chat_subq = (
        select(
            ChatSession.position_id,
            func.count(ChatSession.id).label("c"),
        )
        .where(
            ChatSession.position_id.is_not(None),
            ChatSession.organization_id == user.organization_id,
        )
        .group_by(ChatSession.position_id)
        .subquery()
    )
    stmt = (
        select(
            Position,
            func.coalesce(candidate_subq.c.c, 0),
            func.coalesce(chat_subq.c.c, 0),
        )
        .outerjoin(candidate_subq, candidate_subq.c.position_id == Position.id)
        .outerjoin(chat_subq, chat_subq.c.position_id == Position.id)
        .where(Position.organization_id == user.organization_id)
    )

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
            stmt = stmt.where(Position.folder_id == fid)

    if parent_id is not None:
        folder_ids_subq = (
            select(Folder.id)
            .where(
                Folder.organization_id == user.organization_id,
                or_(Folder.id == parent_id, Folder.parent_id == parent_id),
            )
            .subquery()
        )
        stmt = stmt.where(Position.folder_id.in_(select(folder_ids_subq.c.id)))

    if exclude_archived:
        archived_subq = (
            select(Folder.id)
            .where(
                Folder.organization_id == user.organization_id,
                Folder.is_archived.is_(True),
            )
            .subquery()
        )
        stmt = stmt.where(
            or_(
                Position.folder_id.is_(None),
                Position.folder_id.not_in(select(archived_subq.c.id)),
            )
        ).where(
            # legacy string net: hide re-imported '_archived' (folder_id NULL) rows
            or_(
                Position.folder.is_(None),
                Position.folder.not_like("\\_%", escape="\\"),
            )
        )

    namespace = "positions.list"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "folder_id": folder_id,
            "parent_id": str(parent_id) if parent_id else None,
            "exclude_archived": exclude_archived,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"updated_at", "id"},
            detail="Invalid positions cursor",
        )
        cursor_time = cursor_datetime(
            payload, "updated_at", detail="Invalid positions cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid positions cursor")
        stmt = stmt.where(
            or_(
                Position.updated_at < cursor_time,
                and_(Position.updated_at == cursor_time, Position.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(Position.updated_at.desc(), Position.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return CursorPage(
        items=[
            _to_out(
                p,
                candidate_count=cc,
                chat_session_count=sc,
                compensation_visible=compensation_visible,
            )
            for p, cc, sc in rows
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "updated_at": last.updated_at.astimezone(UTC).isoformat(),
                    "id": str(last.id),
                },
            )
            if has_more and last is not None
            else None
        ),
    )


@router.post("", response_model=PositionOut, status_code=status.HTTP_201_CREATED)
async def create_position(
    body: PositionCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_writer),
) -> PositionOut:
    # ``title`` is mandatory and every content field is unstructured.  Requiring
    # compensation authority for creation is the only classifier-independent
    # way to prevent a restricted writer from changing pay semantics through an
    # unknown phrase or custom JSON key.
    await _require_position_compensation(db, user)
    # When folder_id is given it wins; shadow the folder string to its name so
    # string-based readers stay consistent. Otherwise keep the legacy string.
    folder_str = body.folder
    if body.folder_id is not None:
        folder = await _resolve_folder(db, user, body.folder_id)
        folder_str = folder.name

    position_id = uuid4()
    if body.status == "open":
        await record_usage(
            db,
            organization_id=user.organization_id,
            metric="active_jobs",
            amount=1,
            kind="usage",
            idempotency_key=f"active-job:create:{position_id}",
            resource_type="position",
            resource_id=position_id,
        )
    p = Position(
        id=position_id,
        organization_id=user.organization_id,
        title=body.title,
        description=body.description,
        full_description=body.full_description,
        requirements=body.requirements,
        status=body.status,
        folder=folder_str,
        folder_id=body.folder_id,
        sensitivity=body.sensitivity,
        created_by=user.id,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _to_out(
        p,
        compensation_visible=await _can_view_position_compensation(db, user),
    )


@router.get("/{position_id}", response_model=PositionDetail)
async def get_position(
    position_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_reader),
) -> PositionDetail:
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

    candidate_count = int(
        (
            await db.execute(
                select(func.count(Application.id))
                .join(Candidate, Candidate.id == Application.candidate_id)
                .where(
                    Application.position_id == position_id,
                    Application.organization_id == user.organization_id,
                    Application.closed_at.is_(None),
                    Application.archived_at.is_(None),
                    Candidate.merged_at.is_(None),
                )
            )
        ).scalar_one()
    )
    return PositionDetail(
        **_to_out(
            p,
            candidate_count=candidate_count,
            compensation_visible=await _can_view_position_compensation(db, user),
        ).model_dump()
    )


@router.get(
    "/{position_id}/applications",
    response_model=CursorPage[RankedCandidate],
)
async def list_position_applications(
    position_id: UUID,
    candidate_id: Annotated[list[UUID] | None, Query(max_length=4)] = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_reader),
) -> CursorPage[RankedCandidate]:
    """Return the active candidate pipeline as a stable, bounded page."""

    exists = (
        await db.execute(
            select(Position.id).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")

    scores_visible = await can_access_field(db, user, FieldClass.ASSESSMENT_RESULT)
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    candidate_ids = tuple(sorted(set(candidate_id or ()), key=str))
    stmt = (
        select(Application, Candidate)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(
            Application.position_id == position_id,
            Application.organization_id == user.organization_id,
            Application.closed_at.is_(None),
            Application.archived_at.is_(None),
            Candidate.merged_at.is_(None),
        )
    )
    rank_score = func.coalesce(Score.overall_score, -1)
    if scores_visible:
        stmt = stmt.add_columns(Score).outerjoin(
            Score,
            and_(
                Score.id == Application.legacy_score_id,
                Score.organization_id == Application.organization_id,
            ),
        )
    if candidate_ids:
        stmt = stmt.where(Candidate.id.in_(candidate_ids))

    namespace = "positions.applications"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "position_id": str(position_id),
            "candidate_ids": ",".join(str(item) for item in candidate_ids) or None,
            "ranking": "assessment_score" if scores_visible else "application_time",
        },
    )
    if cursor is not None:
        if scores_visible:
            payload = decode_collection_cursor(
                cursor,
                namespace=namespace,
                scope=scope,
                boundary_keys={"score", "id"},
                detail="Invalid position applications cursor",
            )
            cursor_score = payload.get("score")
            if not isinstance(cursor_score, int) or isinstance(cursor_score, bool):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    {
                        "code": "cursor_invalid",
                        "detail": "Invalid position applications cursor",
                    },
                )
            cursor_id = cursor_uuid(
                payload,
                detail="Invalid position applications cursor",
            )
            stmt = stmt.where(
                or_(
                    rank_score < cursor_score,
                    and_(rank_score == cursor_score, Application.id < cursor_id),
                )
            )
        else:
            payload = decode_collection_cursor(
                cursor,
                namespace=namespace,
                scope=scope,
                boundary_keys={"applied_at", "id"},
                detail="Invalid position applications cursor",
            )
            cursor_time = cursor_datetime(
                payload,
                "applied_at",
                detail="Invalid position applications cursor",
            )
            cursor_id = cursor_uuid(
                payload,
                detail="Invalid position applications cursor",
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

    ordering = (
        (rank_score.desc(), Application.id.desc())
        if scores_visible
        else (Application.applied_at.desc(), Application.id.desc())
    )
    rows = (await db.execute(stmt.order_by(*ordering).limit(limit + 1))).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        _ranked_candidate_out(
            row[2] if scores_visible else None,
            row[1],
            row[0],
            contact_visible=contact_visible,
        )
        for row in page_rows
    ]
    last_application = page_rows[-1][0] if page_rows else None
    last_score = page_rows[-1][2] if scores_visible and page_rows else None
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary=(
                    {
                        "score": (
                            last_score.overall_score
                            if last_score is not None
                            and last_score.overall_score is not None
                            else -1
                        ),
                        "id": str(last_application.id),
                    }
                    if scores_visible
                    else {
                        "applied_at": last_application.applied_at.astimezone(
                            UTC
                        ).isoformat(),
                        "id": str(last_application.id),
                    }
                ),
            )
            if has_more and last_application is not None
            else None
        ),
    )


@router.patch("/move", response_model=list[PositionOut])
async def move_positions(
    body: MovePositionsRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_writer),
) -> list[PositionOut]:
    """Bulk-move positions into a folder (or to Unsorted when folder_id is null).

    Registered BEFORE /{position_id} so "move" isn't parsed as a UUID. Every
    position id and the target folder are org-validated; the legacy folder
    string is shadow-synced to the target folder name."""
    folder_name: str | None = None
    if body.folder_id is not None:
        folder = await _resolve_folder(db, user, body.folder_id)
        folder_name = folder.name

    rows = (
        (
            await db.execute(
                select(Position).where(
                    Position.id.in_(body.position_ids),
                    Position.organization_id == user.organization_id,
                )
            )
        )
        .scalars()
        .all()
    )
    found = {p.id for p in rows}
    missing = [pid for pid in body.position_ids if pid not in found]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")

    for p in rows:
        p.folder_id = body.folder_id
        p.folder = folder_name
    await db.commit()
    for p in rows:
        await db.refresh(p)
    compensation_visible = await _can_view_position_compensation(db, user)
    return [_to_out(p, compensation_visible=compensation_visible) for p in rows]


@router.patch("/{position_id}", response_model=PositionOut)
async def update_position(
    position_id: UUID,
    body: PositionUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_writer),
) -> PositionOut:
    p = (
        await db.execute(
            select(Position)
            .where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")

    updates = body.model_dump(exclude_unset=True)
    compensation_fields = {
        "title",
        "description",
        "full_description",
        "requirements",
        "internal_notes",
        "folder",
        "candidate_profile",
        "public_description",
    }
    if compensation_fields & updates.keys():
        await _require_position_compensation(db, user)

    next_status = updates.get("status", p.status)
    if next_status != p.status and (next_status == "open" or p.status == "open"):
        activating = next_status == "open"
        transition_id = uuid4()
        await record_usage(
            db,
            organization_id=user.organization_id,
            metric="active_jobs",
            amount=1 if activating else -1,
            kind="usage" if activating else "refund",
            idempotency_key=f"active-job:transition:{transition_id}",
            resource_type="position",
            resource_id=p.id,
            metadata={"from": p.status, "to": next_status},
            enforce_limit=activating,
        )

    # Tightening the sensitivity tier invalidates any public_description that
    # was vetted under the looser one: text approved for `nda` still names the
    # stack and the salary, which `restricted` is specifically meant to hide.
    # Drop it so the public page falls back to showing nothing until someone
    # regenerates — the same fail-closed rule redact_position applies.
    if (
        "sensitivity" in updates
        and "public_description" not in updates
        and _SENSITIVITY_RANK.get(updates["sensitivity"], 0)
        > _SENSITIVITY_RANK.get(p.sensitivity, 0)
    ):
        p.public_description = None

    # folder_id is the source of truth; the legacy `folder` string is its
    # write-synced shadow. Handle both here and remove them from the blind
    # setattr loop so a raw `folder` string in the same body can't clobber the
    # synced name (model_dump emits keys in declaration order: folder, folder_id).
    if "folder_id" in updates:
        fid = updates["folder_id"]
        if fid is not None:
            folder = await _resolve_folder(db, user, fid)
            p.folder = folder.name
        else:
            p.folder = None
        p.folder_id = fid
        updates.pop("folder_id", None)
        updates.pop("folder", None)  # folder_id wins; ignore the raw string
    elif "folder" in updates:
        # Bare string write with no folder_id: detach from the tree and keep
        # the string as a legacy/Unsorted label so the two never diverge.
        p.folder = updates.pop("folder")
        p.folder_id = None

    for field, value in updates.items():
        setattr(p, field, value)
    await db.commit()
    await db.refresh(p)
    return _to_out(
        p,
        compensation_visible=await _can_view_position_compensation(db, user),
    )


class GenerateProfileResponse(BaseModel):
    candidate_profile: str | None = None
    public_description: str | None = None
    # True when the role is confidential but redaction left nothing publishable.
    # The public page then shows no description, which is the safe outcome —
    # the caller should surface this rather than treat it as success.
    public_description_insufficient: bool = False
    # True when the profile generated but the redacted-description call errored.
    # The profile is still saved; the redacted text needs regenerating.
    public_description_failed: bool = False


@router.post("/{position_id}/generate-profile", response_model=GenerateProfileResponse)
async def generate_position_profile(
    position_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_ai_writer),
    _capability: None = Depends(_position_profile_generation_unavailable),
) -> GenerateProfileResponse:
    """(Re)generate the ideal-candidate brief, and the redacted public
    description when the role is confidential. Overwrites both."""

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

    profile = await generate_candidate_profile(
        db,
        p,
        user.organization_id,
        locale=user.locale,
    )
    if profile:
        p.candidate_profile = profile

    confidential = normalize_sensitivity(p.sensitivity) != SENSITIVITY_PUBLIC
    public_text: str | None = None
    generation_partial = False
    if confidential:
        had_source = bool((p.full_description or p.description or "").strip())
        try:
            public_text = await generate_public_description(
                db,
                p,
                user.organization_id,
                locale=user.locale,
            )
        except Exception:
            # The profile above already succeeded and its tokens are already
            # billed and logged. Letting this bubble would roll the whole
            # transaction back and throw both away, so keep the profile and
            # tell the caller the redacted text still needs generating.
            generation_partial = True
            had_source = False
        if public_text:
            p.public_description = public_text
        elif had_source:
            # There WAS source text and redaction left nothing publishable —
            # clear any previous, less-redacted text rather than keep serving it.
            p.public_description = None
        # else: nothing to redact in the first place. Leave whatever a human
        # wrote by hand alone — regenerating must not destroy their work.

    await db.commit()
    await db.refresh(p)
    return GenerateProfileResponse(
        candidate_profile=p.candidate_profile,
        public_description=p.public_description,
        public_description_insufficient=(
            confidential and public_text is None and not generation_partial
        ),
        public_description_failed=generation_partial,
    )


@router.delete("/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_position(
    position_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_writer),
) -> None:
    position = (
        await db.execute(
            select(Position)
            .where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    if position.status == "open":
        await record_usage(
            db,
            organization_id=user.organization_id,
            metric="active_jobs",
            amount=-1,
            kind="refund",
            idempotency_key=f"active-job:delete:{position.id}",
            resource_type="position",
            resource_id=position.id,
            enforce_limit=False,
        )
    position.deleted_at = datetime.now(UTC)
    position.deleted_by_user_id = user.id
    await db.commit()


class RescoreAllResponse(BaseModel):
    queued: int
    message: str


@router.get("/{position_id}/rescore-capability", response_model=ChatCapabilityOut)
async def get_rescore_capability(
    position_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_ai_writer),
) -> ChatCapabilityOut:
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


@router.post(
    "/{position_id}/rescore",
    response_model=RescoreAllResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rescore_all_candidates(
    position_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(position_ai_writer),
) -> RescoreAllResponse:
    """Re-run AI scoring for every candidate already attached to this position.

    Used after a recruiter edits the position description or requirements.
    Each pair becomes a durable leased job with bounded retry and DLQ history.
    """
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

    pair_rows = (
        await db.execute(
            select(Application.candidate_id, Application.id)
            .join(Candidate, Candidate.id == Application.candidate_id)
            .where(
                Application.position_id == position_id,
                Application.organization_id == user.organization_id,
                Application.closed_at.is_(None),
                Application.archived_at.is_(None),
                Candidate.merged_at.is_(None),
            )
        )
    ).all()
    candidate_applications = [(row[0], row[1]) for row in pair_rows]

    request_id = uuid4()
    accepted_job_ids: set[UUID] = set()
    for candidate_id, application_id in candidate_applications:
        job, _created = await enqueue_candidate_scoring_job(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate_id,
            position_id=position_id,
            application_id=application_id,
            actor_user_id=user.id,
            idempotency_key=f"position-rescore:{request_id}:{candidate_id}",
        )
        accepted_job_ids.add(job.id)
    await db.commit()

    return RescoreAllResponse(
        queued=len(accepted_job_ids),
        message=f"Queued re-scoring for {len(accepted_job_ids)} candidate(s).",
    )


@router.patch(
    "/{position_id}/candidates/{candidate_id}/stage",
    response_model=RankedCandidate,
)
async def update_candidate_stage(
    position_id: UUID,
    candidate_id: UUID,
    body: StageUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RankedCandidate:
    """Compatibility adapter over the first-class Application lifecycle."""
    row = (
        await db.execute(
            select(Score, Candidate, Position)
            .join(Candidate, Candidate.id == Score.candidate_id)
            .join(Position, Position.id == Score.position_id)
            .where(
                Score.position_id == position_id,
                Score.candidate_id == candidate_id,
                Score.organization_id == user.organization_id,
                Candidate.merged_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Candidate is not in this position's pipeline.",
        )
    score, candidate, position = row
    had_application = (
        await db.execute(
            select(Application.id).where(
                Application.organization_id == user.organization_id,
                Application.candidate_id == candidate_id,
                Application.position_id == position_id,
            )
        )
    ).first() is not None
    application = await resolve_application_for_write(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        application_id=body.application_id,
        source=candidate.source or "legacy",
        actor_user_id=user.id,
        actor_kind="human",
        reason="legacy_pipeline_link",
    )
    if body.expected_version is None and had_application:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "expected_version is required; refresh the pipeline and retry",
        )
    transition = ApplicationStageTransition(
        stage=body.stage,
        expected_version=body.expected_version or application.updated_at,
        disposition_code=body.disposition_code,
        reason=body.reason,
        details={
            "legacy_route": True,
            "position_title": position.title,
        },
    )
    application, _changed = await apply_application_stage_transition(
        db,
        organization_id=user.organization_id,
        application_id=application.id,
        body=transition,
        actor_user_id=user.id,
        actor_kind="human",
    )
    await db.commit()
    await db.refresh(score)
    await db.refresh(application)
    scores_visible = await can_access_field(db, user, FieldClass.ASSESSMENT_RESULT)
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    return _ranked_candidate_out(
        score if scores_visible else None,
        candidate,
        application,
        contact_visible=contact_visible,
    )
