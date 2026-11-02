"""Release-blocking authorization catalog, role, assignment, and tenant tests."""

import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.auth.authorization import (
    CANONICAL_PERMISSIONS,
    FIELD_DEFAULT_ROLES,
    OPERATION_CATALOG,
    ROLE_DEFAULTS,
    ROLES,
    AuthMode,
    FieldClass,
    ResourceScope,
    operation_policy,
)
from app.auth.jwt import encode_access_token
from app.db import TenantBoundaryError, set_tenant_context
from app.main import app
from app.models.application import Application, ApplicationStageEvent
from app.models.assessment import (
    AssessmentInvitation,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.enterprise import EnterprisePolicy, PermissionOverride
from app.models.interview import Interview, InterviewAssignment
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.scorecard import ScorecardAssignment
from app.models.user import User
from app.services.enterprise import effective_permissions

pytestmark = pytest.mark.asyncio


def auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _user(db, organization_id, role: str, label: str) -> User:
    user = User(
        organization_id=organization_id,
        email=f"{label}-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
    )
    db.add(user)
    await db.flush()
    return user


async def seed_assignment_graph(db):
    organization = Organization(
        name="Authorization tenant",
        slug=f"authorization-{uuid4().hex[:10]}",
    )
    other_organization = Organization(
        name="Other authorization tenant",
        slug=f"authorization-other-{uuid4().hex[:10]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    users = {
        role: await _user(db, organization.id, role, role)
        for role in (
            "admin",
            "recruiter",
            "hiring_manager",
            "interviewer",
            "restricted",
        )
    }
    peer_manager = await _user(db, organization.id, "hiring_manager", "peer-manager")
    peer_interviewer = await _user(
        db, organization.id, "interviewer", "peer-interviewer"
    )
    outsider = await _user(db, other_organization.id, "admin", "outsider")

    position = Position(
        organization_id=organization.id,
        title="Assigned position",
        created_by=users["recruiter"].id,
    )
    peer_position = Position(
        organization_id=organization.id,
        title="Peer position",
        created_by=users["recruiter"].id,
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Other tenant position",
        created_by=outsider.id,
    )
    db.add_all([position, peer_position, other_position])
    await db.flush()
    requisition = Requisition(
        organization_id=organization.id,
        created_by_user_id=users["recruiter"].id,
        hiring_manager_user_id=users["hiring_manager"].id,
        position_id=position.id,
        title=position.title,
        business_justification="Assigned headcount",
        salary_min=1000,
        salary_max=2000,
    )
    peer_requisition = Requisition(
        organization_id=organization.id,
        created_by_user_id=users["recruiter"].id,
        hiring_manager_user_id=peer_manager.id,
        position_id=peer_position.id,
        title=peer_position.title,
        business_justification="Peer headcount",
        salary_min=3000,
        salary_max=4000,
    )
    db.add_all([requisition, peer_requisition])
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Assigned candidate",
        raw_cv="private assigned CV email assigned@example.test",
        parsed_cv={"name": "Assigned Candidate", "email": "assigned@example.test"},
        pdf_path="/private/cv/assigned.pdf",
        email_normalized="assigned@example.test",
        phone_normalized="+992900000001",
    )
    peer_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Peer candidate",
        raw_cv="private peer CV",
        parsed_cv={"name": "Peer Candidate", "email": "peer@example.test"},
        email_normalized="peer@example.test",
    )
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="Other tenant candidate",
        raw_cv="other tenant private CV",
    )
    db.add_all([candidate, peer_candidate, other_candidate])
    await db.flush()
    interview = Interview(
        id=uuid4(),
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        created_by=users["recruiter"].id,
    )
    peer_interview = Interview(
        id=uuid4(),
        organization_id=organization.id,
        candidate_id=peer_candidate.id,
        position_id=peer_position.id,
        created_by=users["recruiter"].id,
    )
    assigned_application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source="manual",
    )
    peer_application = Application(
        organization_id=organization.id,
        candidate_id=peer_candidate.id,
        position_id=peer_position.id,
        source="manual",
    )
    other_application = Application(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        source="manual",
    )
    db.add_all([assigned_application, peer_application, other_application])
    await db.flush()
    interview.application_id = assigned_application.id
    peer_interview.application_id = peer_application.id
    db.add_all(
        [
            interview,
            peer_interview,
            InterviewAssignment(
                organization_id=organization.id,
                interview_id=interview.id,
                interviewer_user_id=users["interviewer"].id,
                assigned_by_user_id=users["recruiter"].id,
            ),
            InterviewAssignment(
                organization_id=organization.id,
                interview_id=peer_interview.id,
                interviewer_user_id=peer_interviewer.id,
                assigned_by_user_id=users["recruiter"].id,
            ),
            ScorecardAssignment(
                organization_id=organization.id,
                application_id=assigned_application.id,
                candidate_id=candidate.id,
                position_id=position.id,
                assigned_to_user_id=users["interviewer"].id,
                assigned_by_user_id=users["recruiter"].id,
                template_name_snapshot="Assigned scorecard",
                position_title_snapshot=position.title,
                assignee_email_snapshot=users["interviewer"].email,
            ),
            ScorecardAssignment(
                organization_id=organization.id,
                application_id=peer_application.id,
                candidate_id=peer_candidate.id,
                position_id=peer_position.id,
                assigned_to_user_id=peer_interviewer.id,
                assigned_by_user_id=users["recruiter"].id,
                template_name_snapshot="Peer scorecard",
                position_title_snapshot=peer_position.title,
                assignee_email_snapshot=peer_interviewer.email,
            ),
        ]
    )
    await db.flush()
    assessment_template = AssessmentTemplate(
        organization_id=organization.id,
        created_by_user_id=users["recruiter"].id,
        name=f"Authorization assessment {uuid4().hex[:10]}",
    )
    db.add(assessment_template)
    await db.flush()
    assessment_version = AssessmentTemplateVersion(
        organization_id=organization.id,
        template_id=assessment_template.id,
        version=1,
        title="Authorization assessment",
        passing_percentage=0,
        created_by_user_id=users["recruiter"].id,
    )
    db.add(assessment_version)
    await db.flush()
    assigned_invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=assigned_application.id,
        template_version_id=assessment_version.id,
        token_digest="a" * 64,
        token_prefix="assigned",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        invited_by_user_id=users["recruiter"].id,
    )
    peer_invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=peer_application.id,
        template_version_id=assessment_version.id,
        token_digest="b" * 64,
        token_prefix="peer",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        invited_by_user_id=users["recruiter"].id,
    )
    db.add_all([assigned_invitation, peer_invitation])
    await db.commit()
    return {
        "organization": organization,
        "other_organization": other_organization,
        "users": users,
        "peer_manager": peer_manager,
        "peer_interviewer": peer_interviewer,
        "outsider": outsider,
        "position": position,
        "peer_position": peer_position,
        "other_position": other_position,
        "candidate": candidate,
        "peer_candidate": peer_candidate,
        "other_candidate": other_candidate,
        "assigned_application": assigned_application,
        "peer_application": peer_application,
        "other_application": other_application,
        "interview": interview,
        "peer_interview": peer_interview,
        "assigned_invitation": assigned_invitation,
        "peer_invitation": peer_invitation,
    }


