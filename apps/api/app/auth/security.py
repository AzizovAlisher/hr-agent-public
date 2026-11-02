"""Durable authentication throttling and token-revocation helpers."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import AuthLoginThrottle, RevokedAccessToken


@dataclass(frozen=True)
class ThrottleKey:
    scope: str
    digest: str


class LoginThrottledError(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("authentication throttled")
        self.retry_after_seconds = max(1, retry_after_seconds)


def _authentication_hmac_key() -> bytes:
    settings = get_settings()
    material = settings.auth_mfa_encryption_key or settings.jwt_secret
    return hashlib.sha256(
        b"hr-agent/auth-identifiers/v1\0" + material.encode("utf-8")
    ).digest()


def _identifier_digest(scope: str, identifier: str) -> str:
    normalized = identifier.strip().casefold()
    return hmac.new(
        _authentication_hmac_key(),
        f"{scope}\0{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def throttle_keys(*, email: str, network: str) -> tuple[ThrottleKey, ThrottleKey]:
    return (
        ThrottleKey("account", _identifier_digest("account", email)),
        ThrottleKey("network", _identifier_digest("network", network)),
    )


async def _lock_throttle_keys(
    db: AsyncSession,
    keys: tuple[ThrottleKey, ...],
) -> None:
    # Stable ordering prevents deadlocks when concurrent attempts share only
    # the network or only the account identifier.
    for key in sorted(keys, key=lambda item: (item.scope, item.digest)):
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"auth-throttle:{key.scope}:{key.digest}", 0
                    )
                )
            )
        )


async def _load_throttle_rows(
    db: AsyncSession,
    keys: tuple[ThrottleKey, ...],
) -> dict[ThrottleKey, AuthLoginThrottle]:
    rows: dict[ThrottleKey, AuthLoginThrottle] = {}
    for key in keys:
        row = (
            await db.execute(
                select(AuthLoginThrottle).where(
                    AuthLoginThrottle.scope == key.scope,
                    AuthLoginThrottle.identifier_digest == key.digest,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            rows[key] = row
    return rows


async def ensure_login_allowed(
    db: AsyncSession,
    *,
    email: str,
    network: str,
    now: datetime | None = None,
) -> tuple[ThrottleKey, ThrottleKey]:
    keys = throttle_keys(email=email, network=network)
    await _lock_throttle_keys(db, keys)
    rows = await _load_throttle_rows(db, keys)
    current = now or datetime.now(UTC)
    remaining = [
        int((row.blocked_until - current).total_seconds()) + 1
        for row in rows.values()
        if row.blocked_until is not None and row.blocked_until > current
    ]
    if remaining:
        raise LoginThrottledError(max(remaining))
    return keys


async def record_login_failure(
    db: AsyncSession,
    keys: tuple[ThrottleKey, ...],
    *,
    now: datetime | None = None,
) -> int | None:
    settings = get_settings()
    current = now or datetime.now(UTC)
    rows = await _load_throttle_rows(db, keys)
    blocked_until: list[datetime] = []
    for key in keys:
        row = rows.get(key)
        if row is None:
            row = AuthLoginThrottle(
                scope=key.scope,
                identifier_digest=key.digest,
                failed_attempts=0,
                window_started_at=current,
            )
            db.add(row)
        elif (
            current - row.window_started_at
            >= timedelta(seconds=settings.auth_login_window_seconds)
        ):
            row.failed_attempts = 0
            row.window_started_at = current
            row.blocked_until = None

        row.failed_attempts += 1
        if row.failed_attempts >= settings.auth_login_max_failures:
            row.blocked_until = current + timedelta(
                seconds=settings.auth_login_lockout_seconds
            )
        if row.blocked_until is not None and row.blocked_until > current:
            blocked_until.append(row.blocked_until)

    await db.commit()
    if not blocked_until:
        return None
    return max(
        1,
        max(int((blocked - current).total_seconds()) + 1 for blocked in blocked_until),
    )


async def clear_login_failures(
    db: AsyncSession,
    keys: tuple[ThrottleKey, ...],
) -> None:
    for key in keys:
        await db.execute(
            delete(AuthLoginThrottle).where(
                AuthLoginThrottle.scope == key.scope,
                AuthLoginThrottle.identifier_digest == key.digest,
            )
        )


def access_token_jti_digest(jti: str) -> str:
    return hashlib.sha256(jti.encode("ascii", "strict")).hexdigest()


async def revoke_access_token(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    jti: str,
    expires_at: datetime,
) -> None:
    await db.execute(
        pg_insert(RevokedAccessToken)
        .values(
            organization_id=organization_id,
            user_id=user_id,
            jti_digest=access_token_jti_digest(jti),
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["jti_digest"])
    )
