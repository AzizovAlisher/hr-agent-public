"""Completeness and regression gates for explicit assignment-scope registries."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
import app.models
from app.auth.authorization import OPERATION_CATALOG, ResourceScope
from app.auth.jwt import encode_access_token
from app.auth.scope import (
    _RESOURCE_MODELS,
    _ROUTE_BODY_PARAMETERS,
    _SPECIAL_RESOURCE_CHECKS,
    ROUTE_ASSIGNMENT_SCOPES,
    RouteValueSource,
    enforce_hiring_assignment_scope,
)
from app.db import (
    MODEL_ASSIGNMENT_SCOPES,
    AssignmentScope,
    Base,
    clear_tenant_context,
    set_tenant_context,
)
from app.main import app
from app.middleware.audit import AUDIT_RESOURCE_REGISTRY, _resource
from app.models.application import Application
from app.models.assessment import (
    AssessmentInvitation,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog, PermissionOverride
from app.models.interview import Interview, InterviewAssignment
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.models.workflow import WorkflowRule

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ASSIGNMENT_PARAMETER_NAMES = frozenset(
    {
        "application_id",
        "appointment_id",
        "assignment_id",
        "attempt_id",
        "bundle_id",
        "candidate_id",
        "decision_id",
        "file_id",
        "handoff_id",
        "interview_id",
        "invitation_id",
        "job_id",
        "link_id",
        "mission_id",
        "offer_id",
        "pool_id",
        "position_id",
        "primary_candidate_id",
        "recording_id",
        "requisition_id",
        "rule_id",
        "run_id",
        "sequence_id",
        "session_id",
        "shortlist_id",
        "source_candidate_id",
        "task_id",
        "template_id",
        "thread_id",
        "view_id",
    }
)


@pytest_asyncio.fixture(autouse=True)
async def _production_request_sessions(monkeypatch):
    """Exercise API calls through the non-bypass role in forced-RLS jobs."""

    if os.environ.get("TENANT_RLS_MODE") != "forced":
        yield
        return
    runtime_engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"role": "hr_agent_runtime"}},
    )
    sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", sessions)
    try:
        yield
    finally:
        await runtime_engine.dispose()


ASSIGNMENT_BODY_PARAMETER_NAMES = frozenset(
    {
        "application_id",
        "candidate_id",
        "candidate_ids",
        "duplicate_ids",
        "pool_id",
        "position_id",
        "position_ids",
        "provider_assessment_id",
        "source_activity_id",
    }
)


def _openapi_operations() -> dict[tuple[str, str], dict]:
    return {
        (method.upper(), path): operation
        for path, item in app.openapi()["paths"].items()
        for method, operation in item.items()
        if method.upper() in HTTP_METHODS
    }


def _request_body_property_names(operation: dict) -> set[str]:
    components = app.openapi().get("components", {}).get("schemas", {})

    def properties(schema: dict, seen: frozenset[str] = frozenset()) -> set[str]:
        reference = schema.get("$ref")
        if reference:
            if reference in seen:
                return set()
            name = reference.rsplit("/", 1)[-1]
            return properties(components[name], seen | {reference})
        names = set(schema.get("properties", {}))
        for keyword in ("allOf", "anyOf", "oneOf"):
            for child in schema.get(keyword, []):
                names.update(properties(child, seen))
        return names

    schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    return properties(schema)


def _relationship_model_names() -> set[str]:
    """Return direct and foreign-key descendants of hiring relationships."""

    mappers = tuple(Base.registry.mappers)
    table_to_name = {
        mapper.class_.__table__.name: mapper.class_.__name__ for mapper in mappers
    }
    relationship_tables = {
        mapper.class_.__table__.name
        for mapper in mappers
        if {column.name for column in mapper.columns}
        & {"application_id", "candidate_id", "position_id"}
    }
    changed = True
    while changed:
        changed = False
        for mapper in mappers:
            table = mapper.class_.__table__
            if table.name in relationship_tables:
                continue
            parent_tables = {
                foreign_key.column.table.name
                for column in table.columns
                for foreign_key in column.foreign_keys
            }
            if parent_tables & relationship_tables:
                relationship_tables.add(table.name)
                changed = True
    return {table_to_name[name] for name in relationship_tables}


def test_model_registry_is_immutable_complete_and_field_valid() -> None:
    assert isinstance(MODEL_ASSIGNMENT_SCOPES, MappingProxyType)
    with pytest.raises(TypeError):
        MODEL_ASSIGNMENT_SCOPES["Unreviewed"] = object()  # type: ignore[index]

    mapped = {mapper.class_.__name__: mapper.class_ for mapper in Base.registry.mappers}
    assert set(MODEL_ASSIGNMENT_SCOPES) <= set(mapped)
    assert _relationship_model_names() <= set(MODEL_ASSIGNMENT_SCOPES)
    assert {rule.scope for rule in MODEL_ASSIGNMENT_SCOPES.values()} == set(
        AssignmentScope
    )

    valid_parent_scopes = {
        "ai_session",
        "assessment_attempt",
        "chat_session",
        "communication_thread",
        "hiring_decision",
        "interview",
        "interview_appointment",
        "legacy_application",
        "mission",
        "mission_assessment",
        "mission_step",
        "offer",
        "provider_assessment",
        "requisition",
        "scheduling_link",
        "scorecard_assignment",
        "scorecard_template",
    }
    for name, rule in MODEL_ASSIGNMENT_SCOPES.items():
        model = mapped[name]
        for field in (
            rule.application_field,
            rule.position_field,
            rule.candidate_field,
            rule.identity_field,
            rule.owner_field,
            rule.parent_field,
        ):
            if field is not None:
                assert hasattr(model, field), (name, field)
        if rule.scope is AssignmentScope.APPLICATION_SCOPED:
            assert rule.application_field is not None, name
        if rule.scope is AssignmentScope.EXPLICIT_PARENT_SCOPED:
            assert rule.parent_scope in valid_parent_scopes, name
            if rule.parent_scope not in {
                "legacy_application",
                "mission_assessment",
            }:
                assert rule.parent_field is not None, name


def test_every_assignment_openapi_operation_and_identifier_is_registered() -> None:
    operations = _openapi_operations()
    expected = {
        key
        for key, policy in OPERATION_CATALOG.items()
        if policy.scope is ResourceScope.HIRING_ASSIGNMENT
    }
    assert set(ROUTE_ASSIGNMENT_SCOPES) == expected
    assert isinstance(ROUTE_ASSIGNMENT_SCOPES, MappingProxyType)

    known_resources = set(_RESOURCE_MODELS) | set(_SPECIAL_RESOURCE_CHECKS)
    for key, rule in ROUTE_ASSIGNMENT_SCOPES.items():
        operation = operations[key]
        registered = {(binding.source, binding.name) for binding in rule.bindings}
        for parameter in operation.get("parameters", []):
            name = parameter["name"]
            if name not in ASSIGNMENT_PARAMETER_NAMES:
                continue
            source = (
                RouteValueSource.PATH
                if parameter["in"] == "path"
                else RouteValueSource.QUERY
            )
            assert (source, name) in registered, (key, source, name)
        registered_body = {
            binding.name
            for binding in rule.bindings
            if binding.source in {RouteValueSource.BODY, RouteValueSource.BODY_LIST}
        }
        expected_body = (
            _request_body_property_names(operation) & ASSIGNMENT_BODY_PARAMETER_NAMES
        )
        assert expected_body <= registered_body, (key, expected_body - registered_body)
        assert all(binding.resource in known_resources for binding in rule.bindings)

    nested_workflow_bindings = {
        key: {
            (binding.source, binding.name, binding.resource, binding.required)
            for binding in ROUTE_ASSIGNMENT_SCOPES[key].bindings
        }
        for key in {
            ("POST", "/workflows/rules"),
            ("PATCH", "/workflows/rules/{rule_id}"),
        }
    }
    assert (
        RouteValueSource.BODY_PATH,
        "conditions.position_id",
        "position",
        True,
    ) in nested_workflow_bindings[("POST", "/workflows/rules")]
    assert (
        RouteValueSource.BODY_PATH,
        "conditions.position_id",
        "position",
        False,
    ) in nested_workflow_bindings[("PATCH", "/workflows/rules/{rule_id}")]

    detail_rule = ROUTE_ASSIGNMENT_SCOPES[("GET", "/workflows/rules/{rule_id}")]
    assert detail_rule.scope is AssignmentScope.EXPLICIT_PARENT_SCOPED
    assert "interviewer" in detail_rule.denied_roles
    assert (
        RouteValueSource.PATH,
        "rule_id",
        "workflow_rule",
        True,
    ) in {
        (binding.source, binding.name, binding.resource, binding.required)
        for binding in detail_rule.bindings
    }
    assert "workflow_rule" in _SPECIAL_RESOURCE_CHECKS
    assert "workflow_rule" not in _RESOURCE_MODELS

    exact_cycle_mutations = {
        (
            "PATCH",
            "/positions/{position_id}/candidates/{candidate_id}/stage",
        ),
        (
            "PATCH",
            "/positions/{position_id}/candidates/{candidate_id}/work",
        ),
    }
    for key in exact_cycle_mutations:
        assert any(
            binding.source is RouteValueSource.BODY
            and binding.name == "application_id"
            and not binding.required
            for binding in _ROUTE_BODY_PARAMETERS[key]
        )


def test_audit_registry_uses_exact_resource_identity() -> None:
    candidate_id = uuid4()
    position_id = uuid4()
    application_id = uuid4()
    rule_id = uuid4()
    request = SimpleNamespace(
        path_params={
            "candidate_id": str(candidate_id),
            "position_id": str(position_id),
            "application_id": str(application_id),
            "rule_id": str(rule_id),
        }
    )
    assert _resource(request, "/applications/{application_id}") == (
        "application",
        application_id,
    )
    assert _resource(
        request,
        "/positions/{position_id}/candidates/{candidate_id}/stage",
    ) == ("application_candidate", candidate_id)
    assert _resource(request, "/workflows/rules/{rule_id}") == (
        "workflow_rule",
        rule_id,
    )
    assert _resource(request, "/unregistered/{candidate_id}") == (None, None)
    assert isinstance(AUDIT_RESOURCE_REGISTRY, MappingProxyType)


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


def _scope_request(
    *,
    path_template: str,
    path: str,
    path_params: dict[str, str],
    body: dict,
) -> Request:
    payload = json.dumps(body).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PATCH",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 123),
            "server": ("test", 80),
            "root_path": "",
            "path_params": path_params,
            "route": SimpleNamespace(path=path_template),
        },
        receive,
    )


async def _user(db, organization_id: UUID, role: str, label: str) -> User:
    user = User(
        organization_id=organization_id,
        email=f"registry-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_application_cycle_allow_deny_null_override_api_key_and_audit(db) -> None:
    organization = Organization(
        name="Registry cycle tenant",
        slug=f"registry-cycle-{uuid4().hex[:8]}",
    )
    other_organization = Organization(
        name="Registry other tenant",
        slug=f"registry-other-{uuid4().hex[:8]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    recruiter = await _user(db, organization.id, "recruiter", "recruiter")
    manager = await _user(db, organization.id, "hiring_manager", "manager")
    interviewer = await _user(db, organization.id, "interviewer", "interviewer")
    outsider = await _user(db, other_organization.id, "admin", "outsider")

    position = Position(
        organization_id=organization.id,
        title="Managed exact-cycle role",
        created_by=recruiter.id,
    )
    unmanaged_position = Position(
        organization_id=organization.id,
        title="Unmanaged workflow role",
        created_by=recruiter.id,
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Other tenant position",
        created_by=outsider.id,
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="exact-cycle candidate",
    )
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="cross-tenant canary",
    )
    sole_cycle_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="sole-cycle compatibility candidate",
    )
    db.add_all(
        [
            position,
            unmanaged_position,
            other_position,
            candidate,
            other_candidate,
            sole_cycle_candidate,
        ]
    )
    await db.flush()
    db.add(
        Requisition(
            organization_id=organization.id,
            created_by_user_id=recruiter.id,
            hiring_manager_user_id=manager.id,
            position_id=position.id,
            title=position.title,
            business_justification="Registry coverage",
        )
    )
    managed_rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        name="Managed registry rule",
        trigger="candidate_added",
        conditions={"position_id": str(position.id)},
        actions=[{"type": "move_stage", "stage": "screening"}],
    )
    unmanaged_rule = WorkflowRule(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        name="Unmanaged registry rule",
        trigger="candidate_added",
        conditions={"position_id": str(unmanaged_position.id)},
        actions=[{"type": "move_stage", "stage": "screening"}],
    )
    other_rule = WorkflowRule(
        organization_id=other_organization.id,
        created_by_user_id=outsider.id,
        name="Other-tenant registry rule",
        trigger="candidate_added",
        conditions={"position_id": str(other_position.id)},
        actions=[{"type": "move_stage", "stage": "screening"}],
    )
    db.add_all([managed_rule, unmanaged_rule, other_rule])
    old_application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        cycle_number=1,
        stage="rejected",
        closed_at=datetime.now(UTC) - timedelta(days=2),
        source="manual",
    )
    assigned_application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        cycle_number=2,
        source="manual",
    )
    other_application = Application(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        source="manual",
    )
    sole_cycle_application = Application(
        organization_id=organization.id,
        candidate_id=sole_cycle_candidate.id,
        position_id=position.id,
        source="manual",
    )
    db.add_all(
        [
            old_application,
            assigned_application,
            other_application,
            sole_cycle_application,
        ]
    )
    await db.flush()
    interview = Interview(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=assigned_application.id,
        created_by=recruiter.id,
    )
    db.add(interview)
    await db.flush()
    db.add(
        InterviewAssignment(
            organization_id=organization.id,
            interview_id=interview.id,
            interviewer_user_id=interviewer.id,
            assigned_by_user_id=recruiter.id,
        )
    )

    template = AssessmentTemplate(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        name="Registry assessment",
    )
    db.add(template)
    await db.flush()
    version = AssessmentTemplateVersion(
        organization_id=organization.id,
        template_id=template.id,
        version=1,
        title="Registry assessment v1",
        passing_percentage=0,
        created_by_user_id=recruiter.id,
    )
    db.add(version)
    await db.flush()
    old_invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=old_application.id,
        template_version_id=version.id,
        token_digest=uuid4().hex + uuid4().hex,
        token_prefix="oldcycle",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        invited_by_user_id=recruiter.id,
    )
    assigned_invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=assigned_application.id,
        template_version_id=version.id,
        token_digest=uuid4().hex + uuid4().hex,
        token_prefix="assigned",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        invited_by_user_id=recruiter.id,
    )
    old_task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=old_application.id,
        created_by=recruiter.id,
        title="OLD-CYCLE-TASK-CANARY",
    )
    assigned_task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=assigned_application.id,
        created_by=recruiter.id,
        title="ASSIGNED-CYCLE-TASK-CANARY",
    )
    null_task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=None,
        application_id=None,
        created_by=recruiter.id,
        title="NULL-CYCLE-TASK-CANARY",
    )
    db.add_all(
        [
            old_invitation,
            assigned_invitation,
            old_task,
            assigned_task,
            null_task,
            PermissionOverride(
                organization_id=organization.id,
                role="interviewer",
                permission="applications.write",
                allowed=True,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="interviewer",
                permission="workflows.read",
                allowed=True,
            ),
        ]
    )
    await db.commit()
    organization_id = organization.id
    recruiter_id = recruiter.id
    assigned_application_id = assigned_application.id

    await set_tenant_context(
        db,
        organization_id=organization.id,
        actor_id=interviewer.id,
        identity_kind="user",
        role="interviewer",
    )
    assert set((await db.execute(select(Application.id))).scalars()) == {
        assigned_application.id
    }
    assert set((await db.execute(select(AssessmentInvitation.id))).scalars()) == {
        assigned_invitation.id
    }
    assert set((await db.execute(select(TaskSubmission.id))).scalars()) == set()
    clear_tenant_context(db)

    await set_tenant_context(
        db,
        organization_id=organization.id,
        actor_id=manager.id,
        identity_kind="user",
        role="hiring_manager",
    )
    assert set((await db.execute(select(Application.id))).scalars()) == {
        old_application.id,
        assigned_application.id,
        sole_cycle_application.id,
    }
    assert set((await db.execute(select(TaskSubmission.id))).scalars()) == {
        old_task.id,
        assigned_task.id,
    }

    legacy_template = "/positions/{position_id}/candidates/{candidate_id}/stage"
    await enforce_hiring_assignment_scope(
        db,
        manager,
        _scope_request(
            path_template=legacy_template,
            path=(
                f"/positions/{position.id}/candidates/{sole_cycle_candidate.id}/stage"
            ),
            path_params={
                "position_id": str(position.id),
                "candidate_id": str(sole_cycle_candidate.id),
            },
            body={"stage": "screening"},
        ),
    )
    with pytest.raises(HTTPException) as crossed:
        await enforce_hiring_assignment_scope(
            db,
            manager,
            _scope_request(
                path_template=legacy_template,
                path=(
                    f"/positions/{position.id}/candidates/"
                    f"{sole_cycle_candidate.id}/stage"
                ),
                path_params={
                    "position_id": str(position.id),
                    "candidate_id": str(sole_cycle_candidate.id),
                },
                body={
                    "stage": "screening",
                    "application_id": str(assigned_application.id),
                },
            ),
        )
    assert crossed.value.status_code == 404
    clear_tenant_context(db)

    # API keys keep explicit tenant/scopes at their own boundary and must not
    # accidentally inherit a human assignment-role filter.
    await set_tenant_context(
        db,
        organization_id=organization.id,
        actor_id=uuid4(),
        identity_kind="api_key",
        role=None,
    )
    assert set((await db.execute(select(Application.id))).scalars()) == {
        old_application.id,
        assigned_application.id,
        sole_cycle_application.id,
    }
    clear_tenant_context(db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        managed_detail = await client.get(
            f"/workflows/rules/{managed_rule.id}", headers=_auth(manager)
        )
        unmanaged_detail = await client.get(
            f"/workflows/rules/{unmanaged_rule.id}", headers=_auth(manager)
        )
        other_tenant_detail = await client.get(
            f"/workflows/rules/{other_rule.id}", headers=_auth(manager)
        )
        interviewer_detail = await client.get(
            f"/workflows/rules/{managed_rule.id}", headers=_auth(interviewer)
        )
        response = await client.get(
            f"/applications/{assigned_application.id}/assessment-invitations",
            headers=_auth(recruiter),
        )
    assert managed_detail.status_code == 200, managed_detail.text
    assert managed_detail.json()["conditions"]["position_id"] == str(position.id)
    assert unmanaged_detail.status_code == 404, unmanaged_detail.text
    assert other_tenant_detail.status_code == 404, other_tenant_detail.text
    assert interviewer_detail.status_code == 404, interviewer_detail.text
    assert response.status_code == 200, response.text

    await db.rollback()
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.actor_user_id == recruiter_id,
                AuditLog.path_template
                == "/applications/{application_id}/assessment-invitations",
            )
        )
    ).scalar_one()
    assert audit.metadata_json["sensitive_read"] is True
    assert audit.resource_type == "application"
    assert audit.resource_id == assigned_application_id
