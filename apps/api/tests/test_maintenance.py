"""Operational cleanup retains active security evidence and removes expiry."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.operations import WorkerHeartbeat
from app.models.organization import Organization
from app.models.user import (
    AuthLoginThrottle,
    AuthRefreshSession,
    AuthRefreshToken,
    RevokedAccessToken,
    User,
    UserAccessToken,
)
from app.services.maintenance import cleanup_expired_operational_records


pytestmark = pytest.mark.asyncio


async def test_cleanup_removes_only_expired_security_and_worker_records(db):
    now = datetime.now(UTC)
    marker = uuid4().hex[:8]
    organization = Organization(name="Cleanup", slug=f"cleanup-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"cleanup-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(user)
    await db.flush()
    expired_session = AuthRefreshSession(
        organization_id=organization.id,
        user_id=user.id,
        token_version=0,
        expires_at=now - timedelta(days=31),
    )
    active_session = AuthRefreshSession(
        organization_id=organization.id,
        user_id=user.id,
        token_version=0,
        expires_at=now + timedelta(days=1),
    )
    db.add_all([expired_session, active_session])
    await db.flush()
    db.add_all(
        [
            AuthRefreshToken(
                organization_id=organization.id,
                session_id=expired_session.id,
                token_digest="a" * 64,
                expires_at=expired_session.expires_at,
            ),
            AuthRefreshToken(
                organization_id=organization.id,
                session_id=active_session.id,
                token_digest="b" * 64,
                expires_at=active_session.expires_at,
            ),
            RevokedAccessToken(
                organization_id=organization.id,
                user_id=user.id,
                jti_digest="c" * 64,
                expires_at=now - timedelta(seconds=1),
            ),
            RevokedAccessToken(
                organization_id=organization.id,
                user_id=user.id,
                jti_digest="d" * 64,
                expires_at=now + timedelta(minutes=10),
            ),
            AuthLoginThrottle(
                scope="account",
                identifier_digest="e" * 64,
                failed_attempts=1,
                window_started_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            ),
            AuthLoginThrottle(
                scope="network",
                identifier_digest="f" * 64,
                failed_attempts=1,
                window_started_at=now,
                updated_at=now,
            ),
            WorkerHeartbeat(
                worker_name="durable-work",
                worker_id="old-worker",
                process_started_at=now - timedelta(days=10),
                last_seen_at=now - timedelta(days=8),
            ),
            WorkerHeartbeat(
                worker_name="durable-work",
                worker_id="live-worker",
                process_started_at=now,
                last_seen_at=now,
            ),
            UserAccessToken(
                organization_id=organization.id,
                user_id=user.id,
                created_by_user_id=user.id,
                purpose="password_reset",
                token_digest="1" * 64,
                token_prefix="expired-token",
                created_at=now - timedelta(days=62),
                expires_at=now - timedelta(days=31),
            ),
        ]
    )
    await db.commit()

    removed = await cleanup_expired_operational_records(db, now=now)
    await db.commit()
    assert removed == {
        "auth_login_throttles": 1,
        "revoked_access_tokens": 1,
        "auth_refresh_sessions": 1,
        "worker_heartbeats": 1,
        "user_access_tokens": 1,
    }
    assert int(
        (await db.execute(select(func.count(AuthRefreshSession.id)))).scalar_one()
    ) == 1
    assert int(
        (await db.execute(select(func.count(UserAccessToken.id)))).scalar_one()
    ) == 0
    # Cascading the expired family also removes its generations, while the
    # active generation remains available for rotation/reuse detection.
    assert int(
        (await db.execute(select(func.count(AuthRefreshToken.id)))).scalar_one()
    ) == 1
