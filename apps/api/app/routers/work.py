"""Personal recruiting queue and candidate×position work ownership."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import get_session
from app.models.candidate import Candidate
from app.models.application import Application
from app.models.candidate_activity import CandidateActivity
from app.models.folder import Folder
from app.models.interview import Interview, InterviewQuestion
from app.models.notification import Notification
from app.models.position import Position
from app.models.scheduling import (
    InterviewAppointment,
    InterviewAppointmentInterviewer,
)
from app.models.score import Score
from app.models.user import User
from app.schemas.work import (
    CandidateWorkOut,
    CandidateWorkUpdate,
    MyWorkCandidateOut,
    MyWorkCountsOut,
    MyWorkItemOut,
    MyWorkPageOut,
    MyWorkPositionOut,
    WorkOwnerOut,
)
from app.services.activities import append_candidate_activity
from app.services.enterprise import can_access_field
from app.services.pagination import (
    collection_cursor_scope,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.structured_hiring import resolve_application_for_write

router = APIRouter(tags=["my-work"], dependencies=[Depends(current_user)])
ACTIVE_STAGES = ("new", "screening", "interview", "offer")
settings = get_settings()


def encode_my_work_cursor(
    due_date: date | None, score_id: UUID, *, scope: str
) -> str:
    return encode_collection_cursor(
        "my-work.queue",
        scope=scope,
        boundary={"due_date": due_date.isoformat() if due_date else None, "id": str(score_id)},
    )


def decode_my_work_cursor(cursor: str, *, scope: str) -> tuple[date | None, UUID]:
    payload = decode_collection_cursor(
        cursor,
        namespace="my-work.queue",
        scope=scope,
        boundary_keys={"due_date", "id"},
        detail="Invalid My Work cursor",
    )
    try:
        raw_due = payload["due_date"]
        return (
            date.fromisoformat(str(raw_due)) if raw_due is not None else None,
            cursor_uuid(payload, detail="Invalid My Work cursor"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Invalid My Work cursor.",
        ) from exc


def _work_expressions(
    *, user_id: UUID, today_start: datetime, tomorrow_start: datetime
):
    latest_activity = (
        select(func.max(CandidateActivity.occurred_at))
        .where(
            CandidateActivity.organization_id == Application.organization_id,
            CandidateActivity.candidate_id == Application.candidate_id,
            or_(
                CandidateActivity.application_id == Application.id,
                and_(
                    CandidateActivity.position_id.is_(None),
                    CandidateActivity.application_id.is_(None),
                ),
            ),
        )
        .correlate(Application)
        .scalar_subquery()
    )
    last_activity = func.coalesce(
        latest_activity, Score.scored_at, Application.applied_at, Candidate.created_at
    )
    missing_feedback = exists(
        select(InterviewQuestion.id)
        .join(Interview, Interview.id == InterviewQuestion.interview_id)
        .where(
            Interview.organization_id == Application.organization_id,
            Interview.application_id == Application.id,
            Interview.candidate_id == Application.candidate_id,
            Interview.position_id == Application.position_id,
            Interview.status == "in_progress",
            InterviewQuestion.score.is_(None),
        )
    )
    is_interviewer = exists(
        select(InterviewAppointmentInterviewer.id).where(
            InterviewAppointmentInterviewer.organization_id
            == Application.organization_id,
            InterviewAppointmentInterviewer.appointment_id == InterviewAppointment.id,
            InterviewAppointmentInterviewer.user_id == user_id,
        )
    )
    interview_starts_at = (
        select(func.min(InterviewAppointment.starts_at))
        .where(
            InterviewAppointment.organization_id == Application.organization_id,
            InterviewAppointment.application_id == Application.id,
            InterviewAppointment.candidate_id == Application.candidate_id,
            InterviewAppointment.position_id == Application.position_id,
            InterviewAppointment.status == "scheduled",
            InterviewAppointment.starts_at >= today_start,
            InterviewAppointment.starts_at < tomorrow_start,
            or_(
                InterviewAppointment.organizer_user_id == user_id,
                is_interviewer,
            ),
        )
        .correlate(Application)
        .scalar_subquery()
    )
    return last_activity, missing_feedback, interview_starts_at


@router.get("/my-work", response_model=MyWorkPageOut)
async def list_my_work(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    stale_days: int = Query(default=7, ge=1, le=90),
    filter: Literal[
        "all",
        "overdue",
        "today",
        "upcoming",
        "undated",
        "interviews",
        "stale",
        "feedback",
    ] = "all",
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> MyWorkPageOut:
    contact_visible = await can_access_field(db, user, FieldClass.CONTACT)
    assessment_results_visible = await can_access_field(
        db, user, FieldClass.ASSESSMENT_RESULT
    )
    business_timezone = ZoneInfo(settings.business_timezone)
    today = datetime.now(business_timezone).date()
    today_start = datetime.combine(today, time.min, tzinfo=business_timezone).astimezone(UTC)
    tomorrow_start = datetime.combine(
        today + timedelta(days=1), time.min, tzinfo=business_timezone
    ).astimezone(UTC)
    # Anchor the stale cutoff to the business-day boundary so every page in a
    # cursor traversal evaluates the same cohort throughout that day.
    stale_before = today_start - timedelta(days=stale_days)
    last_activity, missing_feedback, interview_starts_at = _work_expressions(
        user_id=user.id,
        today_start=today_start,
        tomorrow_start=tomorrow_start,
    )
    common_filters = (
        Application.organization_id == user.organization_id,
        Application.owner_user_id == user.id,
        Application.stage.in_(ACTIVE_STAGES),
        Application.closed_at.is_(None),
        Application.archived_at.is_(None),
        Candidate.organization_id == user.organization_id,
        Candidate.merged_at.is_(None),
        Position.organization_id == user.organization_id,
        Position.status != "closed",
        or_(Position.folder_id.is_(None), Folder.is_archived.is_(False)),
        or_(
            Position.folder.is_(None),
            Position.folder.not_like("\\_%", escape="\\"),
        ),
    )

    stmt = (
        select(
            Application,
            Score,
            Candidate,
            Position,
            last_activity,
            missing_feedback,
            interview_starts_at,
        )
        .select_from(Application)
        .join(Score, Score.id == Application.legacy_score_id)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(Position, Position.id == Application.position_id)
        .outerjoin(Folder, Folder.id == Position.folder_id)
        .where(*common_filters)
    )
    filter_condition = {
        "overdue": Application.next_action_due_date < today,
        "today": Application.next_action_due_date == today,
        "upcoming": Application.next_action_due_date > today,
        "undated": Application.next_action_due_date.is_(None),
        "interviews": interview_starts_at.is_not(None),
        "stale": last_activity < stale_before,
        "feedback": missing_feedback,
    }.get(filter)
    if filter_condition is not None:
        stmt = stmt.where(filter_condition)
    cursor_scope = collection_cursor_scope(
        "my-work.queue",
        organization_id=user.organization_id,
        filters={
            "user_id": str(user.id),
            "filter": filter,
            "stale_days": stale_days,
            "today": today.isoformat(),
            "stale_before": stale_before.isoformat(),
        },
    )
    if cursor:
        cursor_due, cursor_id = decode_my_work_cursor(cursor, scope=cursor_scope)
        if cursor_due is None:
            stmt = stmt.where(
                Application.next_action_due_date.is_(None), Score.id > cursor_id
            )
        else:
            stmt = stmt.where(
                or_(
                    Application.next_action_due_date > cursor_due,
                    Application.next_action_due_date.is_(None),
                    and_(
                        Application.next_action_due_date == cursor_due,
                        Score.id > cursor_id,
                    ),
                )
            )

    rows = (
        await db.execute(
            stmt.order_by(
                Application.next_action_due_date.asc().nulls_last(), Score.id.asc()
            ).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    count_row = (
        await db.execute(
            select(
                func.count(Application.id),
                func.count(Application.id).filter(
                    Application.next_action_due_date < today
                ),
                func.count(Application.id).filter(
                    Application.next_action_due_date == today
                ),
                func.count(Application.id).filter(
                    Application.next_action_due_date > today
                ),
                func.count(Application.id).filter(
                    Application.next_action_due_date.is_(None)
                ),
                func.count(Application.id).filter(interview_starts_at.is_not(None)),
                func.count(Application.id).filter(last_activity < stale_before),
                func.count(Application.id).filter(missing_feedback),
            )
            .select_from(Application)
            .join(Score, Score.id == Application.legacy_score_id)
            .join(Candidate, Candidate.id == Application.candidate_id)
            .join(Position, Position.id == Application.position_id)
            .outerjoin(Folder, Folder.id == Position.folder_id)
            .where(*common_filters)
        )
    ).one()

    items = [
        MyWorkItemOut(
            score_id=score.id,
            candidate=MyWorkCandidateOut(
                id=candidate.id,
                source=candidate.source,
                source_ref=candidate.source_ref if contact_visible else None,
            ),
            position=MyWorkPositionOut(
                id=position.id, title=position.title, status=position.status
            ),
            stage=application.stage,
            overall_score=(
                score.overall_score if assessment_results_visible else None
            ),
            owner=WorkOwnerOut(id=user.id, email=user.email),
            next_action_due_date=application.next_action_due_date,
            last_activity_at=last_activity_value,
            is_stale=last_activity_value < stale_before,
            is_missing_feedback=bool(missing_feedback_value),
            interview_starts_at=interview_start_value,
        )
        for (
            application,
            score,
            candidate,
            position,
            last_activity_value,
            missing_feedback_value,
            interview_start_value,
        ) in page_rows
    ]
    next_cursor = None
    if has_more and page_rows:
        last_application = page_rows[-1][0]
        last_score = page_rows[-1][1]
        next_cursor = encode_my_work_cursor(
            last_application.next_action_due_date,
            last_score.id,
            scope=cursor_scope,
        )

    return MyWorkPageOut(
        items=items,
        counts=MyWorkCountsOut(
            all=count_row[0],
            overdue=count_row[1],
            today=count_row[2],
            upcoming=count_row[3],
            undated=count_row[4],
            interviews_today=count_row[5],
            stale=count_row[6],
            missing_feedback=count_row[7],
        ),
        today=today,
        next_cursor=next_cursor,
    )


@router.patch(
    "/positions/{position_id}/candidates/{candidate_id}/work",
    response_model=CandidateWorkOut,
)
async def update_candidate_work(
    position_id: UUID,
    candidate_id: UUID,
    body: CandidateWorkUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateWorkOut:
    mutable_fields = {"owner_user_id", "next_action_due_date"}
    if not body.model_fields_set & mutable_fields:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "At least one work field is required.",
        )

    row = (
        await db.execute(
            select(Score, Candidate, Position)
            .join(Candidate, Candidate.id == Score.candidate_id)
            .join(Position, Position.id == Score.position_id)
            .where(
                Score.position_id == position_id,
                Score.candidate_id == candidate_id,
                Score.organization_id == user.organization_id,
                Candidate.organization_id == user.organization_id,
                Candidate.merged_at.is_(None),
                Position.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).first()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Candidate is not in this position's pipeline.",
        )
    score, _candidate, position = row
    application = await resolve_application_for_write(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        application_id=body.application_id,
        source=_candidate.source or "legacy",
        actor_user_id=user.id,
        actor_kind="human",
        reason="legacy_work_link",
    )

    if (
        "owner_user_id" in body.model_fields_set
        and "expected_owner_user_id" in body.model_fields_set
        and application.owner_user_id != body.expected_owner_user_id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Candidate owner changed since this page was loaded.",
        )
    if (
        "next_action_due_date" in body.model_fields_set
        and "expected_next_action_due_date" in body.model_fields_set
        and application.next_action_due_date != body.expected_next_action_due_date
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Next-action date changed since this page was loaded.",
        )

    target_owner: User | None = None
    if "owner_user_id" in body.model_fields_set and body.owner_user_id is not None:
        target_owner = (
            await db.execute(
                select(User).where(
                    User.id == body.owner_user_id,
                    User.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if target_owner is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignable user not found.")

    owner_changed = (
        "owner_user_id" in body.model_fields_set
        and application.owner_user_id != body.owner_user_id
    )
    due_changed = (
        "next_action_due_date" in body.model_fields_set
        and application.next_action_due_date != body.next_action_due_date
    )

    if owner_changed:
        old_owner_id = application.owner_user_id
        score.owner_user_id = body.owner_user_id
        application.owner_user_id = body.owner_user_id
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate_id,
            actor_user_id=user.id,
            application_id=application.id,
            position_id=position_id,
            event_type="candidate_owner_changed",
            subject_type="score",
            subject_id=score.id,
            payload={
                "from_owner_user_id": str(old_owner_id) if old_owner_id else None,
                "to_owner_user_id": str(body.owner_user_id) if body.owner_user_id else None,
                "position_title": position.title,
            },
        )
        if body.owner_user_id is not None and body.owner_user_id != user.id:
            due_date = (
                body.next_action_due_date
                if "next_action_due_date" in body.model_fields_set
                else application.next_action_due_date
            )
            db.add(
                Notification(
                    organization_id=user.organization_id,
                    user_id=body.owner_user_id,
                    type="candidate_assigned",
                    payload={
                        "candidate_id": str(candidate_id),
                        "position_id": str(position_id),
                        "position_title": position.title,
                        "assigned_by_user_id": str(user.id),
                        "due_date": due_date.isoformat() if due_date else None,
                    },
                )
            )

    if due_changed:
        old_due_date = application.next_action_due_date
        score.next_action_due_date = body.next_action_due_date
        application.next_action_due_date = body.next_action_due_date
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate_id,
            actor_user_id=user.id,
            application_id=application.id,
            position_id=position_id,
            event_type="next_action_due_changed",
            subject_type="score",
            subject_id=score.id,
            payload={
                "from_due_date": old_due_date.isoformat() if old_due_date else None,
                "to_due_date": (
                    body.next_action_due_date.isoformat()
                    if body.next_action_due_date
                    else None
                ),
                "position_title": position.title,
            },
        )

    if owner_changed or due_changed:
        await db.commit()

    resolved_owner = target_owner
    if application.owner_user_id is not None and (
        resolved_owner is None or resolved_owner.id != application.owner_user_id
    ):
        resolved_owner = (
            await db.execute(
                select(User).where(
                    User.id == application.owner_user_id,
                    User.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
    return CandidateWorkOut(
        candidate_id=candidate_id,
        position_id=position_id,
        application_id=application.id,
        owner=(
            WorkOwnerOut(id=resolved_owner.id, email=resolved_owner.email)
            if resolved_owner
            else None
        ),
        next_action_due_date=application.next_action_due_date,
        stage=application.stage,
    )
