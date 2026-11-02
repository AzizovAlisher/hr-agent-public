"""Interview-prep endpoints: AI generator + rubric scoring (org-scoped)."""

from datetime import UTC
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.dependencies import current_user
from app.db import get_session
from app.models.interview import Interview, InterviewAssignment, InterviewQuestion
from app.models.position import Position
from app.models.user import User
from app.schemas.interviews import (
    GenerateRequest,
    InterviewCompleteRequest,
    InterviewDetail,
    InterviewKitVersionCreate,
    InterviewOut,
    InterviewQuestionOut,
    InterviewUpdate,
    ManualInterviewCreate,
    QuestionScoreRequest,
)
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.enterprise import require_permission
from app.services.interview import (
    InterviewGenerationError,
    create_manual_interview,
    create_manual_interview_kit_version,
    generate_interview,
    recompute_overall,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.structured_hiring import resolve_application_for_write

router = APIRouter(tags=["interviews"], dependencies=[Depends(current_user)])


async def _interview_generation_unavailable() -> None:
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "code": "ai_interview_generation_unavailable",
            "detail": (
                "AI interview generation is unavailable in the pilot until it "
                "runs as a durable, retryable job."
            ),
        },
    )


async def _get_owned_interview(
    interview_id: UUID,
    user: User,
    db: AsyncSession,
    *,
    with_questions: bool = False,
) -> Interview:
    stmt = select(Interview).where(
        Interview.id == interview_id,
        Interview.organization_id == user.organization_id,
    )
    if with_questions:
        stmt = stmt.options(
            selectinload(Interview.questions),
            selectinload(Interview.assignments),
        )
    interview = (await db.execute(stmt)).scalar_one_or_none()
    if interview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found.")
    candidate = await resolve_canonical_candidate(
        db, interview.candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found.")
    if user.role == "interviewer":
        assigned = (
            await db.execute(
                select(InterviewAssignment.id).where(
                    InterviewAssignment.organization_id == user.organization_id,
                    InterviewAssignment.interview_id == interview.id,
                    InterviewAssignment.interviewer_user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if assigned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found.")
    interview.candidate_id = candidate.id
    return interview


def _to_out(interview: Interview, question_count: int | None = None) -> InterviewOut:
    return InterviewOut(
        id=interview.id,
        application_id=interview.application_id,
        candidate_id=interview.candidate_id,
        position_id=interview.position_id,
        status=interview.status,
        notes=interview.notes,
        overall_score=interview.overall_score,
        model_used=interview.model_used,
        kit_version=interview.current_kit_version,
        question_count=(
            question_count
            if question_count is not None
            else len(interview.questions or [])
        ),
        created_at=interview.created_at,
        updated_at=interview.updated_at,
    )


def _to_detail(interview: Interview) -> InterviewDetail:
    return InterviewDetail(
        id=interview.id,
        application_id=interview.application_id,
        candidate_id=interview.candidate_id,
        position_id=interview.position_id,
        status=interview.status,
        notes=interview.notes,
        overall_score=interview.overall_score,
        model_used=interview.model_used,
        kit_version=interview.current_kit_version,
        question_count=len(interview.questions),
        created_at=interview.created_at,
        updated_at=interview.updated_at,
        questions=[InterviewQuestionOut.model_validate(q) for q in interview.questions],
        interviewer_user_ids=sorted(
            (assignment.interviewer_user_id for assignment in interview.assignments),
            key=str,
        ),
    )


@router.post(
    "/candidates/{candidate_id}/interviews",
    response_model=InterviewDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_for_candidate(
    candidate_id: UUID,
    body: ManualInterviewCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewDetail:
    await require_permission(db, user, "interviews.manage")
    interview = await create_manual_interview(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate_id,
        position_id=body.position_id,
        application_id=body.application_id,
        questions_input=body.questions,
        interviewer_user_ids=body.interviewer_user_ids,
        notes=body.notes,
        created_by=user.id,
    )
    return _to_detail(interview)


@router.post(
    "/interviews/{interview_id}/kit-versions",
    response_model=InterviewDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_kit_version(
    interview_id: UUID,
    body: InterviewKitVersionCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewDetail:
    await require_permission(db, user, "interviews.manage")
    interview = await create_manual_interview_kit_version(
        db,
        organization_id=user.organization_id,
        interview_id=interview_id,
        expected_version=body.expected_version,
        questions_input=body.questions,
        created_by=user.id,
    )
    return _to_detail(interview)


@router.post(
    "/candidates/{candidate_id}/interviews/generate",
    response_model=InterviewDetail,
    status_code=status.HTTP_201_CREATED,
)
async def generate_for_candidate(
    candidate_id: UUID,
    body: GenerateRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    _capability: None = Depends(_interview_generation_unavailable),
) -> InterviewDetail:
    # Validate parent rows up front, scoped to caller's org.
    cand = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if cand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    pos = (
        await db.execute(
            select(Position).where(
                Position.id == body.position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if pos is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found.")
    application = await resolve_application_for_write(
        db,
        organization_id=user.organization_id,
        candidate_id=cand.id,
        position_id=pos.id,
        application_id=body.application_id,
        source="interview",
        actor_user_id=user.id,
        actor_kind="human",
        reason="interview_application_link",
    )

    try:
        interview = await generate_interview(
            db,
            cand.id,
            body.position_id,
            application_id=application.id,
            count=body.count,
            created_by=user.id,
            organization_id=user.organization_id,
        )
    except InterviewGenerationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    return _to_detail(interview)


@router.get(
    "/candidates/{candidate_id}/interviews",
    response_model=CursorPage[InterviewOut],
)
async def list_for_candidate(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[InterviewOut]:
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    stmt = (
        select(Interview, func.count(InterviewQuestion.id).label("qcount"))
        .outerjoin(InterviewQuestion, InterviewQuestion.interview_id == Interview.id)
        .where(
            Interview.candidate_id == candidate.id,
            Interview.organization_id == user.organization_id,
        )
        .group_by(Interview.id)
    )
    if user.role == "interviewer":
        stmt = stmt.where(
            exists(
                select(InterviewAssignment.id).where(
                    InterviewAssignment.organization_id == user.organization_id,
                    InterviewAssignment.interview_id == Interview.id,
                    InterviewAssignment.interviewer_user_id == user.id,
                )
            )
        )
    namespace = "interviews.candidate"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "candidate_id": str(candidate.id),
            "interviewer_user_id": str(user.id) if user.role == "interviewer" else None,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid interviews cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid interviews cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid interviews cursor")
        stmt = stmt.where(
            or_(
                Interview.created_at < cursor_time,
                and_(Interview.created_at == cursor_time, Interview.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(Interview.created_at.desc(), Interview.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return CursorPage(
        items=[
            _to_out(interview, question_count) for interview, question_count in rows
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": last.created_at.astimezone(UTC).isoformat(),
                    "id": str(last.id),
                },
            )
            if has_more and last is not None
            else None
        ),
    )


@router.get("/interviews/{interview_id}", response_model=InterviewDetail)
async def get_interview(
    interview_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewDetail:
    interview = await _get_owned_interview(interview_id, user, db, with_questions=True)
    return _to_detail(interview)


@router.patch("/interviews/{interview_id}", response_model=InterviewOut)
async def update_interview(
    interview_id: UUID,
    body: InterviewUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewOut:
    await require_permission(db, user, "interviews.manage")
    interview = await _get_owned_interview(interview_id, user, db, with_questions=True)
    if interview.status in {"scored", "archived"}:
        values = body.model_dump(exclude_unset=True)
        if values and any(
            getattr(interview, key) != value for key, value in values.items()
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Submitted interview feedback is immutable",
            )
    old_status = interview.status
    if body.status is not None:
        transitions = {
            "draft": {"in_progress", "scored", "archived"},
            "in_progress": {"scored", "archived"},
            "scored": {"archived"},
            "archived": set(),
        }
        if body.status != old_status and body.status not in transitions[old_status]:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Invalid interview status transition"
            )
        if body.status == "scored" and any(
            question.is_required and question.score is None
            for question in interview.questions
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Score every required interview question before completing",
            )
        interview.status = body.status
    if body.notes is not None:
        interview.notes = body.notes
    if interview.status != old_status:
        position_title = (
            await db.execute(
                select(Position.title).where(Position.id == interview.position_id)
            )
        ).scalar_one_or_none()
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=interview.candidate_id,
            actor_user_id=user.id,
            application_id=interview.application_id,
            position_id=interview.position_id,
            event_type="interview_status_changed",
            subject_type="interview",
            subject_id=interview.id,
            payload={
                "from_status": old_status,
                "to_status": interview.status,
                "position_title": position_title,
            },
        )
    question_count = len(interview.questions)
    await db.commit()
    await db.refresh(interview)
    return _to_out(interview, question_count)


@router.post(
    "/interviews/{interview_id}/complete",
    response_model=InterviewOut,
)
async def complete_interview(
    interview_id: UUID,
    body: InterviewCompleteRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewOut:
    await require_permission(db, user, "scorecards.submit")
    interview = await _get_owned_interview(interview_id, user, db, with_questions=True)
    if interview.status == "archived":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Archived interview feedback is immutable",
        )
    if interview.status == "scored":
        if body.notes is not None and body.notes != interview.notes:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Submitted interview feedback is immutable",
            )
        return _to_out(interview, len(interview.questions))
    missing = [
        question.id
        for question in interview.questions
        if question.is_required and question.score is None
    ]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "interview_required_questions_unscored",
                "detail": "Score every required interview question before completing",
                "question_ids": [str(question_id) for question_id in missing],
            },
        )
    old_status = interview.status
    interview.status = "scored"
    if body.notes is not None:
        interview.notes = body.notes
    position_title = (
        await db.execute(
            select(Position.title).where(Position.id == interview.position_id)
        )
    ).scalar_one_or_none()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=interview.candidate_id,
        actor_user_id=user.id,
        application_id=interview.application_id,
        position_id=interview.position_id,
        event_type="interview_status_changed",
        subject_type="interview",
        subject_id=interview.id,
        payload={
            "from_status": old_status,
            "to_status": interview.status,
            "position_title": position_title,
        },
    )
    question_count = len(interview.questions)
    await db.commit()
    await db.refresh(interview)
    return _to_out(interview, question_count)


@router.patch(
    "/interviews/{interview_id}/questions/{question_id}",
    response_model=InterviewQuestionOut,
)
async def score_question(
    interview_id: UUID,
    question_id: UUID,
    body: QuestionScoreRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> InterviewQuestionOut:
    await require_permission(db, user, "scorecards.submit")
    interview = await _get_owned_interview(interview_id, user, db, with_questions=True)
    if interview.status in {"scored", "archived"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Submitted interview feedback is immutable",
        )
    q = next(
        (question for question in interview.questions if question.id == question_id),
        None,
    )
    if q is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Question not found.")

    # null score clears the rubric for this question
    if body.score is not None:
        q.score = body.score
    elif body.score is None and "score" in body.model_fields_set:
        q.score = None
    if body.notes is not None:
        q.notes = body.notes

    await db.flush()

    # Recompute parent's overall + bump status if this is the first scored Q
    old_status = interview.status
    interview.overall_score = recompute_overall(interview)
    if interview.overall_score is not None and interview.status == "draft":
        interview.status = "in_progress"

    if interview.status != old_status:
        position_title = (
            await db.execute(
                select(Position.title).where(Position.id == interview.position_id)
            )
        ).scalar_one_or_none()
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=interview.candidate_id,
            actor_user_id=user.id,
            application_id=interview.application_id,
            position_id=interview.position_id,
            event_type="interview_status_changed",
            subject_type="interview",
            subject_id=interview.id,
            payload={
                "from_status": old_status,
                "to_status": interview.status,
                "position_title": position_title,
            },
        )

    await db.commit()
    await db.refresh(q)
    return InterviewQuestionOut.model_validate(q)


@router.delete("/interviews/{interview_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_interview(
    interview_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await require_permission(db, user, "interviews.manage")
    interview = await _get_owned_interview(interview_id, user, db)
    position_title = (
        await db.execute(
            select(Position.title).where(Position.id == interview.position_id)
        )
    ).scalar_one_or_none()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=interview.candidate_id,
        actor_user_id=user.id,
        application_id=interview.application_id,
        position_id=interview.position_id,
        event_type="interview_deleted",
        subject_type="interview",
        subject_id=interview.id,
        payload={
            "status": interview.status,
            "overall_score": interview.overall_score,
            "position_title": position_title,
        },
    )
    await db.delete(interview)
    await db.commit()
