"""Lossless, authorized, exactly-once candidate merge reversal coverage."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_processing import CandidateProcessingJob
from app.models.candidate_merge import CandidateMergeBatch, CandidateMergeBatchMember
from app.models.candidate_note import CandidateTag
from app.models.communication import CandidateContactPoint
from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.interview import Interview
from app.models.operations import UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.provider_assessment import ProviderAssessment
from app.models.requisition import CandidateHandoff, CandidateOffer
from app.models.score import Score
from app.models.scorecard import HiringDecision, ScorecardAssignment
from app.models.talent import TalentPool, TalentPoolMember
from app.models.user import User
from app.models.workflow import WorkflowRun
from app.schemas.applications import (
    ApplicationCreate,
    ApplicationEvaluationCreate,
    ApplicationStageTransition,
)
from app.services import candidate_merge as candidate_merge_service
from app.services.applications import (
    append_application_evaluation,
    create_application,
    transition_application_stage,
)
from app.services.candidate_merge import (
    _capture_merge_graph,
    _state_digest,
    merge_candidates,
    reverse_candidate_merge,
)

pytestmark = pytest.mark.asyncio


def _state_shape_diff(before: dict, after: dict) -> dict[str, object]:
    """Describe changed row/field identities without logging private values."""

    result: dict[str, object] = {}
    for table in sorted(set(before) | set(after)):
        before_rows = before.get(table, {})
        after_rows = after.get(table, {})
        detail: dict[str, object] = {}
        if set(before_rows) != set(after_rows):
            detail["missing"] = sorted(set(before_rows) - set(after_rows))
            detail["extra"] = sorted(set(after_rows) - set(before_rows))
        changed = {
            row_id: sorted(
                field
                for field in set(before_rows[row_id]) | set(after_rows[row_id])
                if before_rows[row_id].get(field) != after_rows[row_id].get(field)
            )
            for row_id in set(before_rows) & set(after_rows)
            if before_rows[row_id] != after_rows[row_id]
        }
        if changed:
            detail["changed_fields"] = changed
        if detail:
            result[table] = detail
    return result


async def _close_application(db, user: User, application: Application) -> None:
    await db.refresh(application)
    await transition_application_stage(
        db,
        user,
        application.id,
        ApplicationStageTransition(
            stage="rejected",
            expected_version=application.updated_at,
            disposition_code="not_qualified",
            reason="Closed historical application before identity merge",
        ),
    )


async def _seed_reversible_graph(db, marker: str):
    organization = Organization(
        name=f"Merge reversal {marker}",
        slug=f"merge-reversal-{marker}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    owner = User(
        organization_id=organization.id,
        email=f"owner-{marker}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    colleague = User(
        organization_id=organization.id,
        email=f"colleague-{marker}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    primary = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"primary-{marker}",
        raw_cv="Canonical CV remains private",
        is_favorite=False,
    )
    alias = Candidate(
        organization_id=organization.id,
        source="referral",
        source_ref=f"alias-{marker}",
        raw_cv="Alias CV remains private",
        email_normalized=f"person-{marker}@example.test",
        is_favorite=True,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Reversible role {marker}",
        status="open",
    )
    db.add_all([owner, colleague, primary, alias, position])
    await db.flush()
    position.created_by = owner.id
    await db.commit()

    primary_first = await create_application(
        db,
        owner,
        ApplicationCreate(
            candidate_id=primary.id,
            position_id=position.id,
            source="manual",
        ),
    )
    await _close_application(db, owner, await db.get(Application, primary_first.id))
    primary_active = await create_application(
        db,
        owner,
        ApplicationCreate(
            candidate_id=primary.id,
            position_id=position.id,
            source="manual",
        ),
    )
    alias_first = await create_application(
        db,
        owner,
        ApplicationCreate(
            candidate_id=alias.id,
            position_id=position.id,
            source="referral",
        ),
    )
    await _close_application(db, owner, await db.get(Application, alias_first.id))
    alias_active = await create_application(
        db,
        owner,
        ApplicationCreate(
            candidate_id=alias.id,
            position_id=position.id,
            source="referral",
        ),
    )
    evaluation = await append_application_evaluation(
        db,
        organization_id=organization.id,
        application_id=alias_first.id,
        body=ApplicationEvaluationCreate(
            kind="manual",
            evidence={"review": "structured evidence"},
            scores={"overall": 84},
            provenance={"source": "panel"},
        ),
        actor_user_id=owner.id,
        actor_kind="human",
        allow_closed=True,
    )
    primary_score = await db.get(Score, primary_active.legacy_score_id)
    alias_score = await db.get(Score, alias_active.legacy_score_id)
    assert primary_score is not None and alias_score is not None
    primary_score.overall_score = 68
    alias_score.overall_score = 93

    primary_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=primary.id,
        label="shared",
        created_by=owner.id,
    )
    alias_shared_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=alias.id,
        label="shared",
        created_by=owner.id,
    )
    alias_unique_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=alias.id,
        label="alias-only",
        created_by=owner.id,
    )
    pool = TalentPool(
        organization_id=organization.id,
        created_by_user_id=owner.id,
        name=f"Pool {marker}",
    )
    db.add_all([primary_tag, alias_shared_tag, alias_unique_tag, pool])
    await db.flush()
    primary_member = TalentPoolMember(
        organization_id=organization.id,
        pool_id=pool.id,
        candidate_id=primary.id,
        added_by_user_id=owner.id,
        status="paused",
    )
    alias_member = TalentPoolMember(
        organization_id=organization.id,
        pool_id=pool.id,
        candidate_id=alias.id,
        added_by_user_id=owner.id,
        status="converted",
    )
    primary_contact = CandidateContactPoint(
        organization_id=organization.id,
        candidate_id=primary.id,
        created_by_user_id=owner.id,
        channel="email",
        value=f"person-{marker}@example.test",
        normalized_value=f"person-{marker}@example.test",
        source="manual",
        is_primary=True,
        is_opted_out=False,
    )
    alias_contact = CandidateContactPoint(
        organization_id=organization.id,
        candidate_id=alias.id,
        created_by_user_id=owner.id,
        channel="email",
        value=f"person-{marker}@example.test",
        normalized_value=f"person-{marker}@example.test",
        source="referral",
        is_primary=False,
        is_opted_out=True,
        opted_out_at=datetime.now(UTC) - timedelta(days=2),
    )
    interview = Interview(
        organization_id=organization.id,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        created_by=owner.id,
        status="draft",
    )
    scorecard = ScorecardAssignment(
        organization_id=organization.id,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        assigned_to_user_id=owner.id,
        assigned_by_user_id=owner.id,
        template_name_snapshot="Structured panel",
        template_version_snapshot=1,
        criteria_snapshot=[{"key": "technical", "required": True}],
        position_title_snapshot=position.title,
        assignee_email_snapshot=owner.email,
        status="draft",
    )
    decision = HiringDecision(
        organization_id=organization.id,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        created_by_user_id=owner.id,
        position_title_snapshot=position.title,
        created_by_email_snapshot=owner.email,
        recommendation="hire",
        required_approvals=1,
        status="pending",
    )
    offer = CandidateOffer(
        organization_id=organization.id,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        created_by_user_id=owner.id,
        position_title_snapshot=position.title,
        subject_template_snapshot="Offer",
        body_template_snapshot="Offer body",
        subject="Offer",
        body="Offer body",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        required_approvals=1,
        status="draft",
    )
    db.add_all(
        [
            primary_member,
            alias_member,
            primary_contact,
            alias_contact,
            interview,
            scorecard,
            decision,
            offer,
        ]
    )
    await db.flush()
    handoff = CandidateHandoff(
        organization_id=organization.id,
        offer_id=offer.id,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        position_title_snapshot=position.title,
        status="pending",
        checklist={"accounts": False},
    )
    processing_job = CandidateProcessingJob(
        organization_id=organization.id,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        created_by_user_id=owner.id,
        kind="score",
        status="queued",
        idempotency_key=f"merge-reversal-score:{alias_active.id}",
        attempt_count=0,
        max_attempts=3,
        available_at=datetime.now(UTC),
        result_summary={},
    )
    workflow_run = WorkflowRun(
        organization_id=organization.id,
        rule_id_snapshot=uuid4(),
        rule_version_snapshot=1,
        candidate_id=alias.id,
        position_id=position.id,
        application_id=alias_active.id,
        rule_name_snapshot="Merge reversal cycle",
        trigger_snapshot="candidate_added",
        conditions_snapshot={"position_id": str(position.id)},
        actions_snapshot=[],
        dedupe_key=f"merge-reversal:{alias_active.id}",
        status="queued",
        action_results=[],
        started_at=datetime.now(UTC),
    )
    provider_connection = IntegrationConnection(
        organization_id=organization.id,
        created_by_user_id=owner.id,
        provider="assessment_generic",
        name=f"Merge assessment {marker}",
        status="connected",
        capabilities=["assessment.invite"],
    )
    db.add(provider_connection)
    await db.flush()
    provider_job = IntegrationJob(
        organization_id=organization.id,
        connection_id=provider_connection.id,
        created_by_user_id=owner.id,
        operation="assessment.invite",
        status="succeeded",
        idempotency_key=f"merge-assessment:{alias_active.id}",
        request_fingerprint="a" * 64,
        reference_payload={"application_id": str(alias_active.id)},
        next_attempt_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db.add(provider_job)
    await db.flush()
    provider_assessment = ProviderAssessment(
        organization_id=organization.id,
        connection_id=provider_connection.id,
        application_id=alias_active.id,
        candidate_id=alias.id,
        invitation_job_id=provider_job.id,
        created_by_user_id=owner.id,
        external_template_ref="merge-reversal-template",
        status="invited",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add_all([handoff, processing_job, workflow_run, provider_assessment])
    await db.commit()
    cycle_activity = (
        await db.execute(
            select(CandidateActivity)
            .where(
                CandidateActivity.organization_id == organization.id,
                CandidateActivity.application_id == alias_active.id,
                CandidateActivity.event_type == "application_created",
            )
            .order_by(CandidateActivity.id)
            .limit(1)
        )
    ).scalar_one()
    return {
        "organization": organization,
        "owner": owner,
        "colleague": colleague,
        "primary": primary,
        "alias": alias,
        "position": position,
        "application_ids": {
            primary_first.id,
            primary_active.id,
            alias_first.id,
            alias_active.id,
        },
        "evaluation_id": evaluation.id,
        "cycle_activity_id": cycle_activity.id,
        "processing_job_id": processing_job.id,
        "provider_assessment_id": provider_assessment.id,
        "workflow_run_id": workflow_run.id,
        "linked_ids": {
            interview.id,
            scorecard.id,
            decision.id,
            offer.id,
            handoff.id,
        },
    }


async def _merge_seeded(db, graph: dict):
    return await merge_candidates(
        db,
        organization_id=graph["organization"].id,
        actor_user_id=graph["owner"].id,
        primary_candidate_id=graph["primary"].id,
        duplicate_candidate_ids=[graph["alias"].id],
        score_winners={graph["position"].id: graph["alias"].id},
    )


async def test_reversal_round_trip_restores_exact_encrypted_hiring_graph(db):
    graph = await _seed_reversible_graph(db, "roundtrip")
    organization_id = graph["organization"].id
    owner_id = graph["owner"].id
    primary_id = graph["primary"].id
    alias_id = graph["alias"].id
    before = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=[primary_id, alias_id],
        lock=False,
    )

    outcome = await _merge_seeded(db, graph)
    batch = await db.get(CandidateMergeBatch, outcome.batch_id)
    assert batch is not None
    assert batch.status == "merged"
    assert batch.snapshot_ciphertext
    assert "person-roundtrip@example.test".encode() not in batch.snapshot_ciphertext
    assert (
        len(
            list(
                (
                    await db.execute(
                        select(CandidateMergeBatchMember).where(
                            CandidateMergeBatchMember.batch_id == batch.id
                        )
                    )
                ).scalars()
            )
        )
        == 2
    )
    assert (
        await db.get(CandidateActivity, graph["cycle_activity_id"])
    ).candidate_id == primary_id
    assert (
        await db.get(CandidateProcessingJob, graph["processing_job_id"])
    ).candidate_id == primary_id
    assert (
        await db.get(ProviderAssessment, graph["provider_assessment_id"])
    ).candidate_id == primary_id
    assert (
        await db.get(WorkflowRun, graph["workflow_run_id"])
    ).candidate_id == primary_id

    result = await reverse_candidate_merge(
        db,
        organization_id=organization_id,
        actor_user_id=owner_id,
        actor_role="recruiter",
        batch_id=batch.id,
        expected_version=1,
    )
    assert result.restored_candidate_ids == (alias_id,)
    restored = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=[primary_id, alias_id],
        tracked=(before,),
        lock=False,
    )
    assert _state_digest(restored) == _state_digest(before), _state_shape_diff(
        before, restored
    )
    assert set(restored["applications"]) == {
        str(value) for value in graph["application_ids"]
    }
    assert str(graph["evaluation_id"]) in restored["application_evaluations"]
    assert len(restored["application_stage_events"]) >= 6
    assert {row["candidate_id"]["v"] for row in restored["applications"].values()} == {
        str(primary_id),
        str(alias_id),
    }
    assert (
        await db.get(CandidateActivity, graph["cycle_activity_id"])
    ).candidate_id == alias_id
    assert (
        await db.get(CandidateProcessingJob, graph["processing_job_id"])
    ).candidate_id == alias_id
    assert (
        await db.get(ProviderAssessment, graph["provider_assessment_id"])
    ).candidate_id == alias_id
    assert (
        await db.get(WorkflowRun, graph["workflow_run_id"])
    ).candidate_id == alias_id

    batch = await db.get(CandidateMergeBatch, batch.id)
    assert batch is not None
    assert batch.status == "reversed"
    assert batch.version == 2
    assert batch.reversed_at is not None


async def test_concurrent_and_double_reverse_succeeds_exactly_once(db):
    graph = await _seed_reversible_graph(db, "concurrent")
    outcome = await _merge_seeded(db, graph)
    organization_id = graph["organization"].id
    owner_id = graph["owner"].id

    async def attempt():
        async with SessionLocal() as session:
            try:
                return await reverse_candidate_merge(
                    session,
                    organization_id=organization_id,
                    actor_user_id=owner_id,
                    actor_role="recruiter",
                    batch_id=outcome.batch_id,
                    expected_version=1,
                )
            except HTTPException as exc:
                return exc

    results = await asyncio.gather(attempt(), attempt())
    assert sum(not isinstance(item, HTTPException) for item in results) == 1
    failures = [item for item in results if isinstance(item, HTTPException)]
    assert [failure.status_code for failure in failures] == [409]
    with pytest.raises(HTTPException) as repeated:
        await reverse_candidate_merge(
            db,
            organization_id=organization_id,
            actor_user_id=owner_id,
            actor_role="recruiter",
            batch_id=outcome.batch_id,
            expected_version=1,
        )
    assert repeated.value.status_code == 409
    assert (
        await db.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.idempotency_key
                == f"candidate:merge-reversal:{outcome.batch_id}"
            )
        )
    ).scalar_one() == 1


async def test_reverse_is_owner_and_tenant_bound(db):
    graph = await _seed_reversible_graph(db, "authorization")
    outcome = await _merge_seeded(db, graph)
    with pytest.raises(HTTPException) as non_owner:
        await reverse_candidate_merge(
            db,
            organization_id=graph["organization"].id,
            actor_user_id=graph["colleague"].id,
            actor_role="recruiter",
            batch_id=outcome.batch_id,
            expected_version=1,
        )
    assert non_owner.value.status_code == 403

    other = Organization(
        name="Other merge tenant", slug=f"other-merge-{uuid4().hex[:8]}"
    )
    db.add(other)
    await db.flush()
    outsider = User(
        organization_id=other.id,
        email=f"outside-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(outsider)
    await db.commit()
    with pytest.raises(HTTPException) as cross_tenant:
        await reverse_candidate_merge(
            db,
            organization_id=other.id,
            actor_user_id=outsider.id,
            actor_role="admin",
            batch_id=outcome.batch_id,
            expected_version=1,
        )
    assert cross_tenant.value.status_code == 404
    assert (await db.get(CandidateMergeBatch, outcome.batch_id)).status == "merged"


async def test_tampered_snapshot_and_mid_restore_failure_leave_graph_unchanged(
    db, monkeypatch
):
    graph = await _seed_reversible_graph(db, "rollback")
    outcome = await _merge_seeded(db, graph)
    organization_id = graph["organization"].id
    member_ids = [graph["primary"].id, graph["alias"].id]
    post_state = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=member_ids,
        lock=False,
    )
    batch = await db.get(CandidateMergeBatch, outcome.batch_id)
    assert batch is not None
    batch_id = batch.id
    owner_id = graph["owner"].id
    original_ciphertext = batch.snapshot_ciphertext
    tampered = bytes([original_ciphertext[0] ^ 1]) + original_ciphertext[1:]
    with pytest.raises(DBAPIError, match="one-time transition"):
        await db.execute(
            update(CandidateMergeBatch.__table__)
            .where(CandidateMergeBatch.__table__.c.id == batch_id)
            .values(snapshot_ciphertext=tampered)
        )
    await db.rollback()
    protected_batch = await db.get(CandidateMergeBatch, batch_id)
    assert protected_batch is not None
    assert protected_batch.snapshot_ciphertext == original_ciphertext
    guard_unchanged = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=member_ids,
        tracked=(post_state,),
        lock=False,
    )
    assert _state_digest(guard_unchanged) == _state_digest(post_state)

    # The production trigger must stay fail-closed. To exercise the service's
    # invalid-AES-GCM path, corrupt the bytes only inside a test-database
    # savepoint. Rolling it back atomically restores both the row and trigger.
    database_name = await db.scalar(text("SELECT current_database()"))
    assert database_name is not None and database_name.endswith("_test")
    corruption_scope = await db.begin_nested()
    try:
        await db.execute(
            text(
                "ALTER TABLE public.candidate_merge_batches "
                "DISABLE TRIGGER trg_candidate_merge_batches_append_only"
            )
        )
        await db.execute(
            update(CandidateMergeBatch.__table__)
            .where(CandidateMergeBatch.__table__.c.id == batch_id)
            .values(snapshot_ciphertext=tampered)
        )
        with pytest.raises(HTTPException) as invalid_snapshot:
            await reverse_candidate_merge(
                db,
                organization_id=organization_id,
                actor_user_id=owner_id,
                actor_role="recruiter",
                batch_id=batch_id,
                expected_version=1,
            )
        assert invalid_snapshot.value.detail == {
            "code": "candidate_merge_snapshot_unavailable"
        }
    finally:
        await corruption_scope.rollback()

    db.expire_all()
    restored_batch = await db.get(CandidateMergeBatch, batch_id)
    assert restored_batch is not None
    assert restored_batch.snapshot_ciphertext == original_ciphertext
    trigger_enabled = await db.scalar(
        text(
            "SELECT tgenabled = 'O' FROM pg_trigger "
            "WHERE tgrelid = 'public.candidate_merge_batches'::regclass "
            "AND tgname = 'trg_candidate_merge_batches_append_only'"
        )
    )
    assert trigger_enabled is True
    decrypt_failure_unchanged = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=member_ids,
        tracked=(post_state,),
        lock=False,
    )
    assert _state_digest(decrypt_failure_unchanged) == _state_digest(post_state)

    original_update = candidate_merge_service._update_full_row
    calls = 0

    async def fail_after_mutation(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original_update(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected restore failure")

    monkeypatch.setattr(
        candidate_merge_service, "_update_full_row", fail_after_mutation
    )
    with pytest.raises(RuntimeError, match="injected restore failure"):
        await reverse_candidate_merge(
            db,
            organization_id=organization_id,
            actor_user_id=owner_id,
            actor_role="recruiter",
            batch_id=batch_id,
            expected_version=1,
        )
    after_failure = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=member_ids,
        tracked=(post_state,),
        lock=False,
    )
    assert _state_digest(after_failure) == _state_digest(post_state)
    persisted = await db.get(CandidateMergeBatch, batch_id)
    assert persisted is not None and persisted.status == "merged"


async def test_candidate_erasure_cascades_encrypted_merge_snapshot(db):
    graph = await _seed_reversible_graph(db, "erasure")
    outcome = await _merge_seeded(db, graph)
    assert await db.get(CandidateMergeBatch, outcome.batch_id) is not None
    await db.execute(
        Candidate.__table__.delete().where(
            Candidate.__table__.c.id == graph["primary"].id,
            Candidate.__table__.c.organization_id == graph["organization"].id,
        )
    )
    await db.commit()
    assert await db.get(CandidateMergeBatch, outcome.batch_id) is None
    assert (
        await db.execute(
            select(func.count(CandidateMergeBatchMember.id)).where(
                CandidateMergeBatchMember.batch_id == outcome.batch_id
            )
        )
    ).scalar_one() == 0
