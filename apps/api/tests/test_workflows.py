import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models.application import Application, ApplicationStageEvent
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_note import CandidateTag
from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationMessage,
    CommunicationTemplate,
)
from app.models.notification import Notification
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.scorecard import ScorecardAssignment
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun
from app.routers.workflows import (
    cancel_active_workflow_run,
    create_workflow_rule,
    list_workflow_rules,
    list_workflow_runs,
    pause_workflow_rule,
    preview_workflow_rule,
    process_organization_workflows,
    replay_terminal_workflow_run,
    resume_workflow_rule,
    update_workflow_rule,
)
from app.schemas.workflows import (
    WorkflowDryRunRequest,
    WorkflowRuleCreate,
    WorkflowRuleUpdate,
)
from app.services import workflows as workflow_service
from app.services.activities import append_candidate_activity
from app.services.applications import ensure_application_link
from app.services.candidate_merge import merge_candidates
from app.services.entitlements import EntitlementExceeded
from app.services.workflow_worker import process_workflow_runs_once
from app.services.workflows import (
    WorkflowCancellationRequested,
    materialize_scheduled_workflow_runs,
    process_materialized_workflow_run,
    process_workflows,
    replay_workflow_run,
)

pytestmark = pytest.mark.asyncio
settings = get_settings()


def auth(user: User) -> dict[str, str]:
    token = encode_access_token(user.id, user.role, user.locale)
    return {
        "Authorization": f"Bearer {token}"
    }


async def seed_application(
    db,
    suffix: str,
    *,
    score_value: int = 82,
    stage: str = "new",
    source: str = "manual",
):
    organization = Organization(
        name=f"Org {suffix}", slug=f"org-{suffix}-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    creator = User(
        organization_id=organization.id,
        email=f"creator-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    teammate = User(
        organization_id=organization.id,
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source=source,
        source_ref=f"candidate-{suffix}",
        parsed_cv={"first_name": "Ada", "last_name": "Lovelace"},
        raw_cv="A detailed candidate CV used only as domain test data. " * 2,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {suffix}",
        status="open",
        created_by=creator.id,
    )
    db.add_all([creator, teammate, candidate, position])
    await db.flush()
    score = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=score_value,
        stage=stage,
    )
    db.add(score)
    await db.flush()
    link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=source,
        owner_user_id=score.owner_user_id,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="workflow_fixture_legacy_score_reconciled",
    )
    candidate._fixture_application_id = link.application.id
    await db.commit()
    return organization, creator, teammate, candidate, position, score


async def add_rule(
    db,
    *,
    organization,
    creator,
    name,
    trigger,
    conditions,
    actions,
    starts_at,
):
    rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=creator.id,
        name=name,
        trigger=trigger,
        conditions=conditions,
        actions=actions,
        is_active=True,
        starts_at=starts_at,
    )
    db.add(rule)
    await db.commit()
    return rule


async def add_activity(
    db,
    *,
    organization,
    candidate,
    event_type,
    occurred_at,
    position=None,
    actor=None,
):
    application_id = None
    if position is not None:
        application_id = getattr(candidate, "_fixture_application_id", None)
        if application_id is None:
            link = await ensure_application_link(
                db,
                organization_id=organization.id,
                candidate_id=candidate.id,
                position_id=position.id,
                source=candidate.source or "manual",
                owner_user_id=None,
                actor_user_id=actor.id if actor else None,
                actor_kind="human" if actor else "system",
                reason="workflow_activity_fixture_link",
                occurred_at=occurred_at,
            )
            application_id = link.application.id
            candidate._fixture_application_id = application_id
    activity = append_candidate_activity(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        actor_user_id=actor.id if actor else None,
        application_id=application_id,
        position_id=position.id if position else None,
        event_type=event_type,
        subject_type="candidate",
        subject_id=candidate.id,
        occurred_at=occurred_at,
    )
    await db.commit()
    return activity


