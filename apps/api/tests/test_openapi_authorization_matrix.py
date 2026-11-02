"""OpenAPI-driven authorization and tenant-boundary release gates.

The catalog test is intentionally generated from the running application. A
new route cannot silently avoid these checks by being omitted from a manually
maintained parametrization.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

import app.auth.dependencies as auth_dependencies
import app.db as db_module
from app.auth.authorization import (
    CANONICAL_PERMISSIONS,
    FIELD_DEFAULT_ROLES,
    OPERATION_CATALOG,
    AuthMode,
    FieldClass,
    ResourceScope,
)
from app.auth.dependencies import AuthenticatedRequest
from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.enterprise import (
    AuditLog,
    EnterprisePolicy,
    PermissionOverride,
)
from app.models.interview import Interview
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.user import User
from app.routers.enterprise import create_api_key, revoke_api_key
from app.schemas.enterprise import ApiKeyCreate


HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
UUID_PLACEHOLDER = UUID("00000000-0000-4000-8000-000000000001")
PATH_PARAMETER = re.compile(r"\{([^{}]+)\}")


@pytest_asyncio.fixture
async def _production_request_sessions(monkeypatch):
    """Use the real non-bypass runtime role in migrated forced-RLS runs."""

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


def _openapi_operations() -> dict[tuple[str, str], dict]:
    schema = app.openapi()
    return {
        (method.upper(), path): operation
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if method.upper() in HTTP_METHODS
    }


def _api_routes() -> dict[tuple[str, str], object]:
    """Flatten FastAPI's direct and lazy included-router representations."""

    routes: dict[tuple[str, str], object] = {}
    candidates: list[object] = []
    pending = list(app.routes)
    while pending:
        route = pending.pop()
        if isinstance(route, APIRoute) or all(
            hasattr(route, attribute)
            for attribute in ("path", "methods", "dependant")
        ):
            candidates.append(route)
        elif hasattr(route, "effective_candidates"):
            pending.extend(route.effective_candidates())
    for route in candidates:
        methods = set(getattr(route, "methods", ()))
        path = str(getattr(route, "path"))
        for method in methods & HTTP_METHODS:
            key = (method, path)
            assert key not in routes, f"Duplicate HTTP operation: {key}"
            routes[key] = route
    return routes


def _dependency_calls(route: object) -> set[Callable]:
    pending = [route.dependant]
    seen: set[int] = set()
    calls: set[Callable] = set()
    while pending:
        dependant = pending.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is not None:
            calls.add(dependant.call)
        pending.extend(dependant.dependencies)
    return calls


def _path_value(name: str) -> str:
    if name == "resource_type":
        return "candidate"
    return str(UUID_PLACEHOLDER)


def _concrete_path(
    template: str,
    replacements: dict[str, UUID | str] | None = None,
) -> str:
    values = replacements or {}
    return PATH_PARAMETER.sub(
        lambda match: str(values.get(match.group(1), _path_value(match.group(1)))),
        template,
    )


def _request(method: str, path_template: str) -> Request:
    concrete = _concrete_path(path_template)
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": concrete,
            "raw_path": concrete.encode("ascii"),
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "route": SimpleNamespace(path=path_template),
            "path_params": {},
        }
    )


