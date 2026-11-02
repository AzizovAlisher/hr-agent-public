"""Opaque rotating refresh sessions with family-level reuse detection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import bootstrap_refresh_token, forced_tenant_rls, set_tenant_context
from app.models.user import AuthRefreshSession, AuthRefreshToken, User


class InvalidRefreshTokenError(Exception):
    pass


class RefreshTokenReuseError(InvalidRefreshTokenError):
    pass


@dataclass(frozen=True)
class IssuedRefreshToken:
    session_id: UUID
    token: str = field(repr=False)
    expires_at: datetime


def _refresh_hmac_key() -> bytes:
    settings = get_settings()
    material = settings.auth_mfa_encryption_key or settings.jwt_secret
    return hashlib.sha256(
        b"hr-agent/refresh-token-digest/v1\0" + material.encode("utf-8")
    ).digest()


def refresh_token_digest(token: str) -> str:
    return hmac.new(
        _refresh_hmac_key(),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _new_raw_token() -> str:
    return "hr_rt_" + secrets.token_urlsafe(48)


def _replacement_raw_token(raw_token: str) -> str:
    """Derive one opaque successor without storing recoverable token material.

    A deterministic, domain-separated HMAC lets separate API replicas return
    the same successor during the bounded retry window. Only the keyed digest
    is persisted; callers cannot derive the successor without the server key.
    """

    material = hmac.new(
        _refresh_hmac_key(),
        b"hr-agent/refresh-token-successor/v1\0" + raw_token.encode("utf-8"),
        hashlib.sha384,
    ).digest()
    return "hr_rt_" + base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")


async def create_refresh_session(
    db: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
) -> IssuedRefreshToken:
    current = now or datetime.now(UTC)
    expires_at = current + timedelta(days=get_settings().auth_refresh_expires_days)
    session = AuthRefreshSession(
        organization_id=user.organization_id,
        user_id=user.id,
        token_version=user.token_version,
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()
    raw_token = _new_raw_token()
    db.add(
        AuthRefreshToken(
            organization_id=user.organization_id,
            session_id=session.id,
            token_digest=refresh_token_digest(raw_token),
            expires_at=expires_at,
        )
    )
    await db.flush()
    return IssuedRefreshToken(
        session_id=session.id,
        token=raw_token,
        expires_at=expires_at,
    )


async def revoke_all_refresh_sessions(
    db: AsyncSession,
    *,
    user_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(UTC)
    await db.execute(
        update(AuthRefreshSession)
        .where(
            AuthRefreshSession.user_id == user_id,
            AuthRefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=current, revoke_reason=reason)
    )


async def revoke_refresh_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    reason: str,
    now: datetime | None = None,
) -> bool:
    session = (
        await db.execute(
            select(AuthRefreshSession)
            .where(
                AuthRefreshSession.id == session_id,
                AuthRefreshSession.user_id == user_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        return False
    if session.revoked_at is None:
        session.revoked_at = now or datetime.now(UTC)
        session.revoke_reason = reason
    return True


async def rotate_refresh_token(
    db: AsyncSession,
    raw_token: str,
    *,
    now: datetime | None = None,
) -> tuple[User, IssuedRefreshToken]:
    """Consume one token and issue its successor in the same transaction.

    A near-simultaneous second use caused by two BFF replicas returns the same
    successor during a short bounded grace period. Reuse after that window, or
    after the successor was itself consumed, is treated as theft: every family
    is revoked and the user token version is incremented.
    """

    digest = refresh_token_digest(raw_token)
    current = now or datetime.now(UTC)
    if forced_tenant_rls():
        bootstrap = await bootstrap_refresh_token(
            db,
            digest=digest,
            now=current,
        )
        if bootstrap is None:
            raise InvalidRefreshTokenError()
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=bootstrap.user_id,
            identity_kind="user",
        )
    hint = (
        await db.execute(
            select(AuthRefreshToken.session_id).where(
                AuthRefreshToken.token_digest == digest
            )
        )
    ).scalar_one_or_none()
    if hint is None:
        raise InvalidRefreshTokenError()
    session_hint = await db.get(AuthRefreshSession, hint)
    if session_hint is None:
        raise InvalidRefreshTokenError()

    # All auth mutations take the user lock before the refresh-family lock.
    # This serializes rotation with password/role/status/logout-all changes.
    user = (
        await db.execute(
            select(User)
            .where(User.id == session_hint.user_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    session = (
        await db.execute(
            select(AuthRefreshSession)
            .where(AuthRefreshSession.id == hint)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    token = (
        await db.execute(
            select(AuthRefreshToken)
            .where(AuthRefreshToken.token_digest == digest)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None or session is None or token is None:
        raise InvalidRefreshTokenError()

    if session.revoked_at is not None:
        raise InvalidRefreshTokenError()
    if (
        session.expires_at <= current
        or token.expires_at <= current
    ):
        session.revoked_at = current
        session.revoke_reason = "refresh_expired"
        raise InvalidRefreshTokenError()
    if (
        not user.is_active
        or (user.role == "admin" and not user.mfa_enabled)
        or session.token_version != user.token_version
    ):
        session.revoked_at = current
        session.revoke_reason = "token_version_changed"
        raise InvalidRefreshTokenError()
    if token.consumed_at is not None:
        grace = timedelta(
            seconds=get_settings().auth_refresh_retry_grace_seconds
        )
        replacement = (
            await db.execute(
                select(AuthRefreshToken)
                .where(
                    AuthRefreshToken.id == token.replaced_by_token_id,
                    AuthRefreshToken.session_id == session.id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            replacement is not None
            and current <= token.consumed_at + grace
            and replacement.consumed_at is None
            and replacement.expires_at > current
        ):
            raw_replacement = _replacement_raw_token(raw_token)
            if hmac.compare_digest(
                replacement.token_digest,
                refresh_token_digest(raw_replacement),
            ):
                return user, IssuedRefreshToken(
                    session_id=session.id,
                    token=raw_replacement,
                    expires_at=session.expires_at,
                )
        await revoke_all_refresh_sessions(
            db,
            user_id=user.id,
            reason="refresh_reuse_detected",
            now=current,
        )
        user.token_version += 1
        raise RefreshTokenReuseError()

    token.consumed_at = current
    raw_replacement = _replacement_raw_token(raw_token)
    replacement = AuthRefreshToken(
        organization_id=user.organization_id,
        session_id=session.id,
        token_digest=refresh_token_digest(raw_replacement),
        expires_at=session.expires_at,
    )
    db.add(replacement)
    await db.flush()
    token.replaced_by_token_id = replacement.id
    session.last_used_at = current
    return user, IssuedRefreshToken(
        session_id=session.id,
        token=raw_replacement,
        expires_at=session.expires_at,
    )
