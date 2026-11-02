"""Workflow rule administration and durable run history."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import user_with_permissions
from app.db import get_session
from app.models.communication import CommunicationTemplate
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun
from app.schemas.pagination import CursorPage
from app.schemas.workflows import (
    WorkflowDryRunOut,
    WorkflowDryRunRequest,
    WorkflowProcessOut,
    WorkflowRuleCreate,
    WorkflowRuleOut,
    WorkflowRuleUpdate,
    WorkflowRunOut,
    WorkflowRunPage,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.workflow_authorization import (
    require_workflow_definition_access,
    workflow_access_requirements,
)
from app.services.workflows import (
    WorkflowExecutionError,
    cancel_workflow_run,
    dry_run_workflow_rule,
    materialize_scheduled_workflow_runs,
    replay_workflow_run,
    workflow_cancellation_requested,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])
workflow_manager = user_with_permissions("workflows.manage")
workflow_reader = user_with_permissions("workflows.read")
workflow_executor = user_with_permissions("workflows.execute")
WORKFLOW_RUN_STATUSES = {
    "queued",
    "running",
    "retrying",
    "completed",
    "skipped",
    "failed",
    "dead_letter",
    "cancelled",
}


def _rule_out(rule: WorkflowRule) -> WorkflowRuleOut:
    requirements = workflow_access_requirements(
        conditions=rule.conditions,
        actions=rule.actions,
    )
    return WorkflowRuleOut.model_validate(
        {
            "id": rule.id,
            "version": rule.version,
            "name": rule.name,
            "trigger": rule.trigger,
            "conditions": rule.conditions,
            "actions": rule.actions,
            "required_permissions": list(requirements.permissions),
            "required_fields": [field.value for field in requirements.fields],
            "is_active": rule.is_active,
            "starts_at": rule.starts_at,
            "last_processed_at": rule.last_processed_at,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
        }
    )


def _run_out(run: WorkflowRun) -> WorkflowRunOut:
    requirements = workflow_access_requirements(
        conditions=run.conditions_snapshot,
        actions=run.actions_snapshot,
    )
    return WorkflowRunOut(
        id=run.id,
        rule_id=run.rule_id,
        rule_id_snapshot=run.rule_id_snapshot,
        rule_version=run.rule_version_snapshot,
        replay_of_run_id=run.replay_of_run_id,
        rule_name=run.rule_name_snapshot,
        trigger=run.trigger_snapshot,
        candidate_id=run.candidate_id,
        position_id=run.position_id,
        application_id=run.application_id,
        source_activity_id=run.source_activity_id,
        status=run.status,
        action_results=run.action_results or [],
        required_permissions=list(requirements.permissions),
        required_fields=[field.value for field in requirements.fields],
        error_code=run.error_code,
        available_at=run.available_at,
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
        cancellation_requested=(
            run.status == "running" and workflow_cancellation_requested(run)
        ),
        cancelled_at=run.cancelled_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
    )


async def _validate_references(
    db: AsyncSession,
    *,
    organization_id: UUID,
    body: WorkflowRuleCreate,
) -> None:
    if body.conditions.position_id is not None:
        exists = (
            await db.execute(
                select(Position.id).where(
                    Position.id == body.conditions.position_id,
                    Position.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    for action in body.actions:
        if action.type == "assign_owner":
            exists = (
                await db.execute(
                    select(User.id).where(
                        User.id == action.user_id,
                        User.organization_id == organization_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Team member not found")
        elif action.type == "send_template":
            template = (
                await db.execute(
                    select(CommunicationTemplate).where(
                        CommunicationTemplate.id == action.template_id,
                        CommunicationTemplate.organization_id == organization_id,
                        CommunicationTemplate.channel == action.channel,
                        CommunicationTemplate.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if template is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "Communication template not found"
                )


async def _require_rule_access(
    db: AsyncSession,
    *,
    user: User,
    rule: WorkflowRule,
) -> None:
    await _require_hiring_manager_conditions_scope(
        db, user=user, conditions=rule.conditions
    )
    await require_workflow_definition_access(
        db,
        user,
        conditions=rule.conditions,
        actions=rule.actions,
    )


async def _require_run_access(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
) -> None:
    await _require_hiring_manager_conditions_scope(
        db, user=user, conditions=run.conditions_snapshot
    )
    await require_workflow_definition_access(
        db,
        user,
        conditions=run.conditions_snapshot,
        actions=run.actions_snapshot,
    )


def _hiring_manager_conditions_filter(user: User, column):
    if user.role != "hiring_manager":
        return None
    managed_positions = select(cast(Requisition.position_id, String)).where(
        Requisition.organization_id == user.organization_id,
        Requisition.hiring_manager_user_id == user.id,
        Requisition.position_id.is_not(None),
    )
    return column["position_id"].astext.in_(managed_positions)


async def _require_hiring_manager_conditions_scope(
    db: AsyncSession,
    *,
    user: User,
    conditions,
) -> None:
    if user.role != "hiring_manager":
        return
    if hasattr(conditions, "model_dump"):
        raw_conditions = conditions.model_dump(mode="json", exclude_none=True)
    else:
        raw_conditions = conditions or {}
    raw_position_id = raw_conditions.get("position_id")
    if raw_position_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")
    try:
        position_id = UUID(str(raw_position_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Workflow rule not found"
        ) from exc
    visible = (
        await db.execute(
            select(Requisition.id).where(
                Requisition.organization_id == user.organization_id,
                Requisition.hiring_manager_user_id == user.id,
                Requisition.position_id == position_id,
            )
        )
    ).scalar_one_or_none()
    if visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")


@router.get("/rules", response_model=CursorPage[WorkflowRuleOut])
async def list_workflow_rules(
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_reader),
) -> CursorPage[WorkflowRuleOut]:
    stmt = select(WorkflowRule).where(
        WorkflowRule.organization_id == user.organization_id
    )
    manager_filter = _hiring_manager_conditions_filter(user, WorkflowRule.conditions)
    if manager_filter is not None:
        stmt = stmt.where(manager_filter)
    if not include_inactive:
        stmt = stmt.where(WorkflowRule.is_active.is_(True))
    namespace = "workflows.rules"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "include_inactive": include_inactive,
            "actor_id": str(user.id),
            "actor_role": user.role,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid workflow rules cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid workflow rules cursor"},
            )
        cursor_id = cursor_uuid(payload, detail="Invalid workflow rules cursor")
        stmt = stmt.where(
            or_(
                WorkflowRule.name > cursor_name,
                and_(WorkflowRule.name == cursor_name, WorkflowRule.id > cursor_id),
            )
        )
    rules = list(
        (
            await db.execute(
                stmt.order_by(WorkflowRule.name, WorkflowRule.id).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(rules) > limit
    rules = rules[:limit]
    for rule in rules:
        await _require_rule_access(db, user=user, rule=rule)
    return CursorPage(
        items=[_rule_out(rule) for rule in rules],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"name": rules[-1].name, "id": str(rules[-1].id)},
            )
            if has_more and rules
            else None
        ),
    )


@router.post(
    "/rules", response_model=WorkflowRuleOut, status_code=status.HTTP_201_CREATED
)
async def create_workflow_rule(
    body: WorkflowRuleCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_manager),
) -> WorkflowRuleOut:
    await _require_hiring_manager_conditions_scope(
        db, user=user, conditions=body.conditions
    )
    await require_workflow_definition_access(
        db,
        user,
        conditions=body.conditions,
        actions=body.actions,
    )
    await _validate_references(db, organization_id=user.organization_id, body=body)
    now = datetime.now(UTC)
    rule = WorkflowRule(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        name=body.name,
        trigger=body.trigger,
        conditions=body.conditions.model_dump(mode="json", exclude_none=True),
        actions=[action.model_dump(mode="json") for action in body.actions],
        is_active=body.is_active,
        starts_at=now,
    )
    db.add(rule)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A workflow with this name already exists"
        ) from exc
    await db.refresh(rule)
    return _rule_out(rule)


@router.get("/rules/{rule_id}", response_model=WorkflowRuleOut)
async def get_workflow_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_reader),
) -> WorkflowRuleOut:
    rule = (
        await db.execute(
            select(WorkflowRule).where(
                WorkflowRule.id == rule_id,
                WorkflowRule.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")
    await _require_rule_access(db, user=user, rule=rule)
    return _rule_out(rule)


@router.patch("/rules/{rule_id}", response_model=WorkflowRuleOut)
async def update_workflow_rule(
    rule_id: UUID,
    body: WorkflowRuleUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_manager),
) -> WorkflowRuleOut:
    rule = (
        await db.execute(
            select(WorkflowRule)
            .where(
                WorkflowRule.id == rule_id,
                WorkflowRule.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")
    await _require_rule_access(db, user=user, rule=rule)
    merged = WorkflowRuleCreate.model_validate(
        {
            "name": body.name if body.name is not None else rule.name,
            "trigger": rule.trigger,
            "conditions": (
                body.conditions.model_dump(mode="json", exclude_none=True)
                if body.conditions is not None
                else rule.conditions
            ),
            "actions": (
                [action.model_dump(mode="json") for action in body.actions]
                if body.actions is not None
                else rule.actions
            ),
            "is_active": body.is_active
            if body.is_active is not None
            else rule.is_active,
        }
    )
    await require_workflow_definition_access(
        db,
        user,
        conditions=merged.conditions,
        actions=merged.actions,
    )
    await _require_hiring_manager_conditions_scope(
        db, user=user, conditions=merged.conditions
    )
    await _validate_references(db, organization_id=user.organization_id, body=merged)
    next_conditions = merged.conditions.model_dump(mode="json", exclude_none=True)
    next_actions = [action.model_dump(mode="json") for action in merged.actions]
    semantic_change = (
        rule.name != merged.name
        or rule.conditions != next_conditions
        or rule.actions != next_actions
    )
    rule.name = merged.name
    rule.conditions = next_conditions
    rule.actions = next_actions
    if semantic_change:
        rule.version += 1
    should_restart = semantic_change or (body.is_active is True and not rule.is_active)
    rule.is_active = merged.is_active
    if should_restart:
        rule.starts_at = datetime.now(UTC)
        rule.last_processed_at = None
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A workflow with this name already exists"
        ) from exc
    await db.refresh(rule)
    return _rule_out(rule)


async def _locked_rule(
    db: AsyncSession, *, organization_id: UUID, rule_id: UUID
) -> WorkflowRule:
    rule = (
        await db.execute(
            select(WorkflowRule)
            .where(
                WorkflowRule.id == rule_id,
                WorkflowRule.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")
    return rule


@router.post("/rules/{rule_id}/pause", response_model=WorkflowRuleOut)
async def pause_workflow_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_manager),
) -> WorkflowRuleOut:
    rule = await _locked_rule(db, organization_id=user.organization_id, rule_id=rule_id)
    await _require_rule_access(db, user=user, rule=rule)
    rule.is_active = False
    await db.commit()
    await db.refresh(rule)
    return _rule_out(rule)


@router.post("/rules/{rule_id}/resume", response_model=WorkflowRuleOut)
async def resume_workflow_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_manager),
) -> WorkflowRuleOut:
    rule = await _locked_rule(db, organization_id=user.organization_id, rule_id=rule_id)
    await _require_rule_access(db, user=user, rule=rule)
    if not rule.is_active:
        rule.is_active = True
        rule.starts_at = datetime.now(UTC)
        rule.last_processed_at = None
    await db.commit()
    await db.refresh(rule)
    return _rule_out(rule)


@router.post("/rules/{rule_id}/dry-run", response_model=WorkflowDryRunOut)
async def preview_workflow_rule(
    rule_id: UUID,
    body: WorkflowDryRunRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_manager),
) -> WorkflowDryRunOut:
    rule = (
        await db.execute(
            select(WorkflowRule).where(
                WorkflowRule.id == rule_id,
                WorkflowRule.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")
    await _require_rule_access(db, user=user, rule=rule)
    try:
        return await dry_run_workflow_rule(db, rule=rule, request=body)
    except WorkflowExecutionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.code) from exc
    finally:
        # Release the read transaction and make the no-write contract explicit.
        await db.rollback()


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_workflow_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_manager),
) -> None:
    rule = await _locked_rule(db, organization_id=user.organization_id, rule_id=rule_id)
    await _require_rule_access(db, user=user, rule=rule)
    rule.is_active = False
    await db.commit()


@router.post(
    "/process",
    response_model=WorkflowProcessOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def process_organization_workflows(
    rule_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_executor),
) -> WorkflowProcessOut:
    rules_stmt = select(WorkflowRule).where(
        WorkflowRule.organization_id == user.organization_id,
        WorkflowRule.is_active.is_(True),
    )
    manager_filter = _hiring_manager_conditions_filter(user, WorkflowRule.conditions)
    if manager_filter is not None:
        rules_stmt = rules_stmt.where(manager_filter)
    if rule_id is not None:
        rules_stmt = rules_stmt.where(WorkflowRule.id == rule_id)
    rules_to_process = list((await db.execute(rules_stmt)).scalars())
    if rule_id is not None and not rules_to_process:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow rule not found")
    for rule in rules_to_process:
        await _require_rule_access(db, user=user, rule=rule)
    rules = len(rules_to_process)
    queued = await materialize_scheduled_workflow_runs(
        db,
        organization_id=user.organization_id,
        rule_id=rule_id,
        rule_ids={rule.id for rule in rules_to_process},
        limit=limit,
    )
    await db.commit()
    return WorkflowProcessOut(rules=rules, queued=queued)


def _encode_cursor(created_at: datetime, run_id: UUID, *, scope: str) -> str:
    return encode_collection_cursor(
        "workflows.runs",
        scope=scope,
        boundary={"created_at": created_at.isoformat(), "id": str(run_id)},
    )


def _decode_cursor(value: str, *, scope: str) -> tuple[datetime, UUID]:
    payload = decode_collection_cursor(
        value,
        namespace="workflows.runs",
        scope=scope,
        boundary_keys={"created_at", "id"},
        detail="Invalid workflow cursor",
    )
    return (
        cursor_datetime(payload, "created_at", detail="Invalid workflow cursor"),
        cursor_uuid(payload, detail="Invalid workflow cursor"),
    )


@router.post(
    "/runs/{run_id}/replay",
    response_model=WorkflowRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def replay_terminal_workflow_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_executor),
) -> WorkflowRunOut:
    source = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow run not found")
    await _require_run_access(db, user=user, run=source)
    replay, _created = await replay_workflow_run(
        db,
        organization_id=user.organization_id,
        run_id=run_id,
    )
    await db.commit()
    await db.refresh(replay)
    return _run_out(replay)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=WorkflowRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_active_workflow_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_executor),
) -> WorkflowRunOut:
    source = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow run not found")
    await _require_run_access(db, user=user, run=source)
    run = await cancel_workflow_run(
        db,
        organization_id=user.organization_id,
        run_id=run_id,
    )
    await db.commit()
    await db.refresh(run)
    return _run_out(run)


@router.get("/runs", response_model=WorkflowRunPage)
async def list_workflow_runs(
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
    rule_id: UUID | None = None,
    run_status: str | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_session),
    user: User = Depends(workflow_reader),
) -> WorkflowRunPage:
    stmt = select(WorkflowRun).where(
        WorkflowRun.organization_id == user.organization_id
    )
    manager_filter = _hiring_manager_conditions_filter(
        user, WorkflowRun.conditions_snapshot
    )
    if manager_filter is not None:
        stmt = stmt.where(manager_filter)
    if rule_id is not None:
        stmt = stmt.where(WorkflowRun.rule_id == rule_id)
    if run_status is not None:
        if run_status not in WORKFLOW_RUN_STATUSES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid workflow status"
            )
        stmt = stmt.where(WorkflowRun.status == run_status)
    cursor_scope = collection_cursor_scope(
        "workflows.runs",
        organization_id=user.organization_id,
        filters={
            "rule_id": str(rule_id) if rule_id else None,
            "status": run_status,
            "actor_id": str(user.id),
            "actor_role": user.role,
        },
    )
    if cursor:
        created_at, run_id_value = _decode_cursor(cursor, scope=cursor_scope)
        stmt = stmt.where(
            or_(
                WorkflowRun.created_at < created_at,
                and_(
                    WorkflowRun.created_at == created_at,
                    WorkflowRun.id < run_id_value,
                ),
            )
        )
    runs = list(
        (
            await db.execute(
                stmt.order_by(
                    WorkflowRun.created_at.desc(), WorkflowRun.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    page = runs[:limit]
    for run in page:
        await _require_run_access(db, user=user, run=run)
    return WorkflowRunPage(
        items=[_run_out(run) for run in page],
        next_cursor=(
            _encode_cursor(page[-1].created_at, page[-1].id, scope=cursor_scope)
            if len(runs) > limit and page
            else None
        ),
    )
