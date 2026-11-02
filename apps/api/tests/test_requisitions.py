"""PostgreSQL integration coverage for requisitions, offers, and handoff."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from starlette.requests import Request

from app.db import SessionLocal
from app.models.application import Application, ApplicationStageEvent
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.enterprise import PermissionOverride
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import (
    CandidateHandoff,
    CandidateOffer,
    OfferTemplate,
    Requisition,
    RequisitionApproval,
)
from app.models.score import Score
from app.models.user import User
from app.routers.activities import EVENT_CATEGORIES
from app.routers.requisitions import (
    cancel_requisition,
    create_candidate_offer,
    create_offer_template,
    create_requisition,
    get_public_offer,
    list_candidate_handoffs,
    list_candidate_offers,
    list_requisitions,
    open_requisition,
    respond_public_offer,
    rotate_offer_link,
    send_offer,
    submit_offer,
    submit_requisition,
    update_candidate_handoff,
    update_candidate_offer,
    update_offer_template,
    update_requisition,
    vote_offer,
    vote_requisition,
)
from app.schemas.requisitions import (
    ApprovalVoteRequest,
    CandidateHandoffUpdate,
    CandidateOfferCreate,
    CandidateOfferRotateRequest,
    CandidateOfferUpdate,
    OfferTemplateCreate,
    OfferTemplateUpdate,
    PublicOfferRespondRequest,
    RequisitionCreate,
    RequisitionOpenRequest,
    RequisitionUpdate,
)
from app.services.candidate_merge import merge_candidates
from app.services.enterprise import get_or_create_policy

pytestmark = pytest.mark.asyncio


def public_request(ip: str = "203.0.113.8") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/public/offers/respond",
            "raw_path": b"/public/offers/respond",
            "query_string": b"",
            "headers": [],
            "client": (ip, 443),
            "server": ("testserver", 443),
        }
    )


async def seed_pipeline(db, suffix: str):
    organization = Organization(
        name=f"Offers org {suffix}",
        slug=f"offers-{suffix}-{uuid4().hex[:8]}",
    )
    other_organization = Organization(
        name=f"Other offers org {suffix}",
        slug=f"other-offers-{suffix}-{uuid4().hex[:8]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"admin-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    manager = User(
        organization_id=organization.id,
        email=f"manager-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    recruiter = User(
        organization_id=organization.id,
        email=f"recruiter-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    outsider = User(
        organization_id=other_organization.id,
        email=f"outsider-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{suffix}",
        raw_cv="A sufficiently detailed test candidate profile. " * 3,
        parsed_cv={"full_name": "Amina Rahmonova"},
    )
    position = Position(
        organization_id=organization.id,
        title=f"Platform Engineer {suffix}",
        status="open",
        created_by=admin.id,
    )
    db.add_all([admin, manager, recruiter, outsider, candidate, position])
    await db.flush()
    score = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=88,
        stage="offer",
    )
    db.add(score)
    await db.commit()
    return {
        "organization": organization,
        "other_organization": other_organization,
        "admin": admin,
        "manager": manager,
        "recruiter": recruiter,
        "outsider": outsider,
        "candidate": candidate,
        "position": position,
        "score": score,
    }


async def make_approved_offer(db, seeded: dict, *, required_approvals: int = 1):
    template = await create_offer_template(
        OfferTemplateCreate(
            name=f"Standard offer {uuid4().hex[:6]}",
            subject_template="Offer for {{ position.title }}",
            body_template=(
                "Hello {{candidate.full_name}}. Salary: {{offer.salary}}. "
                "Start: {{offer.start_date}}."
            ),
        ),
        db=db,
        user=seeded["admin"],
    )
    offer = await create_candidate_offer(
        seeded["candidate"].id,
        CandidateOfferCreate(
            position_id=seeded["position"].id,
            template_id=template.id,
            salary_amount=Decimal("15000.00"),
            salary_currency="TJS",
            expires_at=datetime.now(UTC) + timedelta(days=7),
            required_approvals=required_approvals,
        ),
        db=db,
        user=seeded["admin"],
    )
    await submit_offer(offer.id, db=db, user=seeded["admin"])
    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="hiring_manager",
            permission="offers.compensation",
            allowed=True,
        )
    )
    await db.commit()
    approved = await vote_offer(
        offer.id,
        ApprovalVoteRequest(vote="approve", comment="Approved"),
        db=db,
        user=seeded["manager"],
    )
    assert approved.status == ("approved" if required_approvals == 1 else "pending")
    return template, approved


async def activity_count(db, candidate_id: UUID, *event_types: str) -> int:
    stmt = select(func.count(CandidateActivity.id)).where(
        CandidateActivity.candidate_id == candidate_id
    )
    if event_types:
        stmt = stmt.where(CandidateActivity.event_type.in_(event_types))
    return int((await db.execute(stmt)).scalar_one())


async def test_requisition_approval_confidentiality_opening_and_tenant_isolation(db):
    seeded = await seed_pipeline(db, "requisition")
    admin = seeded["admin"]
    manager = seeded["manager"]
    recruiter = seeded["recruiter"]
    outsider = seeded["outsider"]
    organization_id = seeded["organization"].id
    admin_id = admin.id
    manager_id = manager.id
    outsider_id = outsider.id

    with pytest.raises(ValidationError):
        RequisitionCreate(title="   ", business_justification="Needed")
    with pytest.raises(ValidationError):
        RequisitionUpdate(title=None)
    with pytest.raises(HTTPException) as recruiter_salary:
        await create_requisition(
            RequisitionCreate(
                title="Private role",
                business_justification="Growth",
                salary_min=Decimal("10000"),
            ),
            db=db,
            user=recruiter,
        )
    assert recruiter_salary.value.status_code == 403

    requisition = await create_requisition(
        RequisitionCreate(
            title="  Infrastructure Engineer  ",
            department="  Engineering  ",
            business_justification="  Approved annual headcount plan  ",
            headcount=2,
            hiring_manager_user_id=manager.id,
            salary_min=Decimal("12000"),
            salary_max=Decimal("18000"),
            salary_currency="TJS",
            salary_confidential=True,
            required_approvals=1,
        ),
        db=db,
        user=admin,
    )
    assert requisition.title == "Infrastructure Engineer"
    assert requisition.department == "Engineering"
    assert requisition.version == 1
    assert requisition.approval_cycle == 0
    requisition_id = requisition.id

    recruiter_requisitions = await list_requisitions(db=db, user=recruiter)
    recruiter_view = next(
        item for item in recruiter_requisitions.items if item.id == requisition_id
    )
    # Canonical pilot policy grants recruiters requisition-compensation read
    # access by default, while separation of duties still withholds
    # requisition management.  Explicit tenant overrides are covered below.
    assert recruiter_view.compensation_visible is True
    assert recruiter_view.salary_min == Decimal("12000.00")
    assert recruiter_view.salary_max == Decimal("18000.00")
    assert recruiter_view.salary_currency == "TJS"
    with pytest.raises(HTTPException) as unauthorized_update:
        await update_requisition(
            requisition_id,
            RequisitionUpdate(salary_max=Decimal("19000")),
            db=db,
            user=recruiter,
        )
    assert unauthorized_update.value.status_code == 403
    await db.rollback()

    admin = await db.get(User, admin_id)
    manager = await db.get(User, manager_id)
    outsider = await db.get(User, outsider_id)
    assert admin is not None and manager is not None and outsider is not None
    submitted = await submit_requisition(requisition_id, db=db, user=admin)
    assert submitted.status == "pending"
    submitted_retry = await submit_requisition(requisition_id, db=db, user=admin)
    assert submitted_retry.status == "pending"

    with pytest.raises(HTTPException) as self_approval:
        await vote_requisition(
            requisition_id,
            ApprovalVoteRequest(vote="approve", comment="Self approval"),
            db=db,
            user=admin,
        )
    assert self_approval.value.status_code == 403

    approved = await vote_requisition(
        requisition_id,
        ApprovalVoteRequest(vote="approve", comment="  Budget confirmed  "),
        db=db,
        user=manager,
    )
    assert approved.status == "approved"
    assert approved.approvals[0].comment == "Budget confirmed"
    exact_retry = await vote_requisition(
        requisition_id,
        ApprovalVoteRequest(vote="approve", comment="Budget confirmed"),
        db=db,
        user=manager,
    )
    assert exact_retry.status == "approved"

    with pytest.raises(HTTPException) as hidden:
        await open_requisition(
            requisition_id,
            RequisitionOpenRequest(description="Public summary"),
            db=db,
            user=outsider,
        )
    assert hidden.value.status_code == 404
    await db.rollback()

    admin = await db.get(User, admin_id)
    manager = await db.get(User, manager_id)
    assert admin is not None and manager is not None
    opened = await open_requisition(
        requisition_id,
        RequisitionOpenRequest(
            description="Public summary",
            requirements={"skills": ["PostgreSQL"]},
            public_apply_enabled=True,
        ),
        db=db,
        user=admin,
    )
    assert opened.status == "opened"
    assert opened.position_id is not None
    opened_retry = await open_requisition(
        requisition_id,
        RequisitionOpenRequest(description="Ignored retry content"),
        db=db,
        user=admin,
    )
    assert opened_retry.position_id == opened.position_id
    position_count = int(
        (
            await db.execute(
                select(func.count(Position.id)).where(
                    Position.organization_id == organization_id,
                    Position.title == "Infrastructure Engineer",
                )
            )
        ).scalar_one()
    )
    assert position_count == 1

    disposable = await create_requisition(
        RequisitionCreate(
            title="Temporary headcount",
            business_justification="No longer needed",
        ),
        db=db,
        user=admin,
    )
    cancelled = await cancel_requisition(disposable.id, db=db, user=admin)
    assert cancelled.status == "cancelled"
    assert (
        await cancel_requisition(disposable.id, db=db, user=admin)
    ).status == "cancelled"

    manager_email = manager.email
    await db.delete(manager)
    await db.commit()
    db.expire_all()
    approval = (
        await db.execute(
            select(RequisitionApproval).where(
                RequisitionApproval.requisition_id == requisition_id
            )
        )
    ).scalar_one()
    assert approval.user_id is None
    assert approval.user_email_snapshot == manager_email


async def test_offer_capabilities_receive_a_bounded_expiration(db):
    seeded = await seed_pipeline(db, "offer-expiration")
    template = await create_offer_template(
        OfferTemplateCreate(
            name="Bounded offer",
            subject_template="Offer for {{position.title}}",
            body_template="Hello {{candidate.full_name}}",
        ),
        db=db,
        user=seeded["admin"],
    )
    before = datetime.now(UTC)
    created = await create_candidate_offer(
        seeded["candidate"].id,
        CandidateOfferCreate(
            position_id=seeded["position"].id,
            template_id=template.id,
        ),
        db=db,
        user=seeded["admin"],
    )
    assert before + timedelta(days=13) < created.expires_at
    assert created.expires_at < before + timedelta(days=15)

    with pytest.raises(HTTPException) as unbounded:
        await create_candidate_offer(
            seeded["candidate"].id,
            CandidateOfferCreate(
                position_id=seeded["position"].id,
                template_id=template.id,
                expires_at=before + timedelta(days=31),
            ),
            db=db,
            user=seeded["admin"],
        )
    assert unbounded.value.status_code == 422


async def test_offer_cycles_allowlist_public_esignature_handoff_and_snapshots(db):
    seeded = await seed_pipeline(db, "offer-lifecycle")
    admin = seeded["admin"]
    manager = seeded["manager"]
    recruiter = seeded["recruiter"]
    outsider = seeded["outsider"]
    candidate_id = seeded["candidate"].id
    position_id = seeded["position"].id
    organization_id = seeded["organization"].id
    position_title = seeded["position"].title
    admin_id = admin.id
    manager_id = manager.id
    recruiter_id = recruiter.id
    outsider_id = outsider.id

    with pytest.raises(ValidationError):
        OfferTemplateCreate(name=" ", subject_template="Subject", body_template="Body")
    with pytest.raises(HTTPException) as unknown_variable:
        await create_offer_template(
            OfferTemplateCreate(
                name="Unknown variable",
                subject_template="{{candidate.email}}",
                body_template="Body",
            ),
            db=db,
            user=admin,
        )
    assert unknown_variable.value.status_code == 422
    with pytest.raises(HTTPException) as malformed_variable:
        await create_offer_template(
            OfferTemplateCreate(
                name="Malformed variable",
                subject_template="{{candidate.full_name}",
                body_template="Body",
            ),
            db=db,
            user=admin,
        )
    assert malformed_variable.value.status_code == 422

    template = await create_offer_template(
        OfferTemplateCreate(
            name="Executive offer",
            subject_template="Offer: {{POSITION.TITLE}}",
            body_template="Hello {{candidate.full_name}} — {{offer.salary}}",
        ),
        db=db,
        user=admin,
    )
    with pytest.raises(HTTPException) as duplicate_template:
        await create_offer_template(
            OfferTemplateCreate(
                name="  executive OFFER  ",
                subject_template="Subject",
                body_template="Body",
            ),
            db=db,
            user=admin,
        )
    assert duplicate_template.value.status_code == 409
    admin = await db.get(User, admin_id)
    manager = await db.get(User, manager_id)
    recruiter = await db.get(User, recruiter_id)
    outsider = await db.get(User, outsider_id)
    assert admin is not None and manager is not None
    assert recruiter is not None and outsider is not None
    updated_template = await update_offer_template(
        template.id,
        OfferTemplateUpdate(body_template="Hello {{candidate.full_name}}: {{offer.salary}}"),
        db=db,
        user=admin,
    )
    assert updated_template.body_template.endswith("{{offer.salary}}")

    redacted_offers = await list_candidate_offers(candidate_id, db=db, user=recruiter)
    assert redacted_offers.items == []
    with pytest.raises(HTTPException) as hidden_candidate:
        await list_candidate_offers(candidate_id, db=db, user=outsider)
    assert hidden_candidate.value.status_code == 404

    offer = await create_candidate_offer(
        candidate_id,
        CandidateOfferCreate(
            position_id=position_id,
            template_id=template.id,
            salary_amount=Decimal("17000"),
            salary_currency="TJS",
            expires_at=datetime.now(UTC) + timedelta(days=5),
            required_approvals=2,
        ),
        db=db,
        user=admin,
    )
    assert offer.subject == f"Offer: {position_title}"
    assert "17000 TJS" in (offer.body or "")
    assert offer.application_id is not None
    offer_id = offer.id

    policy = await get_or_create_policy(db, organization_id)
    policy.field_visibility = {
        **(policy.field_visibility or {}),
        "compensation": ["admin", "hiring_manager", "recruiter"],
    }
    db.add_all(
        [
            PermissionOverride(
                organization_id=organization_id,
                role="recruiter",
                permission="requisitions.compensation",
                allowed=True,
            ),
            PermissionOverride(
                organization_id=organization_id,
                role="hiring_manager",
                permission="offers.compensation",
                allowed=True,
            ),
        ]
    )
    await db.commit()
    recruiter = await db.get(User, recruiter_id)
    assert recruiter is not None
    recruiter_offers = await list_candidate_offers(candidate_id, db=db, user=recruiter)
    assert recruiter_offers.items[0].salary_amount == Decimal("17000.00")
    assert "17000 TJS" in (recruiter_offers.items[0].body or "")

    await submit_offer(offer_id, db=db, user=admin)
    with pytest.raises(HTTPException) as self_approval:
        await vote_offer(
            offer_id,
            ApprovalVoteRequest(vote="approve", comment="Self approval"),
            db=db,
            user=admin,
        )
    assert self_approval.value.status_code == 403
    rejected = await vote_offer(
        offer_id,
        ApprovalVoteRequest(vote="reject", comment="Revise start date"),
        db=db,
        user=manager,
    )
    assert rejected.status == "draft"
    assert len(rejected.approvals) == 1

    revised_start = datetime.now(UTC).date() + timedelta(days=30)
    revised = await update_candidate_offer(
        offer_id,
        CandidateOfferUpdate(
            body=(
                "Welcome {{candidate.full_name}}. Package {{offer.salary}}. "
                "Start {{offer.start_date}}."
            ),
            start_date=revised_start,
        ),
        db=db,
        user=admin,
    )
    assert revised.status == "draft"
    assert revised.start_date == revised_start
    assert "{{offer.start_date}}" in (revised.body_source or "")
    assert revised_start.isoformat() in (revised.body or "")

    resubmitted = await submit_offer(offer_id, db=db, user=admin)
    assert resubmitted.status == "pending"
    assert len(resubmitted.approvals) == 1
    assert resubmitted.approvals[0].approval_cycle == 1
    assert resubmitted.approval_cycle == 2
    first_vote = await vote_offer(
        offer_id,
        ApprovalVoteRequest(vote="approve", comment="Finance approved"),
        db=db,
        user=recruiter,
    )
    assert first_vote.status == "pending"
    approved = await vote_offer(
        offer_id,
        ApprovalVoteRequest(vote="approve", comment="Hiring approved"),
        db=db,
        user=manager,
    )
    assert approved.status == "approved"
    assert len(approved.approvals) == 3
    activity_before_retry = await activity_count(db, candidate_id)
    exact_vote_retry = await vote_offer(
        offer_id,
        ApprovalVoteRequest(vote="approve", comment="Hiring approved"),
        db=db,
        user=manager,
    )
    assert exact_vote_retry.status == "approved"
    assert await activity_count(db, candidate_id) == activity_before_retry

    sent = await send_offer(offer_id, db=db, user=admin)
    assert sent.offer.status == "sent"
    rotated = await rotate_offer_link(
        offer_id,
        CandidateOfferRotateRequest(expires_in_days=7),
        db=db,
        user=admin,
    )
    assert rotated.token != sent.token
    assert rotated.offer.status == "sent"
    with pytest.raises(HTTPException) as rotated_old_link:
        await get_public_offer(sent.token, db=db)
    assert rotated_old_link.value.status_code == 404
    public = await get_public_offer(rotated.token, db=db)
    assert public.salary_amount == Decimal("17000.00")
    accepted = await respond_public_offer(
        rotated.token,
        PublicOfferRespondRequest(response="accept", signature_name="  Amina Rahmonova  "),
        public_request(),
        db=db,
    )
    assert accepted.status == "accepted"
    with pytest.raises(HTTPException) as consumed_retry:
        await respond_public_offer(
            rotated.token,
            PublicOfferRespondRequest(response="accept", signature_name="Amina Rahmonova"),
            public_request(),
            db=db,
        )
    assert consumed_retry.value.status_code == 410
    with pytest.raises(HTTPException) as consumed_read:
        await get_public_offer(rotated.token, db=db)
    assert consumed_read.value.status_code == 410
    await db.rollback()

    admin = await db.get(User, admin_id)
    assert admin is not None
    score = (
        await db.execute(
            select(Score).where(
                Score.candidate_id == candidate_id,
                Score.position_id == position_id,
            )
        )
    ).scalar_one()
    assert score.stage == "hired"
    application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == admin.organization_id,
                Application.candidate_id == candidate_id,
                Application.position_id == position_id,
            )
        )
    ).scalar_one()
    assert application.stage == "hired"
    offer_transition = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == application.id,
                ApplicationStageEvent.to_stage == "hired",
            )
        )
    ).scalar_one()
    assert offer_transition.actor_kind == "public"
    assert offer_transition.reason == "offer_accepted"
    assert offer_transition.details["offer_id"] == str(offer_id)
    handoffs = await list_candidate_handoffs(candidate_id, db=db, user=admin)
    assert len(handoffs.items) == 1
    handoff = handoffs.items[0]
    assert handoff.position_title == position_title
    assert handoff.application_id == application.id == sent.offer.application_id

    in_progress = await update_candidate_handoff(
        handoff.id,
        CandidateHandoffUpdate(checklist={"documents": True}),
        db=db,
        user=admin,
    )
    assert in_progress.status == "in_progress"
    update_events = await activity_count(db, candidate_id, "handoff_updated")
    no_op = await update_candidate_handoff(
        handoff.id,
        CandidateHandoffUpdate(checklist={"documents": True}),
        db=db,
        user=admin,
    )
    assert no_op.status == "in_progress"
    assert await activity_count(db, candidate_id, "handoff_updated") == update_events
    completed = await update_candidate_handoff(
        handoff.id,
        CandidateHandoffUpdate(
            checklist={"equipment": True, "accounts": True, "first_day": True}
        ),
        db=db,
        user=admin,
    )
    assert completed.status == "completed"
    assert completed.completed_at is not None

    assert EVENT_CATEGORIES["offer_created"] == "pipeline"
    assert EVENT_CATEGORIES["offer_updated"] == "collaboration"
    assert EVENT_CATEGORIES["offer_approval_voted"] == "collaboration"
    assert EVENT_CATEGORIES["offer_accepted"] == "pipeline"
    assert EVENT_CATEGORIES["handoff_updated"] == "collaboration"
    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate_id,
                    CandidateActivity.event_type.in_(
                        {
                            "offer_created",
                            "offer_updated",
                            "offer_approval_voted",
                            "offer_status_changed",
                            "offer_sent",
                            "offer_accepted",
                            "handoff_updated",
                        }
                    ),
                )
            )
        ).scalars()
    )
    assert activities
    assert all("17000" not in repr(item.payload) for item in activities)
    assert all("Welcome" not in repr(item.payload) for item in activities)
    assert all("body" not in item.payload for item in activities)

    template_model = await db.get(OfferTemplate, template.id)
    position_model = await db.get(Position, position_id)
    manager_model = await db.get(User, manager_id)
    assert template_model is not None and position_model is not None and manager_model is not None
    manager_email = manager_model.email
    await db.delete(template_model)
    await db.delete(position_model)
    await db.delete(manager_model)
    await db.commit()
    db.expire_all()

    preserved_offer = (
        await db.execute(
            select(CandidateOffer)
            .options(selectinload(CandidateOffer.approvals))
            .where(CandidateOffer.id == offer_id)
        )
    ).scalar_one()
    preserved_handoff = (
        await db.execute(
            select(CandidateHandoff).where(CandidateHandoff.id == handoff.id)
        )
    ).scalar_one()
    manager_approval = next(
        approval
        for approval in preserved_offer.approvals
        if approval.user_email_snapshot == manager_email
    )
    assert preserved_offer.template_id is None
    assert preserved_offer.template_name_snapshot == "Executive offer"
    assert "{{offer.start_date}}" in preserved_offer.body_template_snapshot
    assert preserved_offer.position_id is None
    assert preserved_offer.position_title_snapshot == position_title
    assert manager_approval.user_id is None
    assert preserved_handoff.position_id is None
    assert preserved_handoff.position_title_snapshot == position_title


async def test_concurrent_acceptance_is_exactly_once_and_merge_rehomes_offer_state(db):
    seeded = await seed_pipeline(db, "concurrent")
    candidate_id = seeded["candidate"].id
    admin_id = seeded["admin"].id
    _, approved = await make_approved_offer(db, seeded)
    sent = await send_offer(approved.id, db=db, user=seeded["admin"])

    async def accept_once(ip: str):
        async with SessionLocal() as session:
            return await respond_public_offer(
                sent.token,
                PublicOfferRespondRequest(
                    response="accept", signature_name="Amina Rahmonova"
                ),
                public_request(ip),
                db=session,
            )

    first, second = await asyncio.gather(
        accept_once("203.0.113.11"),
        accept_once("203.0.113.12"),
        return_exceptions=True,
    )
    outcomes = (first, second)
    assert sum(
        not isinstance(result, Exception) and result.status == "accepted"
        for result in outcomes
    ) == 1
    consumed = next(result for result in outcomes if isinstance(result, HTTPException))
    assert consumed.status_code == 410
    assert (
        await db.execute(
            select(func.count(CandidateHandoff.id)).where(
                CandidateHandoff.offer_id == approved.id
            )
        )
    ).scalar_one() == 1
    assert await activity_count(db, candidate_id, "offer_accepted") == 1
    assert await activity_count(db, candidate_id, "stage_changed") == 1

    primary = Candidate(
        organization_id=seeded["organization"].id,
        source="manual",
        source_ref=f"primary-{uuid4().hex[:8]}",
        raw_cv="Canonical candidate profile with enough content. " * 2,
    )
    db.add(primary)
    await db.commit()
    primary_id = primary.id
    admin = await db.get(User, admin_id)
    assert admin is not None
    await merge_candidates(
        db,
        organization_id=seeded["organization"].id,
        actor_user_id=admin.id,
        primary_candidate_id=primary_id,
        duplicate_candidate_ids=[candidate_id],
        score_winners={},
    )
    moved_offer = await db.get(CandidateOffer, approved.id)
    moved_handoff = (
        await db.execute(
            select(CandidateHandoff).where(CandidateHandoff.offer_id == approved.id)
        )
    ).scalar_one()
    assert moved_offer is not None and moved_offer.candidate_id == primary_id
    assert moved_handoff.candidate_id == primary_id

    with pytest.raises(HTTPException) as other_tenant_handoff:
        await update_candidate_handoff(
            moved_handoff.id,
            CandidateHandoffUpdate(checklist={"documents": True}),
            db=db,
            user=seeded["outsider"],
        )
    assert other_tenant_handoff.value.status_code == 404


async def test_concurrent_requisition_approval_is_serial_and_history_is_immutable(db):
    seeded = await seed_pipeline(db, "concurrent-requisition-approval")
    second_manager = User(
        organization_id=seeded["organization"].id,
        email=f"second-manager-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    db.add(second_manager)
    await db.commit()
    manager_ids = [seeded["manager"].id, second_manager.id]

    requisition = await create_requisition(
        RequisitionCreate(
            title="Concurrent approval role",
            business_justification="Two independent approvers are required",
            required_approvals=2,
        ),
        db=db,
        user=seeded["admin"],
    )
    await submit_requisition(requisition.id, db=db, user=seeded["admin"])

    async def approve(user_id: UUID):
        async with SessionLocal() as session:
            approver = await session.get(User, user_id)
            assert approver is not None
            return await vote_requisition(
                requisition.id,
                ApprovalVoteRequest(vote="approve", comment="Independent approval"),
                db=session,
                user=approver,
            )

    results = await asyncio.gather(*(approve(user_id) for user_id in manager_ids))
    assert {result.status for result in results} == {"pending", "approved"}

    db.expire_all()
    stored = (
        await db.execute(
            select(Requisition)
            .options(selectinload(Requisition.approvals))
            .where(Requisition.id == requisition.id)
        )
    ).scalar_one()
    assert stored.status == "approved"
    assert stored.approval_cycle == 1
    assert len(stored.approvals) == 2
    assert {approval.approval_cycle for approval in stored.approvals} == {1}

    stored.approvals[0].comment = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        await db.commit()
    await db.rollback()

    with pytest.raises(ValidationError, match="rejection reason"):
        ApprovalVoteRequest(vote="reject", comment="   ")