async def test_event_rule_executes_allowlisted_actions_as_system_and_is_idempotent(db):
    (
        organization,
        creator,
        teammate,
        candidate,
        position,
        score,
    ) = await seed_application(db, "event-actions")
    current = datetime.now(UTC)
    contact = CandidateContactPoint(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by_user_id=creator.id,
        channel="email",
        value="ada@example.com",
        normalized_value="ada@example.com",
        is_primary=True,
    )
    account = CommunicationAccount(
        organization_id=organization.id,
        created_by_user_id=creator.id,
        channel="email",
        provider="sandbox",
        name="Workflow sandbox",
        configuration={"mode": "local"},
        is_active=True,
    )
    template = CommunicationTemplate(
        organization_id=organization.id,
        created_by_user_id=creator.id,
        name="Screening invite",
        channel="email",
        language="en",
        subject_template="Next step for {position.title}",
        body_template="Hello {candidate.first_name}, please choose the next step.",
        is_active=True,
    )
    db.add_all([contact, account, template])
    await db.flush()
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Strong candidate screening",
        trigger="candidate_scored",
        conditions={
            "position_id": str(position.id),
            "min_score": 75,
            "max_score": 95,
            "stage_in": ["new"],
            "source_in": ["manual"],
        },
        actions=[
            {"type": "move_stage", "stage": "screening"},
            {"type": "assign_owner", "user_id": str(teammate.id)},
            {"type": "set_due_date", "days_from_now": 2},
            {"type": "add_tag", "label": "priority"},
            {
                "type": "create_task",
                "title": "Technical exercise",
                "prompt": "This private task brief stays out of activity payloads.",
            },
            {
                "type": "send_template",
                "channel": "email",
                "template_id": str(template.id),
            },
            {"type": "notify_owner", "title": "Review the screening response"},
        ],
        starts_at=current - timedelta(hours=1),
    )
    await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        actor=creator,
        event_type="candidate_scored",
        occurred_at=current - timedelta(minutes=5),
    )

    counters = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=current,
    )
    assert counters.__dict__ == {
        "rules": 1,
        "matched": 1,
        "completed": 1,
        "skipped": 0,
        "failed": 0,
    }
    await db.refresh(score)
    assert score.stage == "screening"
    application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == organization.id,
                Application.candidate_id == candidate.id,
                Application.position_id == position.id,
            )
        )
    ).scalar_one()
    assert application.stage == "screening"
    transition_event = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == application.id,
                ApplicationStageEvent.from_stage == "new",
            )
        )
    ).scalar_one()
    assert transition_event.to_stage == "screening"
    assert transition_event.actor_kind == "automation"
    assert transition_event.details["workflow_rule_id"] == str(rule.id)
    assert score.owner_user_id == teammate.id
    assert score.next_action_due_date == (
        current.astimezone(ZoneInfo(settings.business_timezone)).date()
        + timedelta(days=2)
    )
    tag = (
        await db.execute(
            select(CandidateTag).where(CandidateTag.candidate_id == candidate.id)
        )
    ).scalar_one()
    assert tag.label == "priority"
    assert tag.created_by is None
    task = (
        await db.execute(
            select(TaskSubmission).where(TaskSubmission.candidate_id == candidate.id)
        )
    ).scalar_one()
    assert task.created_by is None
    assert task.prompt and "private" in task.prompt.lower()
    message = (
        await db.execute(
            select(CommunicationMessage).where(
                CommunicationMessage.candidate_id == candidate.id
            )
        )
    ).scalar_one()
    assert message.status == "queued"
    assert message.actor_user_id is None

    notifications = list(
        (
            await db.execute(
                select(Notification)
                .where(Notification.organization_id == organization.id)
                .order_by(Notification.type)
            )
        ).scalars()
    )
    assert [item.type for item in notifications] == [
        "candidate_assigned",
        "workflow_reminder",
    ]
    assert all(item.user_id == teammate.id for item in notifications)

    run = (
        await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule.id))
    ).scalar_one()
    assert run.status == "completed"
    assert len(run.action_results) == 7
    assert run.candidate_id == candidate.id
    assert run.position_id == position.id

    automated = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate.id,
                    CandidateActivity.event_type.in_(
                        (
                            "stage_changed",
                            "candidate_owner_changed",
                            "next_action_due_changed",
                            "tag_added",
                            "task_added",
                            "workflow_completed",
                        )
                    ),
                )
            )
        ).scalars()
    )
    assert len(automated) == 6
    assert all(item.actor_user_id is None for item in automated)
    serialized_payloads = str([item.payload for item in automated]).lower()
    assert "private task brief" not in serialized_payloads
    assert "ada@example.com" not in serialized_payloads
    assert "body_template" not in serialized_payloads

    replay = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=current + timedelta(minutes=1),
    )
    assert replay.matched == replay.completed == replay.skipped == replay.failed == 0
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(WorkflowRun.rule_id == rule.id)
        )
    ).scalar_one() == 1


async def test_conditions_skip_and_true_noop_do_not_emit_domain_activity(db):
    organization, creator, _owner, candidate, position, score = await seed_application(
        db, "conditions-noop", score_value=80, stage="new"
    )
    current = datetime.now(UTC)
    existing_tag = CandidateTag(
        organization_id=organization.id,
        candidate_id=candidate.id,
        label="existing",
        created_by=creator.id,
    )
    db.add(existing_tag)
    await db.commit()
    event = await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        actor=creator,
        event_type="stage_changed",
        occurred_at=current - timedelta(minutes=2),
    )
    condition_rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Only exceptional scores",
        trigger="stage_changed",
        conditions={"min_score": 95},
        actions=[{"type": "add_tag", "label": "exceptional"}],
        starts_at=current - timedelta(hours=1),
    )
    skipped = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=condition_rule.id,
        now=current,
    )
    assert skipped.matched == 0
    assert skipped.skipped == 1
    skipped_run = (
        await db.execute(
            select(WorkflowRun).where(WorkflowRun.rule_id == condition_rule.id)
        )
    ).scalar_one()
    assert skipped_run.status == "skipped"

    noop_rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Already applied state",
        trigger="stage_changed",
        conditions={"max_score": 90},
        actions=[
            {"type": "move_stage", "stage": "new"},
            {"type": "add_tag", "label": "existing"},
        ],
        starts_at=event.occurred_at - timedelta(seconds=1),
    )
    completed = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=noop_rule.id,
        now=current,
    )
    assert completed.matched == completed.completed == 1
    noop_run = (
        await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == noop_rule.id))
    ).scalar_one()
    assert [item["status"] for item in noop_run.action_results] == ["noop", "noop"]
    assert (
        await db.execute(
            select(func.count(CandidateActivity.id)).where(
                CandidateActivity.candidate_id == candidate.id,
                CandidateActivity.event_type == "workflow_completed",
            )
        )
    ).scalar_one() == 0
    assert (
        await db.execute(
            select(func.count(CandidateActivity.id)).where(
                CandidateActivity.candidate_id == candidate.id,
                CandidateActivity.event_type == "tag_added",
            )
        )
    ).scalar_one() == 0
    await db.refresh(score)
    assert score.stage == "new"


