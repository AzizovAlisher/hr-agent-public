"""All framework errors use one sanitized, correlated problem contract."""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from starlette.requests import Request

from app.auth.jwt import encode_access_token
from app.db import SessionLocal
from app.main import app
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.user import User
from app.problem import database_problem_handler


pytestmark = pytest.mark.asyncio


def _assert_problem(response: httpx.Response, *, code: str, status: int) -> dict:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["type"] == f"urn:hr-agent:problem:{code}"
    assert payload["code"] == code
    assert payload["status"] == status
    assert payload["request_id"] == response.headers["x-request-id"]
    UUID(payload["request_id"])
    return payload


async def test_not_found_uses_problem_json_without_echoing_query_values():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/route-that-does-not-exist", params={"token": "private-capability"}
        )
    payload = _assert_problem(response, code="not_found", status=404)
    assert "private-capability" not in response.text
    assert payload["detail"] == "Not Found"


async def test_validation_problem_has_stable_field_errors_and_no_input_echo():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "not-an-email", "password": "private-password-value"},
        )
    payload = _assert_problem(response, code="validation_failed", status=422)
    assert payload["detail"] == "Request validation failed"
    assert payload["field_errors"]
    assert all(set(item) == {"field", "message", "code"} for item in payload["field_errors"])
    assert "private-password-value" not in response.text


class _PrivacyGateDriverError(Exception):
    sqlstate = "55P03"


async def test_privacy_erasure_gate_has_retryable_sanitized_problem_contract():
    private_database_detail = "candidate-secret@example.test"
    driver_error = _PrivacyGateDriverError(
        "Tenant privacy erasure is in progress: " + private_database_detail
    )
    error = DBAPIError("private statement", {}, driver_error, False)
    request = Request({"type": "http", "method": "POST", "path": "/"})

    response = await database_problem_handler(request, error)

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "1"
    assert private_database_detail.encode() not in response.body
    assert b"private statement" not in response.body
    assert b'"code":"privacy_erasure_in_progress"' in response.body


async def test_other_database_lock_errors_are_not_mislabeled(monkeypatch):
    driver_error = _PrivacyGateDriverError("unrelated lock timeout")
    error = DBAPIError("statement", {}, driver_error, False)
    request = Request({"type": "http", "method": "POST", "path": "/"})

    async def fake_unhandled(_request, _exc):
        return object()

    monkeypatch.setattr("app.problem.unhandled_problem_handler", fake_unhandled)
    marker = await database_problem_handler(request, error)
    assert marker.__class__ is object


async def test_audit_preflight_preserves_privacy_gate_problem_contract(db):
    gate_function = await db.scalar(
        text("SELECT to_regprocedure('public.privacy_erasure_gate_key(uuid)')")
    )
    if gate_function is None:
        pytest.skip("Database was created from metadata rather than Alembic migrations")

    organization = Organization(name="Problem gate", slug="problem-gate")
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email="problem-gate@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(organization_id=organization.id, source="manual")
    db.add_all([admin, candidate])
    await db.commit()

    token = encode_access_token(admin.id, admin.role, admin.locale)
    async with SessionLocal() as erasure:
        await erasure.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.privacy_erasure_gate_key(organization.id)
                )
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.patch(
                f"/candidates/{candidate.id}",
                json={"is_favorite": True},
                headers={"Authorization": f"Bearer {token}"},
            )
        await erasure.rollback()

    payload = _assert_problem(
        response, code="privacy_erasure_in_progress", status=409
    )
    assert response.headers["retry-after"] == "1"
    assert payload["detail"] == (
        "Organization data is temporarily unavailable while a privacy request completes."
    )
