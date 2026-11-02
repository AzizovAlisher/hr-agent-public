"""Hiring-manager workflow visibility is position scoped and non-enumerating."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun
from app.routers.workflows import (
    cancel_active_workflow_run,
    create_workflow_rule,
    disable_workflow_rule,
    get_workflow_rule,
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


pytestmark = pytest.mark.asyncio


async def _expect_hidden(awaitable) -> None:
    with pytest.raises(HTTPException) as hidden:
        await awaitable
    assert hidden.value.status_code == 404


async def test_hiring_manager_workflows_filter_before_pagination_and_never_mutate_hidden(
    db,
):
    organization = Organization(
        name="HM workflow scope",
        slug=f"hm-workflow-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    manager = User(
        organization_id=organization.id,
        email=f"hm-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    managed_position = Position(
        organization_id=organization.id,
        title="Managed position",
        status="open",
    )
    other_position = Position(
        organization_id=organization.id,
        title="Other position",
        status="open",
    )
    db.add_all([manager, managed_position, other_position])
    await db.flush()
    db.add(
        Requisition(
            organization_id=organization.id,
            created_by_user_id=manager.id,
            hiring_manager_user_id=manager.id,
            position_id=managed_position.id,
            title="Managed requisition",
            business_justification="Approved hiring need",
            status="opened",
        )
    )
    now = datetime.now(UTC)
    action = [{"type": "move_stage", "stage": "screening"}]
    global_rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=manager.id,
        name="A global hidden rule",
        trigger="candidate_added",
        conditions={},
        actions=action,
        starts_at=now - timedelta(hours=1),
    )
    other_rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=manager.id,
        name="B unmanaged hidden rule",
        trigger="candidate_added",
        conditions={"position_id": str(other_position.id)},
        actions=action,
        starts_at=now - timedelta(hours=1),
    )
    managed_rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=manager.id,
        name="Z managed visible rule",
        trigger="candidate_added",
        conditions={"position_id": str(managed_position.id)},
        actions=action,
        starts_at=now - timedelta(hours=1),
    )
    db.add_all([global_rule, other_rule, managed_rule])
    await db.flush()

    def run_for(rule: WorkflowRule, *, status: str, completed: bool) -> WorkflowRun:
        return WorkflowRun(
            organization_id=organization.id,
            rule_id=rule.id,
            rule_id_snapshot=rule.id,
            rule_version_snapshot=rule.version,
            rule_name_snapshot=rule.name,
            trigger_snapshot=rule.trigger,
            conditions_snapshot=dict(rule.conditions),
            actions_snapshot=list(rule.actions),
            dedupe_key=f"hm-scope:{rule.id}",
            status=status,
            action_results=[],
            error_code="provider_timeout" if status == "dead_letter" else None,
            attempt_count=3 if status == "dead_letter" else 0,
            started_at=now,
            completed_at=now if completed else None,
        )

    global_pending = run_for(global_rule, status="queued", completed=False)
    other_terminal = run_for(other_rule, status="dead_letter", completed=True)
    managed_terminal = run_for(managed_rule, status="completed", completed=True)
    db.add_all([global_pending, other_terminal, managed_terminal])
    await db.commit()

    # Invisible names sort before the visible row. Filtering before LIMIT is
    # what makes the visible item appear and prevents a cursor/count side
    # channel from revealing either hidden definition.
    rules = await list_workflow_rules(
        include_inactive=True,
        limit=1,
        cursor=None,
        db=db,
        user=manager,
    )
    assert [item.id for item in rules.items] == [managed_rule.id]
    assert rules.next_cursor is None
    detail = await get_workflow_rule(managed_rule.id, db=db, user=manager)
    assert detail.id == managed_rule.id

    runs = await list_workflow_runs(
        limit=1,
        cursor=None,
        rule_id=None,
        run_status=None,
        db=db,
        user=manager,
    )
    assert [item.id for item in runs.items] == [managed_terminal.id]
    assert runs.next_cursor is None
    hidden_run_filter = await list_workflow_runs(
        limit=10,
        cursor=None,
        rule_id=global_rule.id,
        run_status=None,
        db=db,
        user=manager,
    )
    assert hidden_run_filter.items == []
    assert hidden_run_filter.next_cursor is None

    await _expect_hidden(get_workflow_rule(global_rule.id, db=db, user=manager))
    await _expect_hidden(get_workflow_rule(other_rule.id, db=db, user=manager))
    await _expect_hidden(
        update_workflow_rule(
            global_rule.id,
            WorkflowRuleUpdate(name="Should not persist"),
            db=db,
            user=manager,
        )
    )
    await _expect_hidden(pause_workflow_rule(global_rule.id, db=db, user=manager))
    global_rule = await db.get(WorkflowRule, global_rule.id)
    global_rule.is_active = False
    await db.commit()
    await _expect_hidden(resume_workflow_rule(global_rule.id, db=db, user=manager))
    await _expect_hidden(
        preview_workflow_rule(
            global_rule.id,
            WorkflowDryRunRequest(),
            db=db,
            user=manager,
        )
    )
    await _expect_hidden(disable_workflow_rule(global_rule.id, db=db, user=manager))
    await _expect_hidden(
        process_organization_workflows(
            rule_id=global_rule.id,
            limit=50,
            db=db,
            user=manager,
        )
    )
    await _expect_hidden(
        replay_terminal_workflow_run(other_terminal.id, db=db, user=manager)
    )
    await _expect_hidden(
        cancel_active_workflow_run(global_pending.id, db=db, user=manager)
    )
    await _expect_hidden(
        create_workflow_rule(
            WorkflowRuleCreate.model_validate(
                {
                    "name": "Hidden global creation",
                    "trigger": "candidate_added",
                    "conditions": {},
                    "actions": action,
                }
            ),
            db=db,
            user=manager,
        )
    )

    global_pending = await db.get(WorkflowRun, global_pending.id)
    other_terminal = await db.get(WorkflowRun, other_terminal.id)
    global_rule = await db.get(WorkflowRule, global_rule.id)
    assert global_pending.status == "queued"
    assert other_terminal.status == "dead_letter"
    assert global_rule.name == "A global hidden rule"
    assert global_rule.is_active is False
    assert (
        await db.execute(
            select(func.count(WorkflowRun.id)).where(
                WorkflowRun.organization_id == organization.id,
                WorkflowRun.replay_of_run_id.is_not(None),
            )
        )
    ).scalar_one() == 0
