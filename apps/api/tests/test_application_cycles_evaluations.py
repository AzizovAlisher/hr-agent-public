"""Historical application-cycle and immutable evaluation regressions."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models.application import (
    Application,
    ApplicationEvaluation,
    ApplicationStageEvent,
)
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.routers.candidates import get_candidate, list_candidate_applications
from app.routers.positions import get_position, list_position_applications
from app.schemas.applications import (
    ApplicationCreate,
    ApplicationEvaluationCreate,
    ApplicationStageTransition,
)
from app.services.applications import (
    append_application_evaluation,
    create_application,
    delete_application,
    list_application_evaluations,
    list_applications,
    review_application_evaluation,
    transition_application_stage,
)
from app.services.public_applications import submit_public_application

pytestmark = pytest.mark.asyncio


async def seed_pair(db, suffix: str):
    organization = Organization(
        name=f"Cycles {suffix}",
        slug=f"cycles-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"cycles-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"cycles-{suffix}",
        raw_cv="Historical cycle candidate profile with sufficient evidence. " * 2,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Cycle role {suffix}",
        status="open",
    )
    db.add_all([user, candidate, position])
    await db.flush()
    position.created_by = user.id
    await db.commit()
    return organization, user, candidate, position


async def create_cycle(db, user, candidate, position):
    return await create_application(
        db,
        user,
        ApplicationCreate(
            candidate_id=candidate.id,
            position_id=position.id,
            owner_user_id=user.id,
            source="manual",
        ),
    )


async def reject_cycle(db, user, application_id):
    application = await db.get(Application, application_id)
    assert application is not None
    await db.refresh(application)
    return await transition_application_stage(
        db,
        user,
        application.id,
        ApplicationStageTransition(
            stage="rejected",
            expected_version=application.updated_at,
            disposition_code="not_qualified",
            reason="Closed before a later reapplication",
        ),
    )


async def test_reapply_resets_score_and_archive_preserves_every_cycle(db):
    organization, user, candidate, position = await seed_pair(db, "history")
    organization_id = organization.id
    candidate_id = candidate.id
    position_id = position.id
    user_id = user.id

    first = await create_cycle(db, user, candidate, position)
    first_id = first.id
    score = await db.get(Score, first.legacy_score_id)
    assert score is not None
    original_score_id = score.id
    score.overall_score = 91
    score.score_technical_skills = 94
    score.model_used = "historical/model"
    await db.commit()
    await reject_cycle(db, user, first_id)

    assert (await list_applications(db, organization_id)).items == []
    closed_only = (
        await list_applications(
        db, organization_id, include_closed=True
        )
    ).items
    assert [(item.id, item.cycle_number, item.is_active) for item in closed_only] == [
        (first_id, 1, False)
    ]

    user = await db.get(User, user_id)
    candidate = await db.get(Candidate, candidate_id)
    position = await db.get(Position, position_id)
    assert user is not None and candidate is not None and position is not None
    second = await create_cycle(db, user, candidate, position)
    assert second.cycle_number == 2
    assert second.stage == "new"
    assert second.is_active is True
    assert second.legacy_score_id == original_score_id
    old_application = await db.get(Application, first_id)
    reset_score = await db.get(Score, original_score_id)
    assert old_application is not None and reset_score is not None
    assert old_application.legacy_score_id is None
    assert reset_score.stage == "new"
    assert reset_score.overall_score is None
    assert reset_score.score_technical_skills is None
    assert reset_score.model_used is None

    first_event_count = int(
        (
            await db.execute(
                select(func.count(ApplicationStageEvent.id)).where(
                    ApplicationStageEvent.application_id == first_id
                )
            )
        ).scalar_one()
    )
    await delete_application(db, user, second.id)
    await delete_application(db, user, second.id)
    archived = await db.get(Application, second.id)
    assert archived is not None
    assert archived.archived_at is not None
    assert archived.closed_at is not None
    assert archived.archived_by_user_id == user.id
    assert await db.get(Score, original_score_id) is None
    assert int(
        (
            await db.execute(
                select(func.count(ApplicationStageEvent.id)).where(
                    ApplicationStageEvent.application_id == first_id
                )
            )
        ).scalar_one()
    ) == first_event_count

    archived_activity_count = int(
        (
            await db.execute(
                select(func.count(CandidateActivity.id)).where(
                    CandidateActivity.subject_id == second.id,
                    CandidateActivity.event_type == "application_archived",
                )
            )
        ).scalar_one()
    )
    assert archived_activity_count == 1

    user = await db.get(User, user_id)
    candidate = await db.get(Candidate, candidate_id)
    position = await db.get(Position, position_id)
    assert user is not None and candidate is not None and position is not None
    third = await create_cycle(db, user, candidate, position)
    assert third.cycle_number == 3
    assert third.legacy_score_id != original_score_id

    assert [
        item.id for item in (await list_applications(db, organization_id)).items
    ] == [
        third.id
    ]
    assert {
        item.id
        for item in (
            await list_applications(
                db, organization_id, include_closed=True, include_archived=True
            )
        ).items
    } == {first_id, second.id, third.id}


async def test_concurrent_reapply_allows_exactly_one_active_cycle(db):
    organization, user, candidate, position = await seed_pair(db, "concurrency")
    first = await create_cycle(db, user, candidate, position)
    await reject_cycle(db, user, first.id)
    organization_id = organization.id
    user_id = user.id
    candidate_id = candidate.id
    position_id = position.id

    async def reapply_once():
        async with SessionLocal() as session:
            actor = await session.get(User, user_id)
            assert actor is not None
            try:
                result = await create_application(
                    session,
                    actor,
                    ApplicationCreate(
                        candidate_id=candidate_id,
                        position_id=position_id,
                        source="manual",
                    ),
                )
                return result.id
            except HTTPException as exc:
                return exc.status_code

    outcomes = await asyncio.gather(reapply_once(), reapply_once())
    assert sum(not isinstance(item, int) for item in outcomes) == 1
    assert outcomes.count(409) == 1
    applications = list(
        (
            await db.execute(
                select(Application)
                .where(Application.organization_id == organization_id)
                .order_by(Application.cycle_number)
            )
        ).scalars()
    )
    assert [item.cycle_number for item in applications] == [1, 2]
    assert sum(item.closed_at is None and item.archived_at is None for item in applications) == 1


async def test_public_reapply_creates_a_new_cycle_but_active_replay_is_idempotent(db):
    organization, user, _candidate, position = await seed_pair(db, "public-reapply")

    async def apply(email: str, phone: str, idempotency_key: str):
        return await submit_public_application(
            position=position,
            name="Public Reapplicant",
            phone=phone,
            email=email,
            source="company_website",
            file=None,
            raw_text="Experienced engineer with a complete public application profile. " * 2,
            idempotency_key=idempotency_key,
            db=db,
        )

    first = await apply(
        "reapply@example.test", "+992900123456", "public-cycle-one"
    )
    await reject_cycle(db, user, first.application_id)
    closed_replay = await apply(
        "different-payload@example.test", "+992900999999", "public-cycle-one"
    )
    reapplied = await apply(
        "REAPPLY@example.test", "992 900 123 456", "public-cycle-two"
    )
    replay = await apply(
        "reapply@example.test", "+992900123456", "public-cycle-two"
    )

    assert closed_replay.application_created is False
    assert closed_replay.application_id == first.application_id
    assert reapplied.candidate_id == first.candidate_id
    assert reapplied.application_created is True
    assert reapplied.application_id != first.application_id
    assert replay.application_created is False
    assert replay.application_id == reapplied.application_id
    cycles = list(
        (
            await db.execute(
                select(Application)
                .where(
                    Application.organization_id == organization.id,
                    Application.candidate_id == first.candidate_id,
                    Application.position_id == position.id,
                )
                .order_by(Application.cycle_number)
            )
        ).scalars()
    )
    assert [item.cycle_number for item in cycles] == [1, 2]
    assert cycles[0].closed_at is not None
    assert cycles[0].legacy_score_id is None
    assert cycles[1].closed_at is None
    assert cycles[1].stage == "new"


async def test_evaluations_are_tenant_safe_versioned_and_append_only(db):
    organization, user, candidate, position = await seed_pair(db, "evaluations")
    other_organization, _other_user, _other_candidate, _other_position = await seed_pair(
        db, "evaluation-other"
    )
    application = await create_cycle(db, user, candidate, position)
    application_id = application.id
    user_id = user.id
    organization_id = organization.id
    other_organization_id = other_organization.id

    first = await append_application_evaluation(
        db,
        organization_id=organization_id,
        application_id=application.id,
        body=ApplicationEvaluationCreate(
            kind="ai_scoring",
            evidence={"rubric": "structured evidence"},
            scores={"overall": 82},
            provenance={"model": "private/model-a", "prompt_version": "v1"},
        ),
        actor_user_id=user.id,
        actor_kind="ai",
    )
    second = await append_application_evaluation(
        db,
        organization_id=organization_id,
        application_id=application.id,
        body=ApplicationEvaluationCreate(
            kind="ai_scoring",
            evidence={"rubric": "new evidence"},
            scores={"overall": 86},
            provenance={"model": "private/model-b", "prompt_version": "v1"},
        ),
        actor_user_id=user.id,
        actor_kind="ai",
    )
    deterministic = await append_application_evaluation(
        db,
        organization_id=organization_id,
        application_id=application.id,
        body=ApplicationEvaluationCreate(
            kind="deterministic_scoring",
            evidence={"ai_evaluation_id": str(second.id)},
            scores={"overall": 86},
            provenance={"calculator": "weighted-rubric-v1"},
        ),
        actor_user_id=user.id,
        actor_kind="system",
    )
    first_id = first.id
    second_id = second.id
    assert (first.version, second.version, deterministic.version) == (1, 2, 1)
    assert second.supersedes_evaluation_id == first_id

    reviewed = await review_application_evaluation(
        db,
        organization_id=organization_id,
        application_id=application.id,
        evaluation_id=second_id,
        reviewer=user,
        reason="Human review adjusted the aggregate",
        score_overrides={"overall": 84},
    )
    assert reviewed.version == 3
    assert reviewed.status == "reviewed"
    assert reviewed.supersedes_evaluation_id == second_id
    assert reviewed.scores == {"overall": 84}
    await db.commit()

    with pytest.raises(HTTPException) as stale_review:
        await review_application_evaluation(
            db,
            organization_id=organization_id,
            application_id=application.id,
            evaluation_id=first_id,
            reviewer=user,
            reason="Stale review branch",
        )
    assert stale_review.value.status_code == 409

    rows = await list_application_evaluations(
        db,
        organization_id=organization_id,
        application_id=application.id,
    )
    assert [(item.kind, item.version) for item in rows] == [
        ("ai_scoring", 1),
        ("ai_scoring", 2),
        ("deterministic_scoring", 1),
        ("ai_scoring", 3),
    ]
    assert rows[0].scores == {"overall": 82}
    with pytest.raises(HTTPException) as hidden:
        await list_application_evaluations(
            db,
            organization_id=other_organization_id,
            application_id=application.id,
        )
    assert hidden.value.status_code == 404

    persisted_first = await db.get(ApplicationEvaluation, first_id)
    assert persisted_first is not None
    persisted_first.scores = {"overall": 0}
    with pytest.raises(ValueError, match="append-only"):
        await db.flush()
    await db.rollback()
    persisted_first = await db.get(ApplicationEvaluation, first_id)
    assert persisted_first is not None
    assert persisted_first.scores == {"overall": 82}

    user = await db.get(User, user_id)
    assert user is not None
    await delete_application(db, user, application_id)
    archived_rows = await list_application_evaluations(
        db,
        organization_id=organization_id,
        application_id=application_id,
    )
    assert len(archived_rows) == 4


async def test_position_detail_joins_only_the_active_application_cycle(db):
    _organization, user, candidate, position = await seed_pair(db, "position-active")
    first = await create_cycle(db, user, candidate, position)
    await reject_cycle(db, user, first.id)
    active = await create_cycle(db, user, candidate, position)

    detail = await get_position(position.id, db=db, user=user)
    page = await list_position_applications(
        position.id,
        candidate_id=None,
        limit=50,
        cursor=None,
        db=db,
        user=user,
    )

    assert detail.candidate_count == 1
    assert len(page.items) == 1
    assert page.items[0].candidate_id == candidate.id
    assert page.items[0].application_id == active.id


async def test_position_pipeline_is_cursor_paginated_and_filter_bound(db):
    organization, user, first_candidate, position = await seed_pair(db, "position-page")
    candidates = [first_candidate]
    for index in range(2):
        candidate = Candidate(
            organization_id=organization.id,
            source="manual",
            source_ref=f"position-page-{index}",
            raw_cv="Cursor pagination candidate profile with sufficient evidence. " * 2,
        )
        db.add(candidate)
        await db.flush()
        candidates.append(candidate)
    applications = [
        await create_cycle(db, user, candidate, position) for candidate in candidates
    ]
    for score_value, application in zip((91, 64, None), applications, strict=True):
        score = await db.get(Score, application.legacy_score_id)
        assert score is not None
        score.overall_score = score_value
    await db.commit()

    first_page = await list_position_applications(
        position.id,
        candidate_id=None,
        limit=1,
        cursor=None,
        db=db,
        user=user,
    )
    assert [item.overall_score for item in first_page.items] == [91]
    assert first_page.next_cursor is not None
    second_page = await list_position_applications(
        position.id,
        candidate_id=None,
        limit=1,
        cursor=first_page.next_cursor,
        db=db,
        user=user,
    )
    assert [item.overall_score for item in second_page.items] == [64]
    assert second_page.next_cursor is not None
    third_page = await list_position_applications(
        position.id,
        candidate_id=None,
        limit=1,
        cursor=second_page.next_cursor,
        db=db,
        user=user,
    )
    assert [item.overall_score for item in third_page.items] == [None]
    assert third_page.next_cursor is None

    filtered = await list_position_applications(
        position.id,
        candidate_id=[candidates[1].id],
        limit=4,
        cursor=None,
        db=db,
        user=user,
    )
    assert [item.candidate_id for item in filtered.items] == [candidates[1].id]
    with pytest.raises(HTTPException) as mismatched_cursor:
        await list_position_applications(
            position.id,
            candidate_id=[candidates[1].id],
            limit=1,
            cursor=first_page.next_cursor,
            db=db,
            user=user,
        )
    assert mismatched_cursor.value.status_code == 422


async def test_candidate_detail_is_bounded_and_applications_are_cursor_paginated(db):
    organization, user, candidate, position = await seed_pair(db, "candidate-page")
    historical = await create_cycle(db, user, candidate, position)
    historical_score = await db.get(Score, historical.legacy_score_id)
    assert historical_score is not None
    historical_score.overall_score = 99
    await db.commit()
    await reject_cycle(db, user, historical.id)

    current = await create_cycle(db, user, candidate, position)
    current_score = await db.get(Score, current.legacy_score_id)
    assert current_score is not None
    current_score.overall_score = 73

    second_position = Position(
        organization_id=organization.id,
        title="Second candidate-page role",
        status="open",
        created_by=user.id,
    )
    db.add(second_position)
    await db.flush()
    second = await create_cycle(db, user, candidate, second_position)
    second_score = await db.get(Score, second.legacy_score_id)
    assert second_score is not None
    second_score.overall_score = 88
    await db.commit()

    detail = await get_candidate(candidate.id, db=db, user=user)
    assert "scores" not in detail.model_dump()

    first_page = await list_candidate_applications(
        candidate.id,
        limit=1,
        cursor=None,
        db=db,
        user=user,
    )
    assert len(first_page.items) == 1
    assert first_page.next_cursor is not None
    second_page = await list_candidate_applications(
        candidate.id,
        limit=1,
        cursor=first_page.next_cursor,
        db=db,
        user=user,
    )
    assert len(second_page.items) == 1
    assert second_page.next_cursor is None
    page_items = first_page.items + second_page.items
    assert {item.application_id for item in page_items} == {current.id, second.id}
    assert historical.id not in {item.application_id for item in page_items}
    assert {item.overall_score for item in page_items} == {73, 88}
    assert all(item.candidate_id == candidate.id for item in page_items)

    other_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="candidate-page-other",
        raw_cv="Other cursor-scope candidate with sufficient evidence. " * 2,
    )
    db.add(other_candidate)
    await db.commit()
    with pytest.raises(HTTPException) as mismatched_cursor:
        await list_candidate_applications(
            other_candidate.id,
            limit=1,
            cursor=first_page.next_cursor,
            db=db,
            user=user,
        )
    assert mismatched_cursor.value.status_code == 422
