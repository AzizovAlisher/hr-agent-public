from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.interview import Interview, InterviewQuestion
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.scheduling import InterviewAppointment
from app.models.score import Score
from app.models.user import User
from app.routers.users import list_assignable_users
from app.routers.work import list_my_work, update_candidate_work
from app.schemas.work import CandidateWorkUpdate
from app.services.applications import ensure_application_link

pytestmark = pytest.mark.asyncio


async def seed_application(db, suffix: str = "work"):
    org = Organization(name=f"Org {suffix}", slug=f"org-{suffix}-{uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    owner = User(
        organization_id=org.id,
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    teammate = User(
        organization_id=org.id,
        email=f"teammate-{uuid4().hex[:8]}@example.com",
        password_hash="not-used",
        role="hiring_manager",
        locale="en",
    )
    candidate = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref=f"candidate-{suffix}",
        raw_cv="A sufficiently detailed resume for My Work integration tests. " * 2,
    )
    position = Position(
        organization_id=org.id,
        title=f"Position {suffix}",
        status="open",
        created_by=owner.id,
    )
    db.add_all([owner, teammate, candidate, position])
    await db.flush()
    score = Score(
        organization_id=org.id,
        candidate_id=candidate.id,
        position_id=position.id,
        owner_user_id=owner.id,
        stage="screening",
        overall_score=77,
    )
    db.add(score)
    await db.flush()
    await ensure_application_link(
        db,
        organization_id=org.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=candidate.source,
        owner_user_id=owner.id,
        actor_user_id=owner.id,
        actor_kind="human",
        reason="my_work_test_setup",
    )
    await db.commit()
    return org, owner, teammate, candidate, position, score


async def test_work_update_is_atomic_audited_notified_and_no_op_safe(db):
    org, actor, teammate, candidate, position, score = await seed_application(db)
    score_id = score.id
    due = date.today() + timedelta(days=2)

    result = await update_candidate_work(
        position.id,
        candidate.id,
        CandidateWorkUpdate(owner_user_id=teammate.id, next_action_due_date=due),
        db=db,
        user=actor,
    )
    assert result.owner is not None and result.owner.id == teammate.id
    assert result.next_action_due_date == due

    activities = (
        await db.execute(
            select(CandidateActivity)
            .where(CandidateActivity.candidate_id == candidate.id)
            .order_by(CandidateActivity.event_type)
        )
    ).scalars().all()
    assert [event.event_type for event in activities] == [
        "candidate_owner_changed",
        "next_action_due_changed",
    ]
    assert activities[0].payload["to_owner_user_id"] == str(teammate.id)
    assert "email" not in str(activities[0].payload).lower()

    notifications = (
        await db.execute(
            select(Notification).where(Notification.type == "candidate_assigned")
        )
    ).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].user_id == teammate.id

    # Sending exactly the persisted values is a no-op: no duplicate audit or
    # notification and no independent commit is needed.
    await update_candidate_work(
        position.id,
        candidate.id,
        CandidateWorkUpdate(owner_user_id=teammate.id, next_action_due_date=due),
        db=db,
        user=actor,
    )
    assert len((await db.execute(select(CandidateActivity))).scalars().all()) == 2
    assert len((await db.execute(select(Notification))).scalars().all()) == 1

    # Explicit null clears both fields and remains durable history.
    cleared = await update_candidate_work(
        position.id,
        candidate.id,
        CandidateWorkUpdate(owner_user_id=None, next_action_due_date=None),
        db=db,
        user=actor,
    )
    assert cleared.owner is None
    assert cleared.next_action_due_date is None
    db.expire_all()
    persisted = await db.get(Score, score_id)
    assert persisted is not None and persisted.owner_user_id is None


