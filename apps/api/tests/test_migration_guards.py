"""Database-only integrity guards that metadata-created tests cannot emulate."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models.application import (
    Application,
    ApplicationEvaluation,
    ApplicationStageEvent,
)
from app.models.candidate import Candidate
from app.models.candidate_merge import (
    CandidateMergeBatch,
    CandidateMergeBatchMember,
)
from app.models.interview import Interview, InterviewKitVersion
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import CandidateOffer, OfferApproval
from app.models.score import Score
from app.models.user import User


pytestmark = pytest.mark.asyncio


async def test_all_tenant_tables_have_privacy_write_gate(db):
    gate_function = await db.scalar(
        text("SELECT to_regprocedure('public.privacy_erasure_gate_key(uuid)')")
    )
    if gate_function is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    missing = list(
        (
            await db.execute(
                text(
                    """
                    SELECT relation.relname
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relkind IN ('r', 'p')
                      AND NOT relation.relispartition
                      AND EXISTS (
                          SELECT 1
                          FROM pg_catalog.pg_attribute AS attribute
                          WHERE attribute.attrelid = relation.oid
                            AND attribute.attname = 'organization_id'
                            AND attribute.attnum > 0
                            AND NOT attribute.attisdropped
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM pg_catalog.pg_trigger AS trigger
                          WHERE trigger.tgrelid = relation.oid
                            AND trigger.tgname = 'trg_00_tenant_privacy_write_gate'
                            AND NOT trigger.tgisinternal
                      )
                    ORDER BY relation.relname
                    """
                )
            )
        ).scalars()
    )
    gated_count = await db.scalar(
        text(
            """
            SELECT count(*)
            FROM pg_catalog.pg_trigger
            WHERE tgname = 'trg_00_tenant_privacy_write_gate'
              AND NOT tgisinternal
            """
        )
    )
    assert gated_count and gated_count > 100
    assert missing == []


async def test_tenant_privacy_write_gate_serializes_without_cross_tenant_blocking(db):
    gate_function = await db.scalar(
        text("SELECT to_regprocedure('public.privacy_erasure_gate_key(uuid)')")
    )
    if gate_function is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Gate A", slug=f"gate-a-{marker}")
    adjacent_organization = Organization(name="Gate B", slug=f"gate-b-{marker}")
    db.add_all([organization, adjacent_organization])
    await db.commit()
    organization_id = organization.id
    adjacent_organization_id = adjacent_organization.id

    async with (
        SessionLocal() as existing_writer,
        SessionLocal() as erasure,
        SessionLocal() as blocked_writer,
        SessionLocal() as adjacent_writer,
    ):
        existing_writer.add(
            Candidate(organization_id=organization_id, source="existing-writer")
        )
        await existing_writer.flush()

        async def acquire_exclusive_erasure_gate() -> None:
            await erasure.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.privacy_erasure_gate_key(organization_id)
                    )
                )
            )

        erasure_lock = asyncio.create_task(acquire_exclusive_erasure_gate())
        await asyncio.sleep(0.05)
        assert not erasure_lock.done(), "erasure must wait for an existing writer"
        await existing_writer.commit()
        await asyncio.wait_for(erasure_lock, timeout=2)

        blocked_writer.add(
            Candidate(organization_id=organization_id, source="blocked-writer")
        )
        with pytest.raises(DBAPIError, match="privacy erasure is in progress") as caught:
            await asyncio.wait_for(blocked_writer.flush(), timeout=2)
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        await blocked_writer.rollback()

        adjacent_writer.add(
            Candidate(
                organization_id=adjacent_organization_id,
                source="adjacent-writer",
            )
        )
        await asyncio.wait_for(adjacent_writer.commit(), timeout=2)

        # The erasure transaction itself can perform the deletes/tombstones
        # guarded by the same key while it owns the exclusive lock.
        erasure.add(Candidate(organization_id=organization_id, source="erasure"))
        await asyncio.wait_for(erasure.flush(), timeout=2)
        await erasure.commit()

    async with SessionLocal() as post_erasure_writer:
        post_erasure_writer.add(
            Candidate(organization_id=organization_id, source="after-erasure")
        )
        await asyncio.wait_for(post_erasure_writer.commit(), timeout=2)


async def test_migrated_offer_approval_allows_only_fixed_privacy_tombstones(db):
    trigger = await db.scalar(
        text(
            "SELECT 1 FROM pg_trigger "
            "WHERE tgname = 'trg_offer_approvals_append_only' "
            "AND NOT tgisinternal"
        )
    )
    gate_function = await db.scalar(
        text("SELECT to_regprocedure('public.privacy_erasure_gate_key(uuid)')")
    )
    if trigger is None or gate_function is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Offer guard", slug=f"offer-guard-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"offer-guard-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(organization_id=organization.id, source="manual")
    db.add_all([user, candidate])
    await db.flush()
    offer = CandidateOffer(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by_user_id=user.id,
        position_title_snapshot="Guarded role",
        subject_template_snapshot="Private subject",
        body_template_snapshot="Private body",
        subject="Private subject",
        body="Private body",
        salary_currency="TJS",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        required_approvals=1,
        approval_cycle=1,
        status="approved",
    )
    db.add(offer)
    await db.flush()
    approval = OfferApproval(
        organization_id=organization.id,
        offer_id=offer.id,
        user_id=user.id,
        user_email_snapshot=user.email,
        offer_version_snapshot=1,
        approval_cycle=1,
        offer_snapshot={
            "subject": "Private subject",
            "body": "Private body",
            "salary_currency": "TJS",
            "version": 1,
        },
        vote="approve",
        comment="Private comment",
    )
    db.add(approval)
    await db.commit()
    approval_id = approval.id
    original_identity = (
        approval.organization_id,
        approval.offer_id,
        approval.user_id,
        approval.user_email_snapshot,
        approval.offer_version_snapshot,
        approval.approval_cycle,
        approval.vote,
        approval.created_at,
        approval.updated_at,
    )

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            text("UPDATE offer_approvals SET comment = 'tampered' WHERE id = :id"),
            {"id": approval_id},
        )
    await db.rollback()

    await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
    await db.execute(
        text(
            """
            UPDATE offer_approvals
            SET comment = NULL,
                offer_snapshot = jsonb_set(
                    jsonb_set(
                        offer_snapshot,
                        '{subject}',
                        to_jsonb('[privacy erased]'::text)
                    ),
                    '{body}',
                    to_jsonb('[privacy erased]'::text)
                )
            WHERE id = :id
            """
        ),
        {"id": approval_id},
    )
    await db.commit()
    redacted = await db.get(OfferApproval, approval_id)
    assert redacted is not None
    assert redacted.comment is None
    assert redacted.offer_snapshot == {
        "subject": "[privacy erased]",
        "body": "[privacy erased]",
        "salary_currency": "TJS",
        "version": 1,
    }
    assert (
        redacted.organization_id,
        redacted.offer_id,
        redacted.user_id,
        redacted.user_email_snapshot,
        redacted.offer_version_snapshot,
        redacted.approval_cycle,
        redacted.vote,
        redacted.created_at,
        redacted.updated_at,
    ) == original_identity

    forbidden_updates = (
        "vote = 'reject'",
        "user_email_snapshot = 'changed@example.test'",
        "updated_at = now() + interval '1 second'",
        "offer_snapshot = offer_snapshot || '{\"extra\": true}'::jsonb",
        "comment = 'restored private text'",
    )
    for assignment in forbidden_updates:
        await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(
                text(f"UPDATE offer_approvals SET {assignment} WHERE id = :id"),
                {"id": approval_id},
            )
        await db.rollback()


async def test_migrated_stage_history_rejects_mutation_but_allows_erasure(db):
    trigger = (
        await db.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_application_stage_events_append_only' "
                "AND NOT tgisinternal"
            )
        )
    ).scalar_one_or_none()
    if trigger is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Guard", slug=f"guard-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"guard-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(user)
    await db.flush()
    candidate = Candidate(organization_id=organization.id, source="manual")
    position = Position(
        organization_id=organization.id, title="Guarded", created_by=user.id
    )
    db.add_all([candidate, position])
    await db.flush()
    application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="new",
        source="manual",
    )
    db.add(application)
    await db.flush()
    event = ApplicationStageEvent(
        organization_id=organization.id,
        application_id=application.id,
        from_stage=None,
        to_stage="new",
        actor_kind="system",
        reason="candidate@example.test",
        details={"candidate_email": "candidate@example.test", "source": "guard"},
    )
    db.add(event)
    organization_id = organization.id
    application_id = application.id
    await db.commit()
    event_id = event.id

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(ApplicationStageEvent.__table__)
            .where(ApplicationStageEvent.id == event_id)
            .values(reason="tampered")
        )
    await db.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            delete(ApplicationStageEvent.__table__).where(
                ApplicationStageEvent.id == event_id
            )
        )
    await db.rollback()

    await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
    await db.execute(
        update(ApplicationStageEvent.__table__)
        .where(ApplicationStageEvent.id == event_id)
        .values(reason=None, details={"privacy_erased": True})
    )
    await db.commit()
    redacted = (
        await db.execute(
            select(
                ApplicationStageEvent.organization_id,
                ApplicationStageEvent.application_id,
                ApplicationStageEvent.from_stage,
                ApplicationStageEvent.to_stage,
                ApplicationStageEvent.actor_kind,
                ApplicationStageEvent.reason,
                ApplicationStageEvent.details,
            ).where(ApplicationStageEvent.id == event_id)
        )
    ).one()
    assert redacted == (
        organization_id,
        application_id,
        None,
        "new",
        "system",
        None,
        {"privacy_erased": True},
    )

    # The erasure scope can remove only free-text PII. It cannot forge stage
    # history or replace the fixed JSON tombstone with arbitrary content.
    await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(ApplicationStageEvent.__table__)
            .where(ApplicationStageEvent.id == event_id)
            .values(to_stage="screening")
        )
    await db.rollback()

    await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(ApplicationStageEvent.__table__)
            .where(ApplicationStageEvent.id == event_id)
            .values(details={"privacy_erased": False})
        )
    await db.rollback()

    await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
    await db.execute(
        delete(ApplicationStageEvent.__table__).where(
            ApplicationStageEvent.id == event_id
        )
    )
    await db.commit()
    assert await db.get(ApplicationStageEvent, event_id) is None


async def test_migrated_evaluations_reject_mutation_but_allow_erasure(db):
    trigger = (
        await db.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_application_evaluations_append_only' "
                "AND NOT tgisinternal"
            )
        )
    ).scalar_one_or_none()
    if trigger is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Evaluation guard", slug=f"evaluation-guard-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"evaluation-guard-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(user)
    await db.flush()
    candidate = Candidate(organization_id=organization.id, source="manual")
    position = Position(
        organization_id=organization.id,
        title="Evaluation guarded",
        created_by=user.id,
    )
    db.add_all([candidate, position])
    await db.flush()
    application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="new",
        source="manual",
    )
    db.add(application)
    await db.flush()
    evaluation = ApplicationEvaluation(
        organization_id=organization.id,
        application_id=application.id,
        kind="manual",
        status="completed",
        version=1,
        evidence={"rubric": "audited"},
        scores={"overall": 80},
        provenance={"source": "test"},
        actor_user_id=user.id,
        actor_kind="human",
    )
    db.add(evaluation)
    await db.commit()
    evaluation_id = evaluation.id
    application_id = application.id
    user_id = user.id

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(ApplicationEvaluation.__table__)
            .where(ApplicationEvaluation.id == evaluation_id)
            .values(scores={"overall": 0})
        )
    await db.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            delete(ApplicationEvaluation.__table__).where(
                ApplicationEvaluation.id == evaluation_id
            )
        )
    await db.rollback()

    archived_at = datetime.now(UTC)
    await db.execute(
        update(Application.__table__)
        .where(Application.id == application_id)
        .values(
            closed_at=archived_at,
            archived_at=archived_at,
            archived_by_user_id=user_id,
        )
    )
    await db.commit()
    await db.execute(delete(User.__table__).where(User.id == user_id))
    await db.commit()
    assert (
        await db.execute(
            select(Application.archived_by_user_id).where(
                Application.id == application_id
            )
        )
    ).scalar_one() is None
    assert (
        await db.execute(
            select(ApplicationEvaluation.actor_user_id).where(
                ApplicationEvaluation.id == evaluation_id
            )
        )
    ).scalar_one() is None

    await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
    await db.execute(
        delete(ApplicationEvaluation.__table__).where(
            ApplicationEvaluation.id == evaluation_id
        )
    )
    await db.commit()
    assert await db.get(ApplicationEvaluation, evaluation_id) is None


async def test_migrated_score_trigger_never_reopens_or_manufactures_a_cycle(db):
    trigger = (
        await db.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_scores_sync_application' "
                "AND NOT tgisinternal"
            )
        )
    ).scalar_one_or_none()
    if trigger is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Score guard", slug=f"score-guard-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"score-guard-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(organization_id=organization.id, source="manual")
    position = Position(organization_id=organization.id, title="Score guarded")
    db.add_all([user, candidate, position])
    await db.flush()
    position.created_by = user.id
    score = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="new",
    )
    db.add(score)
    await db.commit()

    application = (
        await db.execute(
            select(Application).where(Application.legacy_score_id == score.id)
        )
    ).scalar_one()
    score.stage = "rejected"
    await db.commit()
    await db.refresh(application)
    assert application.stage == "rejected"
    assert application.closed_at is not None

    score.stage = "new"
    await db.commit()
    await db.refresh(application)
    assert application.stage == "rejected"
    assert application.closed_at is not None
    assert (
        await db.execute(
            select(func.count(Application.id)).where(
                Application.organization_id == organization.id,
                Application.candidate_id == candidate.id,
                Application.position_id == position.id,
            )
        )
    ).scalar_one() == 1


async def test_structured_hiring_application_and_version_guards_are_database_enforced(
    db,
):
    trigger = (
        await db.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_interviews_validate_application' "
                "AND NOT tgisinternal"
            )
        )
    ).scalar_one_or_none()
    if trigger is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Hiring guard", slug=f"hiring-guard-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"hiring-guard-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(organization_id=organization.id, source="manual")
    other_candidate = Candidate(organization_id=organization.id, source="manual")
    position = Position(organization_id=organization.id, title="Application bound")
    db.add_all([user, candidate, other_candidate, position])
    await db.flush()
    position.created_by = user.id
    application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source="manual",
    )
    wrong_application = Application(
        organization_id=organization.id,
        candidate_id=other_candidate.id,
        position_id=position.id,
        source="manual",
    )
    db.add_all([application, wrong_application])
    await db.flush()
    interview = Interview(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=application.id,
        created_by=user.id,
        status="draft",
    )
    db.add(interview)
    await db.flush()
    kit = InterviewKitVersion(
        organization_id=organization.id,
        interview_id=interview.id,
        version=1,
        questions_snapshot=[],
        created_by_user_id=user.id,
    )
    db.add(kit)
    await db.commit()
    interview_id = interview.id
    application_id = application.id
    wrong_application_id = wrong_application.id
    kit_id = kit.id

    # d96 replaces the legacy trigger-only check with one exact-cycle FK that
    # proves tenant, candidate, position, and application identity together.
    with pytest.raises(DBAPIError, match="fk_interview_exact_application"):
        await db.execute(
            update(Interview.__table__)
            .where(Interview.id == interview_id)
            .values(application_id=wrong_application_id)
        )
        await db.commit()
    await db.rollback()

    with pytest.raises(DBAPIError, match="requires an application link"):
        await db.execute(
            update(Interview.__table__)
            .where(Interview.id == interview_id)
            .values(application_id=None)
        )
    await db.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            update(InterviewKitVersion.__table__)
            .where(InterviewKitVersion.id == kit_id)
            .values(questions_snapshot=[{"tampered": True}])
        )
    await db.rollback()

    # ON DELETE SET NULL is an authorized nested FK transition.  It must not be
    # confused with a caller trying to detach an artifact directly.
    await db.execute(
        delete(Application.__table__).where(Application.id == application_id)
    )
    await db.commit()
    assert (
        await db.execute(
            select(Interview.application_id).where(Interview.id == interview_id)
        )
    ).scalar_one() is None

    # Parent deletion may cascade immutable kit history for approved erasure.
    await db.execute(delete(Interview.__table__).where(Interview.id == interview_id))
    await db.commit()
    assert await db.get(InterviewKitVersion, kit_id) is None


async def test_nullable_tenant_edges_preserve_tenant_identity_on_parent_delete(db):
    repaired_edge_count = (
        await db.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname LIKE 'fk_tenant_%' "
                "AND contype = 'f' "
                "AND cardinality(conkey) = 2 "
                "AND confdeltype = 'n' "
                "AND confdelsetcols IS NOT NULL"
            )
        )
    ).scalar_one()
    if repaired_edge_count == 0:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    # E11 repairs every nullable composite edge present at its schema boundary.
    # d96 intentionally converts eleven application-artifact composite edges
    # from subset SET NULL to CASCADE so unordered parent deletion cannot expose
    # a half-null row to the exact application shape constraints.
    assert repaired_edge_count == 127

    application_artifact_parent_edges = list(
        (
            await db.execute(
                text(
                    "SELECT conname, confdeltype = 'c' "
                    "FROM pg_constraint "
                    "WHERE contype = 'f' "
                    "AND conrelid = ANY (ARRAY["
                    "  'candidate_activities'::regclass,"
                    "  'candidate_processing_jobs'::regclass,"
                    "  'chat_sessions'::regclass,"
                    "  'communication_threads'::regclass,"
                    "  'interview_recordings'::regclass,"
                    "  'interview_scheduling_links'::regclass,"
                    "  'interview_appointments'::regclass,"
                    "  'source_candidate_receipts'::regclass,"
                    "  'task_submissions'::regclass,"
                    "  'workflow_runs'::regclass"
                    "]) "
                    "AND confrelid IN ("
                    "  'candidates'::regclass, 'positions'::regclass"
                    ") ORDER BY conname"
                )
            )
        ).all()
    )
    assert len(application_artifact_parent_edges) == 40
    assert all(row[1] for row in application_artifact_parent_edges)
    scorecard_version_edges = list(
        (
            await db.execute(
                text(
                    "SELECT conname, condeferrable, condeferred, "
                    "       confdelsetcols = ARRAY[conkey[2]]::smallint[] "
                    "FROM pg_constraint "
                    "WHERE conname IN ("
                    "  'scorecard_assignments_template_version_id_fkey',"
                    "  'fk_tenant_scorecard_assignments_template_version_id'"
                    ") ORDER BY conname"
                )
            )
        ).all()
    )
    assert len(scorecard_version_edges) == 2
    assert all(
        row.condeferrable and row.condeferred for row in scorecard_version_edges
    )
    # Only the composite edge carries a subset column list; the ordinary edge
    # has the same SET NULL behavior without one.
    assert scorecard_version_edges[0][3] is True


async def test_candidate_merge_batches_are_one_time_append_only_and_erasure_complete(
    db,
):
    trigger = (
        await db.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_candidate_merge_batches_append_only' "
                "AND NOT tgisinternal"
            )
        )
    ).scalar_one_or_none()
    if trigger is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    marker = uuid4().hex[:8]
    organization = Organization(name="Merge guard", slug=f"merge-guard-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"merge-guard-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    primary = Candidate(organization_id=organization.id, source="manual")
    alias = Candidate(organization_id=organization.id, source="manual")
    other_alias = Candidate(organization_id=organization.id, source="manual")
    db.add_all([user, primary, alias, other_alias])
    await db.flush()
    batch = CandidateMergeBatch(
        organization_id=organization.id,
        primary_candidate_id=primary.id,
        created_by_user_id=user.id,
        member_count=2,
        snapshot_ciphertext=b"encrypted-snapshot-payload",
        snapshot_nonce=b"0123456789ab",
        snapshot_key_id="test-key-v1",
        post_state_sha256="a" * 64,
    )
    db.add(batch)
    await db.flush()
    db.add_all(
        [
            CandidateMergeBatchMember(
                organization_id=organization.id,
                batch_id=batch.id,
                candidate_id=primary.id,
                role="primary",
            ),
            CandidateMergeBatchMember(
                organization_id=organization.id,
                batch_id=batch.id,
                candidate_id=alias.id,
                role="alias",
            ),
        ]
    )
    batch_id = batch.id
    alias_id = alias.id
    other_alias_id = other_alias.id
    organization_id = organization.id
    user_id = user.id
    await db.commit()

    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                CandidateMergeBatchMember.__table__.insert().values(
                        id=uuid4(),
                        organization_id=organization_id,
                        batch_id=batch_id,
                        candidate_id=other_alias_id,
                    role="primary",
                )
            )

    with pytest.raises(DBAPIError, match="one-time transition"):
        await db.execute(
            update(CandidateMergeBatch.__table__)
            .where(CandidateMergeBatch.id == batch_id)
            .values(snapshot_ciphertext=b"tampered-snapshot-payload")
        )
    await db.rollback()

    reversed_at = datetime.now(UTC)
    await db.execute(
        update(CandidateMergeBatch.__table__)
        .where(CandidateMergeBatch.id == batch_id)
        .values(
            status="reversed",
            version=2,
            reversed_at=reversed_at,
            reversed_by_user_id=user_id,
        )
    )
    await db.commit()

    with pytest.raises(DBAPIError):
        await db.execute(
            update(CandidateMergeBatch.__table__)
            .where(CandidateMergeBatch.id == batch_id)
            .values(version=3)
        )
    await db.rollback()

    # Erasing either the primary or an alias destroys the whole encrypted
    # snapshot, preventing deleted candidate data from surviving in reversal
    # history.
    await db.execute(delete(Candidate.__table__).where(Candidate.id == alias_id))
    await db.commit()
    assert await db.get(CandidateMergeBatch, batch_id) is None
