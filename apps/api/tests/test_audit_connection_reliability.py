"""Connection-isolation regression coverage for central sensitive-read audit."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import db as db_module
from app.auth.jwt import encode_access_token
from app.config import Settings, get_settings
from app.main import app
from app.middleware import audit as audit_middleware
from app.models.candidate import Candidate
from app.models.candidate_note import CandidateTag
from app.models.enterprise import AuditLog
from app.models.organization import Organization
from app.models.user import User

pytestmark = pytest.mark.asyncio


async def _seed_audit_candidate(db, label: str):
    org = Organization(
        name=f"Audit pool {label}",
        slug=f"audit-pool-{label}-{uuid4().hex[:8]}",
    )
    db.add(org)
    await db.flush()
    admin = User(
        organization_id=org.id,
        email=f"audit-pool-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(
        organization_id=org.id,
        source="manual",
        source_ref="Audit pool candidate",
        raw_cv="Sensitive candidate data used only to exercise central audit.",
    )
    db.add_all([admin, candidate])
    await db.commit()
    return org, admin, candidate


async def _assert_sensitive_audits(db, org, responses) -> None:
    request_ids = {UUID(response.headers["X-Request-ID"]) for response in responses}
    audited_request_ids = set(
        (
            await db.execute(
                select(AuditLog.request_id).where(
                    AuditLog.organization_id == org.id,
                    AuditLog.action == "sensitive_read.get_candidate",
                    AuditLog.request_id.in_(request_ids),
                )
            )
        ).scalars()
    )
    assert audited_request_ids == request_ids


async def test_sensitive_read_audit_does_not_compete_with_request_connections(
    db,
    monkeypatch,
):
    org, admin, candidate = await _seed_audit_candidate(db, "isolation")

    # A one-slot route pool deterministically models the route dependency
    # retaining its only connection while central audit runs. Before the
    # isolation fix, even this single request tried to borrow that same slot and
    # returned audit_unavailable after the checkout timeout. Concurrency is
    # exercised independently by the bounded fanout test below; adding it here
    # would test ordinary route-pool queueing instead of audit isolation.
    database_url = get_settings().database_url
    route_engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
    )
    audit_engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
    )
    route_sessions = async_sessionmaker(route_engine, expire_on_commit=False)
    audit_sessions = async_sessionmaker(audit_engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", route_sessions)
    monkeypatch.setattr(db_module, "AuditSessionLocal", audit_sessions)

    bearer = encode_access_token(admin.id, admin.role, admin.locale)
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            responses = [
                await asyncio.wait_for(
                    client.get(f"/candidates/{candidate.id}", headers=headers),
                    timeout=3.0,
                )
            ]
    finally:
        await route_engine.dispose()
        await audit_engine.dispose()

    assert {response.status_code for response in responses} == {200}
    await _assert_sensitive_audits(db, org, responses)
    assert route_engine.pool is not audit_engine.pool


async def test_default_audit_capacity_drains_candidate_page_fanout(
    db,
    monkeypatch,
):
    org, admin, candidate = await _seed_audit_candidate(db, "fanout")
    configured_default = Settings.model_fields["audit_database_pool_size"].default
    configured_timeout = Settings.model_fields[
        "audit_database_pool_timeout_seconds"
    ].default
    assert configured_default == 8
    assert configured_timeout == 5.0

    database_url = get_settings().database_url
    route_engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=0,
        pool_timeout=1.0,
    )
    audit_engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=configured_default,
        max_overflow=0,
        pool_timeout=configured_timeout,
    )
    route_sessions = async_sessionmaker(route_engine, expire_on_commit=False)
    audit_sessions = async_sessionmaker(audit_engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", route_sessions)
    monkeypatch.setattr(db_module, "AuditSessionLocal", audit_sessions)

    original_bind_actor = audit_middleware._bind_audit_actor

    async def slow_forced_rls_like_actor_binding(session, user_id):
        user = await original_bind_actor(session, user_id)
        # Hold the checked-out audit connection long enough to model the
        # 0.2-1.3 second forced-RLS timings observed in the real stack. With
        # the former two-slot default this 20-request burst timed out.
        await asyncio.sleep(0.6)
        return user

    monkeypatch.setattr(
        audit_middleware,
        "_bind_audit_actor",
        slow_forced_rls_like_actor_binding,
    )

    bearer = encode_access_token(admin.id, admin.role, admin.locale)
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        client.get(f"/candidates/{candidate.id}", headers=headers)
                        for _ in range(20)
                    )
                ),
                timeout=30.0,
            )
    finally:
        await route_engine.dispose()
        await audit_engine.dispose()

    assert {response.status_code for response in responses} == {200}
    await _assert_sensitive_audits(db, org, responses)


async def test_disabled_actor_cannot_be_reenabled_after_mutation_preflight(
    db,
    monkeypatch,
):
    org, admin, candidate = await _seed_audit_candidate(db, "mutation-race")
    bearer = encode_access_token(admin.id, admin.role, admin.locale)
    admin.is_active = False
    await db.commit()

    original_bind_actor = audit_middleware._bind_audit_actor
    inactive_binding_finished = asyncio.Event()
    actor_reenabled = asyncio.Event()

    async def coordinated_actor_binding(session, user_id):
        user = await original_bind_actor(session, user_id)
        assert user is None
        inactive_binding_finished.set()
        await actor_reenabled.wait()
        return user

    monkeypatch.setattr(
        audit_middleware,
        "_bind_audit_actor",
        coordinated_actor_binding,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        request_task = asyncio.create_task(
            client.post(
                f"/candidates/{candidate.id}/tags",
                headers={"Authorization": f"Bearer {bearer}"},
                json={"label": "must-not-commit"},
            )
        )
        await asyncio.wait_for(inactive_binding_finished.wait(), timeout=3.0)
        try:
            async with db_module.SessionLocal() as mutator:
                actor = await mutator.get(User, admin.id)
                assert actor is not None
                actor.is_active = True
                await mutator.commit()
        finally:
            actor_reenabled.set()
        response = await asyncio.wait_for(request_task, timeout=3.0)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["www-authenticate"] == "Bearer"
    assert (
        await db.scalar(
            select(func.count(CandidateTag.id)).where(
                CandidateTag.organization_id == org.id,
                CandidateTag.candidate_id == candidate.id,
            )
        )
        == 0
    )
    request_id = UUID(response.headers["X-Request-ID"])
    assert (
        await db.execute(select(AuditLog.id).where(AuditLog.request_id == request_id))
    ).scalar_one_or_none() is None


@pytest.mark.parametrize("actor_change", ["disable", "delete"])
async def test_sensitive_read_fails_closed_when_actor_changes_before_audit(
    db,
    monkeypatch,
    actor_change,
):
    _org, admin, candidate = await _seed_audit_candidate(db, actor_change)
    original_bind_actor = audit_middleware._bind_audit_actor
    audit_binding_started = asyncio.Event()
    actor_changed = asyncio.Event()

    async def coordinated_actor_binding(session, user_id):
        audit_binding_started.set()
        await actor_changed.wait()
        return await original_bind_actor(session, user_id)

    monkeypatch.setattr(
        audit_middleware,
        "_bind_audit_actor",
        coordinated_actor_binding,
    )

    bearer = encode_access_token(admin.id, admin.role, admin.locale)
    headers = {"Authorization": f"Bearer {bearer}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        request_task = asyncio.create_task(
            client.get(f"/candidates/{candidate.id}", headers=headers)
        )
        await asyncio.wait_for(audit_binding_started.wait(), timeout=3.0)
        try:
            async with db_module.SessionLocal() as mutator:
                actor = await mutator.get(User, admin.id)
                assert actor is not None
                if actor_change == "disable":
                    actor.is_active = False
                else:
                    await mutator.delete(actor)
                await mutator.commit()
        finally:
            actor_changed.set()
        response = await asyncio.wait_for(request_task, timeout=3.0)

    assert response.status_code == 503
    assert response.json()["code"] == "audit_unavailable"
    request_id = UUID(response.headers["X-Request-ID"])
    unaudited_success = (
        await db.execute(select(AuditLog.id).where(AuditLog.request_id == request_id))
    ).scalar_one_or_none()
    assert unaudited_success is None
