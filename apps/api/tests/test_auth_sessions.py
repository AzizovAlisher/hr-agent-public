"""End-to-end authentication, revocation, throttling, and MFA contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.jwt import decode_access_token, encode_access_token
from app.auth.mfa import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    match_totp,
    recovery_code_digest,
    totp_code_for_step,
)
from app.auth.passwords import hash_password
from app.auth.refresh import refresh_token_digest
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models.organization import Organization
from app.models.user import (
    AuthLoginThrottle,
    AuthRefreshSession,
    AuthRefreshToken,
    RevokedAccessToken,
    User,
    UserMfaRecoveryCode,
)


pytestmark = pytest.mark.asyncio


async def _seed_users(db, *roles: str, active: bool = True):
    marker = uuid4().hex[:10]
    organization = Organization(name="Auth sessions", slug=f"auth-sessions-{marker}")
    db.add(organization)
    await db.flush()
    users = []
    for index, role in enumerate(roles):
        user = User(
            organization_id=organization.id,
            email=f"{role}-{index}-{marker}@example.com",
            password_hash=hash_password("Correct horse battery staple!"),
            role=role,
            locale="en",
            is_active=active,
        )
        db.add(user)
        users.append(user)
    await db.commit()
    return organization, users


async def test_local_preview_admin_password_only_login_is_loopback_bound(
    db, monkeypatch
):
    _organization, (admin,) = await _seed_users(db, "admin")
    admin.mfa_enabled = True
    admin.mfa_secret_ciphertext = encrypt_totp_secret("JBSWY3DPEHPK3PXP")
    await db.commit()

    settings = get_settings()
    monkeypatch.setattr(
        "app.routers.auth.get_settings",
        lambda: type(
            "LocalPreviewSettings",
            (),
            {
                "auth_local_preview_admin_email": admin.email,
                "jwt_expires_minutes": settings.jwt_expires_minutes,
            },
        )(),
    )
    credentials = {
        "email": admin.email,
        "password": "Correct horse battery staple!",
    }

    async with _client(ip="127.0.0.1") as client:
        response = await client.post("/auth/login", json=credentials)
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["mfa_required"] is False
        assert body["challenge_token"] is None
        assert decode_access_token(body["access_token"])["mfa"] is True

    async with _client(ip="203.0.113.10") as client:
        response = await client.post("/auth/login", json=credentials)
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] is None
        assert body["mfa_required"] is True
        assert body["challenge_token"]


def _token(user: User) -> str:
    return encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
        mfa_verified=True,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _client(*, ip: str = "203.0.113.10") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 41000)),
        base_url="http://testserver",
    )


async def test_access_token_version_disabled_user_and_challenge_boundaries(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    token = _token(user)
    claims = decode_access_token(token)
    assert claims["typ"] == "access"
    assert claims["ver"] == 0
    assert 0 < claims["exp"] - claims["iat"] <= 60 * 60

    async with _client() as client:
        assert (await client.get("/auth/me", headers=_headers(token))).status_code == 200

        user.token_version += 1
        await db.commit()
        assert (await client.get("/auth/me", headers=_headers(token))).status_code == 401

        fresh = _token(user)
        user.is_active = False
        await db.commit()
        assert (await client.get("/auth/me", headers=_headers(fresh))).status_code == 401


async def test_unknown_wrong_password_and_disabled_login_are_generic(db):
    _organization, users = await _seed_users(db, "recruiter", "hiring_manager")
    enabled, disabled = users
    disabled.is_active = False
    await db.commit()

    attempts = (
        {"email": "missing@example.com", "password": "not-the-password"},
        {"email": enabled.email, "password": "not-the-password"},
        {
            "email": disabled.email,
            "password": "Correct horse battery staple!",
        },
    )
    async with _client() as client:
        responses = [await client.post("/auth/login", json=body) for body in attempts]

    assert [response.status_code for response in responses] == [401, 401, 401]
    errors = [response.json() for response in responses]
    assert len(
        {
            (error.get("detail"), error.get("code"), error.get("status", 401))
            for error in errors
        }
    ) == 1
    error = errors[0]
    assert error["detail"] == "Invalid authentication credentials"
    assert responses[0].headers["content-type"].split(";", 1)[0] in {
        "application/json",
        "application/problem+json",
    }
    public_error_text = f"{error['detail']} {error.get('code', '')}".lower()
    assert all(
        enumerating_term not in public_error_text
        for enumerating_term in ("unknown email", "disabled user", "wrong password")
    )


async def test_login_throttle_serializes_concurrent_failures_and_blocks_correct_password(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    settings = get_settings()

    async def wrong_password() -> int:
        async with _client(ip="198.51.100.77") as client:
            response = await client.post(
                "/auth/login",
                json={"email": user.email, "password": "wrong-password"},
            )
            return response.status_code

    statuses = await asyncio.gather(
        *(wrong_password() for _ in range(settings.auth_login_max_failures + 2))
    )
    assert 200 not in statuses
    assert statuses.count(401) == settings.auth_login_max_failures - 1
    assert statuses.count(429) == 3

    rows = (
        await db.execute(select(AuthLoginThrottle).order_by(AuthLoginThrottle.scope))
    ).scalars().all()
    assert {row.scope for row in rows} == {"account", "network"}
    assert {row.failed_attempts for row in rows} == {settings.auth_login_max_failures}
    assert all(row.blocked_until is not None for row in rows)
    assert all(user.email not in row.identifier_digest for row in rows)

    async with _client(ip="192.0.2.200") as client:
        blocked = await client.post(
            "/auth/login",
            json={
                "email": user.email,
                "password": "Correct horse battery staple!",
            },
        )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


async def test_login_fails_closed_when_the_authoritative_database_is_unavailable():
    async def unavailable_session():
        raise RuntimeError("simulated database outage")
        yield  # pragma: no cover - makes this an async dependency generator

    app.dependency_overrides[get_session] = unavailable_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                client=("203.0.113.55", 41000),
                raise_app_exceptions=False,
            ),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/auth/login",
                json={
                    "email": "user@example.com",
                    "password": "Correct horse battery staple!",
                },
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code >= 500
    assert "access_token" not in response.text
    assert "refresh_token" not in response.text


async def test_logout_revokes_one_token_and_logout_all_revokes_every_version(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    credentials = {
        "email": user.email,
        "password": "Correct horse battery staple!",
    }
    async with _client() as client:
        login_a = (await client.post("/auth/login", json=credentials)).json()
        login_b = (await client.post("/auth/login", json=credentials)).json()
        token_a = login_a["access_token"]
        token_b = login_b["access_token"]
        assert token_a != token_b

        logged_out = await client.post("/auth/logout", headers=_headers(token_a))
        assert logged_out.status_code == 204
        assert (await client.get("/auth/me", headers=_headers(token_a))).status_code == 401
        assert (await client.get("/auth/me", headers=_headers(token_b))).status_code == 200
        assert (
            await client.post(
                "/auth/refresh",
                json={"refresh_token": login_a["refresh_token"]},
            )
        ).status_code == 401

        revoked = (await db.execute(select(RevokedAccessToken))).scalar_one()
        assert revoked.jti_digest != decode_access_token(token_a)["jti"]

        all_logged_out = await client.post(
            "/auth/logout-all", headers=_headers(token_b)
        )
        assert all_logged_out.status_code == 204
        assert (await client.get("/auth/me", headers=_headers(token_b))).status_code == 401
        assert (
            await client.post(
                "/auth/refresh",
                json={"refresh_token": login_b["refresh_token"]},
            )
        ).status_code == 401

        replacement = (await client.post("/auth/login", json=credentials)).json()[
            "access_token"
        ]
        assert decode_access_token(replacement)["ver"] == 1
        assert (await client.get("/auth/me", headers=_headers(replacement))).status_code == 200


async def test_refresh_rotation_session_listing_and_targeted_revocation(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    credentials = {
        "email": user.email,
        "password": "Correct horse battery staple!",
    }
    async with _client() as client:
        login = (await client.post("/auth/login", json=credentials)).json()
        access_token = login["access_token"]
        refresh_token = login["refresh_token"]
        claims = decode_access_token(access_token)
        assert claims["sid"]
        assert login["expires_in"] == get_settings().jwt_expires_minutes * 60

        listed = await client.get("/auth/sessions", headers=_headers(access_token))
        assert listed.status_code == 200
        listed_payload = listed.json()
        assert listed_payload["next_cursor"] is None
        assert listed_payload["items"] == [
            {
                "id": claims["sid"],
                "created_at": listed_payload["items"][0]["created_at"],
                "last_used_at": None,
                "expires_at": listed_payload["items"][0]["expires_at"],
                "current": True,
            }
        ]

        stored = (
            await db.execute(
                select(AuthRefreshToken).where(
                    AuthRefreshToken.session_id == UUID(claims["sid"])
                ).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert stored.token_digest != refresh_token
        assert refresh_token not in repr(stored)

        rotated_response = await client.post(
            "/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert rotated_response.status_code == 200
        assert rotated_response.headers["Cache-Control"] == "private, no-store, max-age=0"
        rotated = rotated_response.json()
        assert rotated["refresh_token"] != refresh_token
        assert decode_access_token(rotated["access_token"])["sid"] == claims["sid"]
        assert (
            await client.get("/auth/me", headers=_headers(access_token))
        ).status_code == 200

        token_rows = (
            await db.execute(
                select(AuthRefreshToken)
                .where(AuthRefreshToken.session_id == UUID(claims["sid"]))
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        assert len(token_rows) == 2
        assert sum(row.consumed_at is not None for row in token_rows) == 1
        assert all(
            raw not in row.token_digest
            for raw in (refresh_token, rotated["refresh_token"])
            for row in token_rows
        )

        revoked = await client.delete(
            f"/auth/sessions/{claims['sid']}",
            headers=_headers(rotated["access_token"]),
        )
        assert revoked.status_code == 204
        assert (
            await client.get(
                "/auth/me", headers=_headers(rotated["access_token"])
            )
        ).status_code == 401
        assert (
            await client.post(
                "/auth/refresh",
                json={"refresh_token": rotated["refresh_token"]},
            )
        ).status_code == 401


async def test_concurrent_refresh_across_two_clients_returns_one_idempotent_rotation(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    credentials = {
        "email": user.email,
        "password": "Correct horse battery staple!",
    }
    async with _client(ip="203.0.113.10") as first_client, _client(
        ip="203.0.113.11"
    ) as second_client:
        login = (await first_client.post("/auth/login", json=credentials)).json()
        results = await asyncio.gather(
            first_client.post(
                "/auth/refresh", json={"refresh_token": login["refresh_token"]}
            ),
            second_client.post(
                "/auth/refresh", json={"refresh_token": login["refresh_token"]}
            ),
        )
        assert [result.status_code for result in results] == [200, 200]
        successful = [result.json() for result in results]
        assert successful[0]["refresh_token"] == successful[1]["refresh_token"]
        assert successful[0]["refresh_token"] != login["refresh_token"]

        await db.refresh(user)
        assert user.token_version == 0
        sessions = (
            await db.execute(
                select(AuthRefreshSession).where(AuthRefreshSession.user_id == user.id)
            )
        ).scalars().all()
        assert sessions
        assert all(session.revoked_at is None for session in sessions)
        token_rows = (
            await db.execute(
                select(AuthRefreshToken).where(
                    AuthRefreshToken.session_id == sessions[0].id
                )
            )
        ).scalars().all()
        assert len(token_rows) == 2
        assert sum(row.consumed_at is not None for row in token_rows) == 1
        for item in successful:
            assert (
                await first_client.get(
                    "/auth/me", headers=_headers(item["access_token"])
                )
            ).status_code == 200


async def test_refresh_reuse_outside_grace_revokes_family(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    credentials = {
        "email": user.email,
        "password": "Correct horse battery staple!",
    }
    async with _client() as client:
        login = (await client.post("/auth/login", json=credentials)).json()
        rotated = await client.post(
            "/auth/refresh", json={"refresh_token": login["refresh_token"]}
        )
        assert rotated.status_code == 200

        consumed = (
            await db.execute(
                select(AuthRefreshToken).where(
                    AuthRefreshToken.token_digest
                    == refresh_token_digest(login["refresh_token"])
                )
            )
        ).scalar_one()
        consumed.consumed_at = datetime.now(UTC) - timedelta(
            seconds=get_settings().auth_refresh_retry_grace_seconds + 1
        )
        await db.commit()

        replay = await client.post(
            "/auth/refresh", json={"refresh_token": login["refresh_token"]}
        )
        assert replay.status_code == 401
        await db.refresh(user)
        assert user.token_version == 1
        sessions = (
            await db.execute(
                select(AuthRefreshSession).where(AuthRefreshSession.user_id == user.id)
            )
        ).scalars().all()
        assert sessions
        assert all(session.revoke_reason == "refresh_reuse_detected" for session in sessions)
        assert (
            await client.post(
                "/auth/refresh",
                json={"refresh_token": rotated.json()["refresh_token"]},
            )
        ).status_code == 401


async def test_expired_consumed_refresh_cannot_revoke_a_newer_session(db):
    _organization, (user,) = await _seed_users(db, "recruiter")
    credentials = {
        "email": user.email,
        "password": "Correct horse battery staple!",
    }
    async with _client() as client:
        first = (await client.post("/auth/login", json=credentials)).json()
        rotated = await client.post(
            "/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert rotated.status_code == 200
        first_session_id = UUID(decode_access_token(first["access_token"])["sid"])

        second = (await client.post("/auth/login", json=credentials)).json()
        first_session = await db.get(AuthRefreshSession, first_session_id)
        assert first_session is not None
        first_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

        replay = await client.post(
            "/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert replay.status_code == 401
        await db.refresh(user)
        assert user.token_version == 0
        assert (
            await client.get(
                "/auth/me", headers=_headers(second["access_token"])
            )
        ).status_code == 200


async def test_session_revocation_is_user_scoped(db):
    _organization, users = await _seed_users(db, "recruiter", "recruiter")
    first, second = users
    async with _client() as client:
        first_login = (
            await client.post(
                "/auth/login",
                json={
                    "email": first.email,
                    "password": "Correct horse battery staple!",
                },
            )
        ).json()
        second_login = (
            await client.post(
                "/auth/login",
                json={
                    "email": second.email,
                    "password": "Correct horse battery staple!",
                },
            )
        ).json()
        second_session = decode_access_token(second_login["access_token"])["sid"]
        denied = await client.delete(
            f"/auth/sessions/{second_session}",
            headers=_headers(first_login["access_token"]),
        )
        assert denied.status_code == 404
        assert (
            await client.get(
                "/auth/me", headers=_headers(second_login["access_token"])
            )
        ).status_code == 200


async def test_password_role_and_status_changes_revoke_tokens_without_lost_updates(db):
    _organization, users = await _seed_users(
        db, "admin", "admin", "recruiter", "hiring_manager"
    )
    admin_a, admin_b, target, password_user = users
    target_token = _token(target)

    async with _client() as client:
        password_login = (
            await client.post(
                "/auth/login",
                json={
                    "email": password_user.email,
                    "password": "Correct horse battery staple!",
                },
            )
        ).json()
        password_token = password_login["access_token"]
        changed_password = await client.post(
            "/users/me/password",
            headers=_headers(password_token),
            json={
                "current_password": "Correct horse battery staple!",
                "new_password": "A replacement password 2026!",
            },
        )
        assert changed_password.status_code == 204
        assert (
            await client.get("/auth/me", headers=_headers(password_token))
        ).status_code == 401
        assert (
            await client.post(
                "/auth/refresh",
                json={"refresh_token": password_login["refresh_token"]},
            )
        ).status_code == 401

        role_change, status_change = await asyncio.gather(
            client.patch(
                f"/users/{target.id}",
                headers=_headers(_token(admin_a)),
                json={"role": "hiring_manager"},
            ),
            client.patch(
                f"/users/{target.id}",
                headers=_headers(_token(admin_b)),
                json={"is_active": False},
            ),
        )
        assert {role_change.status_code, status_change.status_code} == {200}

        await db.refresh(target)
        assert target.role == "hiring_manager"
        assert target.is_active is False
        assert target.token_version == 2
        assert (await client.get("/auth/me", headers=_headers(target_token))).status_code == 401
        assert (
            await client.get("/auth/me", headers=_headers(_token(target)))
        ).status_code == 401


async def test_admin_mfa_enrollment_recovery_concurrency_replay_and_disable(db, monkeypatch):
    fixed_now = datetime(2026, 8, 22, 12, 0, 15, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("app.auth.mfa.datetime", FrozenDateTime)
    _organization, (admin,) = await _seed_users(db, "admin")
    credentials = {
        "email": admin.email,
        "password": "Correct horse battery staple!",
    }

    async with _client() as client:
        initial = await client.post("/auth/login", json=credentials)
        assert initial.status_code == 200
        initial_body = initial.json()
        assert initial_body["access_token"] is None
        assert initial_body["mfa_setup_required"] is True
        setup_challenge = initial_body["challenge_token"]
        assert (
            await client.get("/auth/me", headers=_headers(setup_challenge))
        ).status_code == 401

        setup = await client.post(
            "/auth/mfa/setup", json={"challenge_token": setup_challenge}
        )
        assert setup.status_code == 200
        assert setup.headers["Cache-Control"] == "private, no-store, max-age=0"
        secret = setup.json()["secret"]
        assert setup.json()["provisioning_uri"].startswith("otpauth://totp/")
        await db.refresh(admin)
        assert admin.mfa_pending_secret_ciphertext is not None
        assert secret not in admin.mfa_pending_secret_ciphertext
        assert decrypt_totp_secret(admin.mfa_pending_secret_ciphertext) == secret

        repeated_setup = await client.post(
            "/auth/mfa/setup", json={"challenge_token": setup_challenge}
        )
        assert repeated_setup.status_code == 409
        assert "secret" not in repeated_setup.text.lower()

        current_step = int(fixed_now.timestamp()) // 30
        confirmation = await client.post(
            "/auth/mfa/confirm",
            json={
                "challenge_token": setup_challenge,
                "code": totp_code_for_step(secret, current_step - 1),
            },
        )
        assert confirmation.status_code == 200
        confirmed = confirmation.json()
        assert confirmed["access_token"]
        assert "secret" not in confirmed
        recovery_codes = confirmed["recovery_codes"]
        assert len(recovery_codes) == 10
        assert len(set(recovery_codes)) == 10

        await db.refresh(admin)
        assert admin.mfa_enabled is True
        assert admin.mfa_pending_secret_ciphertext is None
        assert admin.mfa_last_used_step == current_step - 1
        stored_codes = (
            await db.execute(
                select(UserMfaRecoveryCode).where(
                    UserMfaRecoveryCode.user_id == admin.id
                )
            )
        ).scalars().all()
        assert len(stored_codes) == 10
        assert recovery_codes[0] not in {row.code_digest for row in stored_codes}

        challenge_a = (await client.post("/auth/login", json=credentials)).json()[
            "challenge_token"
        ]
        challenge_b = (await client.post("/auth/login", json=credentials)).json()[
            "challenge_token"
        ]
        simultaneous = await asyncio.gather(
            client.post(
                "/auth/mfa/verify",
                json={
                    "challenge_token": challenge_a,
                    "recovery_code": recovery_codes[0],
                },
            ),
            client.post(
                "/auth/mfa/verify",
                json={
                    "challenge_token": challenge_b,
                    "recovery_code": recovery_codes[0],
                },
            ),
        )
        assert sorted(response.status_code for response in simultaneous) == [200, 401]

        totp_challenge = (await client.post("/auth/login", json=credentials)).json()[
            "challenge_token"
        ]
        totp_session = await client.post(
            "/auth/mfa/verify",
            json={
                "challenge_token": totp_challenge,
                "code": totp_code_for_step(secret, current_step),
            },
        )
        assert totp_session.status_code == 200
        totp_token = totp_session.json()["access_token"]

        replay_challenge = (await client.post("/auth/login", json=credentials)).json()[
            "challenge_token"
        ]
        replay = await client.post(
            "/auth/mfa/verify",
            json={
                "challenge_token": replay_challenge,
                "code": totp_code_for_step(secret, current_step),
            },
        )
        assert replay.status_code == 401

        disabled = await client.post(
            "/auth/mfa/disable",
            headers=_headers(totp_token),
            json={
                "current_password": "Correct horse battery staple!",
                "recovery_code": recovery_codes[1],
            },
        )
        assert disabled.status_code == 204
        assert (
            await client.get("/auth/me", headers=_headers(totp_token))
        ).status_code == 401

        await db.refresh(admin)
        assert admin.mfa_enabled is False
        assert admin.mfa_secret_ciphertext is None
        assert admin.mfa_pending_secret_ciphertext is None
        assert (
            await db.execute(
                select(UserMfaRecoveryCode).where(
                    UserMfaRecoveryCode.user_id == admin.id
                )
            )
        ).scalars().all() == []
        restart = await client.post("/auth/login", json=credentials)
        assert restart.json()["mfa_setup_required"] is True
        assert restart.json()["access_token"] is None


async def test_totp_encryption_tamper_recovery_digest_and_replay_primitives():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    at = datetime.fromtimestamp(59, UTC)
    assert totp_code_for_step(secret, 1) == "287082"
    assert match_totp(secret, "287082", at=at) == 1
    assert match_totp(secret, "287082", at=at, last_used_step=1) is None
    assert match_totp(secret, "not-six", at=at) is None

    ciphertext = encrypt_totp_secret(secret)
    assert secret not in ciphertext
    assert decrypt_totp_secret(ciphertext) == secret
    replacement = "A" if ciphertext[-1] != "A" else "B"
    with pytest.raises(ValueError):
        decrypt_totp_secret(ciphertext[:-1] + replacement)

    assert recovery_code_digest("ABCD-EFGH-IJKL") == recovery_code_digest(
        "abcd efgh ijkl"
    )
