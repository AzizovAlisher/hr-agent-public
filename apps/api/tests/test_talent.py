"""PostgreSQL integration coverage for the Talent CRM backend."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.communication import (
    CandidateContactPoint,
    CommunicationMessage,
    CommunicationTemplate,
)
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.talent import (
    TalentNurtureEnrollment,
    TalentNurtureSequence,
    TalentPool,
    TalentPoolMember,
)
from app.models.user import User
from app.routers.talent import (
    add_talent_pool_members,
    create_nurture_sequence,
    create_talent_pool,
    delete_talent_pool,
    launch_nurture_sequence,
    list_nurture_sequences,
    list_talent_pool_members,
    list_talent_pools,
    rediscover_talent_for_position,
    remove_talent_pool_member,
    search_talent,
    update_talent_pool,
)
from app.schemas.talent import (
    TalentNurtureLaunchRequest,
    TalentNurtureSequenceCreate,
    TalentNurtureStepCreate,
    TalentPoolCreate,
    TalentPoolMembersRequest,
    TalentPoolUpdate,
)
from app.services.candidate_merge import merge_candidates


pytestmark = pytest.mark.asyncio


async def seed_tenant(db, suffix: str):
    organization = Organization(
        name=f"Talent org {suffix}",
        slug=f"talent-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"talent-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(user)
    await db.commit()
    return organization, user


async def add_candidate(
    db,
    organization: Organization,
    suffix: str,
    *,
    raw_cv: str | None = None,
    parsed_cv: dict | None = None,
):
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"talent-candidate-{suffix}-{uuid4().hex[:6]}",
        raw_cv=raw_cv or f"A sufficiently detailed candidate profile for {suffix}. " * 2,
        parsed_cv=parsed_cv,
    )
    db.add(candidate)
    await db.flush()
    return candidate


async def add_email_contact(
    db,
    organization_id: UUID,
    candidate_id: UUID,
    user_id: UUID,
    value: str,
    *,
    opted_out: bool = False,
):
    contact = CandidateContactPoint(
        organization_id=organization_id,
        candidate_id=candidate_id,
        created_by_user_id=user_id,
        channel="email",
        value=value,
        normalized_value=value.lower(),
        is_primary=True,
        is_opted_out=opted_out,
        opted_out_at=datetime.now(UTC) if opted_out else None,
    )
    db.add(contact)
    await db.flush()
    return contact


async def add_email_template(
    db,
    organization_id: UUID,
    user_id: UUID,
    name: str,
    body: str,
):
    template = CommunicationTemplate(
        organization_id=organization_id,
        created_by_user_id=user_id,
        name=name,
        channel="email",
        language="en",
        subject_template="Hello {candidate.first_name}",
        body_template=body,
        is_active=True,
    )
    db.add(template)
    await db.flush()
    return template


async def test_pool_crud_is_canonical_tenant_scoped_atomic_and_idempotent(db):
    organization, user = await seed_tenant(db, "pools")
    other_organization, outsider = await seed_tenant(db, "pools-other")
    primary = await add_candidate(db, organization, "pool-primary")
    alias = await add_candidate(db, organization, "pool-alias")
    untouched = await add_candidate(db, organization, "pool-untouched")
    outside_candidate = await add_candidate(
        db, other_organization, "pool-outsider"
    )
    await db.commit()

    organization_id = organization.id
    user_id = user.id
    outsider_id = outsider.id
    primary_id = primary.id
    alias_id = alias.id
    untouched_id = untouched.id
    outside_candidate_id = outside_candidate.id
    await merge_candidates(
        db,
        organization_id=organization_id,
        actor_user_id=user_id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[alias_id],
        score_winners={},
    )

    pool = await create_talent_pool(
        TalentPoolCreate(
            name="Engineering alumni",
            description="Strong past applicants",
            color="#3366AA",
        ),
        db=db,
        user=user,
    )
    other_pool = await create_talent_pool(
        TalentPoolCreate(name="Engineering alumni"), db=db, user=outsider
    )
    assert pool.id != other_pool.id
    pool_id = pool.id

    with pytest.raises(HTTPException) as duplicate_name:
        await create_talent_pool(
            TalentPoolCreate(name="  engineering ALUMNI  "), db=db, user=user
        )
    assert duplicate_name.value.status_code == 409
    with pytest.raises(ValidationError):
        TalentPoolCreate(name="   ")
    with pytest.raises(ValidationError):
        TalentPoolUpdate(name=None)

    user = await db.get(User, user_id)
    assert user is not None
    added = await add_talent_pool_members(
        pool_id,
        TalentPoolMembersRequest(candidate_ids=[alias_id, primary_id]),
        db=db,
        user=user,
    )
    assert added.added == 1
    assert added.unchanged == 0
    repeated = await add_talent_pool_members(
        pool_id,
        TalentPoolMembersRequest(candidate_ids=[alias_id]),
        db=db,
        user=user,
    )
    assert repeated.added == 0
    assert repeated.unchanged == 1

    with pytest.raises(HTTPException) as cross_tenant_batch:
        await add_talent_pool_members(
            pool_id,
            TalentPoolMembersRequest(
                candidate_ids=[untouched_id, outside_candidate_id]
            ),
            db=db,
            user=user,
        )
    assert cross_tenant_batch.value.status_code == 404
    await db.rollback()
    assert (
        await db.execute(
            select(func.count(TalentPoolMember.id)).where(
                TalentPoolMember.pool_id == pool_id,
                TalentPoolMember.candidate_id == untouched_id,
            )
        )
    ).scalar_one() == 0

    user = await db.get(User, user_id)
    outsider = await db.get(User, outsider_id)
    assert user is not None and outsider is not None
    page = await list_talent_pool_members(
        pool_id, limit=50, cursor=None, db=db, user=user
    )
    assert page.total == 1
    assert [item.id for item in page.items] == [primary_id]
    assert page.items[0].pool_member_id is not None
    with pytest.raises(HTTPException) as hidden_pool:
        await list_talent_pool_members(
            pool_id, limit=50, cursor=None, db=db, user=outsider
        )
    assert hidden_pool.value.status_code == 404
    await db.rollback()

    user = await db.get(User, user_id)
    assert user is not None
    updated = await update_talent_pool(
        pool_id,
        TalentPoolUpdate(description="Updated description", color="#112233"),
        db=db,
        user=user,
    )
    assert updated.description == "Updated description"
    assert updated.color == "#112233"
    assert updated.member_count == 1
    own_pools = await list_talent_pools(db=db, user=user)
    assert [item.id for item in own_pools.items] == [pool_id]

    removed = await remove_talent_pool_member(
        pool_id, alias_id, db=db, user=user
    )
    assert removed.removed == 1
    repeated_remove = await remove_talent_pool_member(
        pool_id, primary_id, db=db, user=user
    )
    assert repeated_remove.unchanged == 1
    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == primary_id,
                    CandidateActivity.event_type.in_(
                        ["talent_pool_added", "talent_pool_removed"]
                    ),
                )
            )
        ).scalars()
    )
    assert sorted(event.event_type for event in events) == [
        "talent_pool_added",
        "talent_pool_removed",
    ]
    assert all(event.payload == {"pool_name": "Engineering alumni"} for event in events)

    await delete_talent_pool(pool_id, db=db, user=user)
    assert await db.get(TalentPool, pool_id) is None


async def test_candidate_merge_deduplicates_pools_and_preserves_enrollment_history(db):
    organization, user = await seed_tenant(db, "merge")
    primary = await add_candidate(db, organization, "merge-primary")
    duplicate = await add_candidate(db, organization, "merge-duplicate")
    pool = TalentPool(
        organization_id=organization.id,
        created_by_user_id=user.id,
        name="Merge-safe pool",
    )
    sequence = TalentNurtureSequence(
        organization_id=organization.id,
        pool_id=None,
        created_by_user_id=user.id,
        name="Merge-safe nurture",
        is_active=True,
    )
    db.add_all([pool, sequence])
    await db.flush()
    primary_member = TalentPoolMember(
        organization_id=organization.id,
        pool_id=pool.id,
        candidate_id=primary.id,
        added_by_user_id=user.id,
        status="paused",
    )
    duplicate_member = TalentPoolMember(
        organization_id=organization.id,
        pool_id=pool.id,
        candidate_id=duplicate.id,
        added_by_user_id=user.id,
        status="converted",
    )
    now = datetime.now(UTC)
    primary_enrollment = TalentNurtureEnrollment(
        organization_id=organization.id,
        sequence_id=sequence.id,
        candidate_id=primary.id,
        started_by_user_id=user.id,
        sequence_name_snapshot=sequence.name,
        status="completed",
        scheduled_count=2,
        started_at=now - timedelta(days=10),
        completed_at=now - timedelta(days=5),
    )
    duplicate_enrollment = TalentNurtureEnrollment(
        organization_id=organization.id,
        sequence_id=sequence.id,
        candidate_id=duplicate.id,
        started_by_user_id=user.id,
        sequence_name_snapshot=sequence.name,
        status="partial",
        scheduled_count=1,
        started_at=now - timedelta(days=3),
    )
    db.add_all(
        [
            primary_member,
            duplicate_member,
            primary_enrollment,
            duplicate_enrollment,
        ]
    )
    await db.commit()

    organization_id = organization.id
    user_id = user.id
    primary_id = primary.id
    duplicate_id = duplicate.id
    pool_id = pool.id
    sequence_id = sequence.id
    primary_enrollment_id = primary_enrollment.id
    duplicate_enrollment_id = duplicate_enrollment.id
    await merge_candidates(
        db,
        organization_id=organization_id,
        actor_user_id=user_id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[duplicate_id],
        score_winners={},
    )
    db.expire_all()

    members = list(
        (
            await db.execute(
                select(TalentPoolMember).where(TalentPoolMember.pool_id == pool_id)
            )
        ).scalars()
    )
    assert len(members) == 1
    assert members[0].candidate_id == primary_id
    assert members[0].status == "converted"

    enrollments = list(
        (
            await db.execute(
                select(TalentNurtureEnrollment).where(
                    TalentNurtureEnrollment.id.in_(
                        [primary_enrollment_id, duplicate_enrollment_id]
                    )
                )
            )
        ).scalars()
    )
    assert len(enrollments) == 2
    assert {item.candidate_id for item in enrollments} == {primary_id}
    assert sum(item.sequence_id == sequence_id for item in enrollments) == 1
    assert sum(item.sequence_id is None for item in enrollments) == 1
    assert {item.status for item in enrollments} == {"completed", "partial"}
    assert {item.scheduled_count for item in enrollments} == {1, 2}

    user = await db.get(User, user_id)
    assert user is not None
    page = await list_talent_pool_members(
        pool_id, limit=50, cursor=None, db=db, user=user
    )
    assert page.total == 1
    assert page.items[0].id == primary_id


async def test_search_and_position_rediscovery_are_explainable_and_alias_aware(db):
    organization, user = await seed_tenant(db, "search")
    other_organization, outsider = await seed_tenant(db, "search-other")
    primary = await add_candidate(
        db,
        organization,
        "search-primary",
        raw_cv="General platform engineering and distributed systems.",
    )
    alias = await add_candidate(
        db,
        organization,
        "search-alias",
        raw_cv="Python Kubernetes event-driven architecture specialist.",
    )
    second = await add_candidate(
        db,
        organization,
        "search-second",
        raw_cv="Python Kubernetes backend engineer with PostgreSQL expertise.",
    )
    third = await add_candidate(
        db,
        organization,
        "search-third",
        raw_cv="Python data platform engineer and analyst.",
    )
    outside_candidate = await add_candidate(
        db,
        other_organization,
        "search-outside",
        raw_cv="Python Kubernetes engineer from another tenant.",
    )
    outside_position = Position(
        organization_id=other_organization.id,
        title="Outside Kubernetes role",
        status="open",
    )
    db.add(outside_position)
    await db.commit()

    organization_id = organization.id
    user_id = user.id
    primary_id = primary.id
    alias_id = alias.id
    second_id = second.id
    third_id = third.id
    outside_candidate_id = outside_candidate.id
    outside_position_id = outside_position.id
    await merge_candidates(
        db,
        organization_id=organization_id,
        actor_user_id=user_id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[alias_id],
        score_winners={},
    )

    user = await db.get(User, user_id)
    assert user is not None
    limited = await search_talent(
        q="Python Kubernetes", limit=1, pool_id=None, db=db, user=user
    )
    assert limited.total == 2
    assert len(limited.items) == 1
    assert limited.next_cursor is not None
    continued = await search_talent(
        q="Python Kubernetes",
        limit=1,
        pool_id=None,
        cursor=limited.next_cursor,
        db=db,
        user=user,
    )
    assert continued.total == 2
    assert continued.next_cursor is None
    assert {limited.items[0].id, continued.items[0].id} == {
        primary_id,
        second_id,
    }
    full = await search_talent(
        q="Python Kubernetes", limit=20, pool_id=None, db=db, user=user
    )
    assert {item.id for item in full.items} == {primary_id, second_id}
    assert alias_id not in {item.id for item in full.items}
    assert outside_candidate_id not in {item.id for item in full.items}
    primary_match = next(item for item in full.items if item.id == primary_id)
    assert primary_match.relevance is not None and primary_match.relevance > 0
    assert set(primary_match.match_reasons) == {"python", "kubernetes"}
    with pytest.raises(HTTPException) as blank_query:
        await search_talent(q="   ", limit=20, pool_id=None, db=db, user=user)
    assert blank_query.value.status_code == 422

    pool = await create_talent_pool(
        TalentPoolCreate(name="Search pool"), db=db, user=user
    )
    await add_talent_pool_members(
        pool.id,
        TalentPoolMembersRequest(candidate_ids=[primary_id]),
        db=db,
        user=user,
    )
    pooled = await search_talent(
        q="Python Kubernetes", limit=20, pool_id=pool.id, db=db, user=user
    )
    assert pooled.total == 1
    assert [item.id for item in pooled.items] == [primary_id]
    with pytest.raises(HTTPException) as cross_filter_cursor:
        await search_talent(
            q="Python Kubernetes",
            limit=1,
            pool_id=pool.id,
            cursor=limited.next_cursor,
            db=db,
            user=user,
        )
    assert cross_filter_cursor.value.status_code == 422

    position = Position(
        organization_id=organization_id,
        title="Kubernetes Python Engineer",
        description="Build reliable cloud services",
        requirements={
            "skills": ["Python", "Kubernetes", "PostgreSQL"],
            "location": "Dushanbe",
        },
        status="open",
        created_by=user_id,
    )
    db.add(position)
    await db.flush()
    db.add(
        Score(
            organization_id=organization_id,
            candidate_id=primary_id,
            position_id=position.id,
            overall_score=88,
            stage="screening",
        )
    )
    await db.commit()
    position_id = position.id

    rediscovered_limited = await rediscover_talent_for_position(
        position_id, limit=1, db=db, user=user
    )
    assert rediscovered_limited.total >= 3
    assert len(rediscovered_limited.items) == 1
    assert rediscovered_limited.next_cursor is not None
    rediscovered_next = await rediscover_talent_for_position(
        position_id,
        limit=1,
        cursor=rediscovered_limited.next_cursor,
        db=db,
        user=user,
    )
    assert rediscovered_next.total == rediscovered_limited.total
    assert rediscovered_next.items[0].id != rediscovered_limited.items[0].id
    rediscovered = await rediscover_talent_for_position(
        position_id, limit=20, db=db, user=user
    )
    ids = {item.id for item in rediscovered.items}
    assert {primary_id, second_id, third_id}.issubset(ids)
    assert outside_candidate_id not in ids
    primary_rediscovered = next(
        item for item in rediscovered.items if item.id == primary_id
    )
    assert primary_rediscovered.already_in_pipeline is True
    assert {"python", "kubernetes"}.issubset(
        set(primary_rediscovered.match_reasons)
    )
    assert "dushanbe" not in primary_rediscovered.match_reasons

    outsider = await db.get(User, outsider.id)
    assert outsider is not None
    with pytest.raises(HTTPException) as hidden_position:
        await rediscover_talent_for_position(
            position_id, limit=20, db=db, user=outsider
        )
    assert hidden_position.value.status_code == 404
    with pytest.raises(HTTPException) as other_hidden_position:
        await rediscover_talent_for_position(
            outside_position_id, limit=20, db=db, user=user
        )
    assert other_hidden_position.value.status_code == 404


async def test_nurture_scheduling_opt_outs_idempotency_and_deleted_templates(db):
    organization, user = await seed_tenant(db, "nurture")
    other_organization, outsider = await seed_tenant(db, "nurture-other")
    active = await add_candidate(
        db,
        organization,
        "nurture-active",
        parsed_cv={"first_name": "Ada", "last_name": "Lovelace"},
    )
    active_alias = await add_candidate(db, organization, "nurture-active-alias")
    opted_out = await add_candidate(db, organization, "nurture-opted-out")
    partial_candidate = await add_candidate(db, organization, "nurture-partial")
    skipped_candidate = await add_candidate(db, organization, "nurture-skipped")
    outside_candidate = await add_candidate(
        db, other_organization, "nurture-outside"
    )
    await add_email_contact(
        db,
        organization.id,
        active.id,
        user.id,
        "ada@example.com",
    )
    await add_email_contact(
        db,
        organization.id,
        opted_out.id,
        user.id,
        "opted@example.com",
        opted_out=True,
    )
    await add_email_contact(
        db,
        organization.id,
        partial_candidate.id,
        user.id,
        "partial@example.com",
    )
    await add_email_contact(
        db,
        organization.id,
        skipped_candidate.id,
        user.id,
        "skipped@example.com",
    )
    first_template = await add_email_template(
        db,
        organization.id,
        user.id,
        "Nurture introduction",
        "Hello {candidate.full_name}, we would like to reconnect.",
    )
    second_template = await add_email_template(
        db,
        organization.id,
        user.id,
        "Nurture follow-up",
        "A short follow-up from {organization.name}.",
    )
    await db.commit()

    organization_id = organization.id
    user_id = user.id
    outsider_id = outsider.id
    active_id = active.id
    active_alias_id = active_alias.id
    opted_out_id = opted_out.id
    partial_candidate_id = partial_candidate.id
    skipped_candidate_id = skipped_candidate.id
    outside_candidate_id = outside_candidate.id
    first_template_id = first_template.id
    second_template_id = second_template.id

    with pytest.raises(HTTPException) as channel_mismatch:
        await create_nurture_sequence(
            TalentNurtureSequenceCreate(
                name="Invalid channel sequence",
                steps=[
                    TalentNurtureStepCreate(
                        channel="telegram",
                        template_id=first_template_id,
                        delay_days=1,
                    )
                ],
            ),
            db=db,
            user=user,
        )
    assert channel_mismatch.value.status_code == 422
    await db.rollback()
    assert (
        await db.execute(
            select(func.count(TalentNurtureSequence.id)).where(
                TalentNurtureSequence.organization_id == organization_id,
                TalentNurtureSequence.name == "Invalid channel sequence",
            )
        )
    ).scalar_one() == 0

    user = await db.get(User, user_id)
    assert user is not None
    pool = await create_talent_pool(
        TalentPoolCreate(name="Nurture pool"), db=db, user=user
    )
    pool_id = pool.id
    await add_talent_pool_members(
        pool_id,
        TalentPoolMembersRequest(candidate_ids=[active_id, opted_out_id]),
        db=db,
        user=user,
    )
    sequence_body = TalentNurtureSequenceCreate(
        name="Alumni nurture",
        pool_id=pool_id,
        steps=[
            TalentNurtureStepCreate(
                channel="email", template_id=first_template_id, delay_days=1
            ),
            TalentNurtureStepCreate(
                channel="email", template_id=second_template_id, delay_days=3
            ),
        ],
    )
    sequence = await create_nurture_sequence(
        sequence_body, db=db, user=user
    )
    sequence_id = sequence.id
    with pytest.raises(HTTPException) as duplicate_sequence:
        await create_nurture_sequence(
            TalentNurtureSequenceCreate(
                name="  alumni NURTURE  ",
                pool_id=pool_id,
                steps=[
                    TalentNurtureStepCreate(
                        channel="email",
                        template_id=first_template_id,
                        delay_days=1,
                    )
                ],
            ),
            db=db,
            user=user,
        )
    assert duplicate_sequence.value.status_code == 409
    with pytest.raises(ValidationError):
        TalentNurtureSequenceCreate(
            name="   ",
            steps=[
                TalentNurtureStepCreate(
                    channel="email", template_id=first_template_id, delay_days=1
                )
            ],
        )

    user = await db.get(User, user_id)
    assert user is not None
    started_before = datetime.now(UTC)
    launched = await launch_nurture_sequence(
        sequence_id,
        TalentNurtureLaunchRequest(),
        db=db,
        user=user,
    )
    assert launched.enrolled == 1
    assert launched.already_enrolled == 0
    assert launched.skipped == 1
    assert launched.messages_scheduled == 2
    messages = list(
        (
            await db.execute(
                select(CommunicationMessage)
                .where(CommunicationMessage.candidate_id == active_id)
                .order_by(CommunicationMessage.scheduled_for)
            )
        ).scalars()
    )
    assert len(messages) == 2
    assert all(message.status == "queued" for message in messages)
    assert messages[0].scheduled_for >= started_before + timedelta(days=1)
    assert messages[0].scheduled_for <= datetime.now(UTC) + timedelta(days=1, minutes=1)
    assert messages[1].scheduled_for >= started_before + timedelta(days=3)
    assert messages[1].scheduled_for <= datetime.now(UTC) + timedelta(days=3, minutes=1)
    assert {message.idempotency_key for message in messages} == {
        f"nurture:{sequence_id}:{active_id}:{sequence.steps[0].id}",
        f"nurture:{sequence_id}:{active_id}:{sequence.steps[1].id}",
    }
    active_enrollment = (
        await db.execute(
            select(TalentNurtureEnrollment).where(
                TalentNurtureEnrollment.sequence_id == sequence_id,
                TalentNurtureEnrollment.candidate_id == active_id,
            )
        )
    ).scalar_one()
    assert active_enrollment.status == "active"
    assert active_enrollment.scheduled_count == 2
    assert (
        await db.execute(
            select(func.count(TalentNurtureEnrollment.id)).where(
                TalentNurtureEnrollment.sequence_id == sequence_id,
                TalentNurtureEnrollment.candidate_id == opted_out_id,
            )
        )
    ).scalar_one() == 0
    nurture_events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == active_id,
                    CandidateActivity.event_type == "nurture_started",
                )
            )
        ).scalars()
    )
    assert len(nurture_events) == 1
    assert nurture_events[0].payload == {
        "sequence_name": "Alumni nurture",
        "step_count": 2,
    }
    assert "body" not in str(nurture_events[0].payload).lower()

    await merge_candidates(
        db,
        organization_id=organization_id,
        actor_user_id=user_id,
        primary_candidate_id=active_id,
        duplicate_candidate_ids=[active_alias_id],
        score_winners={},
    )
    alias_retry = await launch_nurture_sequence(
        sequence_id,
        TalentNurtureLaunchRequest(candidate_ids=[active_alias_id]),
        db=db,
        user=user,
    )
    assert alias_retry.enrolled == 0
    assert alias_retry.already_enrolled == 1
    assert alias_retry.messages_scheduled == 0

    retried = await launch_nurture_sequence(
        sequence_id,
        TalentNurtureLaunchRequest(),
        db=db,
        user=user,
    )
    assert retried.enrolled == 0
    assert retried.already_enrolled == 1
    assert retried.skipped == 1
    assert retried.messages_scheduled == 0
    assert (
        await db.execute(
            select(func.count(CommunicationMessage.id)).where(
                CommunicationMessage.candidate_id == active_id
            )
        )
    ).scalar_one() == 2

    outsider = await db.get(User, outsider_id)
    assert outsider is not None
    with pytest.raises(HTTPException) as hidden_sequence:
        await launch_nurture_sequence(
            sequence_id,
            TalentNurtureLaunchRequest(candidate_ids=[active_id]),
            db=db,
            user=outsider,
        )
    assert hidden_sequence.value.status_code == 404
    await db.rollback()

    user = await db.get(User, user_id)
    assert user is not None
    with pytest.raises(HTTPException) as hidden_candidate:
        await launch_nurture_sequence(
            sequence_id,
            TalentNurtureLaunchRequest(candidate_ids=[outside_candidate_id]),
            db=db,
            user=user,
        )
    assert hidden_candidate.value.status_code == 404
    await db.rollback()

    user = await db.get(User, user_id)
    assert user is not None
    deletion_sequence = await create_nurture_sequence(
        TalentNurtureSequenceCreate(
            name="Deletion-safe nurture",
            steps=[
                TalentNurtureStepCreate(
                    channel="email", template_id=first_template_id, delay_days=2
                ),
                TalentNurtureStepCreate(
                    channel="email", template_id=second_template_id, delay_days=4
                ),
            ],
        ),
        db=db,
        user=user,
    )
    deletion_sequence_id = deletion_sequence.id
    stored_first_template = await db.get(CommunicationTemplate, first_template_id)
    assert stored_first_template is not None
    await db.delete(stored_first_template)
    await db.commit()
    db.expire_all()

    user = await db.get(User, user_id)
    assert user is not None
    listed = await list_nurture_sequences(db=db, user=user)
    retained = next(
        item for item in listed.items if item.id == deletion_sequence_id
    )
    assert retained.steps[0].template_id is None
    assert retained.steps[0].template_name == "Nurture introduction"
    assert retained.steps[1].template_id == second_template_id
    partial = await launch_nurture_sequence(
        deletion_sequence_id,
        TalentNurtureLaunchRequest(candidate_ids=[partial_candidate_id]),
        db=db,
        user=user,
    )
    assert partial.enrolled == 1
    assert partial.skipped == 0
    assert partial.messages_scheduled == 1
    partial_enrollment = (
        await db.execute(
            select(TalentNurtureEnrollment).where(
                TalentNurtureEnrollment.sequence_id == deletion_sequence_id,
                TalentNurtureEnrollment.candidate_id == partial_candidate_id,
            )
        )
    ).scalar_one()
    assert partial_enrollment.status == "partial"
    assert partial_enrollment.scheduled_count == 1

    stored_second_template = await db.get(CommunicationTemplate, second_template_id)
    assert stored_second_template is not None
    await db.delete(stored_second_template)
    await db.commit()
    skipped = await launch_nurture_sequence(
        deletion_sequence_id,
        TalentNurtureLaunchRequest(candidate_ids=[skipped_candidate_id]),
        db=db,
        user=user,
    )
    assert skipped.enrolled == 0
    assert skipped.skipped == 1
    assert skipped.messages_scheduled == 0
    assert (
        await db.execute(
            select(func.count(TalentNurtureEnrollment.id)).where(
                TalentNurtureEnrollment.sequence_id == deletion_sequence_id,
                TalentNurtureEnrollment.candidate_id == skipped_candidate_id,
            )
        )
    ).scalar_one() == 0
