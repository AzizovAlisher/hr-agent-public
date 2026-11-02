"""Candidate merge regressions for first-class historical applications."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.application import (
    Application,
    ApplicationEvaluation,
    ApplicationStageEvent,
)
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.schemas.applications import (
    ApplicationCreate,
    ApplicationEvaluationCreate,
    ApplicationStageTransition,
)
from app.services.applications import (
    append_application_evaluation,
    create_application,
    transition_application_stage,
)
from app.services.candidate_merge import merge_candidates


pytestmark = pytest.mark.asyncio


async def _reject(db, user: User, application: Application) -> None:
    await db.refresh(application)
    await transition_application_stage(
        db,
        user,
        application.id,
        ApplicationStageTransition(
            stage="rejected",
            expected_version=application.updated_at,
            disposition_code="not_qualified",
            reason="Historical cycle closed before candidate merge",
        ),
    )


async def test_merge_rehomes_and_renumbers_every_cycle_with_one_active_winner(db):
    marker = uuid4().hex[:8]
    organization = Organization(name="Cycle merge", slug=f"cycle-merge-{marker}")
    db.add(organization)
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"cycle-merge-{marker}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    primary = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"primary-{marker}",
    )
    alias = Candidate(
        organization_id=organization.id,
        source="referral",
        source_ref=f"alias-{marker}",
    )
    position = Position(
        organization_id=organization.id,
        title="Merged application history",
        status="open",
    )
    db.add_all([recruiter, primary, alias, position])
    await db.flush()
    position.created_by = recruiter.id
    await db.commit()

    primary_first = await create_application(
        db,
        recruiter,
        ApplicationCreate(
            candidate_id=primary.id,
            position_id=position.id,
            source="manual",
        ),
    )
    await _reject(db, recruiter, await db.get(Application, primary_first.id))
    primary_active = await create_application(
        db,
        recruiter,
        ApplicationCreate(
            candidate_id=primary.id,
            position_id=position.id,
            source="manual",
        ),
    )

    alias_first = await create_application(
        db,
        recruiter,
        ApplicationCreate(
            candidate_id=alias.id,
            position_id=position.id,
            source="referral",
        ),
    )
    await _reject(db, recruiter, await db.get(Application, alias_first.id))
    alias_active = await create_application(
        db,
        recruiter,
        ApplicationCreate(
            candidate_id=alias.id,
            position_id=position.id,
            source="referral",
        ),
    )

    evaluation = await append_application_evaluation(
        db,
        organization_id=organization.id,
        application_id=primary_first.id,
        body=ApplicationEvaluationCreate(
            kind="manual",
            evidence={"decision": "historical evidence"},
            scores={"overall": 70},
            provenance={"source": "structured_review"},
        ),
        actor_user_id=recruiter.id,
        actor_kind="human",
        allow_closed=True,
    )
    primary_score = await db.get(Score, primary_active.legacy_score_id)
    alias_score = await db.get(Score, alias_active.legacy_score_id)
    assert primary_score is not None and alias_score is not None
    primary_score.overall_score = 72
    alias_score.overall_score = 91
    await db.commit()

    application_ids = {
        primary_first.id,
        primary_active.id,
        alias_first.id,
        alias_active.id,
    }
    event_count = int(
        (
            await db.execute(
                select(func.count(ApplicationStageEvent.id)).where(
                    ApplicationStageEvent.application_id.in_(application_ids)
                )
            )
        ).scalar_one()
    )
    primary_id = primary.id
    alias_id = alias.id
    alias_active_id = alias_active.id
    primary_active_id = primary_active.id
    alias_score_id = alias_score.id
    primary_score_id = primary_score.id
    evaluation_id = evaluation.id
    organization_id = organization.id
    position_id = position.id

    await merge_candidates(
        db,
        organization_id=organization_id,
        actor_user_id=recruiter.id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[alias_id],
        score_winners={position_id: alias_id},
    )

    applications = list(
        (
            await db.execute(
                select(Application)
                .where(
                    Application.organization_id == organization_id,
                    Application.position_id == position_id,
                )
                .order_by(Application.cycle_number)
            )
        ).scalars()
    )
    assert {application.id for application in applications} == application_ids
    assert [application.cycle_number for application in applications] == [1, 2, 3, 4]
    assert {application.candidate_id for application in applications} == {primary_id}
    active = [
        application
        for application in applications
        if application.closed_at is None and application.archived_at is None
    ]
    assert [application.id for application in active] == [alias_active_id]
    assert active[0].cycle_number == 4
    assert active[0].legacy_score_id == alias_score_id

    superseded = next(
        application
        for application in applications
        if application.id == primary_active_id
    )
    assert superseded.closed_at is not None
    assert superseded.archived_at is not None
    assert superseded.archived_by_user_id == recruiter.id
    assert superseded.legacy_score_id is None
    assert all(
        "candidate_merge" in application.application_metadata
        for application in applications
    )

    assert (await db.get(Score, alias_score_id)).candidate_id == primary_id
    assert (await db.get(Score, primary_score_id)).candidate_id == alias_id
    assert (await db.get(ApplicationEvaluation, evaluation_id)).application_id == (
        primary_first.id
    )
    assert int(
        (
            await db.execute(
                select(func.count(ApplicationStageEvent.id)).where(
                    ApplicationStageEvent.application_id.in_(application_ids)
                )
            )
        ).scalar_one()
    ) == event_count
