"""Durable PostgreSQL worker for bounded AI hiring mission steps."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.models.ai_hiring import (
    AiActionBundle,
    AiAgentRun,
    AiMessage,
    AiOrganizationSettings,
    AiSession,
    EvidenceClaim,
    HiringMission,
    MissionCandidateAssessment,
    MissionPlanVersion,
    MissionStep,
    MissionStepAttempt,
)
from app.models.candidate import Candidate
from app.services.ai_hiring_engine import (
    AI_BRIEF_PROMPT_VERSION,
    assess_candidate,
    generate_mission_plan,
    is_russian_text,
    json_for_audit,
)
from app.services.ai_hiring_runtime import (
    _next_message_sequence,
    append_mission_event,
)
from app.services.ai_hiring_safety import stable_input_hash
from app.services.worker_claims import (
    WorkClaim,
    WorkerRlsMode,
    WorkQueue,
    claim_work,
    process_one_claim,
)

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _close_expired_attempt(
    db: AsyncSession, step: MissionStep, *, now: datetime
) -> None:
    attempt = (
        await db.execute(
            select(MissionStepAttempt)
            .where(
                MissionStepAttempt.step_id == step.id,
                MissionStepAttempt.status == "running",
            )
            .order_by(MissionStepAttempt.attempt_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is not None:
        attempt.status = "failed"
        attempt.error_code = "lease_expired"
        attempt.completed_at = now
        if attempt.agent_run_id:
            run = await db.get(AiAgentRun, attempt.agent_run_id)
            if run and run.status == "running":
                run.status = "failed"
                run.error_code = "lease_expired"
                run.completed_at = now


async def claim_ai_steps(
    db: AsyncSession,
    *,
    limit: int,
    worker_id: str,
    organization_id: UUID | None = None,
) -> list[tuple[UUID, UUID, UUID]]:
    """Claim ready or abandoned steps with SKIP LOCKED.

    Returning IDs instead of ORM objects keeps processing safe across the
    claim commit and allows another process to observe the durable lease.
    """

    now = datetime.now(UTC)
    claimable = or_(
        and_(MissionStep.status == "ready", MissionStep.available_at <= now),
        and_(
            MissionStep.status == "running",
            MissionStep.lease_expires_at.is_not(None),
            MissionStep.lease_expires_at <= now,
        ),
    )
    stmt = (
        select(MissionStep)
        .join(HiringMission, HiringMission.id == MissionStep.mission_id)
        .join(
            AiOrganizationSettings,
            AiOrganizationSettings.organization_id == MissionStep.organization_id,
        )
        .where(
            claimable,
            AiOrganizationSettings.feature_enabled.is_(True),
            HiringMission.status.in_(("planning", "running")),
        )
    )
    if organization_id:
        stmt = stmt.where(MissionStep.organization_id == organization_id)
    steps = list(
        (
            await db.execute(
                stmt.order_by(MissionStep.available_at, MissionStep.created_at, MissionStep.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    claimed: list[tuple[UUID, UUID, UUID]] = []
    for step in steps:
        if step.status == "running":
            await _close_expired_attempt(db, step, now=now)
        attempt_number = int(
            (
                await db.execute(
                    select(func.coalesce(func.max(MissionStepAttempt.attempt_number), 0)).where(
                        MissionStepAttempt.step_id == step.id
                    )
                )
            ).scalar_one()
        ) + 1
        if attempt_number > step.max_attempts:
            step.status = "failed"
            step.last_error_code = "attempts_exhausted"
            mission = await db.get(HiringMission, step.mission_id)
            if mission:
                mission.status = "failed"
                mission.last_error_code = "attempts_exhausted"
            continue
        step.status = "running"
        step.lease_owner = worker_id
        step.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        step.started_at = step.started_at or now
        step.last_error_code = None
        run = AiAgentRun(
            organization_id=step.organization_id,
            mission_id=step.mission_id,
            step_id=step.id,
            agent_type=step.step_type,
            status="running",
            input_hash=stable_input_hash(json_for_audit(step.input_data or {})),
            safe_input_metadata={
                "plan_version_id": str(step.plan_version_id),
                "step_key": step.key,
            },
            validation_outcome="pending",
            started_at=now,
        )
        db.add(run)
        await db.flush()
        attempt = MissionStepAttempt(
            organization_id=step.organization_id,
            step_id=step.id,
            agent_run_id=run.id,
            attempt_number=attempt_number,
            status="running",
            worker_id=worker_id,
            input_hash=run.input_hash,
            started_at=now,
        )
        db.add(attempt)
        await db.flush()
        claimed.append((step.id, run.id, attempt.id))
    await db.commit()
    return claimed


async def _promote_dependencies(db: AsyncSession, step: MissionStep) -> None:
    all_steps = list(
        (
            await db.execute(
                select(MissionStep)
                .where(MissionStep.plan_version_id == step.plan_version_id)
                .order_by(MissionStep.ordinal)
            )
        ).scalars()
    )
    by_key = {item.key: item for item in all_steps}
    now = datetime.now(UTC)
    for candidate in all_steps:
        if candidate.status != "pending":
            continue
        dependencies = [by_key.get(key) for key in candidate.dependencies]
        if dependencies and all(item and item.status == "completed" for item in dependencies):
            candidate.status = "ready"
            candidate.available_at = now


async def _complete_step(
    db: AsyncSession,
    *,
    step: MissionStep,
    run: AiAgentRun,
    attempt: MissionStepAttempt,
    safe_result: dict,
    blocked: bool = False,
) -> None:
    now = datetime.now(UTC)
    step.status = "blocked" if blocked else "completed"
    step.safe_result = safe_result
    step.completed_at = now
    step.lease_owner = None
    step.lease_expires_at = None
    attempt.status = "completed"
    attempt.safe_result = safe_result
    attempt.completed_at = now
    run.status = "completed"
    run.validation_outcome = "valid"
    run.safe_result = safe_result
    run.completed_at = now
    run.latency_ms = max(0, int((now - (run.started_at or now)).total_seconds() * 1000))
    if not blocked:
        await _promote_dependencies(db, step)


async def _fail_step(
    db: AsyncSession,
    *,
    step: MissionStep,
    run: AiAgentRun,
    attempt: MissionStepAttempt,
    error_code: str,
) -> None:
    now = datetime.now(UTC)
    attempt.status = "failed"
    attempt.error_code = error_code
    attempt.completed_at = now
    run.status = "failed"
    run.validation_outcome = "invalid"
    run.error_code = error_code
    run.completed_at = now
    run.latency_ms = max(0, int((now - (run.started_at or now)).total_seconds() * 1000))
    step.last_error_code = error_code
    step.lease_owner = None
    step.lease_expires_at = None
    if attempt.attempt_number < step.max_attempts:
        step.status = "ready"
        step.available_at = now + timedelta(seconds=min(30, 2**attempt.attempt_number))
    else:
        step.status = "failed"
        step.completed_at = now
        mission = await db.get(HiringMission, step.mission_id)
        if mission:
            mission.status = "failed"
            mission.last_error_code = error_code
            await append_mission_event(
                db,
                mission,
                event_type="mission_failed",
                actor_kind="ai",
                agent_run_id=run.id,
                subject_type="mission_step",
                subject_id=step.id,
                payload={"error_code": error_code},
            )


async def _generate_plan(
    db: AsyncSession,
    mission: HiringMission,
    plan: MissionPlanVersion,
    run: AiAgentRun,
) -> tuple[dict, bool]:
    settings = (
        await db.execute(
            select(AiOrganizationSettings).where(
                AiOrganizationSettings.organization_id == mission.organization_id
            )
        )
    ).scalar_one()
    generated = await generate_mission_plan(
        db,
        organization_id=mission.organization_id,
        request=mission.original_request,
        default_headcount=settings.default_headcount,
        default_shortlist_size=settings.default_shortlist_size,
        hiring_defaults=settings.hiring_defaults,
        mission_id=mission.id,
        model_budget_cents=mission.model_budget_cents,
    )
    plan.brief_snapshot = generated.brief
    plan.rubric_snapshot = generated.rubric
    mission.brief = generated.brief
    mission.rubric = generated.rubric
    mission.title = generated.brief["role_title"] or mission.title
    mission.assumptions = generated.brief["assumptions"]
    mission.blockers = generated.brief["blockers"]
    mission.headcount = generated.brief["headcount"]
    mission.shortlist_size = generated.brief["shortlist_size"]
    mission.status = "draft" if generated.brief["blockers"] else "running"
    run.model_provider = generated.provider
    run.model = generated.model_used
    run.prompt_version = AI_BRIEF_PROMPT_VERSION
    run.tokens_in = generated.tokens_in
    run.tokens_out = generated.tokens_out
    result = {
        "brief": generated.brief,
        "rubric": generated.rubric,
        "model": generated.model_used,
        "provider": generated.provider,
    }
    await append_mission_event(
        db,
        mission,
        event_type="brief_generated",
        actor_kind="ai",
        agent_run_id=run.id,
        subject_type="plan_version",
        subject_id=plan.id,
        payload={
            "plan_version": plan.version,
            "blocker_count": len(generated.brief["blockers"]),
            "model": generated.model_used,
        },
    )
    return result, bool(generated.brief["blockers"])


async def _discover_candidates(
    db: AsyncSession,
    mission: HiringMission,
    run: AiAgentRun,
) -> dict:
    # Discovery reads canonical, active organization candidates only. External
    # sourcing is intentionally outside this MVP.
    candidate_ids = list(
        (
            await db.execute(
                select(Candidate.id)
                .where(
                    Candidate.organization_id == mission.organization_id,
                    Candidate.merged_at.is_(None),
                )
                .order_by(Candidate.created_at.desc(), Candidate.id)
                .limit(500)
            )
        ).scalars()
    )
    await append_mission_event(
        db,
        mission,
        event_type="candidate_discovery_completed",
        actor_kind="ai",
        agent_run_id=run.id,
        payload={"candidate_count": len(candidate_ids)},
    )
    return {"candidate_ids": [str(value) for value in candidate_ids], "candidate_count": len(candidate_ids)}


async def _assess_candidates(
    db: AsyncSession,
    mission: HiringMission,
    plan: MissionPlanVersion,
    run: AiAgentRun,
) -> dict:
    discovery = (
        await db.execute(
            select(MissionStep).where(
                MissionStep.plan_version_id == plan.id,
                MissionStep.key == "discover",
            )
        )
    ).scalar_one()
    ids = [UUID(value) for value in discovery.safe_result.get("candidate_ids", [])]
    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.organization_id == mission.organization_id,
                    Candidate.id.in_(ids),
                )
                .order_by(Candidate.id)
            )
        ).scalars()
    )
    existing_ids = set(
        (
            await db.execute(
                select(MissionCandidateAssessment.candidate_id).where(
                    MissionCandidateAssessment.mission_id == mission.id,
                    MissionCandidateAssessment.plan_version == plan.version,
                )
            )
        ).scalars()
    )
    created = 0
    for candidate in candidates:
        if candidate.id in existing_ids:
            continue
        generated = assess_candidate(
            candidate_id=candidate.id,
            raw_cv=candidate.raw_cv,
            parsed_cv=candidate.parsed_cv,
            brief=mission.brief,
            rubric=mission.rubric,
        )
        data = generated.assessment
        assessment = MissionCandidateAssessment(
            organization_id=mission.organization_id,
            mission_id=mission.id,
            plan_version_id=plan.id,
            plan_version=plan.version,
            candidate_id=candidate.id,
            agent_run_id=run.id,
            assessment_version=1,
            overall_score=Decimal(str(data["overall_score"])),
            confidence=data["confidence"],
            recommendation=data["recommendation"],
            criteria=data["criteria"],
            concerns=data["concerns"],
            missing_evidence=data["missing_evidence"],
            evidence_coverage=Decimal(str(data["evidence_coverage"])),
            concise_rationale=data["concise_rationale"],
        )
        db.add(assessment)
        await db.flush()
        claim_ids_by_key: dict[str, list[str]] = {}
        for evidence in generated.evidence:
            claim = EvidenceClaim(
                organization_id=mission.organization_id,
                mission_id=mission.id,
                plan_version_id=plan.id,
                assessment_id=assessment.id,
                candidate_id=candidate.id,
                agent_run_id=run.id,
                criterion_key=evidence["criterion_key"],
                claim=evidence["claim"],
                source_type=evidence["source_type"],
                source_id=UUID(evidence["source_id"]) if evidence.get("source_id") else None,
                source_locator={"kind": "masked_cv_excerpt"},
                stance=evidence["stance"],
                confidence=Decimal("0.85") if evidence["confidence"] == "high" else Decimal("0.65"),
                content_hash=evidence["content_hash"],
            )
            db.add(claim)
            await db.flush()
            claim_ids_by_key.setdefault(claim.criterion_key, []).append(str(claim.id))
        assessment.criteria = [
            {
                "key": criterion["key"],
                "score": criterion["score"],
                "evidence_claim_ids": claim_ids_by_key.get(criterion["key"], []),
            }
            for criterion in data["criteria"]
        ]
        created += 1
    await append_mission_event(
        db,
        mission,
        event_type="candidate_assessments_completed",
        actor_kind="ai",
        agent_run_id=run.id,
        payload={"assessment_count": len(candidates), "created_count": created},
    )
    return {"assessment_count": len(candidates), "created_count": created}


async def _finalize_shortlist(
    db: AsyncSession,
    mission: HiringMission,
    plan: MissionPlanVersion,
    run: AiAgentRun,
) -> dict:
    russian = is_russian_text(mission.original_request)
    assessments = list(
        (
            await db.execute(
                select(MissionCandidateAssessment)
                .where(
                    MissionCandidateAssessment.mission_id == mission.id,
                    MissionCandidateAssessment.plan_version == plan.version,
                )
                .order_by(
                    MissionCandidateAssessment.overall_score.desc(),
                    MissionCandidateAssessment.evidence_coverage.desc(),
                    case(
                        (MissionCandidateAssessment.confidence == "high", 3),
                        (MissionCandidateAssessment.confidence == "medium", 2),
                        else_=1,
                    ).desc(),
                    MissionCandidateAssessment.candidate_id,
                )
            )
        ).scalars()
    )
    selected = assessments[: mission.shortlist_size]
    candidate_ids = [str(item.candidate_id) for item in selected]
    existing_types = set(
        (
            await db.execute(
                select(AiActionBundle.action_type).where(
                    AiActionBundle.mission_id == mission.id,
                    AiActionBundle.plan_version == plan.version,
                )
            )
        ).scalars()
    )
    if mission.position_id is None and "create_vacancy" not in existing_types:
        requirements = {
            item["key"]: item.get("description") or item.get("label")
            for item in mission.rubric.get("criteria", [])
        }
        db.add(
            AiActionBundle(
                organization_id=mission.organization_id,
                mission_id=mission.id,
                plan_version_id=plan.id,
                plan_version=plan.version,
                proposed_by_agent_run_id=run.id,
                action_type="create_vacancy",
                summary=(
                    f"Создать внутреннюю вакансию «{mission.title}» в статусе паузы"
                    if russian
                    else f"Create a paused internal vacancy for {mission.title}"
                ),
                preview={"title": mission.title, "status": "paused", "public": False},
                action_payload={
                    "title": mission.title,
                    "description": (mission.brief.get("responsibilities") or [None])[0],
                    "requirements": requirements,
                },
                required_permission="requisitions.manage",
                status="pending",
            )
        )
    if candidate_ids and "apply_shortlist" not in existing_types:
        db.add(
            AiActionBundle(
                organization_id=mission.organization_id,
                mission_id=mission.id,
                plan_version_id=plan.id,
                plan_version=plan.version,
                proposed_by_agent_run_id=run.id,
                action_type="apply_shortlist",
                summary=(
                    f"Добавить {len(candidate_ids)} кандидатов из шорт-листа в вакансию миссии"
                    if russian
                    else f"Apply {len(candidate_ids)} shortlisted candidates to the mission vacancy"
                ),
                preview={
                    "candidate_ids": candidate_ids,
                    "candidate_count": len(candidate_ids),
                    "initial_stage": "new",
                },
                action_payload={"candidate_ids": candidate_ids},
                required_permission="candidates.write",
                status="pending",
            )
        )
    plan.status = "completed"
    mission.status = "awaiting_approval" if candidate_ids or mission.position_id is None else "completed"
    if mission.status == "completed":
        mission.completed_at = datetime.now(UTC)
    await append_mission_event(
        db,
        mission,
        event_type="shortlist_prepared",
        actor_kind="ai",
        agent_run_id=run.id,
        payload={"candidate_ids": candidate_ids, "candidate_count": len(candidate_ids)},
    )
    # Add one concise assistant result message. Candidate PII and CV text are
    # intentionally absent; the canvas fetches structured assessment records.
    session = (
        await db.execute(select(AiSession).where(AiSession.id == mission.session_id).with_for_update())
    ).scalar_one()
    sequence = await _next_message_sequence(db, session.id)
    db.add(
        AiMessage(
            organization_id=mission.organization_id,
            session_id=session.id,
            agent_run_id=run.id,
            role="assistant",
            content=(
                (
                    f"Я оценил {len(assessments)} кандидатов и подготовил шорт-лист из "
                    f"{len(candidate_ids)}. Проверьте доказательства и пакеты согласований."
                )
                if russian
                else (
                    f"I evaluated {len(assessments)} candidates and prepared a shortlist of "
                    f"{len(candidate_ids)}. Review the cited evidence and approval bundles."
                )
            ),
            structured_content={
                "type": "shortlist_ready",
                "mission_id": str(mission.id),
                "candidate_count": len(candidate_ids),
            },
            sequence=sequence,
            status="completed",
            completed_at=datetime.now(UTC),
            redaction_metadata={"contains_candidate_pii": False},
        )
    )
    return {"candidate_ids": candidate_ids, "candidate_count": len(candidate_ids)}


async def process_claimed_step(
    db: AsyncSession,
    *,
    step_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    commit: bool = True,
) -> bool:
    step = await db.get(MissionStep, step_id)
    run = await db.get(AiAgentRun, run_id)
    attempt = await db.get(MissionStepAttempt, attempt_id)
    if not step or not run or not attempt or step.status != "running":
        return False
    mission = await db.get(HiringMission, step.mission_id)
    plan = await db.get(MissionPlanVersion, step.plan_version_id)
    if not mission or not plan:
        await _fail_step(db, step=step, run=run, attempt=attempt, error_code="mission_not_found")
        if commit:
            await db.commit()
        else:
            await db.flush()
        return False
    if mission.current_plan_version != plan.version or plan.status != "active":
        now = datetime.now(UTC)
        step.status = "cancelled"
        step.cancelled_at = now
        step.lease_owner = None
        step.lease_expires_at = None
        attempt.status = "cancelled"
        attempt.completed_at = now
        run.status = "cancelled"
        run.completed_at = now
        run.validation_outcome = "not_applicable"
        if commit:
            await db.commit()
        else:
            await db.flush()
        return False
    try:
        # The savepoint lets the durable worker retain its outer broker lease
        # and atomically record a normalized retry/dead-letter outcome when a
        # step implementation fails.
        async with db.begin_nested():
            blocked = False
            if step.step_type == "generate_plan":
                result, blocked = await _generate_plan(db, mission, plan, run)
            elif step.step_type == "discover_candidates":
                result = await _discover_candidates(db, mission, run)
            elif step.step_type == "assess_candidates":
                result = await _assess_candidates(db, mission, plan, run)
            elif step.step_type == "finalize_shortlist":
                result = await _finalize_shortlist(db, mission, plan, run)
            else:
                raise ValueError("unknown_step_type")
            await _complete_step(
                db,
                step=step,
                run=run,
                attempt=attempt,
                safe_result=result,
                blocked=blocked,
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("AI mission step failed", extra={"step_id": str(step.id)})
        code = str(exc) if str(exc) in {"unknown_step_type"} else type(exc).__name__.lower()[:64]
        # Reload after the savepoint rollback before recording the durable
        # failure in the still-active outer transaction.
        step = await db.get(MissionStep, step_id)
        run = await db.get(AiAgentRun, run_id)
        attempt = await db.get(MissionStepAttempt, attempt_id)
        if step and run and attempt:
            await _fail_step(db, step=step, run=run, attempt=attempt, error_code=code)
            if commit:
                await db.commit()
            else:
                await db.flush()
        return False


async def process_ai_hiring_steps(
    db: AsyncSession,
    *,
    limit: int = 10,
    worker_id: str | None = None,
    organization_id: UUID | None = None,
) -> dict[str, int]:
    claimed = await claim_ai_steps(
        db,
        limit=max(1, min(limit, 100)),
        worker_id=worker_id or default_worker_id(),
        organization_id=organization_id,
    )
    completed = 0
    for step_id, run_id, attempt_id in claimed:
        if await process_claimed_step(
            db, step_id=step_id, run_id=run_id, attempt_id=attempt_id
        ):
            completed += 1
    return {"claimed": len(claimed), "completed": completed}


async def _prepare_broker_claimed_step(
    db: AsyncSession,
    *,
    claim: WorkClaim,
    worker_id: str,
) -> tuple[UUID, UUID, UUID] | None:
    """Create one attempt only after the broker bound the tenant session."""

    now = datetime.now(UTC)
    step = (
        await db.execute(
            select(MissionStep).where(
                MissionStep.id == claim.work_id,
                MissionStep.organization_id == claim.organization_id,
            )
        )
    ).scalar_one_or_none()
    if step is None:
        return None
    claimable = (
        step.status == "ready" and step.available_at <= now
    ) or (
        step.status == "running"
        and step.lease_expires_at is not None
        and step.lease_expires_at <= now
    )
    if not claimable:
        return None
    settings = (
        await db.execute(
            select(AiOrganizationSettings).where(
                AiOrganizationSettings.organization_id == claim.organization_id,
                AiOrganizationSettings.feature_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    mission = await db.get(HiringMission, step.mission_id)
    if settings is None or mission is None or mission.status not in {"planning", "running"}:
        return None
    if step.status == "running":
        await _close_expired_attempt(db, step, now=now)
    attempt_number = int(
        (
            await db.execute(
                select(func.coalesce(func.max(MissionStepAttempt.attempt_number), 0)).where(
                    MissionStepAttempt.step_id == step.id
                )
            )
        ).scalar_one()
    ) + 1
    if attempt_number > step.max_attempts:
        step.status = "failed"
        step.last_error_code = "attempts_exhausted"
        step.completed_at = now
        step.lease_owner = None
        step.lease_expires_at = None
        mission.status = "failed"
        mission.last_error_code = "attempts_exhausted"
        await db.flush()
        return None

    step.status = "running"
    step.lease_owner = worker_id
    step.lease_expires_at = claim.lease_expires_at
    step.started_at = step.started_at or now
    step.last_error_code = None
    run = AiAgentRun(
        organization_id=step.organization_id,
        mission_id=step.mission_id,
        step_id=step.id,
        agent_type=step.step_type,
        status="running",
        input_hash=stable_input_hash(json_for_audit(step.input_data or {})),
        safe_input_metadata={
            "plan_version_id": str(step.plan_version_id),
            "step_key": step.key,
        },
        validation_outcome="pending",
        started_at=now,
    )
    db.add(run)
    await db.flush()
    attempt = MissionStepAttempt(
        organization_id=step.organization_id,
        step_id=step.id,
        agent_run_id=run.id,
        attempt_number=attempt_number,
        status="running",
        worker_id=worker_id,
        input_hash=run.input_hash,
        started_at=now,
    )
    db.add(attempt)
    await db.flush()
    return step.id, run.id, attempt.id


async def process_ai_mission_steps_once(
    *,
    worker_id: str,
    limit: int,
    mode: WorkerRlsMode,
    lease_seconds: int = LEASE_SECONDS,
) -> dict[str, int]:
    async with SessionLocal() as discovery:
        claims = await claim_work(
            discovery,
            queue=WorkQueue.MISSION_STEPS,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
        )
        await discovery.commit()
    counts = {"claimed": len(claims), "completed": 0, "failed": 0, "abandoned": 0}

    async def process(db: AsyncSession, claim: WorkClaim) -> bool:
        prepared = await _prepare_broker_claimed_step(
            db,
            claim=claim,
            worker_id=worker_id,
        )
        if prepared is None:
            return False
        step_id, run_id, attempt_id = prepared
        return await process_claimed_step(
            db,
            step_id=step_id,
            run_id=run_id,
            attempt_id=attempt_id,
            commit=False,
        )

    for claim in claims:
        try:
            if await process_one_claim(claim, process):
                counts["completed"] += 1
            else:
                counts["failed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "AI mission work item abandoned",
                extra={"mission_step_id": str(claim.work_id)},
            )
            counts["abandoned"] += 1
    return counts


async def run_ai_hiring_worker(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    poll_seconds = max(0.25, min(settings.ai_worker_poll_seconds, 300.0))
    claim_limit = max(1, min(settings.ai_worker_claim_limit, 100))
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    lease_seconds = LEASE_SECONDS
    identity = worker_id or default_worker_id()
    while True:
        try:
            await process_ai_mission_steps_once(
                worker_id=identity,
                limit=claim_limit,
                mode=mode,
                lease_seconds=lease_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI hiring worker iteration failed")
        await asyncio.sleep(poll_seconds)


if __name__ == "__main__":
    # Production can run this module as a separate process and set
    # AI_WORKER_ENABLED=false on the API deployment. Local development keeps
    # the same durable worker embedded in FastAPI's lifespan by default.
    asyncio.run(run_ai_hiring_worker())