async def test_legacy_score_triggered_rule_cannot_make_terminal_decision(db):
    organization, creator, _owner, candidate, position, score = await seed_application(
        db, "legacy-ai-terminal"
    )
    current = datetime.now(UTC)
    # Insert through the model helper to simulate a rule persisted before the
    # request schema gained the human-decision guard.
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Legacy automatic rejection",
        trigger="candidate_scored",
        conditions={},
        actions=[
            {
                "type": "move_stage",
                "stage": "rejected",
                "disposition_code": "not_qualified",
                "reason": "Legacy automatic outcome",
            }
        ],
        starts_at=current - timedelta(hours=1),
    )
    await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        actor=creator,
        event_type="candidate_scored",
        occurred_at=current - timedelta(minutes=1),
    )

    result = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=current,
    )
    assert result.matched == result.failed == 1
    await db.refresh(score)
    assert score.stage == "new"
    run = (
        await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule.id))
    ).scalar_one()
    assert run.status == "failed"
    assert run.error_code == "human_decision_required"
    application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == organization.id,
                Application.candidate_id == candidate.id,
                Application.position_id == position.id,
            )
        )
    ).scalar_one()
    assert application.stage == "new"


async def test_action_failure_rolls_back_every_action_and_keeps_one_failure_record(db):
    (
        organization,
        creator,
        doomed_owner,
        candidate,
        position,
        _score,
    ) = await seed_application(db, "failure")
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Rollback all actions",
        trigger="candidate_rescored",
        conditions={},
        actions=[
            {"type": "add_tag", "label": "must-rollback"},
            {"type": "assign_owner", "user_id": str(doomed_owner.id)},
        ],
        starts_at=current - timedelta(hours=1),
    )
    doomed_owner_id = doomed_owner.id
    await db.delete(doomed_owner)
    await db.commit()
    await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        actor=creator,
        event_type="candidate_rescored",
        occurred_at=current - timedelta(minutes=1),
    )
    result = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=current,
    )
    assert result.matched == result.failed == 1
    run = (
        await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule.id))
    ).scalar_one()
    assert run.status == "failed"
    assert run.error_code == "owner_missing"
    assert run.action_results == []
    assert (
        await db.execute(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.candidate_id == candidate.id,
                CandidateTag.label == "must-rollback",
            )
        )
    ).scalar_one() == 0
    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate.id,
                    CandidateActivity.event_type.in_(
                        ("tag_added", "workflow_completed", "workflow_failed")
                    ),
                )
            )
        ).scalars()
    )
    assert [event.event_type for event in events] == ["workflow_failed"]
    assert events[0].actor_user_id is None
    assert events[0].payload["error_code"] == "owner_missing"
    assert str(doomed_owner_id) not in str(events[0].payload)

    replay = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=current + timedelta(minutes=1),
    )
    assert replay.matched == replay.failed == replay.skipped == 0
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(WorkflowRun.rule_id == rule.id)
        )
    ).scalar_one() == 1


async def test_inactivity_rules_advance_past_processed_rows_and_rearm_after_activity(
    db,
):
    organization, creator, owner, candidate, position, score = await seed_application(
        db, "inactive-one", stage="screening"
    )
    current = datetime.now(UTC)
    old = current - timedelta(days=10)
    candidate.created_at = old
    score.scored_at = old
    score.owner_user_id = owner.id

    second = Candidate(
        organization_id=organization.id,
        source="referral",
        source_ref="inactive-second",
        created_at=old + timedelta(minutes=1),
    )
    db.add(second)
    await db.flush()
    second_score = Score(
        organization_id=organization.id,
        candidate_id=second.id,
        position_id=position.id,
        overall_score=70,
        stage="screening",
        owner_user_id=owner.id,
        scored_at=old + timedelta(minutes=1),
    )
    db.add(second_score)
    await db.flush()
    await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        event_type="candidate_added",
        occurred_at=old,
    )
    await add_activity(
        db,
        organization=organization,
        candidate=second,
        position=position,
        event_type="candidate_added",
        occurred_at=old + timedelta(minutes=1),
    )
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Seven day inactivity",
        trigger="candidate_inactive",
        conditions={"inactivity_days": 7, "stage_in": ["screening"]},
        actions=[{"type": "notify_owner", "title": "Candidate needs a follow-up"}],
        starts_at=current - timedelta(minutes=1),
    )

    first = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        limit=1,
        now=current,
    )
    second_pass = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        limit=1,
        now=current,
    )
    exhausted = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        limit=1,
        now=current,
    )
    assert first.completed == second_pass.completed == 1
    assert exhausted.completed == exhausted.skipped == exhausted.matched == 0
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(WorkflowRun.rule_id == rule.id)
        )
    ).scalar_one() == 2

    human_activity_at = current + timedelta(minutes=5)
    await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        actor=creator,
        event_type="note_added",
        occurred_at=human_activity_at,
    )
    rearmed = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=human_activity_at + timedelta(days=8),
    )
    assert rearmed.completed == rearmed.matched == 1
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(WorkflowRun.rule_id == rule.id)
        )
    ).scalar_one() == 3
    reminders = (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.organization_id == organization.id,
                Notification.type == "workflow_reminder",
            )
        )
    ).scalar_one()
    assert reminders == 3


async def test_scheduled_rule_materialization_is_durable_and_idempotent(db):
    organization, creator, owner, candidate, position, score = await seed_application(
        db,
        "scheduled-materialization",
        stage="screening",
    )
    current = datetime.now(UTC)
    old = current - timedelta(days=10)
    candidate.created_at = old
    score.scored_at = old
    score.owner_user_id = owner.id
    await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        event_type="candidate_added",
        occurred_at=old,
    )
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Durable inactivity",
        trigger="candidate_inactive",
        conditions={"inactivity_days": 7, "stage_in": ["screening"]},
        actions=[{"type": "notify_owner", "title": "Follow up"}],
        starts_at=current - timedelta(minutes=1),
    )

    queued = await process_organization_workflows(
        rule_id=rule.id,
        limit=50,
        db=db,
        user=creator,
    )
    assert queued.rules == 1
    assert queued.queued == 1
    assert (
        await materialize_scheduled_workflow_runs(
            db,
            organization_id=organization.id,
            now=current,
        )
        == 0
    )
    run = (
        await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule.id))
    ).scalar_one()
    assert run.status == "queued"
    queued_page = await list_workflow_runs(
        limit=30,
        cursor=None,
        rule_id=rule.id,
        run_status="queued",
        db=db,
        user=creator,
    )
    assert [item.id for item in queued_page.items] == [run.id]
    assert queued_page.items[0].status == "queued"
    with pytest.raises(HTTPException) as stale_processing_status:
        await list_workflow_runs(
            limit=30,
            cursor=None,
            rule_id=rule.id,
            run_status="processing",
            db=db,
            user=creator,
        )
    assert stale_processing_status.value.status_code == 422
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.type == "workflow_reminder"
            )
        )
    ).scalar_one() == 0
    await db.commit()

    processed = await process_workflow_runs_once(
        worker_id="workflow:scheduled-test",
        limit=1,
        mode="off",
    )
    assert processed["completed"] == 1
    await db.rollback()
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.type == "workflow_reminder"
            )
        )
    ).scalar_one() == 1