async def test_every_openapi_operation_has_exact_reviewed_auth_policy():
    schema = app.openapi()
    actual: dict[tuple[str, str], dict] = {
        (method.upper(), path): operation
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert set(actual) == set(OPERATION_CATALOG)
    for key, operation in actual.items():
        policy = operation_policy(*key)
        assert policy is not None
        bearer_declared = any(
            "HTTPBearer" in security for security in operation.get("security", [])
        )
        assert bearer_declared is (policy.auth_mode is AuthMode.BEARER), key
        if (
            policy.auth_mode is AuthMode.BEARER
            and not key[1].startswith("/auth/")
            and not policy.actions
        ):
            # Any authenticated identity, including a fail-closed restricted
            # role, must discover its grants without falling back to role.
            assert key == ("GET", "/enterprise/permissions/me")
            assert policy.scope is ResourceScope.SELF


async def test_generated_operation_role_allow_deny_matrix_is_complete():
    """Exercise the default action and field decision for every bearer route."""

    decisions: dict[tuple[str, str, str], bool] = {}
    for (method, path), policy in OPERATION_CATALOG.items():
        if policy.auth_mode is not AuthMode.BEARER:
            continue
        for role in ROLES:
            action_allowed = all(
                action in ROLE_DEFAULTS.get(role, frozenset())
                for action in policy.actions
            )
            field_allowed = all(
                role in FIELD_DEFAULT_ROLES[field_class]
                for field_class in policy.fields
            )
            decisions[(method, path, role)] = action_allowed and field_allowed

    bearer_operations = {
        key
        for key, policy in OPERATION_CATALOG.items()
        if policy.auth_mode is AuthMode.BEARER
    }
    assert len(decisions) == len(bearer_operations) * len(ROLES)
    assert {(method, path) for method, path, _role in decisions} == bearer_operations
    assert all(
        any(decisions[(method, path, role)] for role in ROLES)
        for method, path in bearer_operations
    )
    assert all(
        not decisions[(method, path, "restricted")]
        for method, path in bearer_operations
        if OPERATION_CATALOG[(method, path)].actions
        or OPERATION_CATALOG[(method, path)].fields
    )


async def test_communication_capabilities_has_exact_read_policy():
    policy = operation_policy("GET", "/communications/capabilities")
    assert policy is not None
    assert policy.auth_mode is AuthMode.BEARER
    assert policy.actions == ("communications.read",)
    assert not policy.fields
    assert policy.scope is ResourceScope.ORGANIZATION


async def test_rescore_capability_matches_rescore_authorization_contract():
    capability = operation_policy("GET", "/positions/{position_id}/rescore-capability")
    mutation = operation_policy("POST", "/positions/{position_id}/rescore")
    assert capability is not None and mutation is not None
    assert capability.actions == mutation.actions
    assert capability.fields == mutation.fields
    assert capability.scope is mutation.scope is ResourceScope.HIRING_ASSIGNMENT


async def test_communication_account_listing_is_safe_for_message_operators():
    policy = operation_policy("GET", "/communications/accounts")
    assert policy is not None
    assert policy.auth_mode is AuthMode.BEARER
    assert policy.actions == ("communications.read",)
    assert not policy.fields
    assert policy.scope is ResourceScope.ORGANIZATION


async def test_default_roles_and_overrides_are_authoritative_and_fail_closed(db):
    seeded = await seed_assignment_graph(db)
    organization = seeded["organization"]
    users = seeded["users"]
    admin_permissions = await effective_permissions(
        db, organization_id=organization.id, role="admin"
    )
    assert all(admin_permissions[action] for action in CANONICAL_PERMISSIONS)
    for role in ("recruiter", "hiring_manager"):
        role_permissions = await effective_permissions(
            db, organization_id=organization.id, role=role
        )
        assert role_permissions["requisitions.compensation"] is True
        assert role in FIELD_DEFAULT_ROLES[FieldClass.COMPENSATION]
    assert (
        await effective_permissions(
            db, organization_id=organization.id, role="restricted"
        )
    )["candidates.read"] is False
    unknown_permissions = await effective_permissions(
        db, organization_id=organization.id, role="unknown-role"
    )
    assert not any(unknown_permissions[action] for action in CANONICAL_PERMISSIONS)
    db.add_all(
        [
            PermissionOverride(
                organization_id=organization.id,
                role="admin",
                permission="candidates.read",
                allowed=False,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="restricted",
                permission="candidates.read",
                allowed=True,
            ),
        ]
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get("/candidates", headers=auth(users["admin"]))
        granted = await client.get("/candidates", headers=auth(users["restricted"]))
        self_permissions = await client.get(
            "/enterprise/permissions/me",
            headers=auth(users["restricted"]),
        )
    assert denied.status_code == 403
    assert granted.status_code == 200
    assert self_permissions.status_code == 200
    assert self_permissions.json()["permissions"]["candidates.read"] is True


async def test_manager_and_interviewer_lists_details_and_fields_are_assignment_scoped(
    db,
):
    seeded = await seed_assignment_graph(db)
    manager = seeded["users"]["hiring_manager"]
    interviewer = seeded["users"]["interviewer"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        positions = await client.get("/positions", headers=auth(manager))
        requisitions = await client.get("/requisitions", headers=auth(manager))
        assigned = await client.get(
            f"/candidates/{seeded['candidate'].id}", headers=auth(manager)
        )
        unassigned = await client.get(
            f"/candidates/{seeded['peer_candidate'].id}", headers=auth(manager)
        )
        cross_tenant = await client.get(
            f"/candidates/{seeded['other_candidate'].id}", headers=auth(manager)
        )
        interviewer_assigned = await client.get(
            f"/candidates/{seeded['candidate'].id}", headers=auth(interviewer)
        )
        interviewer_peer = await client.get(
            f"/candidates/{seeded['peer_candidate'].id}", headers=auth(interviewer)
        )
        manager_applications = await client.get("/applications", headers=auth(manager))
        interviewer_applications = await client.get(
            "/applications", headers=auth(interviewer)
        )
        assigned_assessment_attempts = await client.get(
            f"/assessment-invitations/{seeded['assigned_invitation'].id}/attempts",
            headers=auth(interviewer),
        )
        peer_assessment_attempts = await client.get(
            f"/assessment-invitations/{seeded['peer_invitation'].id}/attempts",
            headers=auth(interviewer),
        )
        scorecards = await client.get(
            f"/candidates/{seeded['candidate'].id}/scorecards",
            headers=auth(interviewer),
        )
        assigned_interview = await client.get(
            f"/interviews/{seeded['interview'].id}", headers=auth(interviewer)
        )
        peer_interview = await client.get(
            f"/interviews/{seeded['peer_interview'].id}", headers=auth(interviewer)
        )
    assert positions.status_code == 200
    assert [item["id"] for item in positions.json()["items"]] == [
        str(seeded["position"].id)
    ]
    assert positions.json()["next_cursor"] is None
    assert requisitions.status_code == 200
    assert len(requisitions.json()["items"]) == 1
    assert requisitions.json()["items"][0]["salary_min"] == "1000.00"
    assert requisitions.json()["next_cursor"] is None
    assert assigned.status_code == 200
    assert assigned.json()["raw_cv"] is None
    assert assigned.json()["parsed_cv"] is None
    assert assigned.json()["pdf_path"] is None
    assert unassigned.status_code == 404
    assert cross_tenant.status_code == 404
    assert interviewer_assigned.status_code == 200
    assert interviewer_assigned.json()["raw_cv"] is None
    assert interviewer_peer.status_code == 404
    assert manager_applications.status_code == 200
    assert [item["candidate_id"] for item in manager_applications.json()["items"]] == [
        str(seeded["candidate"].id)
    ]
    assert manager_applications.json()["next_cursor"] is None
    assert interviewer_applications.status_code == 200
    assert [
        item["candidate_id"] for item in interviewer_applications.json()["items"]
    ] == [str(seeded["candidate"].id)]
    assert interviewer_applications.json()["next_cursor"] is None
    assert assigned_assessment_attempts.status_code == 200
    assert assigned_assessment_attempts.json() == {
        "items": [],
        "next_cursor": None,
    }
    assert peer_assessment_attempts.status_code == 404
    assert scorecards.status_code == 200
    assert len(scorecards.json()["assignments"]) == 1
    assert scorecards.json()["assignments"][0]["is_mine"] is True
    assert assigned_interview.status_code == 200
    assert peer_interview.status_code == 404


async def test_interviewer_assignment_never_leaks_another_application_cycle(db):
    seeded = await seed_assignment_graph(db)
    assigned = seeded["assigned_application"]
    closed_at = datetime.now(UTC) - timedelta(days=30)
    historical = Application(
        organization_id=assigned.organization_id,
        candidate_id=assigned.candidate_id,
        position_id=assigned.position_id,
        source="manual",
        stage="rejected",
        cycle_number=2,
        applied_at=closed_at - timedelta(days=30),
        closed_at=closed_at,
    )
    db.add(historical)
    await db.commit()

    interviewer = seeded["users"]["interviewer"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/applications", headers=auth(interviewer))

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(assigned.id)]
    assert response.json()["next_cursor"] is None


async def test_manager_application_disposition_is_assignment_scoped_and_versioned(db):
    seeded = await seed_assignment_graph(db)
    manager = seeded["users"]["hiring_manager"]
    assigned_application = seeded["assigned_application"]
    initial_version = assigned_application.updated_at.isoformat()
    assert (
        await effective_permissions(
            db,
            organization_id=seeded["organization"].id,
            role="hiring_manager",
        )
    )["candidates.write"] is False

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        transitioned = await client.post(
            f"/applications/{assigned_application.id}/stage",
            headers=auth(manager),
            json={
                "stage": "screening",
                "expected_version": initial_version,
                "reason": "Manager completed assigned screening review",
            },
        )
        assert transitioned.status_code == 200
        transitioned_body = transitioned.json()

        stale = await client.post(
            f"/applications/{assigned_application.id}/stage",
            headers=auth(manager),
            json={
                "stage": "interview",
                "expected_version": initial_version,
                "reason": "Stale browser state",
            },
        )
        unassigned = await client.post(
            f"/applications/{seeded['peer_application'].id}/stage",
            headers=auth(manager),
            json={
                "stage": "screening",
                "expected_version": seeded["peer_application"].updated_at.isoformat(),
            },
        )
        cross_tenant = await client.post(
            f"/applications/{seeded['other_application'].id}/stage",
            headers=auth(manager),
            json={
                "stage": "screening",
                "expected_version": seeded["other_application"].updated_at.isoformat(),
            },
        )
        legacy = await client.patch(
            f"/positions/{seeded['position'].id}/candidates/{seeded['candidate'].id}/stage",
            headers=auth(manager),
            json={
                "stage": "interview",
                "expected_version": transitioned_body["updated_at"],
                "reason": "Assigned manager advanced the candidate",
            },
        )
        legacy_hire_bypass = await client.patch(
            f"/positions/{seeded['position'].id}/candidates/{seeded['candidate'].id}/stage",
            headers=auth(manager),
            json={
                "stage": "hired",
                "expected_version": legacy.json()["application_version"],
                "reason": "Must pass through an accepted offer",
            },
        )
        legacy_reject = await client.patch(
            f"/positions/{seeded['position'].id}/candidates/{seeded['candidate'].id}/stage",
            headers=auth(manager),
            json={
                "stage": "rejected",
                "expected_version": legacy.json()["application_version"],
                "disposition_code": "not_qualified",
                "reason": "Required experience was not demonstrated",
            },
        )

    assert stale.status_code == 409
    assert unassigned.status_code == 404
    assert cross_tenant.status_code == 404
    assert legacy.status_code == 200
    assert legacy.json()["stage"] == "interview"
    assert legacy.json()["application_id"] == str(assigned_application.id)
    assert legacy_hire_bypass.status_code == 409
    assert legacy_reject.status_code == 200
    assert legacy_reject.json()["stage"] == "rejected"
    rejection_event = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == assigned_application.id,
                ApplicationStageEvent.to_stage == "rejected",
            )
        )
    ).scalar_one()
    assert rejection_event.reason == "Required experience was not demonstrated"
    assert rejection_event.details["disposition_code"] == "not_qualified"

    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="hiring_manager",
            permission="applications.disposition",
            allowed=False,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.patch(
            f"/positions/{seeded['position'].id}/candidates/{seeded['candidate'].id}/stage",
            headers=auth(manager),
            json={
                "stage": "offer",
                "expected_version": legacy_reject.json()["application_version"],
                "reason": "Must be blocked by the canonical permission",
            },
        )
    assert denied.status_code == 403


