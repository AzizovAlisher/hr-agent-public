"""Workflow matcher and atomic action executor."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    String,
    and_,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_note import CandidateTag
from app.models.notification import Notification
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.position import Position
from app.models.score import Score
from app.models.scorecard import ScorecardAssignment
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun
from app.schemas.applications import ApplicationStageTransition
from app.schemas.communications import CandidateCommunicationCompose
from app.schemas.workflows import (
    WorkflowDryRunActionOut,
    WorkflowDryRunItemOut,
    WorkflowDryRunOut,
    WorkflowDryRunRequest,
)
from app.services.activities import append_candidate_activity
from app.services.applications import (
    apply_application_stage_transition,
)
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.communications import (
    CommunicationPreparationError,
    create_outbound_message,
    prepare_candidate_communication,
)
from app.services.enterprise import require_permission
from app.services.entitlements import EntitlementExceeded
from app.services.workflow_authorization import require_workflow_definition_access
from app.services.structured_hiring import (
    application_is_active,
    resolve_existing_active_application,
    resolve_linked_application,
)

settings = get_settings()
WORKFLOW_RUN_ENTITLEMENT_METRIC = "workflow_runs"
_CANCELLATION_CONTROL_TYPE = "workflow_control"
_CANCELLATION_REQUESTED_STATUS = "cancellation_requested"


class WorkflowExecutionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class WorkflowCancellationRequested(WorkflowExecutionError):
    def __init__(self) -> None:
        super().__init__("cancellation_requested")


async def _require_active_creator_authority(
    db: AsyncSession,
    *,
    definition: "WorkflowDefinition",
) -> User:
    """Re-authorize the human principal whose rule is about to execute.

    Immutable snapshots protect queued semantics from edits; they are not a
    durable grant of authority.  Disabling the creator or revoking either
    workflows.manage, an action permission, or a field override must stop all
    queued/replayed effects before the first action runs.
    """

    live_rule = (
        await db.execute(
            select(WorkflowRule)
            .where(
                WorkflowRule.id == definition.id,
                WorkflowRule.organization_id == definition.organization_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if live_rule is None or live_rule.created_by_user_id is None:
        raise WorkflowExecutionError("workflow_creator_unavailable")
    creator = (
        await db.execute(
            select(User)
            .where(
                User.id == live_rule.created_by_user_id,
                User.organization_id == definition.organization_id,
                User.is_active.is_(True),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if creator is None:
        raise WorkflowExecutionError("workflow_creator_inactive")
    try:
        await require_permission(db, creator, "workflows.manage")
        await require_workflow_definition_access(
            db,
            creator,
            conditions=definition.conditions,
            actions=definition.actions,
        )
    except HTTPException as exc:
        raise WorkflowExecutionError("workflow_creator_authority_revoked") from exc
    except ValueError as exc:
        raise WorkflowExecutionError("invalid_rule_snapshot") from exc
    return creator


def _period_start(period: str, now: datetime) -> datetime | None:
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


async def _reserve_workflow_run_quota(
    db: AsyncSession,
    *,
    organization_id: UUID,
    run_id: UUID,
    now: datetime,
) -> tuple[int | None, int]:
    """Reserve one contract workflow-run unit exactly once.

    Workflow execution is intentionally metered in this subsystem rather than
    borrowing the integration-run metric.  Pilot contracts are provisioned by
    operators, so an absent/NULL ``workflow_runs`` entitlement is unlimited.
    The shared ledger remains the immutable usage audit trail.
    """

    key = f"workflow-run:{run_id}"
    existing = (
        await db.execute(
            select(UsageLedgerEntry.id).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == WORKFLOW_RUN_ENTITLEMENT_METRIC,
                UsageLedgerEntry.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {
            "key": (
                f"entitlement:{organization_id}:"
                f"{WORKFLOW_RUN_ENTITLEMENT_METRIC}"
            )
        },
    )
    entitlement = (
        await db.execute(
            select(OrganizationEntitlement).where(
                OrganizationEntitlement.organization_id == organization_id,
                OrganizationEntitlement.metric == WORKFLOW_RUN_ENTITLEMENT_METRIC,
                or_(
                    OrganizationEntitlement.effective_from.is_(None),
                    OrganizationEntitlement.effective_from <= now,
                ),
                or_(
                    OrganizationEntitlement.effective_until.is_(None),
                    OrganizationEntitlement.effective_until > now,
                ),
            )
        )
    ).scalar_one_or_none()
    period = entitlement.period if entitlement is not None else "contract"
    usage_conditions = [
        UsageLedgerEntry.organization_id == organization_id,
        UsageLedgerEntry.metric == WORKFLOW_RUN_ENTITLEMENT_METRIC,
    ]
    start = _period_start(period, now)
    if start is not None:
        usage_conditions.append(UsageLedgerEntry.occurred_at >= start)
    used = max(
        0,
        int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(UsageLedgerEntry.amount), 0)).where(
                        *usage_conditions
                    )
                )
            ).scalar_one()
        ),
    )
    if existing is not None:
        return entitlement.limit_value if entitlement is not None else None, used
    # Recheck idempotency after the tenant/metric lock: a concurrent worker may
    # have reserved this same durable run while this transaction waited.
    existing = (
        await db.execute(
            select(UsageLedgerEntry.id).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == WORKFLOW_RUN_ENTITLEMENT_METRIC,
                UsageLedgerEntry.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return entitlement.limit_value if entitlement is not None else None, used
    limit = entitlement.limit_value if entitlement is not None else None
    if limit is not None and used + 1 > limit:
        raise EntitlementExceeded(
            metric=WORKFLOW_RUN_ENTITLEMENT_METRIC,
            limit=limit,
            used=used,
            requested=1,
        )
    db.add(
        UsageLedgerEntry(
            organization_id=organization_id,
            metric=WORKFLOW_RUN_ENTITLEMENT_METRIC,
            amount=1,
            kind="usage",
            idempotency_key=key,
            resource_type="workflow_run",
            resource_id=run_id,
            metadata_json={},
            occurred_at=now,
        )
    )
    await db.flush()
    return limit, used + 1


def workflow_cancellation_requested(run: WorkflowRun) -> bool:
    if run.cancelled_at is not None:
        return True
    return any(
        isinstance(item, dict)
        and item.get("type") == _CANCELLATION_CONTROL_TYPE
        and item.get("status") == _CANCELLATION_REQUESTED_STATUS
        for item in (run.action_results or [])
    )


async def _raise_if_cancellation_requested(
    db: AsyncSession, run: WorkflowRun
) -> None:
    # READ COMMITTED refreshes make a cancellation committed by another API
    # transaction visible between actions without holding a long row lock.
    await db.refresh(run, attribute_names=["cancelled_at", "action_results"])
    if workflow_cancellation_requested(run):
        raise WorkflowCancellationRequested()


async def cancel_workflow_run(
    db: AsyncSession,
    *,
    organization_id: UUID,
    run_id: UUID,
    now: datetime | None = None,
) -> WorkflowRun:
    """Cancel a pending run or durably request cancellation of a running run."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    run = (
        await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Workflow run not found")
    if run.status == "cancelled":
        return run
    if run.status not in {"queued", "retrying", "running"}:
        raise HTTPException(
            409,
            {
                "code": "workflow_run_terminal",
                "detail": "A terminal workflow run cannot be cancelled",
            },
        )
    run.cancelled_at = run.cancelled_at or current
    control = {
        "type": _CANCELLATION_CONTROL_TYPE,
        "status": _CANCELLATION_REQUESTED_STATUS,
    }
    if not any(
        isinstance(item, dict)
        and item.get("type") == _CANCELLATION_CONTROL_TYPE
        for item in (run.action_results or [])
    ):
        run.action_results = [control, *(run.action_results or [])]
    if run.status in {"queued", "retrying"}:
        run.status = "cancelled"
        run.error_code = "cancelled_by_user"
        run.completed_at = current
        _clear_materialized_run_lease(run)
    else:
        run.error_code = "cancellation_requested"
    await db.flush()
    return run


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Immutable rule definition used for matching and action execution."""

    id: UUID
    organization_id: UUID
    name: str
    trigger: str
    conditions: dict[str, Any]
    actions: list[dict[str, Any]]
    version: int


def _definition_from_rule(rule: WorkflowRule) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=rule.id,
        organization_id=rule.organization_id,
        name=rule.name,
        trigger=rule.trigger,
        conditions=dict(rule.conditions or {}),
        actions=[dict(action) for action in (rule.actions or [])],
        version=rule.version,
    )


def _definition_from_run(run: WorkflowRun) -> WorkflowDefinition:
    conditions = run.conditions_snapshot
    actions = run.actions_snapshot
    if (
        run.rule_version_snapshot < 1
        or not isinstance(conditions, dict)
        or not isinstance(actions, list)
        or any(not isinstance(action, dict) for action in actions)
    ):
        raise WorkflowExecutionError("invalid_rule_snapshot")
    return WorkflowDefinition(
        id=run.rule_id_snapshot,
        organization_id=run.organization_id,
        name=run.rule_name_snapshot,
        trigger=run.trigger_snapshot,
        conditions=dict(conditions),
        actions=[dict(action) for action in actions],
        version=run.rule_version_snapshot,
    )


def _run_snapshot(rule: WorkflowRule) -> dict[str, Any]:
    return {
        "rule_id_snapshot": rule.id,
        "rule_version_snapshot": rule.version,
        "rule_name_snapshot": rule.name,
        "trigger_snapshot": rule.trigger,
        "conditions_snapshot": dict(rule.conditions or {}),
        "actions_snapshot": [dict(action) for action in (rule.actions or [])],
    }


@dataclass
class WorkflowContext:
    candidate: Candidate
    position: Position | None
    application: Application | None
    score: Score | None
    activity: CandidateActivity | None


@dataclass
class WorkflowCounters:
    rules: int = 0
    matched: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0

    def add(self, status: str, *, matched: bool) -> None:
        if matched:
            self.matched += 1
        if status == "completed":
            self.completed += 1
        elif status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1


async def _context(
    db: AsyncSession,
    *,
    rule: WorkflowDefinition,
    candidate_id: UUID,
    position_id: UUID | None,
    application_id: UUID | None,
    activity: CandidateActivity | None,
    lock: bool = True,
) -> WorkflowContext | None:
    candidate = await resolve_canonical_candidate(
        db, candidate_id, rule.organization_id, for_update=lock
    )
    if candidate is None:
        return None
    raw_conditions = rule.conditions or {}
    configured_position = raw_conditions.get("position_id")
    effective_position_id = (
        UUID(configured_position) if configured_position else position_id
    )
    score: Score | None = None
    position: Position | None = None
    application: Application | None = None
    if effective_position_id is not None:
        requested_application_id = application_id
        if configured_position and position_id != effective_position_id:
            requested_application_id = None
        try:
            if requested_application_id is not None:
                application = await resolve_linked_application(
                    db,
                    organization_id=rule.organization_id,
                    candidate_id=candidate.id,
                    position_id=effective_position_id,
                    application_id=requested_application_id,
                    lock=lock,
                )
            else:
                application = await resolve_existing_active_application(
                    db,
                    organization_id=rule.organization_id,
                    candidate_id=candidate.id,
                    position_id=effective_position_id,
                    application_id=None,
                    lock=lock,
                )
        except HTTPException as exc:
            raise WorkflowExecutionError("application_missing") from exc
        position = (
            await db.execute(
                select(Position).where(
                    Position.id == effective_position_id,
                    Position.organization_id == rule.organization_id,
                )
            )
        ).scalar_one_or_none()
        if position is None:
            raise WorkflowExecutionError("application_missing")
        if application.legacy_score_id is not None:
            score_stmt = select(Score).where(
                Score.id == application.legacy_score_id,
                Score.organization_id == rule.organization_id,
                Score.candidate_id == candidate.id,
                Score.position_id == effective_position_id,
            )
            if lock:
                score_stmt = score_stmt.with_for_update()
            score = (await db.execute(score_stmt)).scalar_one_or_none()
    elif any(
        raw_conditions.get(key) is not None
        for key in ("min_score", "max_score", "stage_in")
    ):
        stmt = (
            select(Application, Score, Position)
            .outerjoin(Score, Score.id == Application.legacy_score_id)
            .join(Position, Position.id == Application.position_id)
            .where(
                Application.organization_id == rule.organization_id,
                Application.candidate_id == candidate.id,
                Application.closed_at.is_(None),
                Application.archived_at.is_(None),
                Position.organization_id == rule.organization_id,
            )
            .order_by(Score.overall_score.desc().nullslast(), Score.id)
            .limit(1)
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await db.execute(stmt)).first()
        if row is not None:
            application, score, position = row
    return WorkflowContext(
        candidate=candidate,
        position=position,
        application=application,
        score=score,
        activity=activity,
    )


def _matches(rule: WorkflowDefinition, context: WorkflowContext) -> bool:
    conditions = rule.conditions or {}
    if conditions.get("position_id") and context.position is None:
        return False
    if (
        conditions.get("source_in")
        and (context.candidate.source or "").lower() not in conditions["source_in"]
    ):
        return False
    if conditions.get("stage_in") is not None:
        stage = (
            context.application.stage
            if context.application is not None
            else context.score.stage
            if context.score is not None
            else None
        )
        if stage not in conditions["stage_in"]:
            return False
    score_value = context.score.overall_score if context.score is not None else None
    if conditions.get("min_score") is not None and (
        score_value is None or score_value < conditions["min_score"]
    ):
        return False
    if conditions.get("max_score") is not None and (
        score_value is None or score_value > conditions["max_score"]
    ):
        return False
    return True


async def _target_score(
    db: AsyncSession, context: WorkflowContext, rule: WorkflowDefinition
) -> tuple[Application, Score | None, Position]:
    if context.application is not None and context.position is not None:
        if not application_is_active(context.application):
            raise WorkflowExecutionError("application_inactive")
        return context.application, context.score, context.position
    row = (
        await db.execute(
            select(Application, Score, Position)
            .outerjoin(Score, Score.id == Application.legacy_score_id)
            .join(Position, Position.id == Application.position_id)
            .where(
                Application.organization_id == rule.organization_id,
                Application.candidate_id == context.candidate.id,
                Application.closed_at.is_(None),
                Application.archived_at.is_(None),
                Position.organization_id == rule.organization_id,
            )
            .order_by(Score.overall_score.desc().nullslast(), Score.id)
            .with_for_update()
            .limit(1)
        )
    ).first()
    if row is None:
        raise WorkflowExecutionError("application_missing")
    return row[0], row[1], row[2]


async def _execute_actions(
    db: AsyncSession,
    *,
    rule: WorkflowDefinition,
    run: WorkflowRun,
    context: WorkflowContext,
    now: datetime,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, action in enumerate(rule.actions or []):
        # Authorization is intentionally live, even between actions.  A role,
        # permission, field-policy, or user-state revocation committed while a
        # long workflow is running stops the remaining effects; the savepoint
        # then rolls back the partial action set.
        await _require_active_creator_authority(db, definition=rule)
        await _raise_if_cancellation_requested(db, run)
        action_type = action.get("type")
        if action_type == "move_stage":
            if rule.trigger in {
                "candidate_scored",
                "candidate_rescored",
            } and action.get("stage") in {"hired", "rejected"}:
                # Defense in depth for rules stored before schema validation was
                # tightened. An AI-derived score may assist with routing, but it
                # can never produce an irreversible hiring decision.
                raise WorkflowExecutionError("human_decision_required")
            application, _score, position = await _target_score(db, context, rule)
            try:
                transition = ApplicationStageTransition(
                    stage=action["stage"],
                    expected_version=application.updated_at,
                    disposition_code=action.get("disposition_code"),
                    reason=action.get("reason") or f"Workflow: {rule.name}",
                    details={
                        "workflow_rule_id": str(rule.id),
                        "workflow_run_id": str(run.id),
                        "position_title": position.title,
                    },
                )
                _application, changed = await apply_application_stage_transition(
                    db,
                    organization_id=rule.organization_id,
                    application_id=application.id,
                    body=transition,
                    actor_user_id=None,
                    actor_kind="automation",
                    occurred_at=now,
                )
            except (HTTPException, ValidationError) as exc:
                raise WorkflowExecutionError("invalid_stage_transition") from exc
            results.append(
                {"type": action_type, "status": "changed" if changed else "noop"}
            )
        elif action_type == "assign_owner":
            application, score, position = await _target_score(db, context, rule)
            assignee = (
                await db.execute(
                    select(User).where(
                        User.id == UUID(action["user_id"]),
                        User.organization_id == rule.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if assignee is None:
                raise WorkflowExecutionError("owner_missing")
            changed = application.owner_user_id != assignee.id
            if changed:
                application.owner_user_id = assignee.id
                if score is not None:
                    score.owner_user_id = assignee.id
                append_candidate_activity(
                    db,
                    organization_id=rule.organization_id,
                    candidate_id=context.candidate.id,
                    position_id=position.id,
                    application_id=application.id,
                    event_type="candidate_owner_changed",
                    subject_type="score",
                    subject_id=score.id,
                    payload={"position_title": position.title, "automatic": True},
                    occurred_at=now,
                )
                db.add(
                    Notification(
                        organization_id=rule.organization_id,
                        user_id=assignee.id,
                        type="candidate_assigned",
                        payload={
                            "candidate_id": str(context.candidate.id),
                            "position_id": str(position.id),
                            "position_title": position.title,
                            "assigned_by_user_id": None,
                            "due_date": (
                                application.next_action_due_date.isoformat()
                                if application.next_action_due_date
                                else None
                            ),
                            "automatic": True,
                        },
                    )
                )
            results.append(
                {"type": action_type, "status": "changed" if changed else "noop"}
            )
        elif action_type == "set_due_date":
            application, score, position = await _target_score(db, context, rule)
            due = now.astimezone(
                ZoneInfo(settings.business_timezone)
            ).date() + timedelta(days=int(action["days_from_now"]))
            changed = application.next_action_due_date != due
            if changed:
                application.next_action_due_date = due
                if score is not None:
                    score.next_action_due_date = due
                append_candidate_activity(
                    db,
                    organization_id=rule.organization_id,
                    candidate_id=context.candidate.id,
                    position_id=position.id,
                    application_id=application.id,
                    event_type="next_action_due_changed",
                    subject_type="score",
                    subject_id=score.id,
                    payload={
                        "due_date": due.isoformat(),
                        "position_title": position.title,
                        "automatic": True,
                    },
                    occurred_at=now,
                )
            results.append(
                {"type": action_type, "status": "changed" if changed else "noop"}
            )
        elif action_type == "add_tag":
            label = action["label"]
            existing = (
                await db.execute(
                    select(CandidateTag).where(
                        CandidateTag.organization_id == rule.organization_id,
                        CandidateTag.candidate_id == context.candidate.id,
                        func.lower(CandidateTag.label) == label.lower(),
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                tag = CandidateTag(
                    organization_id=rule.organization_id,
                    candidate_id=context.candidate.id,
                    label=label,
                    created_by=None,
                )
                db.add(tag)
                await db.flush()
                append_candidate_activity(
                    db,
                    organization_id=rule.organization_id,
                    candidate_id=context.candidate.id,
                    event_type="tag_added",
                    subject_type="tag",
                    subject_id=tag.id,
                    payload={"label": label, "automatic": True},
                    occurred_at=now,
                )
                changed = True
            else:
                changed = False
            results.append(
                {"type": action_type, "status": "changed" if changed else "noop"}
            )
        elif action_type == "create_task":
            position_id = context.position.id if context.position is not None else None
            task = TaskSubmission(
                organization_id=rule.organization_id,
                candidate_id=context.candidate.id,
                position_id=position_id,
                application_id=(
                    context.application.id
                    if context.application is not None
                    else None
                ),
                created_by=None,
                title=action["title"],
                prompt=action.get("prompt"),
                submission_type="text",
                status="sent",
            )
            db.add(task)
            await db.flush()
            append_candidate_activity(
                db,
                organization_id=rule.organization_id,
                candidate_id=context.candidate.id,
                position_id=position_id,
                application_id=(
                    context.application.id
                    if context.application is not None
                    else None
                ),
                event_type="task_added",
                subject_type="task",
                subject_id=task.id,
                payload={"title": task.title, "status": task.status, "automatic": True},
                occurred_at=now,
            )
            results.append(
                {"type": action_type, "status": "created", "subject_id": str(task.id)}
            )
        elif action_type == "send_template":
            compose = CandidateCommunicationCompose(
                channel=action["channel"],
                template_id=UUID(action["template_id"]),
                position_id=context.position.id
                if context.position is not None
                else None,
                application_id=(
                    context.application.id
                    if context.application is not None
                    else None
                ),
            )
            try:
                prepared = await prepare_candidate_communication(
                    db,
                    organization_id=rule.organization_id,
                    actor=None,
                    candidate=context.candidate,
                    request=compose,
                )
            except CommunicationPreparationError as exc:
                raise WorkflowExecutionError(f"communication_{exc.code}") from exc
            message, created = await create_outbound_message(
                db,
                prepared=prepared,
                actor_user_id=None,
                idempotency_key=f"workflow:{run.id}:{index}",
                scheduled_for=None,
            )
            results.append(
                {
                    "type": action_type,
                    "status": "queued" if created else "noop",
                    "subject_id": str(message.id),
                }
            )
        elif action_type == "notify_owner":
            application, score, position = await _target_score(db, context, rule)
            owner_user_id = application.owner_user_id or (
                score.owner_user_id if score is not None else None
            )
            if owner_user_id is None:
                results.append({"type": action_type, "status": "skipped_no_owner"})
            else:
                notification = Notification(
                    organization_id=rule.organization_id,
                    user_id=owner_user_id,
                    type="workflow_reminder",
                    payload={
                        "candidate_id": str(context.candidate.id),
                        "position_id": str(position.id),
                        "position_title": position.title,
                        "title": action["title"],
                        "workflow_rule_id": str(rule.id),
                    },
                )
                db.add(notification)
                results.append({"type": action_type, "status": "created"})
        else:
            raise WorkflowExecutionError("unsupported_action")
    # Make cancellation linearizable with completion: a request committed
    # while the last action was running rolls the savepoint back before this
    # transaction can publish a completed result.
    await _raise_if_cancellation_requested(db, run)
    if any(item["status"] in {"changed", "created", "queued"} for item in results):
        append_candidate_activity(
            db,
            organization_id=rule.organization_id,
            candidate_id=context.candidate.id,
            position_id=context.position.id if context.position is not None else None,
            application_id=(
                context.application.id if context.application is not None else None
            ),
            event_type="workflow_completed",
            subject_type="workflow_run",
            subject_id=run.id,
            payload={
                "rule_name": rule.name,
                "action_types": [item.get("type") for item in rule.actions or []],
            },
            occurred_at=now,
        )
    return results


async def _process_one(
    db: AsyncSession,
    *,
    rule: WorkflowRule,
    candidate_id: UUID,
    position_id: UUID | None,
    application_id: UUID | None,
    activity: CandidateActivity | None,
    dedupe_key: str,
    now: datetime,
    commit: bool = True,
) -> tuple[str, bool]:
    rule_id_value = rule.id
    organization_id = rule.organization_id
    rule_name = rule.name
    source_activity_id = activity.id if activity is not None else None
    lock_value = f"{rule.id}:{dedupe_key}"
    locked = (
        await db.execute(
            select(func.pg_try_advisory_xact_lock(func.hashtextextended(lock_value, 0)))
        )
    ).scalar_one()
    if not locked:
        if commit:
            await db.commit()
        else:
            await db.flush()
        return "skipped", False
    existing = (
        await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.rule_id == rule.id,
                WorkflowRun.dedupe_key == dedupe_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status != "queued":
        if commit:
            await db.commit()
        else:
            await db.flush()
        return "skipped", False
    if existing is None:
        run = WorkflowRun(
            organization_id=organization_id,
            rule_id=rule_id_value,
            candidate_id=candidate_id,
            position_id=position_id,
            application_id=application_id,
            source_activity_id=source_activity_id,
            **_run_snapshot(rule),
            dedupe_key=dedupe_key,
            # Legacy synchronous evaluation remains uncommitted until terminal;
            # materialized background runs use the broker-managed running state.
            status="queued",
            started_at=now,
        )
        db.add(run)
        await db.flush()
    else:
        # A database trigger may have materialized the event before this
        # compatibility/manual evaluator runs. Consume that same queued row
        # under the advisory + row lock instead of creating or skipping it.
        run = existing
        if (
            run.candidate_id != candidate_id
            or run.position_id != position_id
            or run.application_id != application_id
        ):
            raise WorkflowExecutionError("workflow_input_conflict")
    await _reserve_workflow_run_quota(
        db,
        organization_id=organization_id,
        run_id=run.id,
        now=now,
    )
    run_id = run.id
    resolved_candidate_id = candidate_id
    resolved_position_id = position_id
    resolved_application_id = application_id
    context: WorkflowContext | None = None
    matched = False
    definition: WorkflowDefinition | None = None
    try:
        # A savepoint makes all domain actions atomic while retaining the
        # processing run and advisory lock. On failure, only action mutations
        # roll back; the same run becomes the durable failure record.
        async with db.begin_nested():
            definition = _definition_from_run(run)
            await _require_active_creator_authority(
                db,
                definition=definition,
            )
            context = await _context(
                db,
                rule=definition,
                candidate_id=candidate_id,
                position_id=position_id,
                application_id=application_id,
                activity=activity,
            )
            if context is not None:
                resolved_candidate_id = context.candidate.id
                if context.position is not None:
                    resolved_position_id = context.position.id
                if context.application is not None:
                    resolved_application_id = context.application.id
                run.candidate_id = resolved_candidate_id
                run.position_id = resolved_position_id
                run.application_id = resolved_application_id
                matched = _matches(definition, context)
            if matched and context is not None:
                results = await _execute_actions(
                    db,
                    rule=definition,
                    run=run,
                    context=context,
                    now=now,
                )
            else:
                results = []
        if not matched:
            run.status = "skipped"
            run.completed_at = now
            rule.last_processed_at = now
            if commit:
                await db.commit()
            else:
                await db.flush()
            return "skipped", False
        run.status = "completed"
        run.action_results = results
        run.completed_at = now
        rule.last_processed_at = now
        if commit:
            await db.commit()
        else:
            await db.flush()
        return "completed", True
    except Exception as exc:
        error_code = (
            exc.code if isinstance(exc, WorkflowExecutionError) else "internal_error"
        )
        # SQLAlchemy expires objects changed inside a rolled-back savepoint.
        # Refresh the outer run explicitly before converting it to the failure
        # record; accessing an expired attribute implicitly is not async-safe.
        await db.refresh(run)
        run.candidate_id = resolved_candidate_id
        run.position_id = resolved_position_id
        run.application_id = resolved_application_id
        run.status = "failed"
        run.action_results = []
        run.error_code = error_code[:64]
        run.completed_at = now
        rule.last_processed_at = now
        append_candidate_activity(
            db,
            organization_id=organization_id,
            candidate_id=resolved_candidate_id,
            position_id=resolved_position_id,
            application_id=resolved_application_id,
            event_type="workflow_failed",
            subject_type="workflow_run",
            subject_id=run_id,
            payload={
                "rule_name": rule_name,
                "error_code": run.error_code,
                "action_types": [
                    action.get("type")
                    for action in (
                        definition.actions
                        if definition is not None
                        else run.actions_snapshot or []
                    )
                ],
            },
            occurred_at=now,
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return "failed", True


async def _event_candidates(
    db: AsyncSession, rule: WorkflowRule, limit: int
) -> list[CandidateActivity]:
    already_consumed = exists(
        select(WorkflowRun.id).where(
            WorkflowRun.rule_id == rule.id,
            WorkflowRun.source_activity_id == CandidateActivity.id,
            WorkflowRun.status != "queued",
        )
    )
    stmt = select(CandidateActivity).where(
        CandidateActivity.organization_id == rule.organization_id,
        CandidateActivity.event_type == rule.trigger,
        CandidateActivity.occurred_at >= rule.starts_at,
        ~already_consumed,
    )
    configured_position = (rule.conditions or {}).get("position_id")
    if configured_position:
        stmt = stmt.where(
            CandidateActivity.position_id == UUID(configured_position),
            CandidateActivity.application_id.is_not(None),
        )
    return list(
        (
            await db.execute(
                stmt.order_by(
                    CandidateActivity.occurred_at,
                    CandidateActivity.id,
                ).limit(limit)
            )
        ).scalars()
    )


async def _inactive_candidates(
    db: AsyncSession, rule: WorkflowRule, limit: int, *, now: datetime
) -> list[tuple[UUID, UUID, UUID, datetime]]:
    days = int((rule.conditions or {}).get("inactivity_days", 7))
    latest_activity = (
        select(func.max(CandidateActivity.occurred_at))
        .where(
            CandidateActivity.organization_id == rule.organization_id,
            CandidateActivity.candidate_id == Application.candidate_id,
            CandidateActivity.event_type.notin_(
                ("workflow_completed", "workflow_failed")
            ),
            or_(
                CandidateActivity.application_id == Application.id,
                and_(
                    CandidateActivity.position_id.is_(None),
                    CandidateActivity.application_id.is_(None),
                ),
            ),
        )
        .correlate(Application)
        .scalar_subquery()
    )
    last_activity = func.coalesce(
        latest_activity, Score.scored_at, Application.applied_at, Candidate.created_at
    )
    already_handled = exists(
        select(WorkflowRun.id).where(
            WorkflowRun.rule_id == rule.id,
            WorkflowRun.application_id == Application.id,
            WorkflowRun.started_at >= func.greatest(last_activity, rule.starts_at),
        )
    )
    stmt = (
        select(
            Application.candidate_id,
            Application.position_id,
            Application.id,
            last_activity,
        )
        .join(Candidate, Candidate.id == Application.candidate_id)
        .outerjoin(Score, Score.id == Application.legacy_score_id)
        .where(
            Application.organization_id == rule.organization_id,
            Candidate.organization_id == rule.organization_id,
            Candidate.merged_at.is_(None),
            Application.closed_at.is_(None),
            Application.archived_at.is_(None),
            last_activity <= now - timedelta(days=days),
            ~already_handled,
        )
    )
    configured_position = (rule.conditions or {}).get("position_id")
    if configured_position:
        stmt = stmt.where(Application.position_id == UUID(configured_position))
    return list(
        (
            await db.execute(
                stmt.order_by(last_activity, Application.id).limit(limit)
            )
        ).all()
    )


async def _feedback_candidates(
    db: AsyncSession, rule: WorkflowRule, limit: int, *, now: datetime
) -> list[tuple[UUID, UUID, UUID, UUID, datetime]]:
    rule_version = int(rule.starts_at.timestamp() * 1_000_000)
    due_epoch = cast(
        func.floor(func.extract("epoch", ScorecardAssignment.due_at)),
        BigInteger,
    )
    dedupe_expression = func.concat(
        literal("feedback:"),
        cast(ScorecardAssignment.id, String),
        literal(":"),
        cast(due_epoch, String),
        literal(f":{rule_version}"),
    )
    already_handled = exists(
        select(WorkflowRun.id).where(
            WorkflowRun.rule_id == rule.id,
            WorkflowRun.dedupe_key == dedupe_expression,
        )
    )
    stmt = select(
        ScorecardAssignment.candidate_id,
        ScorecardAssignment.position_id,
        ScorecardAssignment.application_id,
        ScorecardAssignment.id,
        ScorecardAssignment.due_at,
    ).where(
        ScorecardAssignment.organization_id == rule.organization_id,
        ScorecardAssignment.status == "draft",
        ScorecardAssignment.due_at.is_not(None),
        ScorecardAssignment.due_at <= now,
        ScorecardAssignment.position_id.is_not(None),
        ScorecardAssignment.application_id.is_not(None),
        ~already_handled,
    )
    configured_position = (rule.conditions or {}).get("position_id")
    if configured_position:
        stmt = stmt.where(ScorecardAssignment.position_id == UUID(configured_position))
    return list(
        (
            await db.execute(
                stmt.order_by(ScorecardAssignment.due_at, ScorecardAssignment.id).limit(
                    limit
                )
            )
        ).all()
    )


async def materialize_scheduled_workflow_runs(
    db: AsyncSession,
    *,
    organization_id: UUID,
    rule_id: UUID | None = None,
    rule_ids: set[UUID] | None = None,
    limit: int = 50,
    now: datetime | None = None,
) -> int:
    """Queue time-derived workflow inputs inside one tenant transaction.

    Event-derived inputs are materialized by the database activity trigger.
    Inactivity and feedback deadlines need a clock tick, so the tenant
    maintenance claim owns this bounded producer pass. It creates durable runs
    only; action execution remains in the workflow worker.
    """

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    per_rule_limit = max(1, min(limit, 100))
    rules_stmt = select(WorkflowRule).where(
        WorkflowRule.organization_id == organization_id,
        WorkflowRule.is_active.is_(True),
        WorkflowRule.trigger.in_(("candidate_inactive", "feedback_overdue")),
    )
    if rule_id is not None:
        rules_stmt = rules_stmt.where(WorkflowRule.id == rule_id)
    if rule_ids is not None:
        if not rule_ids:
            return 0
        rules_stmt = rules_stmt.where(WorkflowRule.id.in_(rule_ids))
    rules = list(
        (
            await db.execute(
                rules_stmt.order_by(WorkflowRule.created_at, WorkflowRule.id)
            )
        ).scalars()
    )
    created = 0
    for rule in rules:
        inputs: list[tuple[UUID, UUID | None, UUID | None, str]] = []
        if rule.trigger == "candidate_inactive":
            for (
                candidate_id,
                position_id,
                application_id,
                last_activity,
            ) in await _inactive_candidates(db, rule, per_rule_limit, now=current):
                cycle = last_activity.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
                version = int(rule.starts_at.timestamp() * 1_000_000)
                inputs.append(
                    (
                        candidate_id,
                        position_id,
                        application_id,
                        f"inactive:{candidate_id}:{position_id}:{cycle}:{version}",
                    )
                )
        else:
            for (
                candidate_id,
                position_id,
                application_id,
                assignment_id,
                due_at,
            ) in await _feedback_candidates(db, rule, per_rule_limit, now=current):
                due_epoch = int(due_at.timestamp())
                version = int(rule.starts_at.timestamp() * 1_000_000)
                inputs.append(
                    (
                        candidate_id,
                        position_id,
                        application_id,
                        f"feedback:{assignment_id}:{due_epoch}:{version}",
                    )
                )
        for candidate_id, position_id, application_id, dedupe_key in inputs:
            run = WorkflowRun(
                organization_id=organization_id,
                rule_id=rule.id,
                candidate_id=candidate_id,
                position_id=position_id,
                application_id=application_id,
                source_activity_id=None,
                **_run_snapshot(rule),
                dedupe_key=dedupe_key,
                status="queued",
                action_results=[],
                available_at=current,
                attempt_count=0,
                max_attempts=3,
                started_at=current,
            )
            db.add(run)
            await db.flush()
            await _reserve_workflow_run_quota(
                db,
                organization_id=organization_id,
                run_id=run.id,
                now=current,
            )
            created += 1
        # Flush between rules so the next candidate query sees the newly
        # materialized dedupe rows even when this pass has not committed yet.
        await db.flush()
    return created


_DRY_RUN_ACTION_TYPES = {
    "move_stage",
    "assign_owner",
    "set_due_date",
    "add_tag",
    "create_task",
    "send_template",
    "notify_owner",
}
_TERMINAL_RUN_STATUSES = {
    "completed",
    "skipped",
    "failed",
    "dead_letter",
    "cancelled",
}


async def dry_run_workflow_rule(
    db: AsyncSession,
    *,
    rule: WorkflowRule,
    request: WorkflowDryRunRequest,
    now: datetime | None = None,
) -> WorkflowDryRunOut:
    """Preview current matches and action types without any durable effects."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    definition = _definition_from_rule(rule)
    inputs: list[
        tuple[UUID, UUID | None, UUID | None, CandidateActivity | None]
    ] = []
    if request.candidate_id is not None:
        activity: CandidateActivity | None = None
        if request.source_activity_id is not None:
            activity = (
                await db.execute(
                    select(CandidateActivity).where(
                        CandidateActivity.id == request.source_activity_id,
                        CandidateActivity.organization_id == rule.organization_id,
                        CandidateActivity.candidate_id == request.candidate_id,
                    )
                )
            ).scalar_one_or_none()
            if activity is None:
                raise HTTPException(404, "Workflow preview target not found")
        inputs.append(
            (
                request.candidate_id,
                request.position_id
                if request.position_id is not None
                else activity.position_id
                if activity is not None
                else None,
                request.application_id
                if request.application_id is not None
                else activity.application_id
                if activity is not None
                else None,
                activity,
            )
        )
    elif rule.trigger == "candidate_inactive":
        inputs.extend(
            (candidate_id, position_id, application_id, None)
            for candidate_id, position_id, application_id, _last_activity in (
                await _inactive_candidates(db, rule, request.limit, now=current)
            )
        )
    elif rule.trigger == "feedback_overdue":
        inputs.extend(
            (candidate_id, position_id, application_id, None)
            for candidate_id, position_id, application_id, _assignment_id, _due_at in (
                await _feedback_candidates(db, rule, request.limit, now=current)
            )
        )
    else:
        inputs.extend(
            (
                activity.candidate_id,
                activity.position_id,
                activity.application_id,
                activity,
            )
            for activity in await _event_candidates(db, rule, request.limit)
        )

    preview_actions: list[WorkflowDryRunActionOut] = []
    for index, action in enumerate(definition.actions):
        action_type = action.get("type")
        if action_type not in _DRY_RUN_ACTION_TYPES:
            raise WorkflowExecutionError("invalid_rule_snapshot")
        preview_actions.append(
            WorkflowDryRunActionOut(
                index=index,
                type=action_type,
            )
        )

    items: list[WorkflowDryRunItemOut] = []
    for candidate_id, position_id, application_id, activity in inputs[: request.limit]:
        context = await _context(
            db,
            rule=definition,
            candidate_id=candidate_id,
            position_id=position_id,
            application_id=application_id,
            activity=activity,
            lock=False,
        )
        if context is None:
            if request.candidate_id is not None:
                raise HTTPException(404, "Workflow preview target not found")
            continue
        matched = _matches(definition, context)
        items.append(
            WorkflowDryRunItemOut(
                candidate_id=context.candidate.id,
                position_id=(
                    context.position.id if context.position is not None else position_id
                ),
                application_id=(
                    context.application.id
                    if context.application is not None
                    else application_id
                ),
                source_activity_id=activity.id if activity is not None else None,
                matched=matched,
                actions=list(preview_actions) if matched else [],
            )
        )
    return WorkflowDryRunOut(
        rule_id=rule.id,
        rule_version=rule.version,
        evaluated=len(items),
        matched=sum(1 for item in items if item.matched),
        items=items,
    )