async def test_feedback_overdue_is_due_assignment_scoped_and_rearms_when_due_changes(
    db,
):
    organization, creator, owner, candidate, position, score = await seed_application(
        db, "feedback", stage="interview"
    )
    current = datetime.now(UTC)
    score.owner_user_id = owner.id
    link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=candidate.source,
        owner_user_id=owner.id,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="feedback_workflow_test_setup",
    )

    def assignment(name: str, due_at: datetime, status: str = "draft"):
        return ScorecardAssignment(
            organization_id=organization.id,
            application_id=link.application.id,
            candidate_id=candidate.id,
            position_id=position.id,
            template_id=None,
            assigned_to_user_id=owner.id,
            assigned_by_user_id=creator.id,
            template_name_snapshot=name,
            position_title_snapshot=position.title,
            assignee_email_snapshot=owner.email,
            status=status,
            due_at=due_at,
            submitted_at=current if status == "submitted" else None,
        )

    first_assignment = assignment("First panel", current - timedelta(hours=3))
    second_assignment = assignment("Second panel", current - timedelta(hours=2))
    future_assignment = assignment("Future panel", current + timedelta(hours=2))
    submitted_assignment = assignment(
        "Already submitted", current - timedelta(hours=4), status="submitted"
    )
    db.add_all(
        [
            first_assignment,
            second_assignment,
            future_assignment,
            submitted_assignment,
        ]
    )
    await db.commit()
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Feedback SLA",
        trigger="feedback_overdue",
        conditions={"position_id": str(position.id), "stage_in": ["interview"]},
        actions=[{"type": "notify_owner", "title": "Interview feedback is overdue"}],
        starts_at=current - timedelta(minutes=1),
    )

    first = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        limit=1,
        now=current,
    )
    second = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        limit=1,
        now=current,
    )
    exhausted = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        limit=1,
        now=current,
    )
    assert first.completed == second.completed == 1
    assert exhausted.completed == exhausted.skipped == exhausted.matched == 0
    runs = list(
        (
            await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule.id))
        ).scalars()
    )
    assert len(runs) == 2
    assert all(run.dedupe_key.startswith("feedback:") for run in runs)

    first_assignment.due_at = current - timedelta(hours=1)
    await db.commit()
    changed_due = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=current,
    )
    assert changed_due.completed == changed_due.matched == 1
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(WorkflowRun.rule_id == rule.id)
        )
    ).scalar_one() == 3
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.type == "workflow_reminder"
            )
        )
    ).scalar_one() == 3


async def test_tenant_api_isolation_and_candidate_merge_rehome_workflow_runs(db):
    org_one, creator, _owner, primary, first_position, _score = await seed_application(
        db, "tenant-one"
    )
    (
        org_two,
        outsider,
        _other_owner,
        other_candidate,
        other_position,
        _other_score,
    ) = await seed_application(db, "tenant-two")
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=org_one,
        creator=creator,
        name="Tenant private",
        trigger="candidate_added",
        conditions={"position_id": str(first_position.id)},
        actions=[{"type": "add_tag", "label": "private-rule"}],
        starts_at=current - timedelta(hours=1),
    )
    await add_activity(
        db,
        organization=org_one,
        candidate=primary,
        position=first_position,
        event_type="candidate_added",
        occurred_at=current - timedelta(minutes=2),
    )
    await add_activity(
        db,
        organization=org_two,
        candidate=other_candidate,
        position=other_position,
        event_type="candidate_added",
        occurred_at=current - timedelta(minutes=2),
    )
    untouched = await process_workflows(db, organization_id=org_two.id, now=current)
    assert untouched.rules == 0
    outsider_rules = await list_workflow_rules(
        include_inactive=True, db=db, user=outsider
    )
    assert outsider_rules.items == []
    with pytest.raises(HTTPException) as hidden:
        await update_workflow_rule(
            rule.id,
            WorkflowRuleUpdate(name="Stolen"),
            db=db,
            user=outsider,
        )
    assert hidden.value.status_code == 404
    run_page = await list_workflow_runs(
        limit=30,
        cursor=None,
        rule_id=None,
        run_status=None,
        db=db,
        user=outsider,
    )
    assert run_page.items == []
    with pytest.raises(HTTPException) as cross_reference:
        await create_workflow_rule(
            WorkflowRuleCreate.model_validate(
                {
                    "name": "Cross tenant",
                    "trigger": "candidate_added",
                    "conditions": {"position_id": str(first_position.id)},
                    "actions": [{"type": "add_tag", "label": "forbidden"}],
                }
            ),
            db=db,
            user=outsider,
        )
    assert cross_reference.value.status_code == 404

    processed = await process_workflows(
        db, organization_id=org_one.id, rule_id=rule.id, now=current
    )
    assert processed.completed == 1
    private_run = (
        await db.execute(select(WorkflowRun).where(WorkflowRun.rule_id == rule.id))
    ).scalar_one()
    with pytest.raises(HTTPException) as hidden_cancel:
        await cancel_active_workflow_run(private_run.id, db=db, user=outsider)
    assert hidden_cancel.value.status_code == 404
    assert (
        await db.execute(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.organization_id == org_two.id
            )
        )
    ).scalar_one() == 0

    duplicate = Candidate(
        organization_id=org_one.id,
        source="manual",
        source_ref="workflow-merge-alias",
    )
    duplicate_position = Position(
        organization_id=org_one.id,
        title="Merge-only role",
        status="open",
    )
    db.add_all([duplicate, duplicate_position])
    await db.flush()
    db.add(
        Score(
            organization_id=org_one.id,
            candidate_id=duplicate.id,
            position_id=duplicate_position.id,
            overall_score=60,
            stage="screening",
        )
    )
    await db.flush()
    duplicate_link = await ensure_application_link(
        db,
        organization_id=org_one.id,
        candidate_id=duplicate.id,
        position_id=duplicate_position.id,
        source=duplicate.source,
        owner_user_id=None,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="workflow_merge_fixture_link",
    )
    historical_run = WorkflowRun(
        organization_id=org_one.id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=duplicate.id,
        position_id=duplicate_position.id,
        application_id=duplicate_link.application.id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"historical:{duplicate.id}",
        status="completed",
        action_results=[],
        started_at=current,
        completed_at=current,
    )
    db.add(historical_run)
    await db.commit()
    run_id = historical_run.id
    primary_id = primary.id
    duplicate_id = duplicate.id
    await merge_candidates(
        db,
        organization_id=org_one.id,
        actor_user_id=creator.id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[duplicate_id],
        score_winners={},
    )
    db.expire_all()
    assert (await db.get(WorkflowRun, run_id)).candidate_id == primary_id