def _bearer(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _new_user(
    db: AsyncSession,
    organization_id: UUID,
    *,
    role: str,
    label: str,
    active: bool = True,
) -> User:
    row = User(
        organization_id=organization_id,
        email=f"{label}-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
        is_active=active,
    )
    db.add(row)
    await db.flush()
    return row


def test_every_openapi_operation_has_the_exact_runtime_auth_dependency() -> None:
    operations = _openapi_operations()
    routes = _api_routes()
    assert operations.keys() == OPERATION_CATALOG.keys()
    assert operations.keys() <= routes.keys()

    mode_counts = {mode: 0 for mode in AuthMode}
    for key, operation in operations.items():
        policy = OPERATION_CATALOG[key]
        mode_counts[policy.auth_mode] += 1
        calls = _dependency_calls(routes[key])
        has_current_user = auth_dependencies.current_user in calls
        has_authenticated_request = auth_dependencies.authenticated_request in calls
        bearer_declared = any(
            "HTTPBearer" in requirement
            for requirement in operation.get("security", [])
        )
        if policy.auth_mode is AuthMode.BEARER:
            assert has_authenticated_request, key
            assert bearer_declared, key
            if not has_current_user:
                assert policy.scope is ResourceScope.SELF, key
                assert not policy.actions and not policy.fields, key
                assert key[1].startswith("/auth/"), key
        else:
            assert not has_current_user, key
            assert not has_authenticated_request, key
            assert not bearer_declared, key
            assert not policy.actions and not policy.fields, key

        parameters = operation.get("parameters", [])
        if policy.auth_mode is AuthMode.API_KEY:
            assert any(
                parameter["in"] == "header"
                and parameter["name"].lower() == "x-api-key"
                for parameter in parameters
            ), key
        if policy.auth_mode is AuthMode.WEBHOOK_SECRET:
            assert any(
                parameter["in"] == "header"
                and parameter["name"].lower()
                in {
                    "x-webhook-secret",
                    "x-hr-webhook-signature",
                }
                for parameter in parameters
            ), key

    assert mode_counts == {
        mode: sum(
            policy.auth_mode is mode for policy in OPERATION_CATALOG.values()
        )
        for mode in AuthMode
    }
    assert all(count > 0 for count in mode_counts.values())


@pytest.mark.asyncio
async def test_generated_allow_deny_field_and_assignment_matrix_for_every_bearer(
    monkeypatch,
) -> None:
    """Execute the central policy boundary for every OpenAPI bearer operation."""

    user = SimpleNamespace(role="admin")
    authenticated = AuthenticatedRequest(user=user, claims={})
    database = object()
    covered: set[tuple[str, str]] = set()

    async def exercise(
        method: str,
        path: str,
        *,
        denied_action: str | None = None,
        denied_field: FieldClass | None = None,
        deny_assignment: bool = False,
    ) -> tuple[list[str], set[FieldClass], int]:
        actions: list[str] = []
        fields: set[FieldClass] = set()
        assignment_calls = 0

        async def permission(_db, _user, action: str) -> None:
            actions.append(action)
            if action == denied_action:
                raise HTTPException(403, "Permission denied")

        async def field_access(_db, _user, field: FieldClass) -> None:
            fields.add(field)
            if field == denied_field:
                raise HTTPException(403, "Sensitive field access denied")

        async def assignment(_db, _user, _request) -> None:
            nonlocal assignment_calls
            assignment_calls += 1
            if deny_assignment:
                raise HTTPException(404, "Resource not found")

        monkeypatch.setattr(auth_dependencies, "require_permission", permission)
        monkeypatch.setattr(auth_dependencies, "require_field_access", field_access)
        monkeypatch.setattr(
            auth_dependencies,
            "enforce_hiring_assignment_scope",
            assignment,
        )
        result = await auth_dependencies.current_user(
            _request(method, path),
            authenticated=authenticated,
            db=database,
        )
        assert result is user
        return actions, fields, assignment_calls

    for key in _openapi_operations():
        policy = OPERATION_CATALOG[key]
        if policy.auth_mode is not AuthMode.BEARER:
            continue
        method, path = key
        actions, fields, assignment_calls = await exercise(method, path)
        assert actions == list(policy.actions), key
        assert fields == set(policy.fields), key
        assert assignment_calls == (
            1 if policy.scope is ResourceScope.HIRING_ASSIGNMENT else 0
        ), key
        covered.add(key)

        for denied_action in policy.actions:
            with pytest.raises(HTTPException) as denied:
                await exercise(method, path, denied_action=denied_action)
            assert denied.value.status_code == 403, (key, denied_action)
        for denied_field in policy.fields:
            with pytest.raises(HTTPException) as denied:
                await exercise(method, path, denied_field=denied_field)
            assert denied.value.status_code == 403, (key, denied_field)
        if policy.scope is ResourceScope.HIRING_ASSIGNMENT:
            with pytest.raises(HTTPException) as hidden:
                await exercise(method, path, deny_assignment=True)
            assert hidden.value.status_code == 404, key

    assert covered == {
        key
        for key, policy in OPERATION_CATALOG.items()
        if policy.auth_mode is AuthMode.BEARER
    }

    with pytest.raises(HTTPException) as uncatalogued:
        await auth_dependencies.current_user(
            _request("GET", "/uncatalogued-operation"),
            authenticated=authenticated,
            db=database,
        )
    assert uncatalogued.value.status_code == 403


@pytest.mark.asyncio
async def test_every_protected_bearer_operation_denies_restricted_and_disabled_users(
    db: AsyncSession,
) -> None:
    organization = Organization(
        name="Generated authorization matrix",
        slug=f"generated-auth-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    restricted = await _new_user(
        db,
        organization.id,
        role="restricted",
        label="restricted",
    )
    disabled = await _new_user(
        db,
        organization.id,
        role="admin",
        label="disabled",
        active=False,
    )
    await db.commit()

    restricted_failures: list[str] = []
    disabled_failures: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=30,
    ) as client:
        for key in sorted(_openapi_operations()):
            policy = OPERATION_CATALOG[key]
            if policy.auth_mode is not AuthMode.BEARER:
                continue
            method, path = key
            url = _concrete_path(path)
            disabled_response = await client.request(
                method,
                url,
                headers=_bearer(disabled),
            )
            if disabled_response.status_code != 401:
                disabled_failures.append(
                    f"{method} {path}: {disabled_response.status_code}"
                )
            if not policy.actions and not policy.fields:
                continue
            restricted_response = await client.request(
                method,
                url,
                headers=_bearer(restricted),
            )
            if restricted_response.status_code != 403:
                restricted_failures.append(
                    f"{method} {path}: {restricted_response.status_code}"
                )

    assert disabled_failures == []
    assert restricted_failures == []


