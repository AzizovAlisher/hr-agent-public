"""Application service for organization-shared AI hiring missions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.models.ai_hiring import (
    AiActionBundle,
    AiMessage,
    AiOrganizationSettings,
    AiSession,
    HiringMission,
    MissionCandidateAssessment,
    MissionEvent,
    MissionPlanVersion,
    MissionStep,
)
from app.models.candidate import Candidate
from app.models.user import User
from app.services.ai_hiring_engine import (
    deterministic_brief,
    deterministic_rubric,
    is_russian_text,
)
from app.services.ai_hiring_safety import stable_input_hash
from app.services.enterprise import can_access_field, require_permission
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

ACTIVE_STEP_STATUSES = {"pending", "ready", "running"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def settings_out(record: AiOrganizationSettings | None) -> dict[str, Any]:
    return {
        "feature_enabled": record.feature_enabled if record else False,
        "default_headcount": record.default_headcount if record else 1,
        "default_shortlist_size": record.default_shortlist_size if record else 5,
        "default_model_budget_cents": record.default_model_budget_cents if record else None,
        "hiring_defaults": record.hiring_defaults if record else {},
        "autonomy_policy": record.autonomy_policy if record else {},
        "updated_at": _iso(record.updated_at) if record else None,
    }


def session_out(session: AiSession, mission: HiringMission | None = None) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title,
        "status": session.status,
        "created_by_user_id": str(session.created_by_user_id) if session.created_by_user_id else None,
        "creator_user_id": str(session.created_by_user_id) if session.created_by_user_id else None,
        "mission_id": str(mission.id) if mission else None,
        "mission_status": mission.status if mission else None,
        "created_at": _iso(session.created_at),
        "updated_at": _iso(session.updated_at),
        "archived_at": _iso(session.archived_at),
    }


def message_out(message: AiMessage, *, content: str | None = None) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "session_id": str(message.session_id),
        "actor_user_id": str(message.actor_user_id) if message.actor_user_id else None,
        "agent_run_id": str(message.agent_run_id) if message.agent_run_id else None,
        "role": message.role,
        "content": message.content if content is None else content,
        "structured_content": message.structured_content or {},
        "attachments": message.attachments or [],
        "redaction_metadata": message.redaction_metadata or {},
        "sequence": message.sequence,
        "status": message.status,
        "created_at": _iso(message.created_at),
        "completed_at": _iso(message.completed_at),
    }


def mission_out(mission: HiringMission, *, steps: list[MissionStep] | None = None) -> dict[str, Any]:
    payload = {
        "id": str(mission.id),
        "session_id": str(mission.session_id),
        "position_id": str(mission.position_id) if mission.position_id else None,
        "title": mission.title,
        "original_request": mission.original_request,
        "brief": mission.brief or {},
        "rubric": mission.rubric or {},
        "assumptions": mission.assumptions or [],
        "blockers": mission.blockers or [],
        "headcount": mission.headcount,
        "shortlist_size": mission.shortlist_size,
        # Older in-memory fixtures and compatibility adapters may not yet expose
        # the per-mission snapshot. Persisted HiringMission rows always do.
        "model_budget_cents": getattr(mission, "model_budget_cents", None),
        "status": mission.status,
        "created_by_user_id": str(mission.created_by_user_id) if mission.created_by_user_id else None,
        "correlation_id": str(mission.correlation_id),
        "current_plan_version": mission.current_plan_version,
        "last_error_code": mission.last_error_code,
        "started_at": _iso(mission.started_at),
        "completed_at": _iso(mission.completed_at),
        "stopped_at": _iso(mission.stopped_at),
        "created_at": _iso(mission.created_at),
        "updated_at": _iso(mission.updated_at),
    }
    if steps is not None:
        payload["steps"] = [step_out(step) for step in steps]
    return payload


def step_out(step: MissionStep) -> dict[str, Any]:
    return {
        "id": str(step.id),
        "key": step.key,
        "step_type": step.step_type,
        "ordinal": step.ordinal,
        "sequence": step.ordinal,
        "title": step.key.replace("_", " ").title(),
        "dependencies": step.dependencies or [],
        "depends_on": step.dependencies or [],
        "status": step.status,
        "safe_result": step.safe_result or {},
        "result": step.safe_result or {},
        "policy_outcome": step.policy_outcome,
        "last_error_code": step.last_error_code,
        "started_at": _iso(step.started_at),
        "completed_at": _iso(step.completed_at),
    }


def assessment_out(
    assessment: MissionCandidateAssessment,
    *,
    candidate: Candidate | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(assessment.id),
        "candidate_id": str(assessment.candidate_id),
        "candidate": (
            {
                "id": str(candidate.id),
                "source": candidate.source,
                "source_ref": candidate.source_ref,
            }
            if candidate
            else None
        ),
        "plan_version": assessment.plan_version,
        "version": assessment.assessment_version,
        "overall_score": float(assessment.overall_score),
        "confidence": assessment.confidence,
        "recommendation": assessment.recommendation,
        "criteria": assessment.criteria or [],
        "concerns": assessment.concerns or [],
        "missing_evidence": assessment.missing_evidence or [],
        "evidence_coverage": float(assessment.evidence_coverage),
        "concise_rationale": assessment.concise_rationale,
        "evidence": evidence or [],
        "evidence_claims": evidence or [],
        "created_at": _iso(assessment.created_at),
    }


def event_out(event: MissionEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "sequence": event.sequence,
        "event_type": event.event_type,
        "actor_kind": event.actor_kind,
        "actor_user_id": str(event.actor_user_id) if event.actor_user_id else None,
        "agent_run_id": str(event.agent_run_id) if event.agent_run_id else None,
        "subject_type": event.subject_type,
        "subject_id": str(event.subject_id) if event.subject_id else None,
        "payload": event.payload or {},
        "occurred_at": _iso(event.occurred_at),
    }


def bundle_out(bundle: AiActionBundle) -> dict[str, Any]:
    return {
        "id": str(bundle.id),
        "mission_id": str(bundle.mission_id),
        "plan_version": bundle.plan_version,
        "action_type": bundle.action_type,
        "summary": bundle.summary,
        "preview": bundle.preview or {},
        "required_permission": bundle.required_permission,
        "status": bundle.status,
        "approved_by_user_id": str(bundle.approved_by_user_id) if bundle.approved_by_user_id else None,
        "decision_comment": bundle.decision_comment,
        "execution_result": bundle.execution_result or {},
        "result": bundle.execution_result or {},
        "error_code": bundle.error_code,
        "error": bundle.error_code,
        "decided_at": _iso(bundle.decided_at),
        "executed_at": _iso(bundle.executed_at),
        "created_at": _iso(bundle.created_at),
    }


async def require_ai_enabled(db: AsyncSession, user: User) -> AiOrganizationSettings:
    await require_permission(db, user, "ai.use")
    record = (
        await db.execute(
            select(AiOrganizationSettings).where(
                AiOrganizationSettings.organization_id == user.organization_id
            )
        )
    ).scalar_one_or_none()
    if record is None or not record.feature_enabled:
        # The workspace is a separately released product surface. Hide it from
        # organizations where it is not enabled rather than leaking capability.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI Hiring Assistant is not enabled")
    return record


async def get_ai_settings(db: AsyncSession, user: User) -> dict[str, Any]:
    await require_permission(db, user, "ai.manage")
    record = (
        await db.execute(
            select(AiOrganizationSettings).where(
                AiOrganizationSettings.organization_id == user.organization_id
            )
        )
    ).scalar_one_or_none()
    return settings_out(record)


async def put_ai_settings(
    db: AsyncSession,
    user: User,
    values: dict[str, Any],
) -> dict[str, Any]:
    await require_permission(db, user, "ai.manage")
    record = (
        await db.execute(
            select(AiOrganizationSettings)
            .where(AiOrganizationSettings.organization_id == user.organization_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        record = AiOrganizationSettings(organization_id=user.organization_id)
        db.add(record)
    for key in (
        "feature_enabled",
        "default_headcount",
        "default_shortlist_size",
        "default_model_budget_cents",
        "hiring_defaults",
        "autonomy_policy",
    ):
        if key in values:
            setattr(record, key, values[key])
    await db.commit()
    await db.refresh(record)
    return settings_out(record)


async def _viewer_can_see_compensation(db: AsyncSession, user: User) -> bool:
    return await can_access_field(db, user, FieldClass.COMPENSATION)


def _redact_compensation_text(content: str) -> str:
    import re

    return re.sub(
        r"(?i)(salary|budget|compensation|зарплат\w*|бюджет)\s*[:\-]?\s*"
        r"[^\n.;]{1,100}",
        r"\1: [REDACTED]",
        content,
    )


async def session_detail(
    db: AsyncSession, user: User, session_id: UUID
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    session = (
        await db.execute(
            select(AiSession).where(
                AiSession.id == session_id,
                AiSession.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI session not found")
    mission = (
        await db.execute(
            select(HiringMission).where(
                HiringMission.session_id == session.id,
                HiringMission.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    messages = list(
        (
            await db.execute(
                select(AiMessage)
                .where(
                    AiMessage.session_id == session.id,
                    AiMessage.organization_id == user.organization_id,
                )
                .order_by(AiMessage.sequence)
            )
        ).scalars()
    )
    can_see_compensation = await _viewer_can_see_compensation(db, user)
    serialized_messages = [
        message_out(
            item,
            content=item.content if can_see_compensation else _redact_compensation_text(item.content),
        )
        for item in messages
    ]
    result = session_out(session, mission)
    result["messages"] = serialized_messages
    result["mission"] = mission_out(mission) if mission else None
    if mission and not can_see_compensation and result["mission"]:
        result["mission"]["brief"] = {**result["mission"]["brief"], "compensation": None}
    return result


async def list_sessions(
    db: AsyncSession,
    user: User,
    *,
    session_status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    stmt = (
        select(AiSession, HiringMission)
        .outerjoin(HiringMission, HiringMission.session_id == AiSession.id)
        .where(AiSession.organization_id == user.organization_id)
    )
    if session_status:
        stmt = stmt.where(AiSession.status == session_status)
    namespace = "ai.sessions"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"status": session_status},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"updated_at", "id"},
            detail="Invalid AI sessions cursor",
        )
        cursor_time = cursor_datetime(payload, "updated_at", detail="Invalid AI sessions cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid AI sessions cursor")
        stmt = stmt.where(
            or_(
                AiSession.updated_at < cursor_time,
                and_(AiSession.updated_at == cursor_time, AiSession.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(AiSession.updated_at.desc(), AiSession.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return {
        "items": [session_out(session, mission) for session, mission in rows],
        "next_cursor": (
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "updated_at": last.updated_at.astimezone(UTC).isoformat(),
                    "id": str(last.id),
                },
            )
            if has_more and last is not None
            else None
        ),
    }


async def create_session(
    db: AsyncSession, user: User, *, title: str | None = None
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    session = AiSession(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        title=title.strip()[:255] if title and title.strip() else None,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session_out(session)


async def archive_session(db: AsyncSession, user: User, session_id: UUID) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    session = (
        await db.execute(
            select(AiSession)
            .where(
                AiSession.id == session_id,
                AiSession.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI session not found")
    if session.status != "archived":
        session.status = "archived"
        session.archived_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(session)
    return session_out(session)


async def _next_message_sequence(db: AsyncSession, session_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.coalesce(func.max(AiMessage.sequence), 0) + 1).where(
                    AiMessage.session_id == session_id
                )
            )
        ).scalar_one()
    )


async def append_mission_event(
    db: AsyncSession,
    mission: HiringMission,
    *,
    event_type: str,
    actor_kind: str,
    actor_user_id: UUID | None = None,
    agent_run_id: UUID | None = None,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> MissionEvent:
    sequence = int(
        (
            await db.execute(
                select(func.coalesce(func.max(MissionEvent.sequence), 0) + 1).where(
                    MissionEvent.mission_id == mission.id
                )
            )
        ).scalar_one()
    )
    event = MissionEvent(
        organization_id=mission.organization_id,
        mission_id=mission.id,
        sequence=sequence,
        event_type=event_type,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        agent_run_id=agent_run_id,
        subject_type=subject_type,
        subject_id=subject_id,
        payload=payload or {},
    )
    db.add(event)
    return event


def _plan_definition() -> dict[str, Any]:
    return {
        "version": 1,
        "steps": [
            {"key": "plan_request", "type": "generate_plan", "depends_on": []},
            {"key": "discover", "type": "discover_candidates", "depends_on": ["plan_request"]},
            {"key": "assess", "type": "assess_candidates", "depends_on": ["discover"]},
            {"key": "shortlist", "type": "finalize_shortlist", "depends_on": ["assess"]},
        ],
    }


async def _enqueue_plan_version(
    db: AsyncSession,
    *,
    mission: HiringMission,
    request: str,
    settings: AiOrganizationSettings,
    actor_user_id: UUID,
) -> MissionPlanVersion:
    now = datetime.now(UTC)
    if mission.current_plan_version:
        await db.execute(
            update(MissionPlanVersion)
            .where(
                MissionPlanVersion.mission_id == mission.id,
                MissionPlanVersion.version == mission.current_plan_version,
            )
            .values(status="superseded")
        )
        await db.execute(
            update(MissionStep)
            .where(
                MissionStep.mission_id == mission.id,
                MissionStep.status.in_(ACTIVE_STEP_STATUSES),
            )
            .values(status="cancelled", cancelled_at=now, lease_owner=None, lease_expires_at=None)
        )
        # A proposal is valid only for the immutable plan that produced it.
        await db.execute(
            update(AiActionBundle)
            .where(
                AiActionBundle.mission_id == mission.id,
                AiActionBundle.status.in_(("pending", "approved")),
            )
            .values(status="stale", decided_at=now)
        )
    version = mission.current_plan_version + 1
    initial_brief = deterministic_brief(
        request,
        default_headcount=settings.default_headcount,
        default_shortlist_size=settings.default_shortlist_size,
        hiring_defaults=settings.hiring_defaults,
    )
    initial_rubric = deterministic_rubric(initial_brief)
    plan = MissionPlanVersion(
        organization_id=mission.organization_id,
        mission_id=mission.id,
        version=version,
        status="active",
        brief_snapshot=initial_brief,
        rubric_snapshot=initial_rubric,
        plan=_plan_definition(),
        input_hash=stable_input_hash(request),
    )
    db.add(plan)
    await db.flush()
    for ordinal, definition in enumerate(_plan_definition()["steps"]):
        db.add(
            MissionStep(
                organization_id=mission.organization_id,
                mission_id=mission.id,
                plan_version_id=plan.id,
                key=definition["key"],
                step_type=definition["type"],
                ordinal=ordinal,
                dependencies=definition["depends_on"],
                status="ready" if ordinal == 0 else "pending",
                input_data={"request_hash": stable_input_hash(request)},
                policy_outcome="auto",
                idempotency_key=f"mission:{mission.id}:v{version}:{definition['key']}",
                max_attempts=3,
                available_at=now,
            )
        )
    mission.original_request = request
    mission.title = initial_brief["role_title"] or "Hiring mission"
    mission.brief = initial_brief
    mission.rubric = initial_rubric
    mission.assumptions = initial_brief["assumptions"]
    mission.blockers = initial_brief["blockers"]
    mission.headcount = initial_brief["headcount"]
    mission.shortlist_size = initial_brief["shortlist_size"]
    mission.current_plan_version = version
    mission.status = "planning"
    mission.started_at = mission.started_at or now
    mission.completed_at = None
    mission.stopped_at = None
    mission.last_error_code = None
    await append_mission_event(
        db,
        mission,
        event_type="mission_planned" if version == 1 else "mission_replanned",
        actor_kind="human",
        actor_user_id=actor_user_id,
        subject_type="plan_version",
        subject_id=plan.id,
        payload={"plan_version": version},
    )
    return plan


async def submit_message(
    db: AsyncSession,
    user: User,
    session_id: UUID,
    *,
    content: str,
    attachments: list[dict[str, Any]] | None = None,
    attachment_imports: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = await require_ai_enabled(db, user)
    content = content.strip()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Message cannot be empty")
    if len(content) > 12_000:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Message exceeds 12000 characters")
    session = (
        await db.execute(
            select(AiSession)
            .where(
                AiSession.id == session_id,
                AiSession.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI session not found")
    if session.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "Archived sessions are read-only")
    now = datetime.now(UTC)
    sequence = await _next_message_sequence(db, session.id)
    user_message = AiMessage(
        organization_id=user.organization_id,
        session_id=session.id,
        actor_user_id=user.id,
        role="user",
        content=content,
        structured_content=attachment_imports or {},
        attachments=attachments or [],
        sequence=sequence,
        status="completed",
        completed_at=now,
        redaction_metadata={"raw_prompt_logged": False},
    )
    db.add(user_message)
    mission = (
        await db.execute(
            select(HiringMission).where(
                HiringMission.session_id == session.id,
                HiringMission.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if mission is None:
        initial = deterministic_brief(
            content,
            default_headcount=settings.default_headcount,
            default_shortlist_size=settings.default_shortlist_size,
            hiring_defaults=settings.hiring_defaults,
        )
        mission = HiringMission(
            organization_id=user.organization_id,
            session_id=session.id,
            created_by_user_id=user.id,
            title=initial["role_title"] or "Hiring mission",
            original_request=content,
            brief=initial,
            rubric=deterministic_rubric(initial),
            assumptions=initial["assumptions"],
            blockers=initial["blockers"],
            headcount=initial["headcount"],
            shortlist_size=initial["shortlist_size"],
            model_budget_cents=settings.default_model_budget_cents,
            status="draft",
        )
        db.add(mission)
        await db.flush()
    await _enqueue_plan_version(
        db,
        mission=mission,
        request=content,
        settings=settings,
        actor_user_id=user.id,
    )
    if session.title is None:
        session.title = mission.title
    ack = (
        "Я подготовил план найма и начал поиск и оценку кандидатов. "
        "Результаты появятся в рабочей области миссии."
        if is_russian_text(content)
        else "I prepared the hiring plan and started candidate discovery and assessment. "
        "Results will appear in the mission workspace."
    )
    assistant_message = AiMessage(
        organization_id=user.organization_id,
        session_id=session.id,
        role="assistant",
        content=ack,
        structured_content={
            "type": "mission_queued",
            "mission_id": str(mission.id),
            "plan_version": mission.current_plan_version,
        },
        sequence=sequence + 1,
        status="completed",
        completed_at=now,
        redaction_metadata={"raw_prompt_logged": False},
    )
    db.add(assistant_message)
    await db.commit()
    await db.refresh(mission)
    await db.refresh(session)
    await db.refresh(user_message)
    await db.refresh(assistant_message)
    return {
        "user_message": message_out(user_message),
        "assistant_message": message_out(assistant_message),
        "mission": mission_out(mission),
    }


async def get_mission(
    db: AsyncSession, user: User, mission_id: UUID
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    mission = (
        await db.execute(
            select(HiringMission).where(
                HiringMission.id == mission_id,
                HiringMission.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    steps = list(
        (
            await db.execute(
                select(MissionStep)
                .join(MissionPlanVersion, MissionPlanVersion.id == MissionStep.plan_version_id)
                .where(
                    MissionStep.mission_id == mission.id,
                    MissionStep.organization_id == user.organization_id,
                    MissionPlanVersion.version == mission.current_plan_version,
                )
                .order_by(MissionStep.ordinal)
            )
        ).scalars()
    )
    return mission_out(mission, steps=steps)


async def list_missions(
    db: AsyncSession,
    user: User,
    *,
    mission_status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    stmt = select(HiringMission).where(HiringMission.organization_id == user.organization_id)
    if mission_status == "active":
        stmt = stmt.where(
            HiringMission.status.in_(
                ("draft", "planning", "running", "awaiting_approval", "failed", "stopped")
            )
        )
    elif mission_status:
        stmt = stmt.where(HiringMission.status == mission_status)
    namespace = "ai.missions"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"status": mission_status},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"updated_at", "id"},
            detail="Invalid hiring missions cursor",
        )
        cursor_time = cursor_datetime(
            payload, "updated_at", detail="Invalid hiring missions cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid hiring missions cursor")
        stmt = stmt.where(
            or_(
                HiringMission.updated_at < cursor_time,
                and_(HiringMission.updated_at == cursor_time, HiringMission.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(HiringMission.updated_at.desc(), HiringMission.id.desc()).limit(
                    max(1, min(limit, 100)) + 1
                )
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [mission_out(item) for item in rows],
        "next_cursor": (
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "updated_at": rows[-1].updated_at.astimezone(UTC).isoformat(),
                    "id": str(rows[-1].id),
                },
            )
            if has_more and rows
            else None
        ),
    }


async def stop_mission(db: AsyncSession, user: User, mission_id: UUID) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    mission = (
        await db.execute(
            select(HiringMission)
            .where(
                HiringMission.id == mission_id,
                HiringMission.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    if mission.status not in {"stopped", "completed"}:
        now = datetime.now(UTC)
        mission.status = "stopped"
        mission.stopped_at = now
        await db.execute(
            update(MissionStep)
            .where(
                MissionStep.mission_id == mission.id,
                MissionStep.status.in_(ACTIVE_STEP_STATUSES),
            )
            .values(status="cancelled", cancelled_at=now, lease_owner=None, lease_expires_at=None)
        )
        await db.execute(
            update(AiActionBundle)
            .where(
                AiActionBundle.mission_id == mission.id,
                AiActionBundle.status.in_(("pending", "approved")),
            )
            .values(status="stale", decided_at=now)
        )
        await append_mission_event(
            db, mission, event_type="mission_stopped", actor_kind="human", actor_user_id=user.id
        )
        await db.commit()
        await db.refresh(mission)
    return mission_out(mission)


async def retry_mission(db: AsyncSession, user: User, mission_id: UUID) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    mission = (
        await db.execute(
            select(HiringMission)
            .where(
                HiringMission.id == mission_id,
                HiringMission.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    current_plan = (
        await db.execute(
            select(MissionPlanVersion).where(
                MissionPlanVersion.mission_id == mission.id,
                MissionPlanVersion.version == mission.current_plan_version,
            )
        )
    ).scalar_one_or_none()
    if current_plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Mission has no current plan")
    failed = list(
        (
            await db.execute(
                select(MissionStep).where(
                    MissionStep.mission_id == mission.id,
                    MissionStep.plan_version_id == current_plan.id,
                    MissionStep.status.in_(("failed", "blocked")),
                )
            )
        ).scalars()
    )
    if not failed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Mission has no failed steps to retry")
    now = datetime.now(UTC)
    all_steps = list(
        (
            await db.execute(
                select(MissionStep).where(
                    MissionStep.plan_version_id == current_plan.id
                )
            )
        ).scalars()
    )
    by_key = {item.key: item for item in all_steps}
    for step in failed:
        dependencies_complete = all(
            by_key.get(key) is not None and by_key[key].status == "completed"
            for key in step.dependencies
        )
        step.status = "ready" if dependencies_complete else "pending"
        step.available_at = now
        step.last_error_code = None
        step.lease_owner = None
        step.lease_expires_at = None
    mission.status = "running"
    mission.last_error_code = None
    await append_mission_event(
        db, mission, event_type="mission_retried", actor_kind="human", actor_user_id=user.id
    )
    await db.commit()
    await db.refresh(mission)
    return mission_out(mission)


async def replan_mission(
    db: AsyncSession, user: User, mission_id: UUID, *, request: str | None = None
) -> dict[str, Any]:
    settings = await require_ai_enabled(db, user)
    mission = (
        await db.execute(
            select(HiringMission)
            .where(
                HiringMission.id == mission_id,
                HiringMission.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    resolved_request = (request or mission.original_request).strip()
    if not resolved_request:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Request cannot be empty")
    await _enqueue_plan_version(
        db,
        mission=mission,
        request=resolved_request,
        settings=settings,
        actor_user_id=user.id,
    )
    await db.commit()
    await db.refresh(mission)
    return mission_out(mission)


async def list_events(
    db: AsyncSession,
    user: User,
    mission_id: UUID,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    exists = (
        await db.execute(
            select(HiringMission.id).where(
                HiringMission.id == mission_id,
                HiringMission.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    namespace = "ai.mission-events"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"mission_id": str(mission_id)},
    )
    stmt = select(MissionEvent).where(
        MissionEvent.mission_id == mission_id,
        MissionEvent.organization_id == user.organization_id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"sequence", "id"},
            detail="Invalid mission events cursor",
        )
        try:
            cursor_sequence = int(payload["sequence"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid mission events cursor"},
            ) from exc
        cursor_id = cursor_uuid(payload, detail="Invalid mission events cursor")
        stmt = stmt.where(
            or_(
                MissionEvent.sequence > cursor_sequence,
                and_(MissionEvent.sequence == cursor_sequence, MissionEvent.id > cursor_id),
            )
        )
    events = list(
        (
            await db.execute(
                stmt.order_by(MissionEvent.sequence.asc(), MissionEvent.id.asc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(events) > limit
    events = events[:limit]
    return {
        "items": [event_out(item) for item in events],
        "next_cursor": (
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"sequence": events[-1].sequence, "id": str(events[-1].id)},
            )
            if has_more and events
            else None
        ),
    }


async def list_candidates(
    db: AsyncSession,
    user: User,
    mission_id: UUID,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    from app.models.ai_hiring import EvidenceClaim

    await require_ai_enabled(db, user)
    mission = (
        await db.execute(
            select(HiringMission).where(
                HiringMission.id == mission_id,
                HiringMission.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    confidence_rank = case(
        (MissionCandidateAssessment.confidence == "high", 3),
        (MissionCandidateAssessment.confidence == "medium", 2),
        else_=1,
    )
    namespace = "ai.mission-candidates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "mission_id": str(mission.id),
            "plan_version": mission.current_plan_version,
        },
    )
    stmt = (
        select(MissionCandidateAssessment, Candidate)
        .join(Candidate, Candidate.id == MissionCandidateAssessment.candidate_id)
        .where(
            MissionCandidateAssessment.mission_id == mission.id,
            MissionCandidateAssessment.organization_id == user.organization_id,
            MissionCandidateAssessment.plan_version == mission.current_plan_version,
        )
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"score", "coverage", "confidence", "id"},
            detail="Invalid mission candidates cursor",
        )
        try:
            cursor_score = float(payload["score"])
            cursor_coverage = float(payload["coverage"])
            cursor_confidence = int(payload["confidence"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid mission candidates cursor"},
            ) from exc
        cursor_id = cursor_uuid(payload, detail="Invalid mission candidates cursor")
        stmt = stmt.where(
            or_(
                MissionCandidateAssessment.overall_score < cursor_score,
                and_(
                    MissionCandidateAssessment.overall_score == cursor_score,
                    MissionCandidateAssessment.evidence_coverage < cursor_coverage,
                ),
                and_(
                    MissionCandidateAssessment.overall_score == cursor_score,
                    MissionCandidateAssessment.evidence_coverage == cursor_coverage,
                    confidence_rank < cursor_confidence,
                ),
                and_(
                    MissionCandidateAssessment.overall_score == cursor_score,
                    MissionCandidateAssessment.evidence_coverage == cursor_coverage,
                    confidence_rank == cursor_confidence,
                    MissionCandidateAssessment.candidate_id > cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(
                MissionCandidateAssessment.overall_score.desc(),
                MissionCandidateAssessment.evidence_coverage.desc(),
                confidence_rank.desc(),
                MissionCandidateAssessment.candidate_id.asc(),
            ).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    output: list[dict[str, Any]] = []
    for assessment, candidate in rows:
        claims = list(
            (
                await db.execute(
                    select(EvidenceClaim)
                    .where(
                        EvidenceClaim.assessment_id == assessment.id,
                        EvidenceClaim.organization_id == user.organization_id,
                    )
                    .order_by(EvidenceClaim.criterion_key, EvidenceClaim.id)
                )
            ).scalars()
        )
        evidence = [
            {
                "id": str(claim.id),
                "criterion_key": claim.criterion_key,
                "claim": claim.claim,
                "source_type": claim.source_type,
                "source_id": str(claim.source_id) if claim.source_id else None,
                "source_locator": claim.source_locator or {},
                "stance": claim.stance,
                "confidence": float(claim.confidence),
            }
            for claim in claims
        ]
        output.append(assessment_out(assessment, candidate=candidate, evidence=evidence))
    last = rows[-1][0] if rows else None
    confidence_value = (
        3 if last and last.confidence == "high" else 2 if last and last.confidence == "medium" else 1
    )
    return {
        "items": output,
        "next_cursor": (
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "score": float(last.overall_score),
                    "coverage": float(last.evidence_coverage),
                    "confidence": confidence_value,
                    "id": str(last.candidate_id),
                },
            )
            if has_more and last is not None
            else None
        ),
    }


async def list_bundles(
    db: AsyncSession,
    user: User,
    *,
    mission_id: UUID | None = None,
    bundle_status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    await require_ai_enabled(db, user)
    stmt = select(AiActionBundle).where(AiActionBundle.organization_id == user.organization_id)
    if mission_id:
        stmt = stmt.where(AiActionBundle.mission_id == mission_id)
    if bundle_status:
        stmt = stmt.where(AiActionBundle.status == bundle_status)
    namespace = "ai.action-bundles"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "mission_id": str(mission_id) if mission_id else None,
            "status": bundle_status,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid action bundles cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid action bundles cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid action bundles cursor")
        stmt = stmt.where(
            or_(
                AiActionBundle.created_at < cursor_time,
                and_(AiActionBundle.created_at == cursor_time, AiActionBundle.id < cursor_id),
            )
        )
    bundles = list(
        (
            await db.execute(
                stmt.order_by(AiActionBundle.created_at.desc(), AiActionBundle.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(bundles) > limit
    bundles = bundles[:limit]
    return {
        "items": [bundle_out(item) for item in bundles],
        "next_cursor": (
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": bundles[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(bundles[-1].id),
                },
            )
            if has_more and bundles
            else None
        ),
    }


async def decide_bundle(
    db: AsyncSession,
    user: User,
    bundle_id: UUID,
    *,
    decision: str,
    idempotency_key: str,
    comment: str | None = None,
) -> dict[str, Any]:
    from app.models.ai_hiring import AiAgentRun
    from app.services.ai_hiring_engine import json_for_audit
    from app.services.ai_hiring_tools import AI_TOOL_REGISTRY, ToolExecutionContext

    await require_ai_enabled(db, user)
    if decision not in {"approve", "reject"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid decision")
    if not idempotency_key.strip() or len(idempotency_key) > 255:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid Idempotency-Key")
    bundle = (
        await db.execute(
            select(AiActionBundle)
            .where(
                AiActionBundle.id == bundle_id,
                AiActionBundle.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if bundle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI action bundle not found")
    if bundle.approval_idempotency_key == idempotency_key:
        same_decision = (
            decision == "reject" and bundle.status == "rejected"
        ) or (
            decision == "approve"
            and bundle.status in {"approved", "executed", "failed"}
        )
        if not same_decision:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency-Key was already used for another decision",
            )
        return bundle_out(bundle)
    reused = (
        await db.execute(
            select(AiActionBundle.id).where(
                AiActionBundle.organization_id == user.organization_id,
                AiActionBundle.approval_idempotency_key == idempotency_key,
                AiActionBundle.id != bundle.id,
            )
        )
    ).scalar_one_or_none()
    if reused is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency-Key was already used")
    if bundle.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Bundle is {bundle.status}")
    mission = (
        await db.execute(
            select(HiringMission)
            .where(
                HiringMission.id == bundle.mission_id,
                HiringMission.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if mission is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring mission not found")
    if mission.current_plan_version != bundle.plan_version:
        bundle.status = "stale"
        bundle.decided_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "Bundle belongs to an obsolete plan")
    await require_permission(db, user, bundle.required_permission)
    if (
        decision == "approve"
        and bundle.action_type == "apply_shortlist"
        and mission.position_id is None
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Approve and execute the vacancy proposal before applying the shortlist",
        )
    now = datetime.now(UTC)
    bundle.approval_idempotency_key = idempotency_key
    bundle.approved_by_user_id = user.id
    bundle.decision_comment = comment
    bundle.decided_at = now
    if decision == "reject":
        bundle.status = "rejected"
        if bundle.action_type == "create_vacancy":
            await db.execute(
                update(AiActionBundle)
                .where(
                    AiActionBundle.mission_id == mission.id,
                    AiActionBundle.plan_version == bundle.plan_version,
                    AiActionBundle.action_type == "apply_shortlist",
                    AiActionBundle.status == "pending",
                )
                .values(status="stale", decided_at=now)
            )
        await append_mission_event(
            db,
            mission,
            event_type="action_bundle_rejected",
            actor_kind="human",
            actor_user_id=user.id,
            subject_type="action_bundle",
            subject_id=bundle.id,
            payload={"action_type": bundle.action_type},
        )
        remaining = (
            await db.execute(
                select(func.count(AiActionBundle.id)).where(
                    AiActionBundle.mission_id == mission.id,
                    AiActionBundle.plan_version == bundle.plan_version,
                    AiActionBundle.status == "pending",
                )
            )
        ).scalar_one()
        if remaining == 0:
            mission.status = "completed"
            mission.completed_at = datetime.now(UTC)
        await db.commit()
        return bundle_out(bundle)

    tool = AI_TOOL_REGISTRY.get(bundle.action_type)
    if tool.required_permission != bundle.required_permission:
        raise HTTPException(status.HTTP_409_CONFLICT, "Bundle permission does not match tool policy")
    run = AiAgentRun(
        organization_id=user.organization_id,
        session_id=mission.session_id,
        mission_id=mission.id,
        initiated_by_user_id=user.id,
        agent_type="approved_action_executor",
        status="running",
        tool_name=tool.name,
        input_hash=stable_input_hash(json_for_audit(bundle.action_payload)),
        safe_input_metadata={
            "action_type": bundle.action_type,
            "bundle_id": str(bundle.id),
            "plan_version": bundle.plan_version,
        },
        validation_outcome="pending",
        correlation_id=mission.correlation_id,
        causation_id=bundle.proposed_by_agent_run_id,
        started_at=now,
    )
    db.add(run)
    await db.flush()
    bundle.status = "approved"
    try:
        async with db.begin_nested():
            result = await AI_TOOL_REGISTRY.execute(
                bundle.action_type,
                payload=bundle.action_payload,
                context=ToolExecutionContext(
                    db=db,
                    organization_id=user.organization_id,
                    mission_id=mission.id,
                    plan_version_id=bundle.plan_version_id,
                    approved_by_user_id=user.id,
                    agent_run_id=run.id,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - durable boundary records safe error code
        bundle.status = "failed"
        bundle.error_code = type(exc).__name__.lower()[:64]
        run.status = "failed"
        run.validation_outcome = "invalid"
        run.error_code = bundle.error_code
        run.completed_at = datetime.now(UTC)
        await append_mission_event(
            db,
            mission,
            event_type="action_bundle_failed",
            actor_kind="ai",
            agent_run_id=run.id,
            subject_type="action_bundle",
            subject_id=bundle.id,
            payload={"action_type": bundle.action_type, "error_code": bundle.error_code},
        )
        await db.commit()
        return bundle_out(bundle)
    bundle.status = "executed"
    bundle.execution_result = result
    bundle.executed_at = datetime.now(UTC)
    run.status = "completed"
    run.validation_outcome = "valid"
    run.safe_result = result
    run.completed_at = bundle.executed_at
    run.latency_ms = max(0, int((bundle.executed_at - now).total_seconds() * 1_000))
    await append_mission_event(
        db,
        mission,
        event_type="action_bundle_executed",
        actor_kind="ai",
        actor_user_id=user.id,
        agent_run_id=run.id,
        subject_type="action_bundle",
        subject_id=bundle.id,
        payload={"action_type": bundle.action_type},
    )
    remaining = (
        await db.execute(
            select(func.count(AiActionBundle.id)).where(
                AiActionBundle.mission_id == mission.id,
                AiActionBundle.plan_version == bundle.plan_version,
                AiActionBundle.status == "pending",
            )
        )
    ).scalar_one()
    if remaining == 0:
        mission.status = "completed"
        mission.completed_at = datetime.now(UTC)
    await db.commit()
    return bundle_out(bundle)