async def test_queued_run_keeps_snapshot_across_edit_pause_and_resume(db):
    organization, creator, _owner, candidate, position, _score = await seed_application(
        db, "immutable-snapshot"
    )
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Immutable automation",
        trigger="candidate_added",
        conditions={"position_id": str(position.id)},
        actions=[{"type": "add_tag", "label": "snapshot-v1"}],
        starts_at=current - timedelta(hours=1),
    )
    first_activity = await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        event_type="candidate_added",
        occurred_at=current - timedelta(minutes=1),
    )
    queued = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.rule_id == rule.id,
                WorkflowRun.source_activity_id == first_activity.id,
            )
        )
    ).scalar_one_or_none()
    if queued is None:
        queued = WorkflowRun(
            organization_id=organization.id,
            rule_id=rule.id,
            rule_id_snapshot=rule.id,
            rule_version_snapshot=rule.version,
            candidate_id=candidate.id,
            position_id=position.id,
            application_id=first_activity.application_id,
            source_activity_id=first_activity.id,
            rule_name_snapshot=rule.name,
            trigger_snapshot=rule.trigger,
            conditions_snapshot=rule.conditions,
            actions_snapshot=rule.actions,
            dedupe_key=f"activity:{first_activity.id}",
            status="queued",
            action_results=[],
            started_at=first_activity.occurred_at,
        )
        db.add(queued)
        await db.commit()
    assert queued.status == "queued"
    assert queued.rule_version_snapshot == 1
    assert queued.actions_snapshot == [{"type": "add_tag", "label": "snapshot-v1"}]

    updated = await update_workflow_rule(
        rule.id,
        WorkflowRuleUpdate(actions=[{"type": "add_tag", "label": "snapshot-v2"}]),
        db=db,
        user=creator,
    )
    assert updated.version == 2
    paused = await pause_workflow_rule(rule.id, db=db, user=creator)
    assert paused.is_active is False
    assert paused.version == 2
    paused_activity = await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        event_type="candidate_added",
        occurred_at=datetime.now(UTC),
    )
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(
                WorkflowRun.source_activity_id == paused_activity.id
            )
        )
    ).scalar_one() == 0

    lease_token = uuid4()
    queued.status = "running"
    queued.attempt_count = 1
    queued.leased_at = datetime.now(UTC)
    queued.leased_by = "workflow:immutable-snapshot"
    queued.lease_token = lease_token
    queued.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
    await db.flush()
    processed_status = await process_materialized_workflow_run(
        db,
        organization_id=organization.id,
        run_id=queued.id,
    )
    assert processed_status == "completed"
    await db.commit()
    tags = list(
        (
            await db.execute(
                select(CandidateTag.label).where(
                    CandidateTag.organization_id == organization.id,
                    CandidateTag.candidate_id == candidate.id,
                )
            )
        ).scalars()
    )
    assert tags == ["snapshot-v1"]

    resumed = await resume_workflow_rule(rule.id, db=db, user=creator)
    assert resumed.is_active is True
    assert resumed.version == 2
    resumed_activity = await add_activity(
        db,
        organization=organization,
        candidate=candidate,
        position=position,
        event_type="candidate_added",
        occurred_at=datetime.now(UTC),
    )
    processed = await process_workflows(
        db,
        organization_id=organization.id,
        rule_id=rule.id,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert processed.completed == 1
    next_run = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.source_activity_id == resumed_activity.id
            )
        )
    ).scalar_one()
    assert next_run.rule_version_snapshot == 2
    assert next_run.actions_snapshot == [{"type": "add_tag", "label": "snapshot-v2"}]
    tags = set(
        (
            await db.execute(
                select(CandidateTag.label).where(
                    CandidateTag.organization_id == organization.id,
                    CandidateTag.candidate_id == candidate.id,
                )
            )
        ).scalars()
    )
    assert tags == {"snapshot-v1", "snapshot-v2"}


