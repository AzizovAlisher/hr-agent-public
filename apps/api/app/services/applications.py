"""Tenant-safe Application lifecycle with legacy Score synchronization."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application import (
    Application,
    ApplicationEvaluation,
    ApplicationStageEvent,
)
from app.models.candidate import Candidate
from app.models.candidate_processing import CandidateProcessingJob
from app.models.position import Position
from app.models.scheduling import SchedulingLink
from app.models.score import Score
from app.models.user import User
from app.schemas.applications import (
    ApplicationCreate,
    ApplicationEvaluationCreate,
    ApplicationEvaluationOut,
    ApplicationOut,
    ApplicationPage,
    ApplicationStageEventOut,
    ApplicationStageTransition,
    ApplicationUpdate,
)
from app.services.activities import append_candidate_activity
from app.services.pagination import (
    cursor_scope_fingerprint,
    decode_signed_cursor,
    encode_signed_cursor,
)


ALLOWED_STAGE_TRANSITIONS: dict[str, set[str]] = {
    "new": {"screening", "rejected"},
    "screening": {"new", "interview", "rejected"},
    "interview": {"screening", "offer", "rejected"},
    "offer": {"interview", "hired", "rejected"},
    "hired": set(),
    "rejected": set(),
}
TERMINAL_STAGES = frozenset({"hired", "rejected"})
MAX_EVALUATION_PAYLOAD_BYTES = 128 * 1024
_APPLICATION_CURSOR_NAMESPACE = "applications.list"


@dataclass(frozen=True)
class ApplicationLinkResult:
    application: Application
    score: Score | None
    created: bool
    new_pair: bool


async def _tenant_user(db: AsyncSession, organization_id: UUID, user_id: UUID | None) -> None:
    if user_id is None:
        return
    exists = (
        await db.execute(
            select(User.id).where(
                User.id == user_id,
                User.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found")


async def _candidate_position(
    db: AsyncSession, organization_id: UUID, candidate_id: UUID, position_id: UUID
) -> tuple[Candidate, Position]:
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == candidate_id,
                Candidate.organization_id == organization_id,
                Candidate.merged_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    return candidate, position


async def _skip_trigger_event(db: AsyncSession) -> None:
    """Tell the PostgreSQL Score compatibility trigger an event is explicit."""

    await db.execute(
        text("SELECT set_config('hr_agent.skip_application_event', '1', true)")
    )


async def _application_lock(
    db: AsyncSession,
    organization_id: UUID,
    candidate_id: UUID,
    position_id: UUID,
) -> None:
    key = f"application:{organization_id}:{candidate_id}:{position_id}"
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


def _is_active(application: Application) -> bool:
    return application.closed_at is None and application.archived_at is None


async def _pair_applications(
    db: AsyncSession,
    organization_id: UUID,
    candidate_id: UUID,
    position_id: UUID,
    *,
    lock: bool = False,
) -> list[Application]:
    stmt = select(Application).where(
        Application.organization_id == organization_id,
        Application.candidate_id == candidate_id,
        Application.position_id == position_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    return list(
        (
            await db.execute(
                stmt.order_by(Application.cycle_number.desc(), Application.id.desc())
            )
        ).scalars()
    )


def _reset_score_for_cycle(
    score: Score,
    *,
    owner_user_id: UUID | None,
    next_action_due_date: date | None,
    reset_at: datetime,
) -> None:
    for field in (
        "score_technical_skills",
        "score_quality_of_projects",
        "score_previous_experience",
        "score_impact",
        "comment_technical_skills",
        "comment_quality_of_projects",
        "comment_previous_experience",
        "comment_impact",
        "overall_score",
        "overall_recommendation",
        "model_used",
    ):
        setattr(score, field, None)
    score.stage = "new"
    score.owner_user_id = owner_user_id
    score.next_action_due_date = next_action_due_date
    score.scored_at = reset_at


def _validate_evaluation_payload(body: ApplicationEvaluationCreate) -> None:
    for field in ("evidence", "scores", "provenance"):
        value = getattr(body, field)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Evaluation {field} must be finite JSON data",
            ) from exc
        if len(encoded) > MAX_EVALUATION_PAYLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"Evaluation {field} exceeds {MAX_EVALUATION_PAYLOAD_BYTES} bytes",
            )


async def _linked_score(
    db: AsyncSession, application: Application
) -> Score:
    score = (
        await db.get(Score, application.legacy_score_id)
        if application.legacy_score_id
        else None
    )
    if score is not None and (
        score.organization_id != application.organization_id
        or score.candidate_id != application.candidate_id
        or score.position_id != application.position_id
    ):
        score = None
    if score is None:
        score = (
            await db.execute(
                select(Score).where(
                    Score.organization_id == application.organization_id,
                    Score.candidate_id == application.candidate_id,
                    Score.position_id == application.position_id,
                )
            )
        ).scalar_one_or_none()
    if not _is_active(application):
        if score is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Closed application has no legacy score",
            )
        linked_elsewhere = (
            await db.execute(
                select(Application.id).where(
                    Application.legacy_score_id == score.id,
                    Application.id != application.id,
                )
            )
        ).scalar_one_or_none()
        if linked_elsewhere is not None:
            return score
    if score is None:
        await _skip_trigger_event(db)
        score = Score(
            organization_id=application.organization_id,
            candidate_id=application.candidate_id,
            position_id=application.position_id,
            stage=application.stage,
            owner_user_id=application.owner_user_id,
            next_action_due_date=application.next_action_due_date,
        )
        db.add(score)
        await db.flush()
    if _is_active(application) or application.legacy_score_id is not None:
        application.legacy_score_id = score.id
    return score


async def ensure_legacy_score_for_application(
    db: AsyncSession, application: Application
) -> Score:
    """Public compatibility helper for legacy workflows and reconciliation."""

    return await _linked_score(db, application)


async def reconcile_application_from_score(
    db: AsyncSession,
    score: Score,
    *,
    source: str,
    actor_user_id: UUID | None,
    actor_kind: str,
    reason: str,
) -> Application:
    """Repair/synchronize first-class state after a legacy Score workflow.

    Migrated databases perform the state mirror in a trigger; metadata-created
    test databases do not. This helper is intentionally idempotent in both and
    only appends an explicit event when the aggregate still differs.
    """

    linked = await ensure_application_link(
        db,
        organization_id=score.organization_id,
        candidate_id=score.candidate_id,
        position_id=score.position_id,
        source=source,
        owner_user_id=score.owner_user_id,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        reason=reason,
        occurred_at=score.scored_at,
    )
    application = linked.application
    old_stage = application.stage
    if _is_active(application) and old_stage != score.stage:
        application.stage = score.stage
        if score.stage in TERMINAL_STAGES:
            application.closed_at = datetime.now(UTC)
        db.add(
            ApplicationStageEvent(
                organization_id=score.organization_id,
                application_id=application.id,
                from_stage=old_stage,
                to_stage=score.stage,
                actor_user_id=actor_user_id,
                actor_kind=actor_kind,
                reason=reason,
                details={
                    "source": application.source,
                    "owner_user_id": (
                        str(score.owner_user_id) if score.owner_user_id else None
                    ),
                    "score_id": str(score.id),
                },
                occurred_at=datetime.now(UTC),
            )
        )
    if _is_active(application) or application.legacy_score_id == score.id:
        application.legacy_score_id = score.id
        application.owner_user_id = score.owner_user_id
        application.next_action_due_date = score.next_action_due_date
    return application


async def ensure_application_link(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_id: UUID,
    position_id: UUID,
    source: str,
    owner_user_id: UUID | None,
    next_action_due_date: date | None = None,
    actor_user_id: UUID | None,
    actor_kind: str,
    reason: str,
    external_ref: str | None = None,
    metadata: dict | None = None,
    occurred_at: datetime | None = None,
    allow_reapplication: bool = False,
) -> ApplicationLinkResult:
    """Idempotently create the first-class link and its Score shadow."""

    await _application_lock(db, organization_id, candidate_id, position_id)
    applications = await _pair_applications(
        db,
        organization_id,
        candidate_id,
        position_id,
        lock=True,
    )
    active = next((item for item in applications if _is_active(item)), None)
    if active is not None:
        score = await _linked_score(db, active)
        return ApplicationLinkResult(
            application=active, score=score, created=False, new_pair=False
        )

    score = (
        await db.execute(
            select(Score)
            .where(
                Score.organization_id == organization_id,
                Score.candidate_id == candidate_id,
                Score.position_id == position_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    score_was_missing = score is None
    if applications and not allow_reapplication:
        latest = applications[0]
        return ApplicationLinkResult(
            application=latest,
            score=score,
            created=False,
            new_pair=False,
        )

    cycle_number = applications[0].cycle_number + 1 if applications else 1
    is_reapplication = bool(applications)
    if is_reapplication:
        stage = "new"
        effective_owner = owner_user_id
        due_date = next_action_due_date
        event_time = occurred_at or datetime.now(UTC)
    else:
        stage = score.stage if score is not None else "new"
        effective_owner = score.owner_user_id if score is not None else owner_user_id
        due_date = (
            score.next_action_due_date
            if score is not None
            else next_action_due_date
        )
        event_time = occurred_at or (
            score.scored_at if score is not None else datetime.now(UTC)
        )

    if score is not None:
        for historical in applications:
            if historical.legacy_score_id == score.id:
                historical.legacy_score_id = None
        await db.flush()
    application = Application(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        legacy_score_id=score.id if score is not None else None,
        stage=stage,
        cycle_number=cycle_number,
        source=source[:32],
        external_ref=external_ref,
        owner_user_id=effective_owner,
        next_action_due_date=due_date,
        created_by_user_id=actor_user_id,
        application_metadata=metadata or {},
        applied_at=event_time,
        closed_at=event_time if stage in TERMINAL_STAGES else None,
    )
    db.add(application)
    await db.flush()
    db.add(
        ApplicationStageEvent(
            organization_id=organization_id,
            application_id=application.id,
            from_stage=None,
            to_stage=stage,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            reason=reason,
            details={
                "source": application.source,
                "owner_user_id": str(effective_owner) if effective_owner else None,
                "cycle_number": cycle_number,
                "reapplication": is_reapplication,
            },
            occurred_at=event_time,
        )
    )
    if score is None:
        await _skip_trigger_event(db)
        score = Score(
            organization_id=organization_id,
            candidate_id=candidate_id,
            position_id=position_id,
            stage=stage,
            owner_user_id=owner_user_id,
            next_action_due_date=next_action_due_date,
        )
        db.add(score)
        await db.flush()
        application.legacy_score_id = score.id
    elif is_reapplication:
        await _skip_trigger_event(db)
        _reset_score_for_cycle(
            score,
            owner_user_id=owner_user_id,
            next_action_due_date=next_action_due_date,
            reset_at=event_time,
        )
    return ApplicationLinkResult(
        application=application,
        score=score,
        created=True,
        new_pair=score_was_missing or is_reapplication,
    )


async def application_out(
    db: AsyncSession, application: Application, *, include_events: bool = False
) -> ApplicationOut:
    events: list[ApplicationStageEventOut] = []
    if include_events:
        rows = list(
            (
                await db.execute(
                    select(ApplicationStageEvent)
                    .where(ApplicationStageEvent.application_id == application.id)
                    .order_by(
                        ApplicationStageEvent.occurred_at,
                        ApplicationStageEvent.id,
                    )
                )
            ).scalars()
        )
        events = [ApplicationStageEventOut.model_validate(row) for row in rows]
    evaluation_count = int(
        (
            await db.execute(
                select(func.count(ApplicationEvaluation.id)).where(
                    ApplicationEvaluation.organization_id
                    == application.organization_id,
                    ApplicationEvaluation.application_id == application.id,
                )
            )
        ).scalar_one()
    )
    return ApplicationOut(
        id=application.id,
        organization_id=application.organization_id,
        candidate_id=application.candidate_id,
        position_id=application.position_id,
        legacy_score_id=application.legacy_score_id,
        stage=application.stage,
        source=application.source,
        external_ref=application.external_ref,
        owner_user_id=application.owner_user_id,
        next_action_due_date=application.next_action_due_date,
        cycle_number=application.cycle_number,
        is_active=_is_active(application),
        metadata=application.application_metadata,
        applied_at=application.applied_at,
        closed_at=application.closed_at,
        archived_at=application.archived_at,
        archived_by_user_id=application.archived_by_user_id,
        created_at=application.created_at,
        updated_at=application.updated_at,
        evaluation_count=evaluation_count,
        stage_events=events,
    )


async def get_application(
    db: AsyncSession,
    organization_id: UUID,
    application_id: UUID,
    *,
    lock: bool = False,
) -> Application:
    stmt = select(Application).where(
        Application.id == application_id,
        Application.organization_id == organization_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    application = (await db.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    return application


def _application_cursor_scope(
    organization_id: UUID,
    *,
    candidate_id: UUID | None,
    position_id: UUID | None,
    stage: str | None,
    include_closed: bool,
    include_archived: bool,
) -> str:
    return cursor_scope_fingerprint(
        _APPLICATION_CURSOR_NAMESPACE,
        {
            "organization_id": str(organization_id),
            "candidate_id": str(candidate_id) if candidate_id is not None else None,
            "position_id": str(position_id) if position_id is not None else None,
            "stage": stage,
            "include_closed": include_closed,
            "include_archived": include_archived,
        },
    )


def _encode_application_cursor(
    application: Application, *, scope: str
) -> str:
    return encode_signed_cursor(
        _APPLICATION_CURSOR_NAMESPACE,
        {
            "applied_at": application.applied_at.astimezone(UTC).isoformat(),
            "id": str(application.id),
            "scope": scope,
        },
    )


def _decode_application_cursor(cursor: str, *, scope: str) -> tuple[datetime, UUID]:
    try:
        payload = decode_signed_cursor(
            cursor, namespace=_APPLICATION_CURSOR_NAMESPACE
        )
        if set(payload) != {"applied_at", "id", "scope"}:
            raise ValueError("invalid application cursor claims")
        if payload["scope"] != scope:
            raise ValueError("application cursor does not match this query")
        applied_at = datetime.fromisoformat(str(payload["applied_at"]))
        if applied_at.tzinfo is None or applied_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        application_id = UUID(str(payload["id"]))
        return applied_at.astimezone(UTC), application_id
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Invalid applications cursor",
        ) from exc


async def list_applications(
    db: AsyncSession,
    organization_id: UUID,
    *,
    candidate_id: UUID | None = None,
    position_id: UUID | None = None,
    stage: str | None = None,
    include_closed: bool = False,
    include_archived: bool = False,
    limit: int = 100,
    cursor: str | None = None,
) -> ApplicationPage:
    stmt = select(Application).where(Application.organization_id == organization_id)
    visibility = [
        Application.closed_at.is_(None) & Application.archived_at.is_(None)
    ]
    if include_closed:
        visibility.append(
            Application.closed_at.is_not(None)
            & Application.archived_at.is_(None)
        )
    if include_archived:
        visibility.append(Application.archived_at.is_not(None))
    stmt = stmt.where(or_(*visibility))
    if candidate_id is not None:
        stmt = stmt.where(Application.candidate_id == candidate_id)
    if position_id is not None:
        stmt = stmt.where(Application.position_id == position_id)
    if stage is not None:
        stmt = stmt.where(Application.stage == stage)
    scope = _application_cursor_scope(
        organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        stage=stage,
        include_closed=include_closed,
        include_archived=include_archived,
    )
    if cursor is not None:
        cursor_time, cursor_id = _decode_application_cursor(cursor, scope=scope)
        stmt = stmt.where(
            or_(
                Application.applied_at < cursor_time,
                and_(
                    Application.applied_at == cursor_time,
                    Application.id < cursor_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    Application.applied_at.desc(),
                    Application.id.desc(),
                )
                .limit(limit + 1)
            )
        ).scalars()
    )
    visible = rows[:limit]
    return ApplicationPage(
        items=[await application_out(db, item) for item in visible],
        next_cursor=(
            _encode_application_cursor(visible[-1], scope=scope)
            if len(rows) > limit and visible
            else None
        ),
    )


async def create_application(
    db: AsyncSession, user: User, body: ApplicationCreate
) -> ApplicationOut:
    candidate, position = await _candidate_position(
        db, user.organization_id, body.candidate_id, body.position_id
    )
    await _tenant_user(db, user.organization_id, body.owner_user_id)
    try:
        link = await ensure_application_link(
            db,
            organization_id=user.organization_id,
            candidate_id=body.candidate_id,
            position_id=body.position_id,
            source=body.source,
            owner_user_id=body.owner_user_id,
            next_action_due_date=body.next_action_due_date,
            actor_user_id=user.id,
            actor_kind="human",
            reason="application_created",
            external_ref=body.external_ref,
            metadata=body.metadata,
        )
        if (
            _is_active(link.application)
            and not (link.created and link.new_pair)
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An active application already exists",
            )
        if not _is_active(link.application):
            link = await ensure_application_link(
                db,
                organization_id=user.organization_id,
                candidate_id=body.candidate_id,
                position_id=body.position_id,
                source=body.source,
                owner_user_id=body.owner_user_id,
                next_action_due_date=body.next_action_due_date,
                actor_user_id=user.id,
                actor_kind="human",
                reason="application_reapplied",
                external_ref=body.external_ref,
                metadata=body.metadata,
                allow_reapplication=True,
            )
        application = link.application
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An active application already exists",
        ) from exc
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        application_id=application.id,
        position_id=position.id,
        event_type="application_created",
        subject_type="application",
        subject_id=application.id,
        payload={
            "stage": application.stage,
            "source": application.source,
            "cycle_number": application.cycle_number,
        },
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Application already exists"
        ) from exc
    await db.refresh(application)
    return await application_out(db, application, include_events=True)


async def update_application(
    db: AsyncSession, user: User, application_id: UUID, body: ApplicationUpdate
) -> ApplicationOut:
    application = await get_application(
        db, user.organization_id, application_id, lock=True
    )
    if not _is_active(application):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Closed applications cannot be edited",
        )
    expected_version = body.expected_version.astimezone(UTC)
    current_version = application.updated_at.astimezone(UTC)
    if current_version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Application changed; refresh and retry",
        )
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if "owner_user_id" in changes:
        await _tenant_user(db, user.organization_id, changes["owner_user_id"])
    if "metadata" in changes:
        changes["application_metadata"] = changes.pop("metadata")

    effective_changes: dict[str, dict[str, object]] = {}
    for field, value in changes.items():
        previous = getattr(application, field)
        if previous == value:
            continue
        public_field = "metadata" if field == "application_metadata" else field
        effective_changes[public_field] = {
            "before": json.loads(json.dumps(previous, default=str)),
            "after": json.loads(json.dumps(value, default=str)),
        }
        setattr(application, field, value)
    if not effective_changes:
        return await application_out(db, application, include_events=True)

    score = await _linked_score(db, application)
    await _skip_trigger_event(db)
    score.owner_user_id = application.owner_user_id
    score.next_action_due_date = application.next_action_due_date
    event_time = datetime.now(UTC)
    db.add(
        ApplicationStageEvent(
            organization_id=user.organization_id,
            application_id=application.id,
            from_stage=application.stage,
            to_stage=application.stage,
            actor_user_id=user.id,
            actor_kind="human",
            reason="application_updated",
            details={
                "event_kind": "application_fields_updated",
                "changes": effective_changes,
                "source": application.source,
                "owner_user_id": (
                    str(application.owner_user_id)
                    if application.owner_user_id is not None
                    else None
                ),
            },
            occurred_at=event_time,
        )
    )
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=application.candidate_id,
        actor_user_id=user.id,
        application_id=application.id,
        position_id=application.position_id,
        event_type="application_updated",
        subject_type="application",
        subject_id=application.id,
        payload={"changes": effective_changes},
    )
    await db.commit()
    await db.refresh(application)
    return await application_out(db, application, include_events=True)


async def transition_application_stage(
    db: AsyncSession,
    user: User,
    application_id: UUID,
    body: ApplicationStageTransition,
    *,
    occurred_at: datetime | None = None,
) -> ApplicationOut:
    application, _changed = await apply_application_stage_transition(
        db,
        organization_id=user.organization_id,
        application_id=application_id,
        body=body,
        actor_user_id=user.id,
        actor_kind="human",
        occurred_at=occurred_at,
    )
    await db.commit()
    await db.refresh(application)
    return await application_out(db, application, include_events=True)


async def apply_application_stage_transition(
    db: AsyncSession,
    *,
    organization_id: UUID,
    application_id: UUID,
    body: ApplicationStageTransition,
    actor_user_id: UUID | None,
    actor_kind: Literal["human", "system", "public", "automation"],
    occurred_at: datetime | None = None,
) -> tuple[Application, bool]:
    """Apply one canonical, locked stage transition without committing.

    HTTP handlers, automation, decisions, and offer acceptance all share this
    primitive. The caller owns the surrounding transaction so a stage change
    remains atomic with the domain event that caused it.
    """

    application = await get_application(
        db, organization_id, application_id, lock=True
    )
    expected_version = body.expected_version.astimezone(UTC)
    current_version = application.updated_at.astimezone(UTC)
    if current_version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Application changed; refresh and retry",
        )
    if application.archived_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Archived applications cannot transition",
        )
    if application.stage == body.stage:
        return application, False
    old_stage = application.stage
    if body.stage not in ALLOWED_STAGE_TRANSITIONS[old_stage]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Invalid application transition: {old_stage} -> {body.stage}",
        )
    transition_time = occurred_at or datetime.now(UTC)
    application.stage = body.stage
    if body.stage in TERMINAL_STAGES:
        application.closed_at = transition_time
        # Work for a closed cycle must not later write a stale evaluation, nor
        # occupy the legacy pair-wide active-job slot when the person reapplies.
        await db.execute(
            update(CandidateProcessingJob)
            .where(
                CandidateProcessingJob.organization_id == organization_id,
                CandidateProcessingJob.application_id == application.id,
                CandidateProcessingJob.status.in_(("queued", "running", "retrying")),
            )
            .values(
                status="cancelled",
                cancelled_at=transition_time,
                completed_at=transition_time,
                leased_at=None,
                leased_by=None,
                lease_expires_at=None,
                failure_code="application_closed",
                failure_detail="Application cycle closed before processing completed",
            )
        )
        # A public scheduling capability must not remain usable after its
        # authoritative hiring cycle closes.  Existing appointments retain
        # their own management lifecycle; only the invitation is revoked.
        await db.execute(
            update(SchedulingLink)
            .where(
                SchedulingLink.organization_id == organization_id,
                SchedulingLink.application_id == application.id,
                SchedulingLink.revoked_at.is_(None),
            )
            .values(revoked_at=transition_time)
        )
    event_details = dict(body.details)
    event_details.update(
        {
            "source": application.source,
            "owner_user_id": (
                str(application.owner_user_id) if application.owner_user_id else None
            ),
        }
    )
    if body.disposition_code is not None:
        event_details["disposition_code"] = body.disposition_code
    db.add(
        ApplicationStageEvent(
            organization_id=organization_id,
            application_id=application.id,
            from_stage=old_stage,
            to_stage=body.stage,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            reason=body.reason,
            details=event_details,
            occurred_at=transition_time,
        )
    )
    score = await _linked_score(db, application)
    await _skip_trigger_event(db)
    score.stage = body.stage
    activity_payload = {
        "from_stage": old_stage,
        "to_stage": body.stage,
        "disposition_code": body.disposition_code,
        "automatic": actor_kind != "human",
    }
    activity_payload.update(
        {
            key: value
            for key, value in body.details.items()
            if key not in activity_payload
        }
    )
    append_candidate_activity(
        db,
        organization_id=organization_id,
        candidate_id=application.candidate_id,
        actor_user_id=actor_user_id,
        application_id=application.id,
        position_id=application.position_id,
        event_type="stage_changed",
        subject_type="application",
        subject_id=application.id,
        payload=activity_payload,
        occurred_at=transition_time,
    )
    return application, True


async def append_application_evaluation(
    db: AsyncSession,
    *,
    organization_id: UUID,
    application_id: UUID,
    body: ApplicationEvaluationCreate,
    actor_user_id: UUID | None,
    actor_kind: Literal["human", "ai", "system", "automation"],
    allow_closed: bool = False,
) -> ApplicationEvaluation:
    """Append one immutable evaluation version without committing."""

    _validate_evaluation_payload(body)
    await _tenant_user(db, organization_id, actor_user_id)
    application = await get_application(
        db, organization_id, application_id, lock=True
    )
    if application.archived_at is not None or (
        application.closed_at is not None and not allow_closed
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Application is closed for new evaluations",
        )
    latest = (
        await db.execute(
            select(ApplicationEvaluation)
            .where(
                ApplicationEvaluation.organization_id == organization_id,
                ApplicationEvaluation.application_id == application.id,
                ApplicationEvaluation.kind == body.kind,
            )
            .order_by(
                ApplicationEvaluation.version.desc(),
                ApplicationEvaluation.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    evaluation = ApplicationEvaluation(
        organization_id=organization_id,
        application_id=application.id,
        kind=body.kind,
        status=body.status,
        version=(latest.version + 1 if latest is not None else 1),
        supersedes_evaluation_id=latest.id if latest is not None else None,
        evidence=body.evidence,
        scores=body.scores,
        provenance=body.provenance,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
        evaluated_at=body.evaluated_at or datetime.now(UTC),
    )
    db.add(evaluation)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=organization_id,
        candidate_id=application.candidate_id,
        actor_user_id=actor_user_id,
        actor_kind="ai" if actor_kind == "ai" else None,
        application_id=application.id,
        position_id=application.position_id,
        event_type="application_evaluation_recorded",
        subject_type="application_evaluation",
        subject_id=evaluation.id,
        payload={
            "application_id": str(application.id),
            "cycle_number": application.cycle_number,
            "evaluation_kind": evaluation.kind,
            "evaluation_status": evaluation.status,
            "evaluation_version": evaluation.version,
        },
        occurred_at=evaluation.evaluated_at,
    )
    return evaluation


async def list_application_evaluations(
    db: AsyncSession,
    *,
    organization_id: UUID,
    application_id: UUID,
    kind: str | None = None,
) -> list[ApplicationEvaluationOut]:
    await get_application(db, organization_id, application_id)
    stmt = select(ApplicationEvaluation).where(
        ApplicationEvaluation.organization_id == organization_id,
        ApplicationEvaluation.application_id == application_id,
    )
    if kind is not None:
        stmt = stmt.where(ApplicationEvaluation.kind == kind)
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    ApplicationEvaluation.evaluated_at,
                    ApplicationEvaluation.kind,
                    ApplicationEvaluation.version,
                    ApplicationEvaluation.id,
                )
            )
        ).scalars()
    )
    return [ApplicationEvaluationOut.model_validate(row) for row in rows]


async def review_application_evaluation(
    db: AsyncSession,
    *,
    organization_id: UUID,
    application_id: UUID,
    evaluation_id: UUID,
    reviewer: User,
    reason: str,
    void: bool = False,
    score_overrides: dict | None = None,
) -> ApplicationEvaluation:
    """Record review as a new version; the reviewed snapshot is untouched."""

    clean_reason = reason.strip()
    if not clean_reason:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Evaluation review requires a reason",
        )
    application = await get_application(
        db, organization_id, application_id, lock=True
    )
    target = (
        await db.execute(
            select(ApplicationEvaluation).where(
                ApplicationEvaluation.id == evaluation_id,
                ApplicationEvaluation.organization_id == organization_id,
                ApplicationEvaluation.application_id == application.id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evaluation not found")
    latest_version = int(
        (
            await db.execute(
                select(func.max(ApplicationEvaluation.version)).where(
                    ApplicationEvaluation.organization_id == organization_id,
                    ApplicationEvaluation.application_id == application.id,
                    ApplicationEvaluation.kind == target.kind,
                )
            )
        ).scalar_one()
        or 0
    )
    if target.version != latest_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only the latest evaluation version can be reviewed",
        )
    provenance = dict(target.provenance)
    provenance["review"] = {
        "reviewed_evaluation_id": str(target.id),
        "reviewer_user_id": str(reviewer.id),
        "reason": clean_reason,
    }
    return await append_application_evaluation(
        db,
        organization_id=organization_id,
        application_id=application.id,
        body=ApplicationEvaluationCreate(
            kind=target.kind,
            status="voided" if void else "reviewed",
            evidence=dict(target.evidence),
            scores=score_overrides or dict(target.scores),
            provenance=provenance,
        ),
        actor_user_id=reviewer.id,
        actor_kind="human",
        allow_closed=True,
    )


async def delete_application(
    db: AsyncSession, user: User, application_id: UUID
) -> None:
    application = await get_application(db, user.organization_id, application_id)
    await _application_lock(
        db,
        user.organization_id,
        application.candidate_id,
        application.position_id,
    )
    application = await get_application(
        db, user.organization_id, application_id, lock=True
    )
    if application.archived_at is not None:
        return
    now = datetime.now(UTC)
    score = (
        await db.get(Score, application.legacy_score_id)
        if application.legacy_score_id
        else None
    )
    application.legacy_score_id = None
    application.closed_at = application.closed_at or now
    application.archived_at = now
    application.archived_by_user_id = user.id
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=application.candidate_id,
        actor_user_id=user.id,
        application_id=application.id,
        position_id=application.position_id,
        event_type="application_archived",
        subject_type="application",
        subject_id=application.id,
        payload={
            "cycle_number": application.cycle_number,
            "stage": application.stage,
        },
        occurred_at=now,
    )
    await db.flush()
    if score is not None:
        await db.delete(score)
    await db.commit()
