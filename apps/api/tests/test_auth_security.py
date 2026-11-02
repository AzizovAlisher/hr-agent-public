"""Authentication boundary and organization-admin invariants."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.auth.jwt import decode_access_token, encode_access_token
from app.auth.passwords import hash_password, verify_password
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models.organization import Organization
from app.models.user import User
from app.routers.users import update_user
from app.schemas.auth import LoginRequest, UserUpdate

pytestmark = pytest.mark.asyncio


async def _organization_with_admins(db, count: int = 1):
    token = uuid4().hex[:8]
    organization = Organization(name="Auth tenant", slug=f"auth-{token}")
    db.add(organization)
    await db.flush()
    admins = [
        User(
            organization_id=organization.id,
            email=f"admin-{index}-{token}@example.com",
            password_hash="unused",
            role="admin",
            locale="en",
        )
        for index in range(count)
    ]
    db.add_all(admins)
    await db.commit()
    return organization, admins


async def test_passwords_over_bcrypt_byte_limit_are_rejected_not_truncated():
    boundary = "a" * 72
    hashed = hash_password(boundary)
    assert verify_password(boundary, hashed)
    assert not verify_password(boundary + "suffix", hashed)
    LoginRequest(email="user@example.com", password=boundary)
    with pytest.raises(ValidationError):
        LoginRequest(email="user@example.com", password=boundary + "x")
    # Validation is based on UTF-8 bytes, not Python character count.
    LoginRequest(email="user@example.com", password="é" * 36)
    with pytest.raises(ValidationError):
        LoginRequest(email="user@example.com", password="é" * 37)


async def test_last_admin_cannot_demote_self(db):
    _organization, (admin,) = await _organization_with_admins(db)
    with pytest.raises(HTTPException) as rejected:
        await update_user(
            admin.id,
            UserUpdate(role="recruiter"),
            db=db,
            user=admin,
        )
    assert rejected.value.status_code == 409
    await db.refresh(admin)
    assert admin.role == "admin"


async def test_admin_change_preserves_one_remaining_admin(db):
    _organization, admins = await _organization_with_admins(db, count=2)
    updated = await update_user(
        admins[1].id,
        UserUpdate(role="recruiter"),
        db=db,
        user=admins[0],
    )
    assert updated.role == "recruiter"
    with pytest.raises(HTTPException) as rejected:
        await update_user(
            admins[0].id,
            UserUpdate(role="hiring_manager"),
            db=db,
            user=admins[0],
        )
    assert rejected.value.status_code == 409


async def test_disabled_admin_does_not_satisfy_last_active_admin_invariant(db):
    _organization, admins = await _organization_with_admins(db, count=2)
    disabled = await update_user(
        admins[1].id,
        UserUpdate(is_active=False),
        db=db,
        user=admins[0],
    )
    assert disabled.is_active is False
    assert disabled.role == "admin"
    assert admins[1].token_version == 1

    with pytest.raises(HTTPException) as rejected:
        await update_user(
            admins[0].id,
            UserUpdate(role="recruiter"),
            db=db,
            user=admins[0],
        )
    assert rejected.value.status_code == 409


async def test_concurrent_admin_demotions_preserve_one_active_admin(db):
    organization, admins = await _organization_with_admins(db, count=2)
    organization_id = organization.id
    admin_ids = [item.id for item in admins]

    async def demote(admin_id):
        async with SessionLocal() as session:
            actor = await session.get(User, admin_id)
            assert actor is not None
            try:
                updated = await update_user(
                    admin_id,
                    UserUpdate(role="recruiter"),
                    db=session,
                    user=actor,
                )
                return updated.role
            except HTTPException as exc:
                await session.rollback()
                return exc.status_code

    outcomes = await asyncio.gather(*(demote(admin_id) for admin_id in admin_ids))
    assert sorted(outcomes, key=str) == [409, "recruiter"]
    async with SessionLocal() as session:
        remaining = await session.scalar(
            select(func.count(User.id)).where(
                User.organization_id == organization_id,
                User.role == "admin",
                User.is_active.is_(True),
            )
        )
    assert remaining == 1


async def test_access_tokens_require_issuer_audience_jti_and_algorithm():
    user_id = uuid4()
    token = encode_access_token(user_id, "recruiter", "en")
    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["iss"] == get_settings().jwt_issuer
    assert payload["aud"] == get_settings().jwt_audience
    assert payload["jti"]
    assert payload["typ"] == "access"
    assert payload["ver"] == 0
    assert payload["mfa"] is True
    assert payload["sid"] is None
    assert payload["exp"] - int(datetime.now(UTC).timestamp()) <= 60 * 60

    missing_session_claim = dict(payload)
    missing_session_claim.pop("sid")
    with pytest.raises(ValueError):
        decode_access_token(
            jwt.encode(
                missing_session_claim,
                get_settings().jwt_secret,
                algorithm=get_settings().jwt_algorithm,
            )
        )

    settings = get_settings()
    missing_claims = jwt.encode(
        {"sub": str(user_id), "exp": 4_102_444_800, "iat": 1_700_000_000},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(ValueError):
        decode_access_token(missing_claims)
    alternate_algorithm = "HS384" if settings.jwt_algorithm != "HS384" else "HS256"
    wrong_algorithm = jwt.encode(
        {
            "sub": str(user_id),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "jti": str(uuid4()),
            "exp": 4_102_444_800,
            "iat": 1_700_000_000,
        },
        settings.jwt_secret,
        algorithm=alternate_algorithm,
    )
    with pytest.raises(ValueError):
        decode_access_token(wrong_algorithm)


async def test_production_configuration_rejects_development_defaults():
    with pytest.raises(ValidationError):
        Settings(app_environment="production")
    safe = Settings(
        app_environment="production",
        database_url="postgresql+asyncpg://app:unique@db:5432/recruitment",
        web_origin="https://hire.example.com",
        jwt_secret="x" * 48,
        auth_mfa_encryption_key="m" * 48,
        public_rate_limit_backend="valkey",
        public_rate_limit_valkey_url="rediss://valkey.example.test/0",
        public_rate_limit_hmac_secret="r" * 48,
        integration_credential_encryption_key=(
            "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
        ),
        hh_user_agent="Human Platform security test security@example.test",
        malware_scanner_backend="clamav",
        blob_store_backend="s3",
        s3_endpoint="https://minio.example.test",
        s3_bucket="hr-agent-private",
        s3_public_bucket="hr-agent-public",
        s3_access_key="security-test-access",
        s3_secret_key="storage-only-secret-key",
        s3_public_base_url="https://assets.example.test",
        tenant_rls_mode="forced",
        openrouter_base_url="https://openrouter.ai/api/v1",
        worker_readiness_required=True,
    )
    assert safe.app_environment == "production"

    with pytest.raises(ValidationError, match="must be different"):
        Settings(
            **{
                **safe.model_dump(),
                "s3_public_bucket": safe.s3_bucket,
            }
        )


async def test_local_preview_admin_override_is_strictly_environment_bound():
    configured = Settings(
        _env_file=None,
        app_environment="development",
        database_url=(
            "postgresql+asyncpg://preview:local@127.0.0.1:5432/recruitment_v2_preview"
        ),
        web_origin="http://127.0.0.1:3100",
        auth_local_preview_admin_email="  ADMIN@EXAMPLE.COM ",
    )
    assert configured.auth_local_preview_admin_email == "admin@example.com"

    invalid_configurations = (
        {"app_environment": "test"},
        {"app_environment": "production"},
        {
            "database_url": (
                "postgresql+asyncpg://preview:local@127.0.0.1:5432/recruitment_v2"
            )
        },
        {"web_origin": "http://192.168.1.20:3100"},
        {"web_origin": "https://127.0.0.1:3100"},
        {"auth_local_preview_admin_email": "not-an-email"},
    )
    base = {
        "_env_file": None,
        "app_environment": "development",
        "database_url": (
            "postgresql+asyncpg://preview:local@127.0.0.1:5432/recruitment_v2_preview"
        ),
        "web_origin": "http://127.0.0.1:3100",
        "auth_local_preview_admin_email": "admin@example.com",
    }
    for override in invalid_configurations:
        with pytest.raises(ValidationError, match="AUTH_LOCAL_PREVIEW_ADMIN_EMAIL"):
            Settings(**{**base, **override})