async def test_dry_run_returns_match_and_actions_without_writes(db):
    organization, creator, _owner, candidate, position, _score = await seed_application(
        db, "dry-run"
    )
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="No-effect preview",
        trigger="stage_changed",
        conditions={"position_id": str(position.id), "min_score": 70},
        actions=[
            {"type": "add_tag", "label": "must-not-exist"},
            {"type": "create_task", "title": "Must not exist"},
            {"type": "notify_owner", "title": "Must not notify"},
        ],
        starts_at=datetime.now(UTC) - timedelta(hours=1),
    )
    candidate_id = candidate.id
    position_id = position.id
    before = {
        "runs": (await db.execute(select(func.count(WorkflowRun.id)))).scalar_one(),
        "tags": (await db.execute(select(func.count(CandidateTag.id)))).scalar_one(),
        "tasks": (await db.execute(select(func.count(TaskSubmission.id)))).scalar_one(),
        "notifications": (
            await db.execute(select(func.count(Notification.id)))
        ).scalar_one(),
        "activities": (
            await db.execute(select(func.count(CandidateActivity.id)))
        ).scalar_one(),
    }
    preview = await preview_workflow_rule(
        rule.id,
        WorkflowDryRunRequest(
            candidate_id=candidate_id,
            position_id=position_id,
        ),
        db=db,
        user=creator,
    )
    assert preview.rule_version == 1
    assert preview.evaluated == preview.matched == 1
    assert [action.type for action in preview.items[0].actions] == [
        "add_tag",
        "create_task",
        "notify_owner",
    ]
    after = {
        "runs": (await db.execute(select(func.count(WorkflowRun.id)))).scalar_one(),
        "tags": (await db.execute(select(func.count(CandidateTag.id)))).scalar_one(),
        "tasks": (await db.execute(select(func.count(TaskSubmission.id)))).scalar_one(),
        "notifications": (
            await db.execute(select(func.count(Notification.id)))
        ).scalar_one(),
        "activities": (
            await db.execute(select(func.count(CandidateActivity.id)))
        ).scalar_one(),
    }
    assert after == before


async def test_terminal_run_replay_is_idempotent_and_fails_closed_after_rule_deletion(
    db,
):
    organization, creator, _owner, candidate, position, _score = await seed_application(
        db, "durable-replay"
    )
    organization_id = organization.id
    candidate_id = candidate.id
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Replayable workflow",
        trigger="candidate_added",
        conditions={"position_id": str(position.id)},
        actions=[{"type": "add_tag", "label": "replayed"}],
        starts_at=current - timedelta(hours=1),
    )
    source = WorkflowRun(
        organization_id=organization_id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=candidate._fixture_application_id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"dead-letter:{candidate_id}",
        status="dead_letter",
        action_results=[],
        error_code="provider_timeout",
        attempt_count=3,
        max_attempts=3,
        started_at=current,
        completed_at=current,
    )
    db.add(source)
    await db.commit()

    first = await replay_terminal_workflow_run(source.id, db=db, user=creator)
    second = await replay_terminal_workflow_run(source.id, db=db, user=creator)
    assert first.id == second.id
    assert first.replay_of_run_id == source.id
    assert first.rule_version == rule.version
    replays = list(
        (
            await db.execute(
                select(WorkflowRun).where(WorkflowRun.replay_of_run_id == source.id)
            )
        ).scalars()
    )
    assert len(replays) == 1
    assert replays[0].actions_snapshot == source.actions_snapshot
    assert replays[0].conditions_snapshot == source.conditions_snapshot
    assert (
        await db.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == "workflow_runs",
                UsageLedgerEntry.resource_id == first.id,
            )
        )
    ).scalar_one() == 1

    await db.delete(rule)
    await db.commit()
    replay_row = await db.get(WorkflowRun, first.id)
    assert replay_row is not None
    replay_row.status = "running"
    replay_row.attempt_count = 1
    replay_row.leased_at = datetime.now(UTC)
    replay_row.leased_by = "workflow:deleted-rule-replay"
    replay_row.lease_token = uuid4()
    replay_row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
    await db.flush()
    processed_status = await process_materialized_workflow_run(
        db,
        organization_id=organization_id,
        run_id=replay_row.id,
    )
    assert processed_status == "dead_letter"
    await db.commit()
    db.expire_all()
    replay = await db.get(WorkflowRun, first.id)
    assert replay is not None
    assert replay.rule_id is None
    assert replay.status == "dead_letter"
    assert replay.error_code == "workflow_creator_unavailable"
    assert (
        await db.execute(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.organization_id == organization_id,
                CandidateTag.candidate_id == candidate_id,
                CandidateTag.label == "replayed",
            )
        )
    ).scalar_one() == 0


@pytest.mark.parametrize("initial_status", ["queued", "retrying"])
async def test_pending_run_cancellation_is_immediate_and_idempotent(
    db, initial_status
):
    organization, creator, _owner, candidate, position, _score = (
        await seed_application(db, f"cancel-{initial_status}")
    )
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name=f"Cancel {initial_status}",
        trigger="candidate_added",
        conditions={},
        actions=[{"type": "add_tag", "label": "must-not-run"}],
        starts_at=current - timedelta(hours=1),
    )
    run = WorkflowRun(
        organization_id=organization.id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=candidate._fixture_application_id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"cancel:{initial_status}:{candidate.id}",
        status=initial_status,
        action_results=[],
        error_code="internal_error" if initial_status == "retrying" else None,
        available_at=current + timedelta(minutes=1),
        attempt_count=1 if initial_status == "retrying" else 0,
        max_attempts=3,
        started_at=current,
    )
    db.add(run)
    await db.commit()

    first = await cancel_active_workflow_run(run.id, db=db, user=creator)
    second = await cancel_active_workflow_run(run.id, db=db, user=creator)
    assert first.id == second.id == run.id
    assert first.status == second.status == "cancelled"
    assert first.error_code == "cancelled_by_user"
    assert first.cancelled_at is not None
    assert first.completed_at is not None
    assert first.cancellation_requested is False
    assert first.attempt_count == (1 if initial_status == "retrying" else 0)
    assert first.max_attempts == 3