async def test_work_update_rejects_cross_tenant_owner_and_empty_patch(db):
    _org, actor, _teammate, candidate, position, _score = await seed_application(
        db, "tenant"
    )
    other_org = Organization(name="Other", slug=f"other-{uuid4().hex[:8]}")
    db.add(other_org)
    await db.flush()
    outsider = User(
        organization_id=other_org.id,
        email=f"outside-{uuid4().hex[:8]}@example.com",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    db.add(outsider)
    await db.commit()

    with pytest.raises(HTTPException) as cross_tenant:
        await update_candidate_work(
            position.id,
            candidate.id,
            CandidateWorkUpdate(owner_user_id=outsider.id),
            db=db,
            user=actor,
        )
    assert cross_tenant.value.status_code == 404

    with pytest.raises(HTTPException) as empty:
        await update_candidate_work(
            position.id,
            candidate.id,
            CandidateWorkUpdate(),
            db=db,
            user=actor,
        )
    assert empty.value.status_code == 422


async def test_work_update_compare_and_set_blocks_stale_same_field_only(db):
    _org, actor, teammate, candidate, position, score = await seed_application(
        db, "concurrency"
    )
    score_id = score.id
    teammate_id = teammate.id

    # Another teammate wins the owner edit after this page was loaded.
    application = (
        await db.execute(
            select(Application).where(Application.legacy_score_id == score.id)
        )
    ).scalar_one()
    score.owner_user_id = teammate.id
    application.owner_user_id = teammate.id
    await db.commit()

    # Editing only the due date does not carry or overwrite a stale owner.
    due = date.today() + timedelta(days=4)
    result = await update_candidate_work(
        position.id,
        candidate.id,
        CandidateWorkUpdate(
            next_action_due_date=due,
            expected_next_action_due_date=None,
        ),
        db=db,
        user=actor,
    )
    assert result.owner is not None and result.owner.id == teammate.id
    assert result.next_action_due_date == due

    # A stale compare-and-set on that same owner field is rejected atomically.
    with pytest.raises(HTTPException) as conflict:
        await update_candidate_work(
            position.id,
            candidate.id,
            CandidateWorkUpdate(
                owner_user_id=actor.id,
                expected_owner_user_id=actor.id,
            ),
            db=db,
            user=actor,
        )
    assert conflict.value.status_code == 409

    db.expire_all()
    persisted = await db.get(Score, score_id)
    assert persisted is not None
    assert persisted.owner_user_id == teammate_id
    assert persisted.next_action_due_date == due


async def test_my_work_counts_order_cursor_stale_and_missing_feedback(db):
    org, owner, _teammate, candidate, position, first = await seed_application(
        db, "queue"
    )
    business_timezone = ZoneInfo(get_settings().business_timezone)
    today = datetime.now(business_timezone).date()
    now = datetime.now(UTC)
    first.next_action_due_date = today - timedelta(days=1)
    first.scored_at = now
    first_application = (
        await db.execute(
            select(Application).where(Application.legacy_score_id == first.id)
        )
    ).scalar_one()
    first_application.next_action_due_date = first.next_action_due_date

    candidates = [candidate]
    scores = [first]
    for index, (due, stage, scored_at) in enumerate(
        [
            (today, "interview", now),
            (today + timedelta(days=3), "screening", now),
            (None, "new", now - timedelta(days=10)),
            (today, "hired", now),
        ],
        start=1,
    ):
        candidate_row = Candidate(
            organization_id=org.id,
            source="manual",
            source_ref=f"queue-{index}",
            raw_cv="Resume content long enough for queue testing. " * 2,
            created_at=scored_at,
        )
        db.add(candidate_row)
        await db.flush()
        score = Score(
            organization_id=org.id,
            candidate_id=candidate_row.id,
            position_id=position.id,
            owner_user_id=owner.id,
            next_action_due_date=due,
            stage=stage,
            overall_score=70 + index,
            scored_at=scored_at,
        )
        db.add(score)
        candidates.append(candidate_row)
        scores.append(score)

    await db.flush()
    for candidate_row, score in zip(candidates[1:], scores[1:], strict=True):
        await ensure_application_link(
            db,
            organization_id=org.id,
            candidate_id=candidate_row.id,
            position_id=position.id,
            source=candidate_row.source,
            owner_user_id=owner.id,
            actor_user_id=owner.id,
            actor_kind="human",
            reason="my_work_queue_test_setup",
        )
    interview_link = await ensure_application_link(
        db,
        organization_id=org.id,
        candidate_id=candidates[1].id,
        position_id=position.id,
        source=candidates[1].source,
        owner_user_id=owner.id,
        actor_user_id=owner.id,
        actor_kind="human",
        reason="my_work_feedback_test_setup",
    )
    interview = Interview(
        organization_id=org.id,
        application_id=interview_link.application.id,
        candidate_id=candidates[1].id,
        position_id=position.id,
        created_by=owner.id,
        status="in_progress",
    )
    db.add(interview)
    await db.flush()
    db.add(
        InterviewQuestion(
            organization_id=org.id,
            interview_id=interview.id,
            position=0,
            category="technical",
            question_text="How would you design this service?",
            score=None,
        )
    )
    interview_start = datetime.combine(
        today, time(hour=14), tzinfo=business_timezone
    ).astimezone(UTC)
    db.add(
        InterviewAppointment(
            organization_id=org.id,
            application_id=interview_link.application.id,
            candidate_id=candidates[1].id,
            position_id=position.id,
            interview_id=interview.id,
            organizer_user_id=owner.id,
            status="scheduled",
            starts_at=interview_start,
            ends_at=interview_start + timedelta(hours=1),
            candidate_timezone=get_settings().business_timezone,
            location_mode="video",
            position_title_snapshot=position.title,
            provider="local",
            sync_status="local",
            manage_token_digest=uuid4().hex,
            idempotency_key=uuid4().hex,
        )
    )
    await db.commit()

    first_page = await list_my_work(
        limit=2, cursor=None, stale_days=7, db=db, user=owner
    )
    assert [item.next_action_due_date for item in first_page.items] == [
        today - timedelta(days=1),
        today,
    ]
    assert first_page.next_cursor is not None
    assert first_page.counts.model_dump() == {
        "all": 4,
        "overdue": 1,
        "today": 1,
        "upcoming": 1,
        "undated": 1,
        "interviews_today": 1,
        "stale": 1,
        "missing_feedback": 1,
    }
    assert first_page.items[1].is_missing_feedback is True

    second_page = await list_my_work(
        limit=2,
        cursor=first_page.next_cursor,
        stale_days=7,
        db=db,
        user=owner,
    )
    assert [item.next_action_due_date for item in second_page.items] == [
        today + timedelta(days=3),
        None,
    ]
    assert second_page.items[1].is_stale is True
    assert second_page.next_cursor is None

    stale_page = await list_my_work(
        limit=50,
        cursor=None,
        stale_days=7,
        filter="stale",
        db=db,
        user=owner,
    )
    assert [item.score_id for item in stale_page.items] == [scores[3].id]


async def test_assignable_directory_is_tenant_scoped_for_non_admin(db):
    org, owner, teammate, _candidate, _position, _score = await seed_application(
        db, "directory"
    )
    other_org = Organization(name="Hidden", slug=f"hidden-{uuid4().hex[:8]}")
    db.add(other_org)
    await db.flush()
    outsider = User(
        organization_id=other_org.id,
        email=f"hidden-{uuid4().hex[:8]}@example.com",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    db.add(outsider)
    await db.commit()

    options = await list_assignable_users(db=db, user=owner)
    assert {option.id for option in options.items} == {owner.id, teammate.id}
    assert outsider.id not in {option.id for option in options.items}
    assert all(not hasattr(option, "organization_id") for option in options.items)
