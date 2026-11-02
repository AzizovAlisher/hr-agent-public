"""One-time user invitation and password-reset capabilities."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.config import get_settings
from app.db import forced_tenant_rls, resolve_capability, set_tenant_context
from app.models.integration import IntegrationConnection
from app.models.organization import Organization
from app.models.user import User, UserAccessToken
from app.services.integrations.crypto import IntegrationCipher, IntegrationCryptoError
from app.services.integrations.jobs import IntegrationJobError, enqueue_job


INVITATION_LIFETIME = timedelta(days=7)
PASSWORD_RESET_LIFETIME = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ResolvedUserAccess:
    token: UserAccessToken
    user: User
    organization: Organization


def _digest(raw_token: str) -> str:
    settings = get_settings()
    key = hashlib.sha256(
        b"hr-agent/user-access/v1\0" + settings.jwt_secret.encode()
    ).digest()
    return hmac.new(key, raw_token.encode("ascii", "strict"), hashlib.sha256).hexdigest()


def _new_raw_token() -> str:
    return secrets.token_urlsafe(32)


def access_url(*, purpose: str, raw_token: str) -> str:
    origin = get_settings().web_origin.rstrip("/")
    route = "invite" if purpose == "invitation" else "reset-password"
    return f"{origin}/{route}/{raw_token}"


async def create_user_access_token(
    db: AsyncSession,
    *,
    user: User,
    purpose: str,
    created_by_user_id: UUID | None,
    lifetime: timedelta,
    now: datetime | None = None,
) -> tuple[UserAccessToken, str]:
    if purpose not in {"invitation", "password_reset"}:
        raise ValueError("Unknown user access token purpose")
    current = now or datetime.now(UTC)
    if lifetime <= timedelta(0) or lifetime > timedelta(days=30):
        raise ValueError("Invalid user access token lifetime")
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"user-access:{user.organization_id}:{user.id}:{purpose}", 0
                )
            )
        )
    )
    await db.execute(
        update(UserAccessToken)
        .where(
            UserAccessToken.organization_id == user.organization_id,
            UserAccessToken.user_id == user.id,
            UserAccessToken.purpose == purpose,
            UserAccessToken.consumed_at.is_(None),
            UserAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=current)
    )
    raw_token = _new_raw_token()
    record = UserAccessToken(
        organization_id=user.organization_id,
        user_id=user.id,
        created_by_user_id=created_by_user_id,
        purpose=purpose,
        token_digest=_digest(raw_token),
        token_prefix=raw_token[:12],
        expires_at=current + lifetime,
    )
    db.add(record)
    await db.flush()
    return record, raw_token


async def resolve_user_access_token(
    db: AsyncSession,
    *,
    raw_token: str,
    purpose: str,
    lock: bool = False,
    now: datetime | None = None,
) -> ResolvedUserAccess:
    if not 32 <= len(raw_token) <= 128:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, {"code": "access_token_not_found"}
        )
    current = now or datetime.now(UTC)
    digest = _digest(raw_token)
    if forced_tenant_rls():
        bootstrap = await resolve_capability(
            db,
            kind="user_access",
            digest=digest,
        )
        if bootstrap is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, {"code": "access_token_not_found"}
            )
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    statement = (
        select(UserAccessToken, User, Organization)
        .join(
            User,
            (User.id == UserAccessToken.user_id)
            & (User.organization_id == UserAccessToken.organization_id),
        )
        .join(Organization, Organization.id == UserAccessToken.organization_id)
        .where(
            UserAccessToken.token_digest == digest,
            UserAccessToken.purpose == purpose,
        )
    )
    if lock:
        statement = statement.with_for_update(of=[UserAccessToken, User])
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, {"code": "access_token_not_found"}
        )
    token, user, organization = row
    if token.revoked_at is not None:
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "access_token_revoked"}
        )
    if token.consumed_at is not None:
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "access_token_consumed"}
        )
    if token.expires_at <= current:
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "access_token_expired"}
        )
    return ResolvedUserAccess(token=token, user=user, organization=organization)


async def enqueue_user_access_email(
    db: AsyncSession,
    *,
    user: User,
    record: UserAccessToken,
    raw_token: str,
) -> bool:
    """Queue a provider email without persisting its private token in plaintext."""

    settings = get_settings()
    if not settings.integration_credential_encryption_key:
        return False
    connection = (
        await db.execute(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.organization_id == user.organization_id,
                IntegrationConnection.provider.in_(
                    ["google_workspace", "microsoft_365"]
                ),
                IntegrationConnection.status == "connected",
                IntegrationConnection.revoked_at.is_(None),
            )
            .order_by(IntegrationConnection.last_success_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()
    if connection is None:
        return False
    try:
        cipher = IntegrationCipher.from_base64(
            settings.integration_credential_encryption_key,
            key_id=settings.integration_credential_key_id,
        )
        url = access_url(purpose=record.purpose, raw_token=raw_token)
        russian = user.locale == "ru"
        if record.purpose == "invitation":
            subject = "Приглашение в команду" if russian else "Your team invitation"
            body = (
                f"Завершите регистрацию по защищённой ссылке:\n{url}\n"
                if russian
                else f"Complete your account using this secure link:\n{url}\n"
            )
        else:
            subject = "Сброс пароля" if russian else "Reset your password"
            body = (
                f"Задайте новый пароль по защищённой ссылке:\n{url}\n"
                if russian
                else f"Set a new password using this secure link:\n{url}\n"
            )
        await enqueue_job(
            db,
            connection=connection,
            actor_user_id=record.created_by_user_id or user.id,
            operation="email.send",
            idempotency_key=f"user-access:{record.id}",
            references={"user_access_token_id": str(record.id)},
            sensitive_payload={
                "to": user.email,
                "subject": subject,
                "body_text": body,
            },
            cipher=cipher,
        )
    except (IntegrationCryptoError, IntegrationJobError, ValueError):
        return False
    return True


def set_user_password(user: User, password: str, *, activate: bool) -> None:
    user.password_hash = hash_password(password)
    if activate:
        user.is_active = True
    user.token_version += 1