async def test_assessment_read_permission_does_not_require_management_permission(db):
    seeded = await seed_assignment_graph(db)
    interviewer = seeded["users"]["interviewer"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        readable = await client.get(
            "/assessment-templates",
            headers=auth(interviewer),
        )
        denied_write = await client.post(
            "/assessment-templates",
            headers=auth(interviewer),
            json={"name": "Interviewer must not create this"},
        )

    assert readable.status_code == 200
    assert len(readable.json()["items"]) == 1
    assert readable.json()["next_cursor"] is None
    assert denied_write.status_code == 403


async def test_candidate_documents_require_action_and_field_policy_and_exports_redact_by_default(
    db,
):
    seeded = await seed_assignment_graph(db)
    organization = seeded["organization"]
    manager = seeded["users"]["hiring_manager"]
    recruiter = seeded["users"]["recruiter"]
    policy = EnterprisePolicy(
        organization_id=organization.id,
        field_visibility={
            "candidate_contacts": ["admin", "recruiter", "hiring_manager"],
            "candidate_cv": ["admin", "recruiter", "hiring_manager"],
            "exports": ["admin", "recruiter"],
        },
    )
    db.add(policy)
    db.add_all(
        [
            PermissionOverride(
                organization_id=organization.id,
                role="hiring_manager",
                permission="candidates.contact",
                allowed=True,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="hiring_manager",
                permission="candidates.documents",
                allowed=True,
            ),
        ]
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        visible = await client.get(
            f"/candidates/{seeded['candidate'].id}", headers=auth(manager)
        )
        redacted_export = await client.post(
            "/candidate-export",
            headers=auth(recruiter),
            json={"candidate_ids": [str(seeded["candidate"].id)]},
        )
        contact_export = await client.post(
            "/candidate-export",
            headers=auth(recruiter),
            json={
                "candidate_ids": [str(seeded["candidate"].id)],
                "include_contacts": True,
            },
        )
    assert visible.status_code == 200
    assert "private assigned CV" in visible.json()["raw_cv"]
    assert redacted_export.status_code == 200
    redacted_row = next(
        csv.DictReader(io.StringIO(redacted_export.text.lstrip("\ufeff")))
    )
    visible_row = next(
        csv.DictReader(io.StringIO(contact_export.text.lstrip("\ufeff")))
    )
    assert redacted_row["email"] == "" and redacted_row["phone"] == ""
    assert visible_row["email"] == "assigned@example.test"
    assert visible_row["phone"] == "'+992900000001"


async def test_tenant_context_reapplies_after_commit_and_rejects_cross_tenant_writes(
    db,
):
    seeded = await seed_assignment_graph(db)
    organization = seeded["organization"]
    await set_tenant_context(
        db,
        organization_id=organization.id,
        actor_id=seeded["users"]["recruiter"].id,
        identity_kind="user",
        role="recruiter",
    )
    visible_ids = set((await db.execute(select(Candidate.id))).scalars())
    assert seeded["candidate"].id in visible_ids
    assert seeded["other_candidate"].id not in visible_ids
    await db.commit()
    setting = (
        await db.execute(text("SELECT current_setting('app.organization_id', true)"))
    ).scalar_one()
    assert setting == str(organization.id)

    db.add(
        Candidate(
            organization_id=seeded["other_organization"].id,
            source="manual",
            source_ref="forbidden nested tenant write",
        )
    )
    with pytest.raises(TenantBoundaryError):
        await db.flush()
    await db.rollback()
