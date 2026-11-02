"""Headcount approvals, confidential offers, e-signature, and hired handoff."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import (
    forced_tenant_rls,
    get_session,
    resolve_capability,
    set_tenant_context,
)
from app.models.candidate import Candidate
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import (
    CandidateHandoff,
    CandidateOffer,
    OfferApproval,
    OfferTemplate,
    Requisition,
    RequisitionApproval,
)
from app.models.user import User
from app.schemas.applications import ApplicationStageTransition
from app.schemas.pagination import CursorPage
from app.schemas.requisitions import (
    ApprovalOut,
    ApprovalVoteRequest,
    CandidateHandoffOut,
    CandidateHandoffUpdate,
    CandidateOfferCreate,
    CandidateOfferOut,
    CandidateOfferRotateRequest,
    CandidateOfferSendOut,
    CandidateOfferUpdate,
    OfferTemplateCreate,
    OfferTemplateOut,
    OfferTemplateUpdate,
    PublicOfferOut,
    PublicOfferRespondRequest,
    RequisitionCreate,
    RequisitionOpenRequest,
    RequisitionOut,
    RequisitionUpdate,
)
from app.services.activities import append_candidate_activity
from app.services.applications import (
    apply_application_stage_transition,
)
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.enterprise import (
    approval_separation_required,
    can_access_field,
    require_permission,
)
from app.services.entitlements import record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.structured_hiring import (
    resolve_application_for_write,
    resolve_linked_application,
)

router = APIRouter(tags=["requisitions-offers"])
settings = get_settings()
_TEMPLATE_PATTERN = re.compile(r"{{\s*([a-z0-9_.]+)\s*}}", re.IGNORECASE)
_OFFER_VARIABLES = {
    "candidate.full_name",
    "position.title",
    "organization.name",
    "offer.salary",
    "offer.start_date",
}
_REQUISITION_COMPENSATION_FIELDS = {
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_confidential",
}
_HANDOFF_CHECKLIST_ITEMS = {"documents", "equipment", "accounts", "first_day"}
_PUBLIC_OFFER_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}
_DEFAULT_OFFER_LIFETIME = timedelta(days=14)
_MAX_OFFER_LIFETIME = timedelta(days=30)


def _public_offer_error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_PUBLIC_OFFER_HEADERS,
    )


def _bounded_offer_expiration(
    requested: datetime | None,
    *,
    now: datetime,
) -> datetime:
    expires_at = requested or now + _DEFAULT_OFFER_LIFETIME
    if expires_at <= now:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Offer expiration must be in the future",
        )
    if expires_at > now + _MAX_OFFER_LIFETIME:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Offer expiration cannot be more than 30 days away",
        )
    return expires_at


async def _can_view_compensation(
    db: AsyncSession,
    user: User,
    *,
    permission: str,
) -> bool:
    return await can_access_field(
        db,
        user,
        FieldClass.COMPENSATION,
        required_permission=permission,
    )


async def _require_compensation(
    db: AsyncSession,
    user: User,
    *,
    permission: str,
) -> None:
    if not await _can_view_compensation(db, user, permission=permission):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Compensation access is restricted by organization policy",
        )


async def _require_requisition_management(db: AsyncSession, user: User) -> None:
    await require_permission(db, user, "requisitions.manage")


async def _require_requisition_approval(db: AsyncSession, user: User) -> None:
    await require_permission(db, user, "requisitions.approve")


async def _require_offer_management(db: AsyncSession, user: User) -> None:
    await require_permission(db, user, "offers.manage")


async def _require_offer_read(db: AsyncSession, user: User) -> None:
    await require_permission(db, user, "offers.read")


def _clean_comment(value: str | None) -> str | None:
    return value.strip() or None if value else None


def _approval_out(approval: RequisitionApproval | OfferApproval) -> ApprovalOut:
    subject_version = (
        approval.requisition_version_snapshot
        if isinstance(approval, RequisitionApproval)
        else approval.offer_version_snapshot
    )
    return ApprovalOut(
        id=approval.id,
        user_id=approval.user_id,
        user_email=approval.user_email_snapshot,
        subject_version=subject_version,
        approval_cycle=approval.approval_cycle,
        vote=approval.vote,
        comment=approval.comment,
        created_at=approval.created_at,
        updated_at=approval.updated_at,
    )


def _requisition_out(
    requisition: Requisition, *, compensation_visible: bool
) -> RequisitionOut:
    visible = not requisition.salary_confidential or compensation_visible
    return RequisitionOut(
        id=requisition.id,
        title=requisition.title,
        department=requisition.department,
        business_justification=requisition.business_justification,
        headcount=requisition.headcount,
        hiring_manager_user_id=requisition.hiring_manager_user_id,
        position_id=requisition.position_id,
        salary_min=requisition.salary_min if visible else None,
        salary_max=requisition.salary_max if visible else None,
        salary_currency=requisition.salary_currency if visible else None,
        salary_confidential=requisition.salary_confidential,
        compensation_visible=visible,
        status=requisition.status,
        required_approvals=requisition.required_approvals,
        version=requisition.version,
        approval_cycle=requisition.approval_cycle,
        approvals=[
            _approval_out(approval)
            for approval in sorted(requisition.approvals, key=lambda item: (item.created_at, item.id))
        ],
        submitted_at=requisition.submitted_at,
        decided_at=requisition.decided_at,
        created_at=requisition.created_at,
        updated_at=requisition.updated_at,
    )


def _offer_out(offer: CandidateOffer, *, compensation_visible: bool) -> CandidateOfferOut:
    visible = compensation_visible
    return CandidateOfferOut(
        id=offer.id,
        application_id=offer.application_id,
        candidate_id=offer.candidate_id,
        position_id=offer.position_id,
        position_title=offer.position_title_snapshot,
        template_id=offer.template_id,
        template_name=offer.template_name_snapshot,
        subject_source=offer.subject_template_snapshot if visible else None,
        body_source=offer.body_template_snapshot if visible else None,
        subject=offer.subject if visible else None,
        body=offer.body if visible else None,
        salary_amount=offer.salary_amount if visible else None,
        salary_currency=offer.salary_currency if visible else None,
        compensation_visible=visible,
        start_date=offer.start_date,
        expires_at=offer.expires_at,
        required_approvals=offer.required_approvals,
        version=offer.version,
        approval_cycle=offer.approval_cycle,
        status=offer.status,
        approvals=[
            _approval_out(approval)
            for approval in sorted(offer.approvals, key=lambda item: (item.created_at, item.id))
        ],
        sent_at=offer.sent_at,
        responded_at=offer.responded_at,
        signature_name=offer.signature_name,
        created_at=offer.created_at,
        updated_at=offer.updated_at,
    )


async def _requisition_response(
    db: AsyncSession, requisition: Requisition, user: User
) -> RequisitionOut:
    return _requisition_out(
        requisition,
        compensation_visible=await _can_view_compensation(
            db, user, permission="requisitions.compensation"
        ),
    )


async def _offer_response(
    db: AsyncSession, offer: CandidateOffer, user: User
) -> CandidateOfferOut:
    return _offer_out(
        offer,
        compensation_visible=await _can_view_compensation(
            db, user, permission="offers.compensation"
        ),
    )


def _public_offer_out(offer: CandidateOffer, organization: Organization | None) -> PublicOfferOut:
    return PublicOfferOut(
        organization_name=organization.name if organization else "",
        position_title=offer.position_title_snapshot,
        subject=offer.subject,
        body=offer.body,
        salary_amount=offer.salary_amount,
        salary_currency=offer.salary_currency,
        start_date=offer.start_date,
        expires_at=offer.expires_at,
        status=offer.status,
    )


def _handoff_out(handoff: CandidateHandoff) -> CandidateHandoffOut:
    return CandidateHandoffOut(
        id=handoff.id,
        application_id=handoff.application_id,
        offer_id=handoff.offer_id,
        candidate_id=handoff.candidate_id,
        position_id=handoff.position_id,
        position_title=handoff.position_title_snapshot,
        status=handoff.status,
        checklist=handoff.checklist,
        completed_at=handoff.completed_at,
        created_at=handoff.created_at,
        updated_at=handoff.updated_at,
    )


def _requisition_snapshot(requisition: Requisition) -> dict:
    return {
        "title": requisition.title,
        "department": requisition.department,
        "business_justification": requisition.business_justification,
        "headcount": requisition.headcount,
        "hiring_manager_user_id": (
            str(requisition.hiring_manager_user_id)
            if requisition.hiring_manager_user_id
            else None
        ),
        "salary_min": (
            str(requisition.salary_min) if requisition.salary_min is not None else None
        ),
        "salary_max": (
            str(requisition.salary_max) if requisition.salary_max is not None else None
        ),
        "salary_currency": requisition.salary_currency,
        "salary_confidential": requisition.salary_confidential,
        "required_approvals": requisition.required_approvals,
        "version": requisition.version,
    }


def _offer_snapshot(offer: CandidateOffer) -> dict:
    return {
        "application_id": str(offer.application_id) if offer.application_id else None,
        "candidate_id": str(offer.candidate_id),
        "position_id": str(offer.position_id) if offer.position_id else None,
        "subject": offer.subject,
        "body": offer.body,
        "salary_amount": (
            str(offer.salary_amount) if offer.salary_amount is not None else None
        ),
        "salary_currency": offer.salary_currency,
        "start_date": offer.start_date.isoformat() if offer.start_date else None,
        "expires_at": offer.expires_at.isoformat() if offer.expires_at else None,
        "required_approvals": offer.required_approvals,
        "version": offer.version,
    }


async def _requisition(
    db: AsyncSession, requisition_id: UUID, user: User, *, lock: bool = False
) -> Requisition:
    stmt = (
        select(Requisition)
        .options(selectinload(Requisition.approvals))
        .execution_options(populate_existing=True)
        .where(
            Requisition.id == requisition_id,
            Requisition.organization_id == user.organization_id,
        )
    )
    if lock:
        stmt = stmt.with_for_update()
    requisition = (await db.execute(stmt)).scalar_one_or_none()
    if requisition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requisition not found")
    return requisition


@router.get("/requisitions", response_model=CursorPage[RequisitionOut])
async def list_requisitions(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[RequisitionOut]:
    namespace = "requisitions.list"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    stmt = (
        select(Requisition)
        .options(selectinload(Requisition.approvals))
        .where(Requisition.organization_id == user.organization_id)
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid requisitions cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid requisitions cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid requisitions cursor")
        stmt = stmt.where(
            or_(
                Requisition.created_at < cursor_time,
                and_(Requisition.created_at == cursor_time, Requisition.id < cursor_id),
            )
        )
    requisitions = list(
        (
            await db.execute(
                stmt.order_by(Requisition.created_at.desc(), Requisition.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    compensation_visible = await _can_view_compensation(
        db, user, permission="requisitions.compensation"
    )
    has_more = len(requisitions) > limit
    requisitions = requisitions[:limit]
    return CursorPage(
        items=[
            _requisition_out(item, compensation_visible=compensation_visible)
            for item in requisitions
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": requisitions[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(requisitions[-1].id),
                },
            )
            if has_more and requisitions
            else None
        ),
    )


@router.post(
    "/requisitions", response_model=RequisitionOut, status_code=status.HTTP_201_CREATED
)
async def create_requisition(
    body: RequisitionCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RequisitionOut:
    await _require_requisition_management(db, user)
    if (
        body.salary_min is not None
        or body.salary_max is not None
        or not body.salary_confidential
    ):
        await _require_compensation(
            db, user, permission="requisitions.compensation"
        )
    if body.hiring_manager_user_id is not None:
        manager = (
            await db.execute(
                select(User.id).where(
                    User.id == body.hiring_manager_user_id,
                    User.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if manager is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring manager not found")
    requisition = Requisition(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        **body.model_dump(),
    )
    db.add(requisition)
    await db.commit()
    refreshed = await _requisition(db, requisition.id, user)
    return await _requisition_response(db, refreshed, user)


@router.patch("/requisitions/{requisition_id}", response_model=RequisitionOut)
async def update_requisition(
    requisition_id: UUID,
    body: RequisitionUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RequisitionOut:
    await _require_requisition_management(db, user)
    requisition = await _requisition(db, requisition_id, user, lock=True)
    if requisition.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only draft requisitions can be edited")
    if body.hiring_manager_user_id is not None:
        manager = (
            await db.execute(
                select(User.id).where(
                    User.id == body.hiring_manager_user_id,
                    User.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if manager is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Hiring manager not found")
    values = body.model_dump(exclude_unset=True)
    if _REQUISITION_COMPENSATION_FIELDS & values.keys():
        await _require_compensation(
            db, user, permission="requisitions.compensation"
        )
    min_salary = values.get("salary_min", requisition.salary_min)
    max_salary = values.get("salary_max", requisition.salary_max)
    if min_salary is not None and max_salary is not None and min_salary > max_salary:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid salary range")
    changed = False
    for field, value in values.items():
        if getattr(requisition, field) != value:
            setattr(requisition, field, value)
            changed = True
    if changed:
        requisition.version += 1
    await db.commit()
    refreshed = await _requisition(db, requisition.id, user)
    return await _requisition_response(db, refreshed, user)


@router.post("/requisitions/{requisition_id}/submit", response_model=RequisitionOut)
async def submit_requisition(
    requisition_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RequisitionOut:
    await _require_requisition_management(db, user)
    requisition = await _requisition(db, requisition_id, user, lock=True)
    if requisition.status == "pending":
        await db.commit()
        return await _requisition_response(db, requisition, user)
    if requisition.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Requisition cannot be submitted")
    requisition.status = "pending"
    requisition.approval_cycle += 1
    requisition.submitted_at = datetime.now(UTC)
    admins = list(
        (
            await db.execute(
                select(User.id).where(
                    User.organization_id == user.organization_id,
                    User.role.in_(("admin", "hiring_manager")),
                )
            )
        ).scalars()
    )
    for user_id in admins:
        db.add(
            Notification(
                organization_id=user.organization_id,
                user_id=user_id,
                type="requisition_pending",
                payload={"requisition_id": str(requisition.id), "title": requisition.title},
            )
        )
    await db.commit()
    refreshed = await _requisition(db, requisition.id, user)
    return await _requisition_response(db, refreshed, user)


@router.post("/requisitions/{requisition_id}/vote", response_model=RequisitionOut)
async def vote_requisition(
    requisition_id: UUID,
    body: ApprovalVoteRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RequisitionOut:
    await _require_requisition_approval(db, user)
    requisition = await _requisition(db, requisition_id, user, lock=True)
    if requisition.created_by_user_id == user.id and await approval_separation_required(
        db,
        organization_id=user.organization_id,
        category="requisitions",
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The requisition author cannot approve their own requisition",
        )
    approval = next(
        (
            item
            for item in requisition.approvals
            if item.user_id == user.id
            and item.approval_cycle == requisition.approval_cycle
        ),
        None,
    )
    clean_comment = _clean_comment(body.comment)
    if requisition.status != "pending":
        expected_status = "approved" if body.vote == "approve" else "rejected"
        if (
            requisition.status == expected_status
            and approval is not None
            and approval.vote == body.vote
            and approval.comment == clean_comment
        ):
            await db.commit()
            return await _requisition_response(db, requisition, user)
        raise HTTPException(status.HTTP_409_CONFLICT, "Requisition is not pending")
    if approval is None:
        approval = RequisitionApproval(
            organization_id=user.organization_id,
            requisition_id=requisition.id,
            user_id=user.id,
            user_email_snapshot=user.email,
            requisition_version_snapshot=requisition.version,
            approval_cycle=requisition.approval_cycle,
            requisition_snapshot=_requisition_snapshot(requisition),
            vote=body.vote,
            comment=clean_comment,
        )
        db.add(approval)
        requisition.approvals.append(approval)
    elif approval.vote == body.vote and approval.comment == clean_comment:
        await db.commit()
        return await _requisition_response(db, requisition, user)
    else:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A recorded approval vote is immutable",
        )
    votes = [
        item.vote
        for item in requisition.approvals
        if item.approval_cycle == requisition.approval_cycle
    ]
    if "reject" in votes:
        requisition.status = "rejected"
        requisition.decided_at = datetime.now(UTC)
    elif votes.count("approve") >= requisition.required_approvals:
        requisition.status = "approved"
        requisition.decided_at = datetime.now(UTC)
    await db.commit()
    refreshed = await _requisition(db, requisition.id, user)
    return await _requisition_response(db, refreshed, user)


@router.post("/requisitions/{requisition_id}/open", response_model=RequisitionOut)
async def open_requisition(
    requisition_id: UUID,
    body: RequisitionOpenRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RequisitionOut:
    await _require_requisition_management(db, user)
    requisition = await _requisition(db, requisition_id, user, lock=True)
    if requisition.status == "opened" and requisition.position_id is not None:
        await db.commit()
        return await _requisition_response(db, requisition, user)
    if requisition.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, "Requisition must be approved first")
    position_id = uuid4()
    await record_usage(
        db,
        organization_id=user.organization_id,
        metric="active_jobs",
        amount=1,
        kind="usage",
        idempotency_key=f"active-job:requisition:{requisition.id}",
        resource_type="position",
        resource_id=position_id,
    )
    position = Position(
        id=position_id,
        organization_id=user.organization_id,
        title=requisition.title,
        description=body.description,
        requirements=body.requirements,
        status="open",
        public_apply_enabled=body.public_apply_enabled,
        created_by=user.id,
    )
    db.add(position)
    await db.flush()
    requisition.position_id = position.id
    requisition.status = "opened"
    await db.commit()
    refreshed = await _requisition(db, requisition.id, user)
    return await _requisition_response(db, refreshed, user)


@router.post("/requisitions/{requisition_id}/cancel", response_model=RequisitionOut)
async def cancel_requisition(
    requisition_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RequisitionOut:
    await _require_requisition_management(db, user)
    requisition = await _requisition(db, requisition_id, user, lock=True)
    if requisition.status == "cancelled":
        await db.commit()
        return await _requisition_response(db, requisition, user)
    if requisition.status not in {"draft", "pending", "approved"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Requisition cannot be cancelled")
    requisition.status = "cancelled"
    requisition.decided_at = datetime.now(UTC)
    await db.commit()
    refreshed = await _requisition(db, requisition.id, user)
    return await _requisition_response(db, refreshed, user)


def _template_out(template: OfferTemplate) -> OfferTemplateOut:
    return OfferTemplateOut(
        id=template.id,
        name=template.name,
        subject_template=template.subject_template,
        body_template=template.body_template,
        is_active=template.is_active,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _validate_offer_template(value: str) -> None:
    matches = _TEMPLATE_PATTERN.findall(value)
    unknown = {match.lower() for match in matches} - _OFFER_VARIABLES
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown offer template variable: {sorted(unknown)[0]}",
        )
    remainder = _TEMPLATE_PATTERN.sub("", value)
    if "{{" in remainder or "}}" in remainder:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Malformed offer template variable",
        )


def _candidate_name(candidate: Candidate) -> str:
    parsed = candidate.parsed_cv if isinstance(candidate.parsed_cv, dict) else {}
    for key in ("applicant_name", "full_name", "name"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return (candidate.source_ref or "Candidate")[:200]


def _render_offer(value: str, variables: dict[str, str], *, max_length: int) -> str:
    _validate_offer_template(value)
    rendered = _TEMPLATE_PATTERN.sub(
        lambda match: variables[match.group(1).lower()], value
    )
    if len(rendered) > max_length:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Rendered offer content exceeds {max_length} characters",
        )
    return rendered


async def _offer_template(
    db: AsyncSession, template_id: UUID, user: User, *, lock: bool = False
) -> OfferTemplate:
    stmt = select(OfferTemplate).where(
        OfferTemplate.id == template_id,
        OfferTemplate.organization_id == user.organization_id,
    )
    if lock:
        stmt = stmt.with_for_update()
    template = (await db.execute(stmt)).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer template not found")
    return template


@router.get("/offer-templates", response_model=CursorPage[OfferTemplateOut])
async def list_offer_templates(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[OfferTemplateOut]:
    await _require_offer_read(db, user)
    namespace = "offers.templates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"active": True},
    )
    stmt = select(OfferTemplate).where(
        OfferTemplate.organization_id == user.organization_id,
        OfferTemplate.is_active.is_(True),
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid offer templates cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid offer templates cursor"},
            )
        cursor_id = cursor_uuid(payload, detail="Invalid offer templates cursor")
        stmt = stmt.where(
            or_(
                OfferTemplate.name > cursor_name,
                and_(OfferTemplate.name == cursor_name, OfferTemplate.id > cursor_id),
            )
        )
    templates = list(
        (
            await db.execute(
                stmt.order_by(OfferTemplate.name, OfferTemplate.id).limit(limit + 1)
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


@router.post("/offer-templates", response_model=OfferTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_offer_template(
    body: OfferTemplateCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> OfferTemplateOut:
    await _require_offer_management(db, user)
    _validate_offer_template(body.subject_template)
    _validate_offer_template(body.body_template)
    template = OfferTemplate(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        **body.model_dump(),
    )
    db.add(template)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer template name exists") from exc
    await db.refresh(template)
    return _template_out(template)


@router.patch("/offer-templates/{template_id}", response_model=OfferTemplateOut)
async def update_offer_template(
    template_id: UUID,
    body: OfferTemplateUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> OfferTemplateOut:
    await _require_offer_management(db, user)
    template = await _offer_template(db, template_id, user, lock=True)
    values = body.model_dump(exclude_unset=True)
    if "subject_template" in values:
        _validate_offer_template(values["subject_template"])
    if "body_template" in values:
        _validate_offer_template(values["body_template"])
    for field, value in values.items():
        setattr(template, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer template name exists") from exc
    await db.refresh(template)
    return _template_out(template)


async def _offer(db: AsyncSession, offer_id: UUID, user: User, *, lock: bool = False) -> CandidateOffer:
    stmt = (
        select(CandidateOffer)
        .options(selectinload(CandidateOffer.approvals))
        .execution_options(populate_existing=True)
        .where(
            CandidateOffer.id == offer_id,
            CandidateOffer.organization_id == user.organization_id,
        )
    )
    if lock:
        stmt = stmt.with_for_update()
    offer = (await db.execute(stmt)).scalar_one_or_none()
    if offer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer not found")
    return offer


@router.get(
    "/candidates/{candidate_id}/offers",
    response_model=CursorPage[CandidateOfferOut],
)
async def list_candidate_offers(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CandidateOfferOut]:
    await _require_offer_read(db, user)
    candidate = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    namespace = "offers.candidate"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = (
        select(CandidateOffer)
        .options(selectinload(CandidateOffer.approvals))
        .where(
            CandidateOffer.organization_id == user.organization_id,
            CandidateOffer.candidate_id == candidate.id,
        )
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid candidate offers cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid candidate offers cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid candidate offers cursor")
        stmt = stmt.where(
            or_(
                CandidateOffer.created_at < cursor_time,
                and_(CandidateOffer.created_at == cursor_time, CandidateOffer.id < cursor_id),
            )
        )
    offers = list(
        (
            await db.execute(
                stmt.order_by(CandidateOffer.created_at.desc(), CandidateOffer.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    compensation_visible = await _can_view_compensation(
        db, user, permission="offers.compensation"
    )
    has_more = len(offers) > limit
    offers = offers[:limit]
    return CursorPage(
        items=[
            _offer_out(offer, compensation_visible=compensation_visible)
            for offer in offers
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": offers[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(offers[-1].id),
                },
            )
            if has_more and offers
            else None
        ),
    )


@router.post(
    "/candidates/{candidate_id}/offers",
    response_model=CandidateOfferOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_candidate_offer(
    candidate_id: UUID,
    body: CandidateOfferCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferOut:
    await _require_offer_management(db, user)
    if body.salary_amount is not None:
        await _require_compensation(db, user, permission="offers.compensation")
    expires_at = _bounded_offer_expiration(body.expires_at, now=datetime.now(UTC))
    candidate = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    position = (
        await db.execute(
            select(Position).where(
                Position.id == body.position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    application = await resolve_application_for_write(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=body.application_id,
        source="offer",
        actor_user_id=user.id,
        actor_kind="human",
        reason="offer_application_link",
    )
    template = None
    if body.template_id is not None:
        template = (
            await db.execute(
                select(OfferTemplate).where(
                    OfferTemplate.id == body.template_id,
                    OfferTemplate.organization_id == user.organization_id,
                    OfferTemplate.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if template is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Offer template not found")
    organization = await db.get(Organization, user.organization_id)
    salary_text = (
        f"{body.salary_amount} {body.salary_currency}" if body.salary_amount is not None else ""
    )
    variables = {
        "candidate.full_name": _candidate_name(candidate),
        "position.title": position.title,
        "organization.name": organization.name if organization else "",
        "offer.salary": salary_text,
        "offer.start_date": body.start_date.isoformat() if body.start_date else "",
    }
    subject_source = template.subject_template if template else (body.subject or "")
    body_source = template.body_template if template else (body.body or "")
    subject = _render_offer(subject_source, variables, max_length=500)
    rendered_body = _render_offer(body_source, variables, max_length=30_000)
    offer = CandidateOffer(
        organization_id=user.organization_id,
        application_id=application.id,
        candidate_id=candidate.id,
        position_id=position.id,
        template_id=template.id if template else None,
        created_by_user_id=user.id,
        position_title_snapshot=position.title,
        template_name_snapshot=template.name if template else None,
        subject_template_snapshot=subject_source,
        body_template_snapshot=body_source,
        subject=subject,
        body=rendered_body,
        salary_amount=body.salary_amount,
        salary_currency=body.salary_currency,
        start_date=body.start_date,
        expires_at=expires_at,
        required_approvals=body.required_approvals,
    )
    db.add(offer)
    await db.flush()
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        application_id=application.id,
        position_id=position.id,
        event_type="offer_created",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={"position_title": position.title, "status": offer.status},
    )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return await _offer_response(db, refreshed, user)


@router.patch("/offers/{offer_id}", response_model=CandidateOfferOut)
async def update_candidate_offer(
    offer_id: UUID,
    body: CandidateOfferUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferOut:
    await _require_offer_management(db, user)
    offer = await _offer(db, offer_id, user, lock=True)
    if offer.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only draft offers can be edited")
    candidate = await resolve_canonical_candidate(
        db, offer.candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")

    values = body.model_dump(exclude_unset=True)
    if "salary_amount" in values or (
        "salary_currency" in values and offer.salary_amount is not None
    ):
        await _require_compensation(db, user, permission="offers.compensation")
    expires_at = _bounded_offer_expiration(
        values.get("expires_at", offer.expires_at),
        now=datetime.now(UTC),
    )
    salary_amount = values.get("salary_amount", offer.salary_amount)
    salary_currency = values.get("salary_currency", offer.salary_currency)
    start_date = values.get("start_date", offer.start_date)
    subject_source = values.get("subject", offer.subject_template_snapshot)
    body_source = values.get("body", offer.body_template_snapshot)
    organization = await db.get(Organization, user.organization_id)
    variables = {
        "candidate.full_name": _candidate_name(candidate),
        "position.title": offer.position_title_snapshot,
        "organization.name": organization.name if organization else "",
        "offer.salary": (
            f"{salary_amount} {salary_currency}"
            if salary_amount is not None
            else ""
        ),
        "offer.start_date": start_date.isoformat() if start_date else "",
    }
    rendered_subject = _render_offer(subject_source, variables, max_length=500)
    rendered_body = _render_offer(body_source, variables, max_length=30_000)
    next_values = {
        "subject_template_snapshot": subject_source,
        "body_template_snapshot": body_source,
        "subject": rendered_subject,
        "body": rendered_body,
        "salary_amount": salary_amount,
        "salary_currency": salary_currency,
        "start_date": start_date,
        "expires_at": expires_at,
        "required_approvals": values.get(
            "required_approvals", offer.required_approvals
        ),
    }
    changed = any(getattr(offer, field) != value for field, value in next_values.items())
    if not changed:
        await db.commit()
        return await _offer_response(db, offer, user)
    for field, value in next_values.items():
        setattr(offer, field, value)
    offer.version += 1
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=offer.candidate_id,
        actor_user_id=user.id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type="offer_updated",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={
            "changed_fields": sorted(values),
            "position_title": offer.position_title_snapshot,
        },
    )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return await _offer_response(db, refreshed, user)


@router.post("/offers/{offer_id}/submit", response_model=CandidateOfferOut)
async def submit_offer(
    offer_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferOut:
    await _require_offer_management(db, user)
    offer = await _offer(db, offer_id, user, lock=True)
    if offer.status == "pending":
        await db.commit()
        return await _offer_response(db, offer, user)
    if offer.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer cannot be submitted")
    offer.status = "pending"
    offer.approval_cycle += 1
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=offer.candidate_id,
        actor_user_id=user.id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type="offer_status_changed",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={"from_status": "draft", "to_status": "pending"},
    )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return await _offer_response(db, refreshed, user)


@router.post("/offers/{offer_id}/vote", response_model=CandidateOfferOut)
async def vote_offer(
    offer_id: UUID,
    body: ApprovalVoteRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferOut:
    await _require_offer_management(db, user)
    offer = await _offer(db, offer_id, user, lock=True)
    if offer.created_by_user_id == user.id and await approval_separation_required(
        db,
        organization_id=user.organization_id,
        category="offers",
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The offer author cannot approve their own offer",
        )
    approval = next(
        (
            item
            for item in offer.approvals
            if item.user_id == user.id
            and item.approval_cycle == offer.approval_cycle
        ),
        None,
    )
    clean_comment = _clean_comment(body.comment)
    if offer.status != "pending":
        expected_status = "approved" if body.vote == "approve" else "draft"
        if (
            offer.status == expected_status
            and approval is not None
            and approval.vote == body.vote
            and approval.comment == clean_comment
        ):
            await db.commit()
            return await _offer_response(db, offer, user)
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer is not pending")
    if approval is None:
        approval = OfferApproval(
            organization_id=user.organization_id,
            offer_id=offer.id,
            user_id=user.id,
            user_email_snapshot=user.email,
            offer_version_snapshot=offer.version,
            approval_cycle=offer.approval_cycle,
            offer_snapshot=_offer_snapshot(offer),
            vote=body.vote,
            comment=clean_comment,
        )
        db.add(approval)
        offer.approvals.append(approval)
    elif approval.vote == body.vote and approval.comment == clean_comment:
        await db.commit()
        return await _offer_response(db, offer, user)
    else:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A recorded approval vote is immutable",
        )
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=offer.candidate_id,
        actor_user_id=user.id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type="offer_approval_voted",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={"vote": body.vote},
    )
    previous = offer.status
    votes = [
        item.vote
        for item in offer.approvals
        if item.approval_cycle == offer.approval_cycle
    ]
    if "reject" in votes:
        offer.status = "draft"
    elif votes.count("approve") >= offer.required_approvals:
        offer.status = "approved"
    if offer.status != previous:
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=offer.candidate_id,
            actor_user_id=user.id,
            application_id=offer.application_id,
            position_id=offer.position_id,
            event_type="offer_status_changed",
            subject_type="candidate_offer",
            subject_id=offer.id,
            payload={"from_status": previous, "to_status": offer.status},
        )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return await _offer_response(db, refreshed, user)


@router.post("/offers/{offer_id}/send", response_model=CandidateOfferSendOut)
async def send_offer(
    offer_id: UUID,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferSendOut:
    if response is not None:
        response.headers.update(_PUBLIC_OFFER_HEADERS)
    await _require_offer_management(db, user)
    offer = await _offer(db, offer_id, user, lock=True)
    if offer.status != "approved":
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer must be approved before sending")
    now = datetime.now(UTC)
    if offer.expires_at is None:
        offer.expires_at = now + _DEFAULT_OFFER_LIFETIME
    elif offer.expires_at > now + _MAX_OFFER_LIFETIME:
        offer.expires_at = now + _MAX_OFFER_LIFETIME
    if offer.expires_at <= now:
        offer.status = "expired"
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=offer.candidate_id,
            actor_user_id=user.id,
            application_id=offer.application_id,
            position_id=offer.position_id,
            event_type="offer_status_changed",
            subject_type="candidate_offer",
            subject_id=offer.id,
            payload={"from_status": "approved", "to_status": "expired"},
        )
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer has expired")
    token = secrets.token_urlsafe(32)
    offer.token_digest = hashlib.sha256(token.encode()).hexdigest()
    offer.status = "sent"
    offer.sent_at = now
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=offer.candidate_id,
        actor_user_id=user.id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type="offer_sent",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={"position_title": offer.position_title_snapshot, "status": "sent"},
    )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return CandidateOfferSendOut(
        offer=await _offer_response(db, refreshed, user),
        token=token,
        url=f"{settings.web_origin.rstrip('/')}/offer/{token}",
    )


@router.post("/offers/{offer_id}/rotate", response_model=CandidateOfferSendOut)
async def rotate_offer_link(
    offer_id: UUID,
    body: CandidateOfferRotateRequest,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferSendOut:
    """Atomically replace a sent or expired offer capability token."""

    if response is not None:
        response.headers.update(_PUBLIC_OFFER_HEADERS)
    await _require_offer_management(db, user)
    offer = await _offer(db, offer_id, user, lock=True)
    if offer.status not in {"sent", "expired"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a sent or expired offer link can be rotated",
        )
    now = datetime.now(UTC)
    previous_status = offer.status
    token = secrets.token_urlsafe(32)
    offer.token_digest = hashlib.sha256(token.encode()).hexdigest()
    offer.status = "sent"
    offer.sent_at = now
    offer.expires_at = now + timedelta(days=body.expires_in_days)
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=offer.candidate_id,
        actor_user_id=user.id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type="offer_link_rotated",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={
            "from_status": previous_status,
            "to_status": "sent",
            "expires_at": offer.expires_at.isoformat(),
        },
    )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return CandidateOfferSendOut(
        offer=await _offer_response(db, refreshed, user),
        token=token,
        url=f"{settings.web_origin.rstrip('/')}/offer/{token}",
    )


@router.post("/offers/{offer_id}/withdraw", response_model=CandidateOfferOut)
async def withdraw_offer(
    offer_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateOfferOut:
    await _require_offer_management(db, user)
    offer = await _offer(db, offer_id, user, lock=True)
    if offer.status == "withdrawn":
        await db.commit()
        return await _offer_response(db, offer, user)
    if offer.status in {"accepted", "declined", "expired"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "Offer is already final")
    previous = offer.status
    offer.status = "withdrawn"
    offer.token_digest = None
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=offer.candidate_id,
        actor_user_id=user.id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type="offer_status_changed",
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={"from_status": previous, "to_status": "withdrawn"},
    )
    await db.commit()
    refreshed = await _offer(db, offer.id, user)
    return await _offer_response(db, refreshed, user)


async def _public_offer(db: AsyncSession, token: str, *, lock: bool = False) -> CandidateOffer:
    if len(token) < 32 or len(token) > 128:
        raise _public_offer_error(status.HTTP_404_NOT_FOUND, "Offer not found")
    digest = hashlib.sha256(token.encode()).hexdigest()
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_capability(db, kind="offer", digest=digest)
        if bootstrap is None:
            raise _public_offer_error(status.HTTP_404_NOT_FOUND, "Offer not found")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    stmt = select(CandidateOffer).where(CandidateOffer.token_digest == digest)
    if bootstrap is not None:
        stmt = stmt.where(
            CandidateOffer.id == bootstrap.capability_id,
            CandidateOffer.organization_id == bootstrap.organization_id,
        )
    if lock:
        stmt = stmt.with_for_update()
    offer = (await db.execute(stmt)).scalar_one_or_none()
    if offer is None or offer.status not in {"sent", "accepted", "declined", "expired"}:
        raise _public_offer_error(status.HTTP_404_NOT_FOUND, "Offer not found")
    if offer.status != "sent":
        raise _public_offer_error(
            status.HTTP_410_GONE, "Offer is no longer available"
        )
    if offer.expires_at is None or offer.expires_at <= datetime.now(UTC):
        offer.status = "expired"
        await db.commit()
        raise _public_offer_error(
            status.HTTP_410_GONE, "Offer is no longer available"
        )
    return offer


@router.get("/public/offers/{token}", response_model=PublicOfferOut)
async def get_public_offer(
    token: str,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicOfferOut:
    if response is not None:
        response.headers.update(_PUBLIC_OFFER_HEADERS)
    offer = await _public_offer(db, token)
    organization = await db.get(Organization, offer.organization_id)
    return _public_offer_out(offer, organization)


@router.post("/public/offers/{token}/respond", response_model=PublicOfferOut)
async def respond_public_offer(
    token: str,
    body: PublicOfferRespondRequest,
    request: Request,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicOfferOut:
    if response is not None:
        response.headers.update(_PUBLIC_OFFER_HEADERS)
    offer = await _public_offer(db, token, lock=True)
    signature_name = body.signature_name
    now = datetime.now(UTC)
    offer.status = "accepted" if body.response == "accept" else "declined"
    offer.responded_at = now
    offer.signature_name = signature_name
    if body.response == "accept":
        remote = request.client.host if request.client else "unknown"
        offer.signature_ip_hash = hashlib.sha256(
            f"{settings.jwt_secret}:{remote}".encode()
        ).hexdigest()
    append_candidate_activity(
        db,
        organization_id=offer.organization_id,
        candidate_id=offer.candidate_id,
        application_id=offer.application_id,
        position_id=offer.position_id,
        event_type=("offer_accepted" if body.response == "accept" else "offer_declined"),
        subject_type="candidate_offer",
        subject_id=offer.id,
        payload={"position_title": offer.position_title_snapshot, "status": offer.status},
        occurred_at=now,
    )
    if body.response == "accept":
        handoff_application_id = offer.application_id
        if offer.position_id is not None:
            application = await resolve_linked_application(
                db,
                organization_id=offer.organization_id,
                application_id=offer.application_id,
                candidate_id=offer.candidate_id,
                position_id=offer.position_id,
                lock=True,
            )
            handoff_application_id = application.id
            if offer.application_id is None:
                offer.application_id = application.id
            await apply_application_stage_transition(
                db,
                organization_id=offer.organization_id,
                application_id=application.id,
                body=ApplicationStageTransition(
                    stage="hired",
                    expected_version=application.updated_at,
                    reason="offer_accepted",
                    details={"offer_id": str(offer.id)},
                ),
                actor_user_id=None,
                actor_kind="public",
                occurred_at=now,
            )
        handoff = CandidateHandoff(
            organization_id=offer.organization_id,
            application_id=handoff_application_id,
            offer_id=offer.id,
            candidate_id=offer.candidate_id,
            position_id=offer.position_id,
            position_title_snapshot=offer.position_title_snapshot,
            status="pending",
            checklist={item: False for item in sorted(_HANDOFF_CHECKLIST_ITEMS)},
        )
        db.add(handoff)
        recipients = list(
            (
                await db.execute(
                    select(User.id).where(
                        User.organization_id == offer.organization_id,
                        User.role.in_(("admin", "hiring_manager")),
                    )
                )
            ).scalars()
        )
        for user_id in recipients:
            db.add(
                Notification(
                    organization_id=offer.organization_id,
                    user_id=user_id,
                    type="offer_accepted",
                    payload={
                        "candidate_id": str(offer.candidate_id),
                        "position_id": (
                            str(offer.position_id)
                            if offer.position_id is not None
                            else None
                        ),
                        "offer_id": str(offer.id),
                        "position_title": offer.position_title_snapshot,
                    },
                )
            )
    await db.commit()
    organization = await db.get(Organization, offer.organization_id)
    return _public_offer_out(offer, organization)


@router.get(
    "/candidates/{candidate_id}/handoffs",
    response_model=CursorPage[CandidateHandoffOut],
)
async def list_candidate_handoffs(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[
        str | None, Query(min_length=32, max_length=2_048)
    ] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CandidateHandoffOut]:
    await _require_offer_read(db, user)
    candidate = await resolve_canonical_candidate(db, candidate_id, user.organization_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    namespace = "handoffs.candidate"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(CandidateHandoff).where(
        CandidateHandoff.organization_id == user.organization_id,
        CandidateHandoff.candidate_id == candidate.id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid candidate handoffs cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid candidate handoffs cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid candidate handoffs cursor")
        stmt = stmt.where(
            or_(
                CandidateHandoff.created_at < cursor_time,
                and_(CandidateHandoff.created_at == cursor_time, CandidateHandoff.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(CandidateHandoff.created_at.desc(), CandidateHandoff.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[_handoff_out(item) for item in rows],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": rows[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(rows[-1].id),
                },
            )
            if has_more and rows
            else None
        ),
    )


async def _handoff(
    db: AsyncSession, handoff_id: UUID, user: User, *, lock: bool = False
) -> CandidateHandoff:
    stmt = select(CandidateHandoff).where(
        CandidateHandoff.id == handoff_id,
        CandidateHandoff.organization_id == user.organization_id,
    ).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    handoff = (await db.execute(stmt)).scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate handoff not found")
    return handoff


@router.patch("/handoffs/{handoff_id}", response_model=CandidateHandoffOut)
async def update_candidate_handoff(
    handoff_id: UUID,
    body: CandidateHandoffUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateHandoffOut:
    await _require_offer_management(db, user)
    handoff = await _handoff(db, handoff_id, user, lock=True)
    checklist = {
        item: bool((handoff.checklist or {}).get(item, False))
        for item in sorted(_HANDOFF_CHECKLIST_ITEMS)
    }
    changed_items: list[str] = []
    for item, complete in body.checklist.items():
        if checklist[item] != complete:
            checklist[item] = complete
            changed_items.append(item)
    if not changed_items:
        await db.commit()
        return _handoff_out(handoff)

    previous_status = handoff.status
    handoff.checklist = checklist
    if all(checklist.values()):
        handoff.status = "completed"
        handoff.completed_at = datetime.now(UTC)
    elif any(checklist.values()):
        handoff.status = "in_progress"
        handoff.completed_at = None
    else:
        handoff.status = "pending"
        handoff.completed_at = None
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=handoff.candidate_id,
        actor_user_id=user.id,
        application_id=handoff.application_id,
        position_id=handoff.position_id,
        event_type="handoff_updated",
        subject_type="candidate_handoff",
        subject_id=handoff.id,
        payload={
            "changed_items": sorted(changed_items),
            "from_status": previous_status,
            "to_status": handoff.status,
            "position_title": handoff.position_title_snapshot,
        },
    )
    await db.commit()
    refreshed = await _handoff(db, handoff.id, user)
    return _handoff_out(refreshed)