async def replay_workflow_run(
    db: AsyncSession,
    *,
    organization_id: UUID,
    run_id: UUID,
    now: datetime | None = None,
) -> tuple[WorkflowRun, bool]:
    """Create one durable replay child for a terminal run, exactly once."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    source = (
        await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(404, "Workflow run not found")
    if source.status not in _TERMINAL_RUN_STATUSES:
        raise HTTPException(409, "Only terminal workflow runs can be replayed")
    existing = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.organization_id == organization_id,
                WorkflowRun.replay_of_run_id == source.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await _reserve_workflow_run_quota(
            db,
            organization_id=organization_id,
            run_id=existing.id,
            now=current,
        )
        return existing, False
    replay = WorkflowRun(
        organization_id=organization_id,
        rule_id=source.rule_id,
        rule_id_snapshot=source.rule_id_snapshot,
        rule_version_snapshot=source.rule_version_snapshot,
        candidate_id=source.candidate_id,
        position_id=source.position_id,
        application_id=source.application_id,
        source_activity_id=source.source_activity_id,
        rule_name_snapshot=source.rule_name_snapshot,
        trigger_snapshot=source.trigger_snapshot,
        conditions_snapshot=dict(source.conditions_snapshot or {}),
        actions_snapshot=[dict(action) for action in source.actions_snapshot or []],
        replay_of_run_id=source.id,
        dedupe_key=f"replay:{source.id}",
        status="queued",
        action_results=[],
        error_code=None,
        available_at=current,
        attempt_count=0,
        max_attempts=source.max_attempts,
        started_at=current,
    )
    db.add(replay)
    await db.flush()
    await _reserve_workflow_run_quota(
        db,
        organization_id=organization_id,
        run_id=replay.id,
        now=current,
    )
    return replay, True


async def process_workflows(
    db: AsyncSession,
    *,
    organization_id: UUID | None = None,
    rule_id: UUID | None = None,
    limit: int = 50,
    now: datetime | None = None,
    commit: bool = True,
) -> WorkflowCounters:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    stmt = select(WorkflowRule).where(WorkflowRule.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(WorkflowRule.organization_id == organization_id)
    if rule_id is not None:
        stmt = stmt.where(WorkflowRule.id == rule_id)
    rules = list(
        (
            await db.execute(stmt.order_by(WorkflowRule.created_at, WorkflowRule.id))
        ).scalars()
    )
    counters = WorkflowCounters(rules=len(rules))
    per_rule_limit = max(1, min(limit, 100))
    for rule in rules:
        if rule.trigger == "candidate_inactive":
            for (
                candidate_id,
                position_id,
                application_id,
                last_activity,
            ) in await _inactive_candidates(db, rule, per_rule_limit, now=current):
                cycle = last_activity.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
                version = int(rule.starts_at.timestamp() * 1_000_000)
                dedupe = f"inactive:{candidate_id}:{position_id}:{cycle}:{version}"
                status_value, matched = await _process_one(
                    db,
                    rule=rule,
                    candidate_id=candidate_id,
                    position_id=position_id,
                    application_id=application_id,
                    activity=None,
                    dedupe_key=dedupe,
                    now=current,
                    commit=commit,
                )
                counters.add(status_value, matched=matched)
        elif rule.trigger == "feedback_overdue":
            for (
                candidate_id,
                position_id,
                application_id,
                assignment_id,
                due_at,
            ) in await _feedback_candidates(db, rule, per_rule_limit, now=current):
                due_epoch = int(due_at.timestamp())
                version = int(rule.starts_at.timestamp() * 1_000_000)
                dedupe = f"feedback:{assignment_id}:{due_epoch}:{version}"
                status_value, matched = await _process_one(
                    db,
                    rule=rule,
                    candidate_id=candidate_id,
                    position_id=position_id,
                    application_id=application_id,
                    activity=None,
                    dedupe_key=dedupe,
                    now=current,
                    commit=commit,
                )
                counters.add(status_value, matched=matched)
        else:
            for activity in await _event_candidates(db, rule, per_rule_limit):
                status_value, matched = await _process_one(
                    db,
                    rule=rule,
                    candidate_id=activity.candidate_id,
                    position_id=activity.position_id,
                    application_id=activity.application_id,
                    activity=activity,
                    dedupe_key=f"activity:{activity.id}",
                    now=current,
                    commit=commit,
                )
                counters.add(status_value, matched=matched)
    return counters


def _clear_materialized_run_lease(run: WorkflowRun) -> None:
    run.leased_at = None
    run.leased_by = None
    run.lease_token = None
    run.lease_expires_at = None


async def process_materialized_workflow_run(
    db: AsyncSession,
    *,
    organization_id: UUID,
    run_id: UUID,
    now: datetime | None = None,
) -> str | None:
    """Execute one previously materialized WorkflowRun without committing.

    Broker discovery and lease validation are intentionally outside this
    domain function. The caller owns one tenant transaction containing the
    actions, run transition, lease acknowledgement, and any resulting local
    communication enqueue.
    """

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    run = (
        await db.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if run is None or run.status != "running":
        return None
    try:
        await _reserve_workflow_run_quota(
            db,
            organization_id=organization_id,
            run_id=run.id,
            now=current,
        )
    except EntitlementExceeded as exc:
        run.status = "dead_letter"
        run.action_results = [
            {
                "type": "quota",
                "status": "blocked",
                "metric": exc.metric,
                "limit": exc.limit,
                "used": exc.used,
                "requested": exc.requested,
            }
        ]
        run.error_code = "entitlement_exceeded"
        run.completed_at = current
        _clear_materialized_run_lease(run)
        await db.flush()
        return run.status
    # The immutable definition still executes, but authority is deliberately
    # live: deleting the rule, disabling its creator, or revoking a required
    # permission/field policy stops the queued effect before its first action.
    live_rule = (
        await db.execute(
            select(WorkflowRule).where(
                WorkflowRule.id == run.rule_id,
                WorkflowRule.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    try:
        await _raise_if_cancellation_requested(db, run)
    except WorkflowCancellationRequested:
        run.status = "cancelled"
        run.error_code = "cancelled_by_user"
        run.completed_at = current
        run.cancelled_at = run.cancelled_at or current
        _clear_materialized_run_lease(run)
        await db.flush()
        return run.status
    if run.candidate_id is None:
        run.status = "skipped"
        run.error_code = "candidate_missing"
        run.completed_at = current
        if live_rule is not None:
            live_rule.last_processed_at = current
        _clear_materialized_run_lease(run)
        await db.flush()
        return run.status

    activity: CandidateActivity | None = None
    if run.source_activity_id is not None:
        activity = (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.id == run.source_activity_id,
                    CandidateActivity.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
    try:
        async with db.begin_nested():
            definition = _definition_from_run(run)
            await _require_active_creator_authority(
                db,
                definition=definition,
            )
            context = await _context(
                db,
                rule=definition,
                candidate_id=run.candidate_id,
                position_id=run.position_id,
                application_id=run.application_id,
                activity=activity,
            )
            matched = context is not None and _matches(definition, context)
            results = (
                await _execute_actions(
                    db,
                    rule=definition,
                    run=run,
                    context=context,
                    now=current,
                )
                if matched and context is not None
                else []
            )
        run.status = "completed" if matched else "skipped"
        run.action_results = results
        run.error_code = None
        run.completed_at = current
        if live_rule is not None:
            live_rule.last_processed_at = current
        _clear_materialized_run_lease(run)
        await db.flush()
        return run.status
    except WorkflowCancellationRequested:
        # The nested transaction removed every partial action. Refresh the run
        # to retain the API transaction's durable cancellation marker.
        await db.refresh(run)
        run.status = "cancelled"
        run.error_code = "cancelled_by_user"
        run.completed_at = current
        run.cancelled_at = run.cancelled_at or current
        _clear_materialized_run_lease(run)
        if live_rule is not None:
            live_rule.last_processed_at = current
        await db.flush()
        return run.status
    except Exception as exc:
        # The savepoint already removed partial actions. Refresh any expired
        # state before recording the durable retry/DLQ outcome.
        await db.refresh(run)
        error_code = (
            exc.code if isinstance(exc, WorkflowExecutionError) else "internal_error"
        )
        run.action_results = []
        run.error_code = error_code[:64]
        _clear_materialized_run_lease(run)
        if (
            not isinstance(exc, WorkflowExecutionError)
            and run.attempt_count < run.max_attempts
        ):
            run.status = "retrying"
            run.available_at = current + timedelta(
                seconds=min(5 * (2 ** max(run.attempt_count - 1, 0)), 300)
            )
            run.completed_at = None
        else:
            run.status = "dead_letter"
            run.completed_at = current
        if live_rule is not None:
            live_rule.last_processed_at = current
        if run.candidate_id is not None:
            append_candidate_activity(
                db,
                organization_id=organization_id,
                candidate_id=run.candidate_id,
                position_id=run.position_id,
                application_id=run.application_id,
                event_type="workflow_failed",
                subject_type="workflow_run",
                subject_id=run.id,
                payload={
                    "rule_name": run.rule_name_snapshot,
                    "error_code": run.error_code,
                    "action_types": [
                        action.get("type") for action in run.actions_snapshot or []
                    ],
                },
                occurred_at=current,
            )
        await db.flush()
        return run.status
