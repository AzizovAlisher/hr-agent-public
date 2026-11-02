"""Least-privilege and live-revocation coverage for workflow automation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.db import SessionLocal
from app.main import app
from app.models.candidate import Candidate
from app.models.candidate_note import CandidateTag
from app.models.enterprise import EnterprisePolicy, PermissionOverride
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun
from app.services import workflows as workflow_service
from app.services.applications import ensure_application_link
from app.services.workflow_authorization import workflow_access_requirements
from app.services.workflows import process_materialized_workflow_run

pytestmark = pytest.mark.asyncio


def auth(user: User) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {encode_access_token(user.id, user.role, user.locale)}"
    }


async def seed(db, label: str):
    organization = Organization(
        name=f"Workflow authorization {label}",
        slug=f"workflow-authz-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    creator = User(
        organization_id=organization.id,
        email=f"workflow-admin-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    owner = User(
        organization_id=organization.id,
        email=f"workflow-owner-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{label}",
    )
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {label}",
        status="open",
        created_by=creator.id,
    )
    db.add_all([creator, owner, candidate, position])
    await db.flush()
    score = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=80,
        stage="new",
    )
    db.add(score)
    await db.flush()
    application_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=candidate.source or "manual",
        owner_user_id=score.owner_user_id,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="workflow_authorization_fixture_link",
    )
    await db.commit()
    return (
        organization,
        creator,
        owner,
        candidate,
        position,
        application_link.application,
        score,
    )


async def add_rule(
    db,
    *,
    organization,
    creator,
    label,
    conditions,
    actions,
):
    rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=creator.id,
        name=f"Workflow {label}",
        trigger="candidate_added",
        conditions=conditions,
        actions=actions,
        is_active=True,
        starts_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add(rule)
    await db.commit()
    return rule


def materialized_run(
    *, organization, rule, candidate, position, application, status="running"
):
    now = datetime.now(UTC)
    running = status == "running"
    return WorkflowRun(
        organization_id=organization.id,
        rule_id=rule.id,
        rule_id_snapshot=rule.id,
        rule_version_snapshot=rule.version,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=application.id,
        rule_name_snapshot=rule.name,
        trigger_snapshot=rule.trigger,
        conditions_snapshot=rule.conditions,
        actions_snapshot=rule.actions,
        dedupe_key=f"authz:{rule.id}:{uuid4()}",
        status=status,
        action_results=[],
        error_code="fixture" if status == "failed" else None,
        attempt_count=1 if running else 0,
        leased_at=now if running else None,
        leased_by="workflow:authorization-test" if running else None,
        lease_token=uuid4() if running else None,
        lease_expires_at=now + timedelta(minutes=2) if running else None,
        started_at=now,
        completed_at=now if status == "failed" else None,
    )


async def grant_workflow_surface(db, organization_id):
    for permission in ("workflows.manage", "workflows.read", "workflows.execute"):
        db.add(
            PermissionOverride(
                organization_id=organization_id,
                role="restricted",
                permission=permission,
                allowed=True,
            )
        )
    await db.commit()


async def test_definition_declares_all_underlying_permissions_and_fields():
    requirements = workflow_access_requirements(
        conditions={"min_score": 50},
        actions=[
            {"type": "move_stage", "stage": "screening"},
            {"type": "assign_owner", "user_id": str(uuid4())},
            {"type": "set_due_date", "days_from_now": 1},
            {"type": "add_tag", "label": "priority"},
            {"type": "create_task", "title": "Exercise"},
            {
                "type": "send_template",
                "channel": "email",
                "template_id": str(uuid4()),
            },
            {"type": "notify_owner", "title": "Review"},
        ],
    )
    assert requirements.permissions == (
        "applications.disposition",
        "applications.write",
        "assessments.results",
        "candidates.write",
        "communications.send",
        "interviews.manage",
    )
    assert tuple(field.value for field in requirements.fields) == (
        "assessment_results",
        "candidate_contacts",
    )


async def test_workflows_manage_only_cannot_author_any_underlying_action(db):
    (
        organization,
        _admin,
        owner,
        _candidate,
        position,
        _application,
        _score,
    ) = await seed(db, "manage-only")
    restricted = User(
        organization_id=organization.id,
        email=f"workflow-restricted-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="restricted",
        locale="en",
    )
    db.add(restricted)
    await db.flush()
    await grant_workflow_surface(db, organization.id)

    action_cases = [
        {"type": "move_stage", "stage": "screening"},
        {"type": "assign_owner", "user_id": str(owner.id)},
        {"type": "set_due_date", "days_from_now": 1},
        {"type": "add_tag", "label": "forbidden"},
        {"type": "create_task", "title": "Forbidden task"},
        {
            "type": "send_template",
            "channel": "email",
            "template_id": str(uuid4()),
        },
        {"type": "notify_owner", "title": "Forbidden notification"},
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for index, action in enumerate(action_cases):
            denied = await client.post(
                "/workflows/rules",
                headers=auth(restricted),
                json={
                    "name": f"Forbidden action {index}",
                    "trigger": "candidate_added",
                    "conditions": {"position_id": str(position.id)},
                    "actions": [action],
                },
            )
            assert denied.status_code == 403, (action, denied.text)


async def test_score_definition_is_protected_at_every_workflow_surface(db):
    (
        organization,
        admin,
        _owner,
        candidate,
        position,
        application,
        _score,
    ) = await seed(db, "score-surface")
    restricted = User(
        organization_id=organization.id,
        email=f"workflow-score-restricted-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="restricted",
        locale="en",
    )
    db.add(restricted)
    await db.flush()
    await grant_workflow_surface(db, organization.id)
    rule = await add_rule(
        db,
        organization=organization,
        creator=admin,
        label="protected score",
        conditions={"position_id": str(position.id), "min_score": 50},
        actions=[{"type": "add_tag", "label": "protected"}],
    )
    terminal = materialized_run(
        organization=organization,
        rule=rule,
        candidate=candidate,
        position=position,
        application=application,
        status="failed",
    )
    db.add(terminal)
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        create = await client.post(
            "/workflows/rules",
            headers=auth(restricted),
            json={
                "name": "Forbidden score inference",
                "trigger": "candidate_added",
                "conditions": {
                    "position_id": str(position.id),
                    "min_score": 50,
                },
                "actions": [{"type": "add_tag", "label": "score-inference"}],
            },
        )
        requests = [
            create,
            await client.get("/workflows/rules", headers=auth(restricted)),
            await client.patch(
                f"/workflows/rules/{rule.id}",
                headers=auth(restricted),
                json={"name": "Still protected"},
            ),
            await client.post(
                f"/workflows/rules/{rule.id}/dry-run",
                headers=auth(restricted),
                json={"candidate_id": str(candidate.id)},
            ),
            await client.post(
                f"/workflows/rules/{rule.id}/resume",
                headers=auth(restricted),
            ),
            await client.post(
                "/workflows/process",
                headers=auth(restricted),
                params={"rule_id": str(rule.id)},
            ),
            await client.get("/workflows/runs", headers=auth(restricted)),
            await client.post(
                f"/workflows/runs/{terminal.id}/replay",
                headers=auth(restricted),
            ),
        ]
    assert [response.status_code for response in requests] == [403] * len(requests)


@pytest.mark.parametrize(
    ("revocation", "expected_error"),
    [
        ("disabled", "workflow_creator_inactive"),
        ("permission", "workflow_creator_authority_revoked"),
        ("field", "workflow_creator_authority_revoked"),
    ],
)
async def test_queued_run_rechecks_creator_and_field_policy(
    db,
    revocation,
    expected_error,
):
    (
        organization,
        creator,
        _owner,
        candidate,
        position,
        application,
        _score,
    ) = await seed(db, f"queued-{revocation}")
    conditions = (
        {"position_id": str(position.id), "min_score": 1}
        if revocation == "field"
        else {"position_id": str(position.id)}
    )
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        label=f"queued {revocation}",
        conditions=conditions,
        actions=[{"type": "add_tag", "label": f"blocked-{revocation}"}],
    )
    run = materialized_run(
        organization=organization,
        rule=rule,
        candidate=candidate,
        position=position,
        application=application,
        status="queued",
    )
    db.add(run)
    await db.commit()

    if revocation == "disabled":
        creator.is_active = False
    elif revocation == "permission":
        db.add(
            PermissionOverride(
                organization_id=organization.id,
                role="admin",
                permission="candidates.write",
                allowed=False,
            )
        )
    else:
        db.add(
            EnterprisePolicy(
                organization_id=organization.id,
                field_visibility={"assessment_results": ["recruiter"]},
            )
        )
    await db.commit()

    now = datetime.now(UTC)
    run.status = "running"
    run.attempt_count = 1
    run.leased_at = now
    run.leased_by = f"workflow:{revocation}"
    run.lease_token = uuid4()
    run.lease_expires_at = now + timedelta(minutes=2)
    await db.flush()
    outcome = await process_materialized_workflow_run(
        db,
        organization_id=organization.id,
        run_id=run.id,
    )
    assert outcome == "dead_letter"
    assert run.error_code == expected_error
    assert (
        await db.execute(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.organization_id == organization.id,
                CandidateTag.candidate_id == candidate.id,
            )
        )
    ).scalar_one() == 0


async def test_live_revocation_rolls_back_partial_notification_effects(db, monkeypatch):
    (
        organization,
        creator,
        owner,
        candidate,
        position,
        application,
        score,
    ) = await seed(db, "live-revocation")
    rule = await add_rule(
        db,
        organization=organization,
        creator=creator,
        label="live revocation",
        conditions={"position_id": str(position.id)},
        actions=[
            {"type": "assign_owner", "user_id": str(owner.id)},
            {"type": "notify_owner", "title": "Must be rolled back"},
        ],
    )
    run = materialized_run(
        organization=organization,
        rule=rule,
        candidate=candidate,
        position=position,
        application=application,
    )
    db.add(run)
    await db.commit()

    original = workflow_service._require_active_creator_authority
    checks = 0

    async def revoke_before_second_action(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 3:
            async with SessionLocal() as separate:
                separate.add(
                    PermissionOverride(
                        organization_id=organization.id,
                        role="admin",
                        permission="communications.send",
                        allowed=False,
                    )
                )
                await separate.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        workflow_service,
        "_require_active_creator_authority",
        revoke_before_second_action,
    )
    outcome = await process_materialized_workflow_run(
        db,
        organization_id=organization.id,
        run_id=run.id,
    )
    assert outcome == "dead_letter"
    assert run.error_code == "workflow_creator_authority_revoked"
    await db.refresh(score)
    assert score.owner_user_id is None
    assert (
        await db.execute(
            select(func.count(Notification.id)).where(
                Notification.organization_id == organization.id,
            )
        )
    ).scalar_one() == 0


async def test_delivery_actions_reject_same_rule_notification_amplification():
    from pydantic import ValidationError

    from app.schemas.workflows import WorkflowRuleCreate

    with pytest.raises(ValidationError, match="notification_amplification"):
        WorkflowRuleCreate.model_validate(
            {
                "name": "Amplification",
                "trigger": "candidate_added",
                "actions": [
                    {"type": "notify_owner", "title": "First"},
                    {"type": "notify_owner", "title": "Second"},
                ],
            }
        )