@pytest.mark.asyncio
async def test_generated_cross_tenant_ids_are_hidden_before_every_matching_handler(
    db: AsyncSession,
) -> None:
    organization = Organization(
        name="Assignment tenant",
        slug=f"assignment-tenant-{uuid4().hex[:10]}",
    )
    other_organization = Organization(
        name="Cross tenant",
        slug=f"cross-tenant-{uuid4().hex[:10]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    manager = await _new_user(
        db,
        organization.id,
        role="hiring_manager",
        label="manager",
    )
    outsider = await _new_user(
        db,
        other_organization.id,
        role="admin",
        label="outsider",
    )
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="cross-tenant-candidate-canary",
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Cross-tenant position canary",
        created_by=outsider.id,
    )
    db.add_all([other_candidate, other_position])
    await db.flush()
    other_application = Application(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        source="manual",
    )
    other_requisition = Requisition(
        organization_id=other_organization.id,
        created_by_user_id=outsider.id,
        hiring_manager_user_id=outsider.id,
        position_id=other_position.id,
        title="Cross-tenant requisition canary",
        business_justification="Must remain invisible to another tenant.",
    )
    db.add_all([other_application, other_requisition])
    await db.flush()
    other_interview = Interview(
        organization_id=other_organization.id,
        candidate_id=other_candidate.id,
        position_id=other_position.id,
        application_id=other_application.id,
        created_by=outsider.id,
    )
    db.add(other_interview)
    db.add_all(
        PermissionOverride(
            organization_id=organization.id,
            role="hiring_manager",
            permission=permission,
            allowed=True,
        )
        for permission in CANONICAL_PERMISSIONS
    )
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            field_visibility={
                field.value: ["hiring_manager"] for field in FIELD_DEFAULT_ROLES
            },
        )
    )
    await db.commit()

    replacements = {
        "candidate_id": other_candidate.id,
        "primary_candidate_id": other_candidate.id,
        "source_candidate_id": other_candidate.id,
        "position_id": other_position.id,
        "application_id": other_application.id,
        "interview_id": other_interview.id,
        "requisition_id": other_requisition.id,
    }
    covered: set[tuple[str, str]] = set()
    failures: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=30,
    ) as client:
        for key in sorted(_openapi_operations()):
            policy = OPERATION_CATALOG[key]
            if (
                policy.auth_mode is not AuthMode.BEARER
                or policy.scope is not ResourceScope.HIRING_ASSIGNMENT
                or not any(
                    f"{{{parameter}}}" in key[1] for parameter in replacements
                )
            ):
                continue
            method, path = key
            response = await client.request(
                method,
                _concrete_path(path, replacements),
                headers=_bearer(manager),
            )
            covered.add(key)
            if response.status_code != 404:
                failures.append(f"{method} {path}: {response.status_code}")

    expected = {
        key
        for key, policy in OPERATION_CATALOG.items()
        if policy.auth_mode is AuthMode.BEARER
        and policy.scope is ResourceScope.HIRING_ASSIGNMENT
        and any(f"{{{parameter}}}" in key[1] for parameter in replacements)
    }
    assert covered == expected
    assert len(covered) >= 75
    assert failures == []