async def test_running_cancellation_survives_reclaim_and_stops_before_effects(db):
    organization, creator, _owner, candidate, position, _score = (
        await seed_application(db, "cancel-running")
    )
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Durable running cancellation",
        trigger="candidate_added",
        conditions={},
        actions=[{"type": "add_tag", "label": "must-not-run"}],
        starts_at=current - timedelta(hours=1),
    )
    run = WorkflowRun(
        organization_id=organization.id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=candidate._fixture_application_id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"cancel:running:{candidate.id}",
        status="running",
        action_results=[],
        available_at=current,
        attempt_count=1,
        max_attempts=3,
        leased_at=current,
        leased_by="workflow:cancel-test",
        lease_token=uuid4(),
        lease_expires_at=current + timedelta(minutes=2),
        started_at=current,
    )
    db.add(run)
    await db.commit()

    requested = await cancel_active_workflow_run(run.id, db=db, user=creator)
    assert requested.status == "running"
    assert requested.cancellation_requested is True
    assert requested.error_code == "cancellation_requested"
    # The database claim function clears cancelled_at when reclaiming an
    # expired lease. The persisted control marker must still stop the worker.
    row = await db.get(WorkflowRun, run.id)
    row.cancelled_at = None
    row.error_code = None
    await db.commit()

    outcome = await process_materialized_workflow_run(
        db,
        organization_id=organization.id,
        run_id=run.id,
        now=current + timedelta(seconds=1),
    )
    assert outcome == "cancelled"
    await db.commit()
    row = await db.get(WorkflowRun, run.id)
    assert row.status == "cancelled"
    assert row.cancelled_at is not None
    assert row.completed_at is not None
    assert row.lease_token is None
    assert (
        await db.execute(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.organization_id == organization.id,
                CandidateTag.candidate_id == candidate.id,
                CandidateTag.label == "must-not-run",
            )
        )
    ).scalar_one() == 0
    history = await list_workflow_runs(
        limit=30,
        cursor=None,
        rule_id=rule.id,
        run_status=None,
        db=db,
        user=creator,
    )
    assert history.items[0].status == "cancelled"
    assert history.items[0].attempt_count == 1
    assert history.items[0].max_attempts == 3


async def test_cancellation_between_actions_rolls_back_prior_effects(db, monkeypatch):
    organization, creator, _owner, candidate, position, _score = (
        await seed_application(db, "cancel-between-actions")
    )
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Cancel between actions",
        trigger="candidate_added",
        conditions={},
        actions=[
            {"type": "add_tag", "label": "rolled-back"},
            {"type": "create_task", "title": "must-not-be-created"},
        ],
        starts_at=current - timedelta(hours=1),
    )
    run = WorkflowRun(
        organization_id=organization.id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=candidate._fixture_application_id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"cancel:between-actions:{candidate.id}",
        status="running",
        action_results=[],
        available_at=current,
        attempt_count=1,
        max_attempts=3,
        leased_at=current,
        leased_by="workflow:cancel-between-actions",
        lease_token=uuid4(),
        lease_expires_at=current + timedelta(minutes=2),
        started_at=current,
    )
    db.add(run)
    await db.commit()
    checks = 0

    async def cancellation_after_first_action(_db, _run):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise WorkflowCancellationRequested()

    monkeypatch.setattr(
        workflow_service,
        "_raise_if_cancellation_requested",
        cancellation_after_first_action,
    )
    outcome = await process_materialized_workflow_run(
        db,
        organization_id=organization.id,
        run_id=run.id,
        now=current + timedelta(seconds=1),
    )
    assert outcome == "cancelled"
    await db.commit()
    assert checks == 3
    assert (
        await db.execute(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.organization_id == organization.id,
                CandidateTag.candidate_id == candidate.id,
                CandidateTag.label == "rolled-back",
            )
        )
    ).scalar_one() == 0
    assert (
        await db.execute(
            select(func.count(TaskSubmission.id)).where(
                TaskSubmission.organization_id == organization.id,
                TaskSubmission.candidate_id == candidate.id,
                TaskSubmission.title == "must-not-be-created",
            )
        )
    ).scalar_one() == 0


async def test_workflow_run_quota_is_atomic_across_concurrent_replays(db):
    organization, creator, _owner, candidate, position, _score = (
        await seed_application(db, "atomic-quota")
    )
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Quota-controlled replay",
        trigger="candidate_added",
        conditions={},
        actions=[{"type": "add_tag", "label": "quota"}],
        starts_at=current - timedelta(hours=1),
    )
    entitlement = OrganizationEntitlement(
        organization_id=organization.id,
        metric="workflow_runs",
        limit_value=1,
        period="contract",
    )
    sources = [
        WorkflowRun(
            organization_id=organization.id,
            rule_id=rule.id,
            rule_id_snapshot=rule.id,
            rule_version_snapshot=rule.version,
            candidate_id=candidate.id,
            position_id=position.id,
            application_id=candidate._fixture_application_id,
            rule_name_snapshot=rule.name,
            trigger_snapshot=rule.trigger,
            conditions_snapshot=rule.conditions,
            actions_snapshot=rule.actions,
            dedupe_key=f"quota-source:{index}:{candidate.id}",
            status="dead_letter",
            action_results=[],
            error_code="provider_timeout",
            attempt_count=3,
            max_attempts=3,
            started_at=current,
            completed_at=current,
        )
        for index in range(2)
    ]
    db.add_all([entitlement, *sources])
    await db.commit()
    source_ids = [source.id for source in sources]
    organization_id = organization.id

    async def attempt(run_id):
        async with SessionLocal() as session:
            try:
                replay, _created = await replay_workflow_run(
                    session,
                    organization_id=organization_id,
                    run_id=run_id,
                    now=current,
                )
                await session.commit()
                return replay.id
            except EntitlementExceeded as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.gather(*(attempt(run_id) for run_id in source_ids))
    blocked = [item for item in outcomes if isinstance(item, EntitlementExceeded)]
    succeeded = [item for item in outcomes if not isinstance(item, EntitlementExceeded)]
    assert len(blocked) == len(succeeded) == 1
    assert blocked[0].metric == "workflow_runs"
    assert blocked[0].limit == blocked[0].used == blocked[0].requested == 1
    db.expire_all()
    assert (
        await db.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == "workflow_runs",
            )
        )
    ).scalar_one() == 1
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(
                WorkflowRun.organization_id == organization_id,
                WorkflowRun.replay_of_run_id.is_not(None),
            )
        )
    ).scalar_one() == 1


