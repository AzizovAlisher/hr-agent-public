"""Invitation and password-reset token lifecycle, isolation, and revocation."""

from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import func, select

from app.auth.passwords import hash_password, verify_password
from app.auth.refresh import create_refresh_session
from app.main import app
from app.models.organization import Organization
from app.models.user import AuthRefreshSession, User, UserAccessToken
from app.routers.auth import (
    accept_user_invitation,
    confirm_password_reset,
    preview_user_invitation,
    request_password_reset,
)
from app.routers.users import create_admin_password_reset, create_user_invitation
from app.schemas.auth import (
    InvitationAcceptRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    UserInvitationCreate,
)

pytestmark = pytest.mark.asyncio


async def _tenant(db, label: str) -> tuple[Organization, User]:
    marker = uuid4().hex[:10]
    organization = Organization(name=label, slug=f"{label.lower()}-{marker}")
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"admin-{marker}@example.test",
        password_hash=hash_password("Admin-password-1"),
        role="admin",
        locale="en",
    )
    db.add(admin)
    await db.commit()
    return organization, admin


def _raw_token(access_url: str) -> str:
    return access_url.rsplit("/", 1)[-1]


def _request(client_host: str = "203.0.113.20") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/auth/password-reset/request",
            "raw_path": b"/auth/password-reset/request",
            "query_string": b"",
            "headers": [],
            "client": (client_host, 443),
            "server": ("example.com", 443),
        }
    )


async def test_invitation_is_one_time_rotatable_and_activates_user(db):
    organization, admin = await _tenant(db, "Invitation")
    first_response = Response()
    first = await create_user_invitation(
        UserInvitationCreate(
            email="new.member@example.com",
            role="interviewer",
            locale="ru",
        ),
        response=first_response,
        db=db,
        user=admin,
    )
    assert first_response.headers["cache-control"] == "private, no-store, max-age=0"
    assert first.delivery_enqueued is False
    invited = await db.get(User, first.user_id)
    assert invited is not None
    assert invited.organization_id == organization.id
    assert invited.is_active is False
    assert invited.role == "interviewer"
    assert not verify_password(_raw_token(first.access_url), invited.password_hash)

    first_token = _raw_token(first.access_url)
    stored = await db.get(UserAccessToken, first.id)
    assert stored is not None
    assert stored.token_digest != first_token
    preview = await preview_user_invitation(first_token, Response(), db)
    assert preview.email == "new.member@example.com"
    assert preview.organization_name == organization.name

    rotated = await create_user_invitation(
        UserInvitationCreate(
            email="new.member@example.com",
            role="recruiter",
            locale="en",
        ),
        response=Response(),
        db=db,
        user=admin,
    )
    with pytest.raises(HTTPException) as revoked:
        await preview_user_invitation(first_token, Response(), db)
    assert revoked.value.status_code == 410
    assert revoked.value.detail == {"code": "access_token_revoked"}

    raw = _raw_token(rotated.access_url)
    await accept_user_invitation(
        raw,
        InvitationAcceptRequest(password="Accepted-password-1"),
        Response(),
        db,
    )
    await db.refresh(invited)
    assert invited.is_active is True
    assert invited.role == "recruiter"
    assert verify_password("Accepted-password-1", invited.password_hash)
    with pytest.raises(HTTPException) as replay:
        await preview_user_invitation(raw, Response(), db)
    assert replay.value.status_code == 410
    assert replay.value.detail == {"code": "access_token_consumed"}


async def test_admin_reset_revokes_sessions_and_cross_tenant_ids_are_hidden(db):
    organization, admin = await _tenant(db, "Reset")
    other_organization, other_admin = await _tenant(db, "ResetOther")
    target = User(
        organization_id=organization.id,
        email=f"reset-{uuid4().hex[:8]}@example.test",
        password_hash=hash_password("Old-password-1"),
        role="recruiter",
        locale="en",
    )
    db.add(target)
    await db.flush()
    issued = await create_refresh_session(db, target)
    await db.commit()

    with pytest.raises(HTTPException) as hidden:
        await create_admin_password_reset(
            target.id,
            Response(),
            db=db,
            user=other_admin,
        )
    assert hidden.value.status_code == 404
    assert other_organization.id != organization.id

    reset = await create_admin_password_reset(
        target.id,
        Response(),
        db=db,
        user=admin,
    )
    raw = _raw_token(reset.access_url)
    old_version = target.token_version
    await confirm_password_reset(
        raw,
        PasswordResetConfirmRequest(password="New-password-2"),
        Response(),
        db,
    )
    await db.refresh(target)
    assert target.token_version == old_version + 1
    assert verify_password("New-password-2", target.password_hash)
    assert not verify_password("Old-password-1", target.password_hash)
    session = (
        await db.execute(
            select(AuthRefreshSession).where(AuthRefreshSession.id == issued.session_id)
        )
    ).scalar_one()
    assert session.revoked_at is not None
    assert session.revoke_reason == "password_reset"

    with pytest.raises(HTTPException) as replay:
        await confirm_password_reset(
            raw,
            PasswordResetConfirmRequest(password="Third-password-3"),
            Response(),
            db,
        )
    assert replay.value.status_code == 410
    assert replay.value.detail == {"code": "access_token_consumed"}


async def test_public_reset_request_is_generic_and_leaves_no_undelivered_token(db):
    organization, _admin = await _tenant(db, "PublicReset")
    target = User(
        organization_id=organization.id,
        email="known.reset@example.com",
        password_hash=hash_password("Existing-password-1"),
        role="recruiter",
        locale="en",
    )
    db.add(target)
    await db.commit()
    target_id = target.id

    known_response = Response()
    known = await request_password_reset(
        PasswordResetRequest(email=target.email),
        _request(),
        known_response,
        db,
    )
    unknown_response = Response()
    unknown = await request_password_reset(
        PasswordResetRequest(email="unknown.reset@example.com"),
        _request(),
        unknown_response,
        db,
    )
    assert known == unknown
    assert known.accepted is True
    assert known_response.headers["cache-control"] == "private, no-store, max-age=0"
    assert unknown_response.headers["cache-control"] == "private, no-store, max-age=0"
    assert (
        int(
            await db.scalar(
                select(func.count(UserAccessToken.id)).where(
                    UserAccessToken.user_id == target_id,
                    UserAccessToken.purpose == "password_reset",
                    UserAccessToken.revoked_at.is_(None),
                )
            )
            or 0
        )
        == 0
    )


async def test_public_reset_request_stays_generic_when_delivery_fails(db, monkeypatch):
    organization, _admin = await _tenant(db, "ResetDeliveryFailure")
    target = User(
        organization_id=organization.id,
        email="known.provider.failure@example.com",
        password_hash=hash_password("Existing-password-1"),
        role="recruiter",
        locale="en",
    )
    db.add(target)
    await db.commit()
    target_id = target.id

    async def provider_failure(*_args, **_kwargs):
        raise RuntimeError("provider response containing private diagnostics")

    monkeypatch.setattr(
        "app.routers.auth.enqueue_user_access_email",
        provider_failure,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=app,
            client=("203.0.113.21", 443),
        ),
        base_url="https://example.test",
    ) as client:
        response = await client.post(
            "/auth/password-reset/request",
            json={"email": target.email},
        )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert (
        int(
            await db.scalar(
                select(func.count(UserAccessToken.id)).where(
                    UserAccessToken.user_id == target_id,
                    UserAccessToken.purpose == "password_reset",
                )
            )
            or 0
        )
        == 0
    )