@pytest.mark.asyncio
async def test_api_key_routes_are_scoped_revocable_and_durably_audited(
    db: AsyncSession,
    _production_request_sessions,
) -> None:
    organization = Organization(
        name="API key tenant",
        slug=f"api-key-tenant-{uuid4().hex[:10]}",
    )
    other_organization = Organization(
        name="Other API key tenant",
        slug=f"other-api-key-{uuid4().hex[:10]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    admin = await _new_user(
        db, organization.id, role="admin", label="api-key-admin"
    )
    other_admin = await _new_user(
        db,
        other_organization.id,
        role="admin",
        label="other-api-key-admin",
    )
    own_candidates = [
        Candidate(
            organization_id=organization.id,
            source="manual",
            source_ref=f"tenant-a-private-canary-{index}",
        )
        for index in range(2)
    ]
    other_candidate = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="tenant-b-private-canary",
    )
    own_position = Position(
        organization_id=organization.id,
        title="Tenant A API position",
        created_by=admin.id,
    )
    other_position = Position(
        organization_id=other_organization.id,
        title="Tenant B API position",
        created_by=other_admin.id,
    )
    db.add_all([*own_candidates, other_candidate, own_position, other_position])
    await db.commit()

    candidate_key = await create_api_key(
        ApiKeyCreate(name="Candidate reader", scopes=["candidates:read"]),
        db=db,
        user=admin,
    )
    position_key = await create_api_key(
        ApiKeyCreate(name="Position reader", scopes=["positions:read"]),
        db=db,
        user=admin,
    )
    other_candidate_key = await create_api_key(
        ApiKeyCreate(name="Other candidate reader", scopes=["candidates:read"]),
        db=db,
        user=other_admin,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/enterprise/api/v1/candidates")
        ).status_code == 401
        assert (
            await client.get(
                "/enterprise/api/v1/candidates",
                headers={"X-API-Key": "not-a-real-key"},
            )
        ).status_code == 401
        own_response = await client.get(
            "/enterprise/api/v1/candidates?limit=1",
            headers={"X-API-Key": candidate_key.secret},
        )
        assert own_response.status_code == 200
        assert (
            await client.get(
                "/enterprise/api/v1/positions",
                headers={"X-API-Key": candidate_key.secret},
            )
        ).status_code == 401
        position_response = await client.get(
            "/enterprise/api/v1/positions",
            headers={"X-API-Key": position_key.secret},
        )
        other_response = await client.get(
            "/enterprise/api/v1/candidates",
            headers={"X-API-Key": other_candidate_key.secret},
        )
        replayed_cursor = await client.get(
            "/enterprise/api/v1/candidates",
            params={"cursor": own_response.json()["next_cursor"]},
            headers={"X-API-Key": other_candidate_key.secret},
        )

    assert {item["id"] for item in own_response.json()["items"]}.issubset(
        {str(candidate.id) for candidate in own_candidates}
    )
    assert own_response.json()["next_cursor"] is not None
    assert own_response.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert own_response.headers["Pragma"] == "no-cache"
    assert position_response.status_code == 200
    assert position_response.headers["Cache-Control"] == (
        "private, no-store, max-age=0"
    )
    assert [item["id"] for item in position_response.json()["items"]] == [
        str(own_position.id)
    ]
    assert other_response.status_code == 200
    assert [item["id"] for item in other_response.json()["items"]] == [
        str(other_candidate.id)
    ]
    assert replayed_cursor.status_code == 422

    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.request_id == UUID(own_response.headers["X-Request-ID"])
            )
        )
    ).scalar_one()
    assert audit.organization_id == organization.id
    assert audit.actor_user_id == admin.id
    assert audit.actor_email_snapshot is None
    assert audit.action == "api_key_read.candidates"
    assert audit.resource_type == "api_key"
    assert audit.resource_id == candidate_key.id
    assert audit.path_template == "/enterprise/api/v1/candidates"
    assert audit.status_code == 200
    assert audit.metadata_json == {
        "identity_kind": "api_key",
        "api_key_id": str(candidate_key.id),
        "required_scope": "candidates:read",
        "query_keys": ["limit"],
        "sensitive_read": True,
    }
    serialized_audit = json.dumps(audit.metadata_json, sort_keys=True)
    assert "tenant-a-private-canary" not in serialized_audit
    assert candidate_key.secret not in serialized_audit
    position_audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.request_id
                == UUID(position_response.headers["X-Request-ID"])
            )
        )
    ).scalar_one()
    assert position_audit.action == "api_key_read.positions"
    assert position_audit.resource_type == "api_key"
    assert position_audit.resource_id == position_key.id
    assert position_audit.path_template == "/enterprise/api/v1/positions"
    assert position_audit.status_code == 200
    assert position_audit.metadata_json["required_scope"] == "positions:read"

    await revoke_api_key(candidate_key.id, db=db, user=admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        revoked = await client.get(
            "/enterprise/api/v1/candidates",
            headers={"X-API-Key": candidate_key.secret},
        )
    assert revoked.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_commit", [2, 3])
async def test_api_key_read_fails_closed_and_keeps_started_intent_when_available(
    db: AsyncSession,
    monkeypatch,
    _production_request_sessions,
    failing_commit: int,
) -> None:
    organization = Organization(
        name="API audit failure tenant",
        slug=f"api-audit-failure-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    admin = await _new_user(
        db,
        organization.id,
        role="admin",
        label="api-audit-failure-admin",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="must-not-be-released-when-audit-fails",
    )
    db.add(candidate)
    await db.commit()
    created = await create_api_key(
        ApiKeyCreate(name="Fail closed reader", scopes=["candidates:read"]),
        db=db,
        user=admin,
    )

    original_commit = AsyncSession.commit
    commit_calls = 0

    async def fail_final_audit_commit(session: AsyncSession) -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == failing_commit:
            raise RuntimeError("simulated audit outage")
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_final_audit_commit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/enterprise/api/v1/candidates",
            headers={"X-API-Key": created.secret},
        )
    monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert commit_calls == failing_commit
    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    assert "must-not-be-released-when-audit-fails" not in response.text
    intent = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.request_id == UUID(response.headers["X-Request-ID"])
            )
        )
    ).scalar_one_or_none()
    if failing_commit == 2:
        assert intent is None
        return
    assert intent is not None
    assert intent.organization_id == organization.id
    assert intent.resource_type == "api_key"
    assert intent.resource_id == created.id
    assert intent.status_code == 102
    assert intent.metadata_json["audit_state"] == "started"
