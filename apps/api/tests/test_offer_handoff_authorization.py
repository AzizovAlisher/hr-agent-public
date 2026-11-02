"""Offer and handoff action/field authorization through the real HTTP boundary."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.enterprise import PermissionOverride
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import (
    CandidateHandoff,
    CandidateOffer,
    OfferTemplate,
    Requisition,
)
from app.models.user import User

pytestmark = pytest.mark.asyncio


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed(db):
    organization = Organization(
        name="Offer authorization tenant",
        slug=f"offer-auth-{uuid4().hex[:10]}",
    )
    other_organization = Organization(
        name="Other offer authorization tenant",
        slug=f"other-offer-auth-{uuid4().hex[:10]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"offer-recruiter-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    manager = User(
        organization_id=organization.id,
        email=f"offer-manager-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    interviewer = User(
        organization_id=organization.id,
        email=f"offer-interviewer-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    outsider = User(
        organization_id=other_organization.id,
        email=f"other-offer-recruiter-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add_all([recruiter, manager, interviewer, outsider])
    await db.flush()
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="offer-auth-candidate",
        parsed_cv={"name": "Offer Candidate"},
    )
    position = Position(
        organization_id=organization.id,
        title="Offer role",
        status="open",
        created_by=recruiter.id,
    )
    wrong_position = Position(
        organization_id=organization.id,
        title="Wrong offer role",
        status="open",
        created_by=recruiter.id,
    )
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="other-offer-auth-candidate",
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Other offer role",
        status="open",
        created_by=outsider.id,
    )
    db.add_all([candidate, position, wrong_position, other_candidate, other_position])
    await db.flush()
    requisition = Requisition(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        hiring_manager_user_id=manager.id,
        position_id=position.id,
        title=position.title,
        business_justification="Approved assigned hiring plan",
        status="opened",
    )
    historical_application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="hired",
        source="manual",
        cycle_number=1,
        closed_at=datetime.now(UTC) - timedelta(days=30),
        created_by_user_id=recruiter.id,
    )
    active_application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        stage="offer",
        source="manual",
        cycle_number=2,
        created_by_user_id=recruiter.id,
    )
    other_application = Application(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        stage="offer",
        source="manual",
        created_by_user_id=outsider.id,
    )
    template = OfferTemplate(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        name="Standard offer",
        subject_template="Offer for {{position.title}}",
        body_template="Package {{offer.salary}}",
    )
    db.add_all(
        [
            requisition,
            historical_application,
            active_application,
            other_application,
            template,
        ]
    )
    await db.flush()
    draft_offer = CandidateOffer(
        organization_id=organization.id,
        application_id=active_application.id,
        candidate_id=candidate.id,
        position_id=position.id,
        created_by_user_id=recruiter.id,
        position_title_snapshot=position.title,
        subject_template_snapshot="Offer for Offer role",
        body_template_snapshot="Salary 17000 TJS",
        subject="Offer for Offer role",
        body="Salary 17000 TJS",
        salary_amount=Decimal("17000.00"),
        salary_currency="TJS",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        required_approvals=1,
        status="draft",
    )
    accepted_offer = CandidateOffer(
        organization_id=organization.id,
        application_id=historical_application.id,
        candidate_id=candidate.id,
        position_id=position.id,
        created_by_user_id=recruiter.id,
        position_title_snapshot=position.title,
        subject_template_snapshot="Accepted offer",
        body_template_snapshot="Accepted historical package",
        subject="Accepted offer",
        body="Accepted historical package",
        salary_amount=Decimal("15000.00"),
        salary_currency="TJS",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        required_approvals=1,
        status="accepted",
        responded_at=datetime.now(UTC) - timedelta(days=30),
    )
    db.add_all([draft_offer, accepted_offer])
    await db.flush()
    handoff = CandidateHandoff(
        organization_id=organization.id,
        application_id=historical_application.id,
        offer_id=accepted_offer.id,
        candidate_id=candidate.id,
        position_id=position.id,
        position_title_snapshot=position.title,
        status="pending",
        checklist={
            "documents": False,
            "equipment": False,
            "accounts": False,
            "first_day": False,
        },
    )
    db.add(handoff)
    await db.commit()
    return {
        "organization": organization,
        "recruiter": recruiter,
        "manager": manager,
        "interviewer": interviewer,
        "outsider": outsider,
        "candidate": candidate,
        "position": position,
        "wrong_position": wrong_position,
        "historical_application": historical_application,
        "active_application": active_application,
        "other_application": other_application,
        "draft_offer": draft_offer,
        "handoff": handoff,
    }


def _manual_offer(
    *,
    application_id,
    position_id,
    subject: str,
    salary_amount: str | None = None,
) -> dict:
    payload = {
        "application_id": str(application_id),
        "position_id": str(position_id),
        "subject": subject,
        "body": "Offer terms without numeric compensation",
        "expires_at": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
    }
    if salary_amount is not None:
        payload["salary_amount"] = salary_amount
    return payload


async def test_offer_and_handoff_actions_redaction_and_cycle_integrity(db):
    seeded = await _seed(db)
    recruiter = seeded["recruiter"]
    manager = seeded["manager"]
    interviewer = seeded["interviewer"]
    outsider = seeded["outsider"]
    candidate = seeded["candidate"]
    active_application = seeded["active_application"]
    draft_offer = seeded["draft_offer"]
    handoff = seeded["handoff"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        recruiter_offers = await client.get(
            f"/candidates/{candidate.id}/offers", headers=_auth(recruiter)
        )
        manager_offers = await client.get(
            f"/candidates/{candidate.id}/offers", headers=_auth(manager)
        )
        interviewer_offers = await client.get(
            f"/candidates/{candidate.id}/offers", headers=_auth(interviewer)
        )
        outsider_offers = await client.get(
            f"/candidates/{candidate.id}/offers", headers=_auth(outsider)
        )
        assert recruiter_offers.status_code == 200
        assert manager_offers.status_code == 200
        assert interviewer_offers.status_code == 403
        assert outsider_offers.status_code == 404
        for response in (recruiter_offers, manager_offers):
            offer = next(
                item for item in response.json()["items"] if item["id"] == str(draft_offer.id)
            )
            assert offer["application_id"] == str(active_application.id)
            assert offer["compensation_visible"] is False
            assert offer["salary_amount"] is None
            assert offer["salary_currency"] is None
            assert offer["subject"] is None and offer["body"] is None

        templates = await client.get("/offer-templates", headers=_auth(recruiter))
        created_template = await client.post(
            "/offer-templates",
            headers=_auth(recruiter),
            json={
                "name": "Recruiter managed template",
                "subject_template": "Offer for {{position.title}}",
                "body_template": "Welcome {{candidate.full_name}}",
            },
        )
        assert templates.status_code == 200
        assert created_template.status_code == 201

        numeric_create = await client.post(
            f"/candidates/{candidate.id}/offers",
            headers=_auth(recruiter),
            json=_manual_offer(
                application_id=active_application.id,
                position_id=seeded["position"].id,
                subject="Unauthorized numeric package",
                salary_amount="19000",
            ),
        )
        closed_cycle = await client.post(
            f"/candidates/{candidate.id}/offers",
            headers=_auth(recruiter),
            json=_manual_offer(
                application_id=seeded["historical_application"].id,
                position_id=seeded["position"].id,
                subject="Closed cycle",
            ),
        )
        mismatched_position = await client.post(
            f"/candidates/{candidate.id}/offers",
            headers=_auth(recruiter),
            json=_manual_offer(
                application_id=active_application.id,
                position_id=seeded["wrong_position"].id,
                subject="Wrong position",
            ),
        )
        cross_tenant_application = await client.post(
            f"/candidates/{candidate.id}/offers",
            headers=_auth(recruiter),
            json=_manual_offer(
                application_id=seeded["other_application"].id,
                position_id=seeded["position"].id,
                subject="Cross tenant application",
            ),
        )
        salaryless_create = await client.post(
            f"/candidates/{candidate.id}/offers",
            headers=_auth(recruiter),
            json=_manual_offer(
                application_id=active_application.id,
                position_id=seeded["position"].id,
                subject="Salaryless offer",
            ),
        )
        assert numeric_create.status_code == 403
        assert closed_cycle.status_code == 409
        assert mismatched_position.status_code == 409
        assert cross_tenant_application.status_code == 404
        assert salaryless_create.status_code == 201, salaryless_create.text
        assert salaryless_create.json()["application_id"] == str(active_application.id)
        assert salaryless_create.json()["compensation_visible"] is False

        non_comp_update = await client.patch(
            f"/offers/{draft_offer.id}",
            headers=_auth(recruiter),
            json={"start_date": date(2027, 1, 15).isoformat()},
        )
        numeric_update = await client.patch(
            f"/offers/{draft_offer.id}",
            headers=_auth(recruiter),
            json={"salary_amount": "18000"},
        )
        manager_non_comp_update = await client.patch(
            f"/offers/{draft_offer.id}",
            headers=_auth(manager),
            json={"required_approvals": 1},
        )
        outsider_update = await client.patch(
            f"/offers/{draft_offer.id}",
            headers=_auth(outsider),
            json={"required_approvals": 3},
        )
        assert non_comp_update.status_code == 200
        assert non_comp_update.json()["compensation_visible"] is False
        assert numeric_update.status_code == 403
        assert manager_non_comp_update.status_code == 200
        assert manager_non_comp_update.json()["compensation_visible"] is False
        assert outsider_update.status_code == 404

        submitted = await client.post(
            f"/offers/{draft_offer.id}/submit", headers=_auth(recruiter)
        )
        approved = await client.post(
            f"/offers/{draft_offer.id}/vote",
            headers=_auth(manager),
            json={"vote": "approve", "comment": "Approved without salary access"},
        )
        sent = await client.post(
            f"/offers/{draft_offer.id}/send", headers=_auth(manager)
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "pending"
        assert submitted.json()["compensation_visible"] is False
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["compensation_visible"] is False
        assert sent.status_code == 200
        assert sent.json()["offer"]["status"] == "sent"
        assert sent.json()["offer"]["compensation_visible"] is False
        assert sent.json()["offer"]["salary_amount"] is None

        recruiter_handoffs = await client.get(
            f"/candidates/{candidate.id}/handoffs", headers=_auth(recruiter)
        )
        manager_handoffs = await client.get(
            f"/candidates/{candidate.id}/handoffs", headers=_auth(manager)
        )
        interviewer_handoffs = await client.get(
            f"/candidates/{candidate.id}/handoffs", headers=_auth(interviewer)
        )
        outsider_handoffs = await client.get(
            f"/candidates/{candidate.id}/handoffs", headers=_auth(outsider)
        )
        assert recruiter_handoffs.status_code == 200
        assert manager_handoffs.status_code == 200
        assert interviewer_handoffs.status_code == 403
        assert outsider_handoffs.status_code == 404
        assert recruiter_handoffs.json()["items"][0]["application_id"] == str(
            seeded["historical_application"].id
        )

        manager_handoff_update = await client.patch(
            f"/handoffs/{handoff.id}",
            headers=_auth(manager),
            json={"checklist": {"documents": True}},
        )
        interviewer_handoff_update = await client.patch(
            f"/handoffs/{handoff.id}",
            headers=_auth(interviewer),
            json={"checklist": {"accounts": True}},
        )
        outsider_handoff_update = await client.patch(
            f"/handoffs/{handoff.id}",
            headers=_auth(outsider),
            json={"checklist": {"accounts": True}},
        )
        assert manager_handoff_update.status_code == 200
        assert interviewer_handoff_update.status_code == 403
        assert outsider_handoff_update.status_code == 404

    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="hiring_manager",
            permission="offers.compensation",
            allowed=True,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        visible_manager = await client.get(
            f"/candidates/{candidate.id}/offers", headers=_auth(manager)
        )
    assert visible_manager.status_code == 200
    visible_offer = next(
        item
        for item in visible_manager.json()["items"]
        if item["id"] == str(draft_offer.id)
    )
    assert visible_offer["compensation_visible"] is True
    assert visible_offer["salary_amount"] == "17000.00"
    assert visible_offer["body"] == "Salary 17000 TJS"

    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="recruiter",
            permission="offers.manage",
            allowed=False,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        read_only_offers = await client.get(
            f"/candidates/{candidate.id}/offers", headers=_auth(recruiter)
        )
        read_only_templates = await client.get(
            "/offer-templates", headers=_auth(recruiter)
        )
        denied_offer_update = await client.patch(
            f"/offers/{draft_offer.id}",
            headers=_auth(recruiter),
            json={"required_approvals": 4},
        )
        denied_template_create = await client.post(
            "/offer-templates",
            headers=_auth(recruiter),
            json={
                "name": "Denied template",
                "subject_template": "Denied",
                "body_template": "Denied",
            },
        )
        read_only_handoffs = await client.get(
            f"/candidates/{candidate.id}/handoffs", headers=_auth(recruiter)
        )
        denied_handoff_update = await client.patch(
            f"/handoffs/{handoff.id}",
            headers=_auth(recruiter),
            json={"checklist": {"equipment": True}},
        )
    assert read_only_offers.status_code == 200
    assert read_only_templates.status_code == 200
    assert denied_offer_update.status_code == 403
    assert denied_template_create.status_code == 403
    assert read_only_handoffs.status_code == 200
    assert denied_handoff_update.status_code == 403
