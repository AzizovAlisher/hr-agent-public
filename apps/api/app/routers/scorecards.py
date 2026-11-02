"""Structured scorecards with blind feedback and approval decisions."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.dependencies import current_user
from app.db import get_session
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.notification import Notification
from app.models.position import Position
from app.models.scorecard import (
    HiringDecision,
    HiringDecisionApproval,
    ScorecardAssignment,
    ScorecardCriterion,
    ScorecardRating,
    ScorecardTemplate,
    ScorecardTemplateVersion,
)
from app.models.user import User
from app.schemas.applications import ApplicationStageTransition
from app.schemas.pagination import CursorPage
from app.schemas.scorecards import (
    HiringDecisionApprovalOut,
    HiringDecisionCreate,
    HiringDecisionOut,
    HiringDecisionVoteRequest,
    ScorecardAssignmentOut,
    ScorecardAssignRequest,
    ScorecardCriterionOut,
    ScorecardDraftUpdate,
    ScorecardPanelOut,
    ScorecardRatingOut,
    ScorecardTemplateCreate,
    ScorecardTemplateOut,
    ScorecardTemplateUpdate,
)
from app.services.activities import append_candidate_activity
from app.services.applications import (
    apply_application_stage_transition,
)
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.enterprise import approval_separation_required
from app.services.pagination import (
    collection_cursor_scope,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.structured_hiring import (
    resolve_application_for_write,
    resolve_linked_application,
)

router = APIRouter(tags=["scorecards"])


def _criteria_snapshot(criteria: list[ScorecardCriterion]) -> list[dict]:
    return [
        {
            "id": str(criterion.id),
            "key": criterion.key,
            "label": criterion.label,
            "description": criterion.description,
            "weight": criterion.weight,
            "position": criterion.position,
            "is_required": criterion.is_required,
        }
        for criterion in sorted(criteria, key=lambda item: (item.position, item.id))
    ]


def _snapshot_criteria(snapshot: list[dict] | None) -> list[ScorecardCriterionOut]:
    values: list[ScorecardCriterionOut] = []
    for item in snapshot or []:
        try:
            values.append(
                ScorecardCriterionOut(
                    id=UUID(str(item["id"])),
                    key=str(item["key"]),
                    label=str(item["label"]),
                    description=(
                        str(item["description"])
                        if item.get("description") is not None
                        else None
                    ),
                    weight=int(item["weight"]),
                    position=int(item["position"]),
                    is_required=bool(item["is_required"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Scorecard assignment has an invalid criteria snapshot",
            ) from None
    return sorted(values, key=lambda item: (item.position, item.key))


def _decision_snapshot(decision: HiringDecision) -> dict:
    return {
        "application_id": str(decision.application_id) if decision.application_id else None,
        "candidate_id": str(decision.candidate_id),
        "position_id": str(decision.position_id) if decision.position_id else None,
        "recommendation": decision.recommendation,
        "summary": decision.summary,
        "required_approvals": decision.required_approvals,
        "version": decision.version,
    }


async def _candidate(
    db: AsyncSession, candidate_id: UUID, user: User
) -> Candidate:
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return candidate


async def _position(
    db: AsyncSession, position_id: UUID, user: User
) -> Position:
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    return position


def _template_out(template: ScorecardTemplate) -> ScorecardTemplateOut:
    return ScorecardTemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        position_id=template.position_id,
        is_active=template.is_active,
        current_version=template.current_version,
        criteria=[
            ScorecardCriterionOut.model_validate(criterion)
            for criterion in template.criteria
        ],
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _weighted_average(ratings: list[ScorecardRating]) -> float | None:
    total_weight = sum(rating.criterion_weight_snapshot for rating in ratings)
    if not total_weight:
        return None
    total = sum(
        rating.rating * rating.criterion_weight_snapshot for rating in ratings
    )
    return round(total / total_weight, 2)


def _decision_out(decision: HiringDecision) -> HiringDecisionOut:
    return HiringDecisionOut(
        id=decision.id,
        application_id=decision.application_id,
        candidate_id=decision.candidate_id,
        position_id=decision.position_id,
        position_title=decision.position_title_snapshot,
        created_by_user_id=decision.created_by_user_id,
        created_by_email=decision.created_by_email_snapshot,
        recommendation=decision.recommendation,
        summary=decision.summary,
        version=decision.version,
        approval_cycle=decision.approval_cycle,
        required_approvals=decision.required_approvals,
        status=decision.status,
        decided_at=decision.decided_at,
        approvals=[
            HiringDecisionApprovalOut(
                id=approval.id,
                user_id=approval.user_id,
                user_email=approval.user_email_snapshot,
                decision_version=approval.decision_version_snapshot,
                approval_cycle=approval.approval_cycle,
                vote=approval.vote,
                comment=approval.comment,
                created_at=approval.created_at,
                updated_at=approval.updated_at,
            )
            for approval in sorted(
                decision.approvals, key=lambda item: (item.created_at, item.id)
            )
        ],
        created_at=decision.created_at,
        updated_at=decision.updated_at,
    )


@router.get(
    "/scorecard-templates", response_model=CursorPage[ScorecardTemplateOut]
)
async def list_scorecard_templates(
    position_id: UUID | None = None,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[ScorecardTemplateOut]:
    stmt = (
        select(ScorecardTemplate)
        .options(selectinload(ScorecardTemplate.criteria))
        .where(ScorecardTemplate.organization_id == user.organization_id)
    )
    if position_id is not None:
        await _position(db, position_id, user)
        stmt = stmt.where(
            (ScorecardTemplate.position_id == position_id)
            | (ScorecardTemplate.position_id.is_(None))
        )
    if not include_inactive:
        stmt = stmt.where(ScorecardTemplate.is_active.is_(True))
    namespace = "scorecards.templates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "position_id": str(position_id) if position_id else None,
            "include_inactive": include_inactive,
        },
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid scorecard templates cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid scorecard templates cursor"},
            )
        cursor_id = cursor_uuid(payload, detail="Invalid scorecard templates cursor")
        stmt = stmt.where(
            or_(
                ScorecardTemplate.name > cursor_name,
                and_(ScorecardTemplate.name == cursor_name, ScorecardTemplate.id > cursor_id),
            )
        )
    templates = list(
        (
            await db.execute(
                stmt.order_by(
                    ScorecardTemplate.name.asc(),
                    ScorecardTemplate.id.asc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(templates) > limit
    templates = templates[:limit]
    return CursorPage(
        items=[_template_out(template) for template in templates],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"name": templates[-1].name, "id": str(templates[-1].id)},
            )
            if has_more and templates
            else None
        ),
    )


@router.post(
    "/scorecard-templates",
    response_model=ScorecardTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_scorecard_template(
    body: ScorecardTemplateCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ScorecardTemplateOut:
    if body.position_id is not None:
        await _position(db, body.position_id, user)
    template = ScorecardTemplate(
        organization_id=user.organization_id,
        position_id=body.position_id,
        created_by_user_id=user.id,
        name=body.name,
        description=body.description,
    )
    db.add(template)
    try:
        await db.flush()
        criterion_rows: list[ScorecardCriterion] = []
        for index, criterion in enumerate(body.criteria):
            row = ScorecardCriterion(
                organization_id=user.organization_id,
                template_id=template.id,
                key=criterion.key,
                label=criterion.label,
                description=criterion.description,
                weight=criterion.weight,
                position=index,
                is_required=criterion.is_required,
            )
            db.add(row)
            criterion_rows.append(row)
        await db.flush()
        db.add(
            ScorecardTemplateVersion(
                organization_id=user.organization_id,
                template_id=template.id,
                version=template.current_version,
                name_snapshot=template.name,
                description_snapshot=template.description,
                position_id_snapshot=template.position_id,
                criteria_snapshot=_criteria_snapshot(criterion_rows),
                created_by_user_id=user.id,
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A scorecard template with this name already exists"
        ) from exc
    refreshed = (
        await db.execute(
            select(ScorecardTemplate)
            .options(selectinload(ScorecardTemplate.criteria))
            .execution_options(populate_existing=True)
            .where(ScorecardTemplate.id == template.id)
        )
    ).scalar_one()
    return _template_out(refreshed)


@router.patch(
    "/scorecard-templates/{template_id}", response_model=ScorecardTemplateOut
)
async def update_scorecard_template(
    template_id: UUID,
    body: ScorecardTemplateUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ScorecardTemplateOut:
    template = (
        await db.execute(
            select(ScorecardTemplate)
            .options(selectinload(ScorecardTemplate.criteria))
            .where(
                ScorecardTemplate.id == template_id,
                ScorecardTemplate.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scorecard template not found")
    values = body.model_dump(exclude_unset=True, exclude={"criteria"})
    semantic_changed = any(
        field in {"name", "description"} and getattr(template, field) != value
        for field, value in values.items()
    )
    for field, value in values.items():
        setattr(template, field, value)

    criteria_changed = False
    if body.criteria is not None:
        current_definition = [
            {
                "key": item.key,
                "label": item.label,
                "description": item.description,
                "weight": item.weight,
                "is_required": item.is_required,
            }
            for item in sorted(
                template.criteria, key=lambda item: (item.position, item.id)
            )
        ]
        requested_definition = [
            {
                "key": item.key,
                "label": item.label,
                "description": item.description,
                "weight": item.weight,
                "is_required": item.is_required,
            }
            for item in body.criteria
        ]
        criteria_changed = current_definition != requested_definition
    if criteria_changed:
        for criterion in list(template.criteria):
            await db.delete(criterion)
        await db.flush()
        criterion_rows = []
        for index, criterion in enumerate(body.criteria or []):
            row = ScorecardCriterion(
                organization_id=user.organization_id,
                template_id=template.id,
                key=criterion.key,
                label=criterion.label,
                description=criterion.description,
                weight=criterion.weight,
                position=index,
                is_required=criterion.is_required,
            )
            db.add(row)
            criterion_rows.append(row)
        await db.flush()
    else:
        criterion_rows = list(template.criteria)

    if semantic_changed or criteria_changed:
        template.current_version += 1
        db.add(
            ScorecardTemplateVersion(
                organization_id=user.organization_id,
                template_id=template.id,
                version=template.current_version,
                name_snapshot=template.name,
                description_snapshot=template.description,
                position_id_snapshot=template.position_id,
                criteria_snapshot=_criteria_snapshot(criterion_rows),
                created_by_user_id=user.id,
            )
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A scorecard template with this name already exists"
        ) from exc
    refreshed = (
        await db.execute(
            select(ScorecardTemplate)
            .options(selectinload(ScorecardTemplate.criteria))
            .execution_options(populate_existing=True)
            .where(ScorecardTemplate.id == template.id)
        )
    ).scalar_one()
    return _template_out(refreshed)


async def _panel(
    db: AsyncSession,
    *,
    candidate: Candidate,
    user: User,
    position_id: UUID | None,
    application_id: UUID | None,
) -> ScorecardPanelOut:
    stmt = (
        select(ScorecardAssignment)
        .options(selectinload(ScorecardAssignment.ratings))
        .where(
            ScorecardAssignment.organization_id == user.organization_id,
            ScorecardAssignment.candidate_id == candidate.id,
        )
    )
    if position_id is not None:
        stmt = stmt.where(ScorecardAssignment.position_id == position_id)
    if application_id is not None:
        stmt = stmt.where(ScorecardAssignment.application_id == application_id)
    assignments = list(
        (
            await db.execute(
                stmt.order_by(
                    ScorecardAssignment.created_at,
                    ScorecardAssignment.id,
                )
            )
        ).scalars()
    )
    completion_count = sum(item.status == "submitted" for item in assignments)
    revealed = bool(assignments) and completion_count == len(assignments)
    all_ratings: list[ScorecardRating] = []
    assignment_outputs: list[ScorecardAssignmentOut] = []
    for assignment in assignments:
        is_mine = assignment.assigned_to_user_id == user.id
        can_see = revealed or is_mine or not assignment.blind_until_all_submitted
        ratings = sorted(
            assignment.ratings,
            key=lambda item: (item.criterion_key_snapshot, item.id),
        )
        if revealed:
            all_ratings.extend(ratings)
        assignment_outputs.append(
            ScorecardAssignmentOut(
                id=assignment.id,
                application_id=assignment.application_id,
                candidate_id=assignment.candidate_id,
                position_id=assignment.position_id,
                template_id=assignment.template_id,
                template_name=assignment.template_name_snapshot,
                template_version=assignment.template_version_snapshot,
                criteria=_snapshot_criteria(assignment.criteria_snapshot),
                position_title=assignment.position_title_snapshot,
                assigned_to_user_id=assignment.assigned_to_user_id,
                assignee_email=assignment.assignee_email_snapshot,
                is_mine=is_mine,
                status=assignment.status,
                due_at=assignment.due_at,
                submitted_at=assignment.submitted_at,
                ratings=(
                    [
                        ScorecardRatingOut(
                            id=rating.id,
                            criterion_id=rating.criterion_id,
                            criterion_key=rating.criterion_key_snapshot,
                            criterion_label=rating.criterion_label_snapshot,
                            weight=rating.criterion_weight_snapshot,
                            rating=rating.rating,
                            comment=rating.comment,
                        )
                        for rating in ratings
                    ]
                    if can_see
                    else None
                ),
                weighted_average=_weighted_average(ratings) if can_see else None,
                created_at=assignment.created_at,
                updated_at=assignment.updated_at,
            )
        )

    decision_stmt = (
        select(HiringDecision)
        .options(selectinload(HiringDecision.approvals))
        .where(
            HiringDecision.organization_id == user.organization_id,
            HiringDecision.candidate_id == candidate.id,
        )
    )
    if position_id is not None:
        decision_stmt = decision_stmt.where(HiringDecision.position_id == position_id)
    if application_id is not None:
        decision_stmt = decision_stmt.where(
            HiringDecision.application_id == application_id
        )
    decision = (
        await db.execute(
            decision_stmt.order_by(
                HiringDecision.created_at.desc(), HiringDecision.id.desc()
            ).limit(1)
        )
    ).scalar_one_or_none()
    return ScorecardPanelOut(
        assignments=assignment_outputs,
        revealed=revealed,
        aggregate_average=_weighted_average(all_ratings) if revealed else None,
        completion_count=completion_count,
        assignment_count=len(assignments),
        decision=_decision_out(decision) if decision is not None else None,
    )


@router.get(
    "/candidates/{candidate_id}/scorecards", response_model=ScorecardPanelOut
)
async def get_candidate_scorecards(
    candidate_id: UUID,
    position_id: UUID | None = Query(default=None),
    application_id: UUID | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ScorecardPanelOut:
    candidate = await _candidate(db, candidate_id, user)
    if position_id is not None:
        await _position(db, position_id, user)
    resolved_application_id: UUID | None = None
    if application_id is not None:
        application = await resolve_linked_application(
            db,
            organization_id=user.organization_id,
            application_id=application_id,
            candidate_id=candidate.id,
            position_id=position_id,
        )
        resolved_application_id = application.id
        position_id = application.position_id
    elif position_id is not None:
        active = list(
            (
                await db.execute(
                    select(Application).where(
                        Application.organization_id == user.organization_id,
                        Application.candidate_id == candidate.id,
                        Application.position_id == position_id,
                        Application.closed_at.is_(None),
                        Application.archived_at.is_(None),
                    )
                )
            ).scalars()
        )
        if len(active) > 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Application cycle is ambiguous; provide application_id",
            )
        if active:
            resolved_application_id = active[0].id
    return await _panel(
        db,
        candidate=candidate,
        user=user,
        position_id=position_id,
        application_id=resolved_application_id,
    )


@router.post(
    "/candidates/{candidate_id}/scorecards/assign",
    response_model=ScorecardPanelOut,
    status_code=status.HTTP_201_CREATED,
)
async def assign_candidate_scorecards(
    candidate_id: UUID,
    body: ScorecardAssignRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ScorecardPanelOut:
    candidate = await _candidate(db, candidate_id, user)
    position = await _position(db, body.position_id, user)
    application = await resolve_application_for_write(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=body.application_id,
        source="scorecard",
        actor_user_id=user.id,
        actor_kind="human",
        reason="scorecard_application_link",
    )
    template = (
        await db.execute(
            select(ScorecardTemplate)
            .options(selectinload(ScorecardTemplate.criteria))
            .where(
                ScorecardTemplate.id == body.template_id,
                ScorecardTemplate.organization_id == user.organization_id,
                ScorecardTemplate.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if template is None or (
        template.position_id is not None and template.position_id != position.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scorecard template not found")
    template_version = (
        await db.execute(
            select(ScorecardTemplateVersion).where(
                ScorecardTemplateVersion.organization_id == user.organization_id,
                ScorecardTemplateVersion.template_id == template.id,
                ScorecardTemplateVersion.version == template.current_version,
            )
        )
    ).scalar_one_or_none()
    if template_version is None:
        template_version = ScorecardTemplateVersion(
            organization_id=user.organization_id,
            template_id=template.id,
            version=template.current_version,
            name_snapshot=template.name,
            description_snapshot=template.description,
            position_id_snapshot=template.position_id,
            criteria_snapshot=_criteria_snapshot(list(template.criteria)),
            created_by_user_id=user.id,
        )
        db.add(template_version)
        await db.flush()
    existing_decision = (
        await db.execute(
            select(HiringDecision.id).where(
                HiringDecision.organization_id == user.organization_id,
                HiringDecision.application_id == application.id,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing_decision is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Scorecards cannot be assigned after a hiring decision is created",
        )
    assignees = list(
        (
            await db.execute(
                select(User).where(
                    User.organization_id == user.organization_id,
                    User.id.in_(body.user_ids),
                )
            )
        ).scalars()
    )
    if len(assignees) != len(body.user_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team member not found")
    for assignee in assignees:
        exists = (
            await db.execute(
                select(ScorecardAssignment.id).where(
                    ScorecardAssignment.organization_id == user.organization_id,
                    ScorecardAssignment.application_id == application.id,
                    ScorecardAssignment.template_id == template.id,
                    ScorecardAssignment.assigned_to_user_id == assignee.id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        assignment = ScorecardAssignment(
            organization_id=user.organization_id,
            application_id=application.id,
            candidate_id=candidate.id,
            position_id=position.id,
            template_id=template.id,
            template_version_id=template_version.id,
            assigned_to_user_id=assignee.id,
            assigned_by_user_id=user.id,
            template_name_snapshot=template.name,
            template_version_snapshot=template.current_version,
            criteria_snapshot=template_version.criteria_snapshot,
            position_title_snapshot=position.title,
            assignee_email_snapshot=assignee.email,
            due_at=body.due_at,
        )
        db.add(assignment)
        await db.flush()
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            application_id=application.id,
            position_id=position.id,
            event_type="scorecard_assigned",
            subject_type="scorecard_assignment",
            subject_id=assignment.id,
            payload={
                "template_name": template.name,
                "template_version": template.current_version,
                "application_id": str(application.id),
                "position_title": position.title,
            },
        )
        db.add(
            Notification(
                organization_id=user.organization_id,
                user_id=assignee.id,
                type="scorecard_assigned",
                payload={
                    "candidate_id": str(candidate.id),
                    "position_id": str(position.id),
                    "assignment_id": str(assignment.id),
                    "application_id": str(application.id),
                    "position_title": position.title,
                    "template_name": template.name,
                },
            )
        )
    await db.commit()
    return await _panel(
        db,
        candidate=candidate,
        user=user,
        position_id=position.id,
        application_id=application.id,
    )


async def _editable_assignment(
    db: AsyncSession,
    assignment_id: UUID,
    user: User,
    *,
    allow_submitted: bool = False,
) -> ScorecardAssignment:
    assignment = (
        await db.execute(
            select(ScorecardAssignment)
            .options(selectinload(ScorecardAssignment.ratings))
            .where(
                ScorecardAssignment.id == assignment_id,
                ScorecardAssignment.organization_id == user.organization_id,
                ScorecardAssignment.assigned_to_user_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scorecard assignment not found")
    if assignment.status == "submitted" and not allow_submitted:
        raise HTTPException(status.HTTP_409_CONFLICT, "Scorecard is already submitted")
    return assignment


@router.patch(
    "/scorecard-assignments/{assignment_id}",
    response_model=ScorecardAssignmentOut,
)
async def update_scorecard_draft(
    assignment_id: UUID,
    body: ScorecardDraftUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ScorecardAssignmentOut:
    assignment = await _editable_assignment(db, assignment_id, user)
    criteria = _snapshot_criteria(assignment.criteria_snapshot)
    if not criteria:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Scorecard assignment has no immutable criteria snapshot",
        )
    by_id = {criterion.id: criterion for criterion in criteria}
    invalid_ids = {
        entry.criterion_id for entry in body.ratings if entry.criterion_id not in by_id
    }
    if invalid_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Criterion does not belong to this scorecard",
        )
    requested_ids = [entry.criterion_id for entry in body.ratings]
    live_ids = set(
        (
            await db.execute(
                select(ScorecardCriterion.id).where(
                    ScorecardCriterion.organization_id == user.organization_id,
                    ScorecardCriterion.id.in_(requested_ids),
                )
            )
        ).scalars()
    )
    existing = {
        rating.criterion_key_snapshot: rating for rating in assignment.ratings
    }
    for entry in body.ratings:
        criterion = by_id.get(entry.criterion_id)
        assert criterion is not None
        rating = existing.get(criterion.key)
        clean_comment = entry.comment.strip() or None if entry.comment is not None else None
        if rating is None:
            assignment.ratings.append(
                ScorecardRating(
                    organization_id=user.organization_id,
                    assignment_id=assignment.id,
                    criterion_id=(
                        criterion.id if criterion.id in live_ids else None
                    ),
                    criterion_key_snapshot=criterion.key,
                    criterion_label_snapshot=criterion.label,
                    criterion_weight_snapshot=criterion.weight,
                    rating=entry.rating,
                    comment=clean_comment,
                )
            )
        elif rating.rating != entry.rating or rating.comment != clean_comment:
            rating.rating = entry.rating
            rating.comment = clean_comment
    await db.commit()
    refreshed = (
        await db.execute(
            select(ScorecardAssignment)
            .options(selectinload(ScorecardAssignment.ratings))
            .where(ScorecardAssignment.id == assignment.id)
        )
    ).scalar_one()
    ratings = sorted(
        refreshed.ratings, key=lambda item: (item.criterion_key_snapshot, item.id)
    )
    return ScorecardAssignmentOut(
        id=refreshed.id,
        application_id=refreshed.application_id,
        candidate_id=refreshed.candidate_id,
        position_id=refreshed.position_id,
        template_id=refreshed.template_id,
        template_name=refreshed.template_name_snapshot,
        template_version=refreshed.template_version_snapshot,
        criteria=_snapshot_criteria(refreshed.criteria_snapshot),
        position_title=refreshed.position_title_snapshot,
        assigned_to_user_id=refreshed.assigned_to_user_id,
        assignee_email=refreshed.assignee_email_snapshot,
        is_mine=True,
        status=refreshed.status,
        due_at=refreshed.due_at,
        submitted_at=refreshed.submitted_at,
        ratings=[
            ScorecardRatingOut(
                id=rating.id,
                criterion_id=rating.criterion_id,
                criterion_key=rating.criterion_key_snapshot,
                criterion_label=rating.criterion_label_snapshot,
                weight=rating.criterion_weight_snapshot,
                rating=rating.rating,
                comment=rating.comment,
            )
            for rating in ratings
        ],
        weighted_average=_weighted_average(ratings),
        created_at=refreshed.created_at,
        updated_at=refreshed.updated_at,
    )


@router.post(
    "/scorecard-assignments/{assignment_id}/submit",
    response_model=ScorecardPanelOut,
)
async def submit_scorecard(
    assignment_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ScorecardPanelOut:
    assignment = await _editable_assignment(
        db, assignment_id, user, allow_submitted=True
    )
    if assignment.status == "submitted":
        await db.commit()
        candidate = await _candidate(db, assignment.candidate_id, user)
        return await _panel(
            db,
            candidate=candidate,
            user=user,
            position_id=assignment.position_id,
            application_id=assignment.application_id,
        )
    criteria = _snapshot_criteria(assignment.criteria_snapshot)
    if not criteria:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Scorecard assignment has no immutable criteria snapshot",
        )
    required = {criterion.key for criterion in criteria if criterion.is_required}
    rated = {rating.criterion_key_snapshot for rating in assignment.ratings}
    if not assignment.ratings or required - rated:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Complete every required criterion before submitting",
        )
    now = datetime.now(UTC)
    assignment.status = "submitted"
    assignment.submitted_at = now
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=assignment.candidate_id,
        actor_user_id=user.id,
        application_id=assignment.application_id,
        position_id=assignment.position_id,
        event_type="scorecard_submitted",
        subject_type="scorecard_assignment",
        subject_id=assignment.id,
        payload={"template_name": assignment.template_name_snapshot},
        occurred_at=now,
    )
    await db.commit()
    candidate = await _candidate(db, assignment.candidate_id, user)
    return await _panel(
        db,
        candidate=candidate,
        user=user,
        position_id=assignment.position_id,
        application_id=assignment.application_id,
    )


@router.post(
    "/candidates/{candidate_id}/decisions",
    response_model=HiringDecisionOut,
)
async def create_or_update_hiring_decision(
    candidate_id: UUID,
    body: HiringDecisionCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> HiringDecisionOut:
    candidate = await _candidate(db, candidate_id, user)
    position = await _position(db, body.position_id, user)
    application = await resolve_application_for_write(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=body.application_id,
        source="scorecard",
        actor_user_id=user.id,
        actor_kind="human",
        reason="hiring_decision_application_link",
    )
    assignments = list(
        (
            await db.execute(
                select(ScorecardAssignment).where(
                    ScorecardAssignment.organization_id == user.organization_id,
                    ScorecardAssignment.application_id == application.id,
                )
            )
        ).scalars()
    )
    if not assignments or any(item.status != "submitted" for item in assignments):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "All assigned scorecards must be submitted before a decision",
        )
    decision = (
        await db.execute(
            select(HiringDecision)
            .options(selectinload(HiringDecision.approvals))
            .where(
                HiringDecision.organization_id == user.organization_id,
                HiringDecision.application_id == application.id,
            )
            .order_by(HiringDecision.created_at.desc(), HiringDecision.id.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    event_type = "hiring_decision_created"
    clean_summary = body.summary.strip() or None if body.summary else None
    if decision is None:
        decision = HiringDecision(
            organization_id=user.organization_id,
            application_id=application.id,
            candidate_id=candidate.id,
            position_id=position.id,
            created_by_user_id=user.id,
            position_title_snapshot=position.title,
            created_by_email_snapshot=user.email,
            recommendation=body.recommendation,
            summary=clean_summary,
            required_approvals=body.required_approvals,
        )
        db.add(decision)
        await db.flush()
    else:
        if decision.status != "pending":
            raise HTTPException(status.HTTP_409_CONFLICT, "Decision is already final")
        changed = (
            decision.recommendation != body.recommendation
            or decision.summary != clean_summary
            or decision.required_approvals != body.required_approvals
        )
        if not changed:
            await db.commit()
            return _decision_out(decision)
        decision.recommendation = body.recommendation
        decision.summary = clean_summary
        decision.required_approvals = body.required_approvals
        # Preserve prior votes as immutable review evidence and open a fresh
        # cycle for the new recommendation/summary/threshold.
        decision.version += 1
        decision.approval_cycle += 1
        decision.decided_at = None
        event_type = "hiring_decision_updated"
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        application_id=application.id,
        position_id=position.id,
        event_type=event_type,
        subject_type="hiring_decision",
        subject_id=decision.id,
        payload={
            "recommendation": decision.recommendation,
            "application_id": str(application.id),
            "version": decision.version,
            "approval_cycle": decision.approval_cycle,
            "position_title": position.title,
            "required_approvals": decision.required_approvals,
        },
    )
    await db.commit()
    refreshed = (
        await db.execute(
            select(HiringDecision)
            .options(selectinload(HiringDecision.approvals))
            .where(HiringDecision.id == decision.id)
        )
    ).scalar_one()
    return _decision_out(refreshed)


@router.post(
    "/hiring-decisions/{decision_id}/vote",
    response_model=HiringDecisionOut,
)
async def vote_hiring_decision(
    decision_id: UUID,
    body: HiringDecisionVoteRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> HiringDecisionOut:
    decision = (
        await db.execute(
            select(HiringDecision)
            .options(selectinload(HiringDecision.approvals))
            .where(
                HiringDecision.id == decision_id,
                HiringDecision.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if decision is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring decision not found")
    if decision.created_by_user_id == user.id and await approval_separation_required(
        db,
        organization_id=user.organization_id,
        category="hiring_decisions",
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The decision author cannot approve their own decision",
        )
    approval = next(
        (
            item
            for item in decision.approvals
            if item.user_id == user.id
            and item.approval_cycle == decision.approval_cycle
        ),
        None,
    )
    clean_comment = body.comment.strip() or None if body.comment else None
    if decision.status != "pending":
        if (
            approval is not None
            and approval.vote == body.vote
            and approval.comment == clean_comment
        ):
            await db.commit()
            return _decision_out(decision)
        raise HTTPException(status.HTTP_409_CONFLICT, "Decision is already final")
    if (
        approval is not None
        and approval.vote == body.vote
        and approval.comment == clean_comment
    ):
        await db.commit()
        return _decision_out(decision)
    if approval is None:
        approval = HiringDecisionApproval(
            organization_id=user.organization_id,
            decision_id=decision.id,
            user_id=user.id,
            user_email_snapshot=user.email,
            decision_version_snapshot=decision.version,
            approval_cycle=decision.approval_cycle,
            decision_snapshot=_decision_snapshot(decision),
            vote=body.vote,
            comment=clean_comment,
        )
        db.add(approval)
        decision.approvals.append(approval)
    else:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A recorded approval vote is immutable",
        )
    now = datetime.now(UTC)
    votes = [
        item.vote
        for item in decision.approvals
        if item.approval_cycle == decision.approval_cycle
    ]
    if "reject" in votes:
        decision.status = "rejected"
        decision.decided_at = now
    elif votes.count("approve") >= decision.required_approvals:
        decision.status = "approved"
        decision.decided_at = now
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=decision.candidate_id,
        actor_user_id=user.id,
        application_id=decision.application_id,
        position_id=decision.position_id,
        event_type=(
            "hiring_decision_finalized"
            if decision.status != "pending"
            else "hiring_decision_voted"
        ),
        subject_type="hiring_decision",
        subject_id=decision.id,
        payload={
            "vote": body.vote,
            "status": decision.status,
            "recommendation": decision.recommendation,
            "application_id": (
                str(decision.application_id) if decision.application_id else None
            ),
            "decision_version": decision.version,
            "approval_cycle": decision.approval_cycle,
        },
        occurred_at=now,
    )
    if decision.status == "approved" and decision.position_id is not None:
        if decision.application_id is None:
            application = await resolve_application_for_write(
                db,
                organization_id=user.organization_id,
                candidate_id=decision.candidate_id,
                position_id=decision.position_id,
                application_id=None,
                source="scorecard",
                actor_user_id=user.id,
                actor_kind="human",
                reason="legacy_hiring_decision_application_link",
                occurred_at=now,
            )
            decision.application_id = application.id
        else:
            application = await resolve_linked_application(
                db,
                organization_id=user.organization_id,
                application_id=decision.application_id,
                candidate_id=decision.candidate_id,
                position_id=decision.position_id,
                lock=True,
            )
        stage_by_recommendation = {
            "advance": "interview",
            "reject": "rejected",
            # A panel recommendation starts the offer step. Only public offer
            # acceptance may perform the final offer -> hired transition.
            "hire": "offer",
        }
        next_stage = stage_by_recommendation.get(decision.recommendation)
        if decision.recommendation == "advance":
            if application.stage not in {"new", "screening"}:
                next_stage = None
        if next_stage is not None:
            await apply_application_stage_transition(
                db,
                organization_id=user.organization_id,
                application_id=application.id,
                body=ApplicationStageTransition(
                    stage=next_stage,
                    expected_version=application.updated_at,
                    disposition_code=(
                        "other" if decision.recommendation == "reject" else None
                    ),
                    reason=decision.summary or "Approved hiring decision",
                    details={
                        "hiring_decision_id": str(decision.id),
                        "recommendation": decision.recommendation,
                    },
                ),
                actor_user_id=user.id,
                actor_kind="human",
                occurred_at=now,
            )
    await db.commit()
    refreshed = (
        await db.execute(
            select(HiringDecision)
            .options(selectinload(HiringDecision.approvals))
            .where(HiringDecision.id == decision.id)
        )
    ).scalar_one()
    return _decision_out(refreshed)


# Registered here because main.py already mounts the scorecards router. Keeping
# the new domain routers composed avoids a main-module edit during this wave.
from app.routers.applications import (
    public_router as public_assessment_router,  # noqa: E402
)
from app.routers.applications import router as applications_router  # noqa: E402

router.include_router(applications_router)
router.include_router(public_assessment_router)