async def test_workflow_quota_http_problem_exposes_current_usage(db):
    organization, creator, _owner, candidate, position, _score = (
        await seed_application(db, "quota-problem")
    )
    current = datetime.now(UTC)
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Blocked replay",
        trigger="candidate_added",
        conditions={},
        actions=[{"type": "add_tag", "label": "blocked"}],
        starts_at=current - timedelta(hours=1),
    )
    source = WorkflowRun(
        organization_id=organization.id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=candidate._fixture_application_id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"quota-http:{candidate.id}",
        status="dead_letter",
        action_results=[],
        error_code="provider_timeout",
        attempt_count=3,
        max_attempts=3,
        started_at=current,
        completed_at=current,
    )
    db.add_all(
        [
            OrganizationEntitlement(
                organization_id=organization.id,
                metric="workflow_runs",
                limit_value=0,
                period="contract",
            ),
            source,
        ]
    )
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/workflows/runs/{source.id}/replay",
            headers=auth(creator),
        )
    assert response.status_code == 429
    problem = response.json()
    assert problem["code"] == "entitlement_exceeded"
    assert problem["metric"] == "workflow_runs"
    assert problem["limit"] == problem["used"] == 0
    assert problem["requested"] == 1
    assert problem["request_id"] == response.headers["X-Request-ID"]


async def test_rule_version_changes_only_for_semantic_edits(db):
    (
        organization,
        creator,
        _owner,
        _candidate,
        _position,
        _score,
    ) = await seed_application(db, "rule-version")
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        name="Versioned workflow",
        trigger="candidate_added",
        conditions={},
        actions=[{"type": "add_tag", "label": "v1"}],
        starts_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert rule.version == 1
    first_pause = await pause_workflow_rule(rule.id, db=db, user=creator)
    second_pause = await pause_workflow_rule(rule.id, db=db, user=creator)
    assert first_pause.version == second_pause.version == 1
    assert first_pause.is_active is second_pause.is_active is False
    first_resume = await resume_workflow_rule(rule.id, db=db, user=creator)
    second_resume = await resume_workflow_rule(rule.id, db=db, user=creator)
    assert first_resume.version == second_resume.version == 1
    assert first_resume.is_active is second_resume.is_active is True
    unchanged = await update_workflow_rule(
        rule.id,
        WorkflowRuleUpdate(name="Versioned workflow"),
        db=db,
        user=creator,
    )
    assert unchanged.version == 1
    changed = await update_workflow_rule(
        rule.id,
        WorkflowRuleUpdate(name="Versioned workflow v2"),
        db=db,
        user=creator,
    )
    assert changed.version == 2


async def test_workflow_schema_rejects_blank_and_executable_actions(db):
    with pytest.raises(ValidationError):
        WorkflowRuleCreate.model_validate(
            {
                "name": "   ",
                "trigger": "candidate_added",
                "actions": [{"type": "add_tag", "label": "valid"}],
            }
        )
    with pytest.raises(ValidationError):
        WorkflowRuleCreate.model_validate(
            {
                "name": "Missing rejection evidence",
                "trigger": "candidate_scored",
                "actions": [{"type": "move_stage", "stage": "rejected"}],
            }
        )
    valid_rejection = WorkflowRuleCreate.model_validate(
        {
            "name": "Audited rejection",
            "trigger": "stage_changed",
            "actions": [
                {
                    "type": "move_stage",
                    "stage": "rejected",
                    "disposition_code": "not_qualified",
                    "reason": "Screening criteria were not met",
                }
            ],
        }
    )
    assert valid_rejection.actions[0].type == "move_stage"
    for trigger in ("candidate_scored", "candidate_rescored"):
        for terminal_stage in ("hired", "rejected"):
            action = {"type": "move_stage", "stage": terminal_stage}
            if terminal_stage == "rejected":
                action.update(
                    disposition_code="not_qualified",
                    reason="Human review is still required",
                )
            with pytest.raises(ValidationError, match="human_decision_required"):
                WorkflowRuleCreate.model_validate(
                    {
                        "name": "Unsafe AI decision",
                        "trigger": trigger,
                        "actions": [action],
                    }
                )
    with pytest.raises(ValidationError):
        WorkflowRuleCreate.model_validate(
            {
                "name": "Unsafe",
                "trigger": "candidate_added",
                "actions": [{"type": "shell", "command": "echo nope"}],
            }
        )
    with pytest.raises(ValidationError):
        WorkflowRuleCreate.model_validate(
            {
                "name": "Wrong periodic condition",
                "trigger": "stage_changed",
                "conditions": {"inactivity_days": 7},
                "actions": [{"type": "add_tag", "label": "valid"}],
            }
        )
