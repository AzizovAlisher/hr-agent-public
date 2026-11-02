"""Fail-closed tenant policy coverage for approval separation of duties."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog, EnterprisePolicy
from app.models.organization import Organization
from app.models.requisition import CandidateOffer, Requisition
from app.models.scorecard import HiringDecision
from app.models.user import User
from app.routers.requisitions import vote_offer, vote_requisition
from app.routers.scorecards import vote_hiring_decision
from app.schemas.enterprise import (
    ApprovalSeparationOfDuties,
    EnterprisePolicyUpdate,
)
from app.schemas.requisitions import ApprovalVoteRequest
from app.schemas.scorecards import HiringDecisionVoteRequest
from app.services.enterprise import normalize_approval_separation_policy


pytestmark = pytest.mark.asyncio


async def _seed_pending_approvals(db, suffix: str):
    organization = Organization(
        name=f"Approval policy {suffix}",
        slug=f"approval-policy-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    creator = User(
        organization_id=organization.id,
        email=f"approval-policy-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"Approval candidate {suffix}",
    )
    db.add_all([creator, candidate])
    await db.flush()
    requisition = Requisition(
        organization_id=organization.id,
        created_by_user_id=creator.id,
        title="Approval-controlled requisition",
        business_justification="The role has approved budget and headcount.",
        status="pending",
        approval_cycle=1,
        submitted_at=datetime.now(UTC),
    )
    offer = CandidateOffer(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by_user_id=creator.id,
        position_title_snapshot="Approval-controlled role",
        subject_template_snapshot="Candidate offer",
        body_template_snapshot="Candidate offer body",
        subject="Candidate offer",
        body="Candidate offer body",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        status="pending",
        approval_cycle=1,
    )
    decision = HiringDecision(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by_user_id=creator.id,
        position_title_snapshot="Approval-controlled role",
        created_by_email_snapshot=creator.email,
        recommendation="advance",
        summary="Proceed after structured review.",
        status="pending",
        approval_cycle=1,
    )
    db.add_all([requisition, offer, decision])
    await db.commit()
    return organization, creator, requisition, offer, decision


async def test_policy_schema_requires_exact_strict_boolean_object():
    exact = ApprovalSeparationOfDuties(
        requisitions=True,
        offers=False,
        hiring_decisions=True,
    )
    assert exact.model_dump() == {
        "requisitions": True,
        "offers": False,
        "hiring_decisions": True,
    }

    invalid_values = [
        {"requisitions": True, "offers": True},
        {
            "requisitions": True,
            "offers": True,
            "hiring_decisions": True,
            "extra": False,
        },
        {
            "requisitions": "true",
            "offers": True,
            "hiring_decisions": True,
        },
    ]
    for value in invalid_values:
        with pytest.raises(ValidationError):
            EnterprisePolicyUpdate(approval_separation_of_duties=value)


async def test_policy_normalization_is_unconditionally_fail_closed():
    expected_default = {
        "requisitions": True,
        "offers": True,
        "hiring_decisions": True,
    }
    malformed_values = [
        None,
        [],
        {"requisitions": False, "offers": False},
        {
            "requisitions": False,
            "offers": False,
            "hiring_decisions": False,
            "unexpected": False,
        },
        {
            "requisitions": False,
            "offers": 0,
            "hiring_decisions": False,
        },
    ]
    for value in malformed_values:
        assert normalize_approval_separation_policy(value) == expected_default

    explicitly_disabled = {
        "requisitions": False,
        "offers": False,
        "hiring_decisions": False,
    }
    assert (
        normalize_approval_separation_policy(explicitly_disabled)
        == explicitly_disabled
    )


async def test_database_rejects_non_exact_policy_object(db):
    organization = Organization(
        name="Invalid approval policy",
        slug=f"invalid-approval-policy-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            approval_separation_of_duties={
                "requisitions": False,
                "offers": False,
            },
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_all_approval_routes_default_deny_and_honor_explicit_opt_out(db):
    organization, creator, requisition, offer, decision = await _seed_pending_approvals(
        db, "routes"
    )
    requisition_id = requisition.id
    offer_id = offer.id
    decision_id = decision.id

    with pytest.raises(HTTPException) as requisition_denied:
        await vote_requisition(
            requisition_id,
            ApprovalVoteRequest(vote="approve"),
            db=db,
            user=creator,
        )
    assert requisition_denied.value.status_code == 403

    with pytest.raises(HTTPException) as offer_denied:
        await vote_offer(
            offer_id,
            ApprovalVoteRequest(vote="approve"),
            db=db,
            user=creator,
        )
    assert offer_denied.value.status_code == 403

    with pytest.raises(HTTPException) as decision_denied:
        await vote_hiring_decision(
            decision_id,
            HiringDecisionVoteRequest(vote="approve"),
            db=db,
            user=creator,
        )
    assert decision_denied.value.status_code == 403

    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            approval_separation_of_duties={
                "requisitions": False,
                "offers": False,
                "hiring_decisions": False,
            },
        )
    )
    await db.commit()
    creator = await db.get(User, creator.id)
    assert creator is not None

    approved_requisition = await vote_requisition(
        requisition_id,
        ApprovalVoteRequest(vote="approve"),
        db=db,
        user=creator,
    )
    assert approved_requisition.status == "approved"

    creator = await db.get(User, creator.id)
    assert creator is not None
    approved_offer = await vote_offer(
        offer_id,
        ApprovalVoteRequest(vote="approve"),
        db=db,
        user=creator,
    )
    assert approved_offer.status == "approved"

    creator = await db.get(User, creator.id)
    assert creator is not None
    approved_decision = await vote_hiring_decision(
        decision_id,
        HiringDecisionVoteRequest(vote="approve"),
        db=db,
        user=creator,
    )
    assert approved_decision.status == "approved"


async def test_policy_http_update_is_returned_and_body_free_audited(db):
    organization, admin, *_unused = await _seed_pending_approvals(db, "audit")
    token = encode_access_token(
        admin.id,
        admin.role,
        admin.locale,
        token_version=admin.token_version,
    )
    configured = {
        "requisitions": True,
        "offers": False,
        "hiring_decisions": True,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            "/enterprise/policy",
            headers={"Authorization": f"Bearer {token}"},
            json={"approval_separation_of_duties": configured},
        )

    assert response.status_code == 200
    assert response.json()["approval_separation_of_duties"] == configured
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.action == "update_policy",
            )
        )
    ).scalar_one()
    assert audit.path_template == "/enterprise/policy"
    assert audit.metadata_json == {"query_keys": []}
    assert "approval_separation_of_duties" not in str(audit.metadata_json)
