from __future__ import annotations

import logging
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import (
    AuthenticatedRequest,
    authenticated_request,
    current_user,
)
from app.auth.jwt import (
    MfaChallengePurpose,
    decode_mfa_challenge,
    encode_access_token,
    encode_mfa_challenge,
)
from app.auth.mfa import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    match_totp,
    provisioning_uri,
    recovery_code_digest,
)
from app.auth.passwords import verify_password
from app.auth.refresh import (
    InvalidRefreshTokenError,
    IssuedRefreshToken,
    create_refresh_session,
    revoke_all_refresh_sessions,
    revoke_refresh_session,
    rotate_refresh_token,
)
from app.auth.security import (
    LoginThrottledError,
    ThrottleKey,
    clear_login_failures,
    ensure_login_allowed,
    record_login_failure,
    revoke_access_token,
)
from app.config import get_settings
from app.db import (
    bootstrap_user_by_email,
    bootstrap_user_by_id,
    forced_tenant_rls,
    get_session,
    set_tenant_context,
)
from app.models.organization import Organization
from app.models.user import (
    AuthRefreshSession,
    User,
    UserAccessToken,
    UserMfaRecoveryCode,
)
from app.schemas.auth import (
    InvitationAcceptRequest,
    LoginRequest,
    LoginResponse,
    MeOut,
    MfaConfirmRequest,
    MfaConfirmResponse,
    MfaDisableRequest,
    MfaSetupRequest,
    MfaSetupResponse,
    MfaVerifyRequest,
    OrganizationOut,
    PasswordResetAcceptedOut,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RefreshRequest,
    RefreshSessionOut,
    UserAccessPreviewOut,
)
from app.schemas.pagination import CursorPage
from app.services.entitlements import record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.user_access import (
    PASSWORD_RESET_LIFETIME,
    create_user_access_token,
    enqueue_user_access_email,
    resolve_user_access_token,
    set_user_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# A real bcrypt hash keeps unknown-account work comparable to wrong-password
# work without allocating a new hash (and random salt) for each request.
_DUMMY_PASSWORD_HASH = "$2b$12$pnbF2WazHp/Lxr/T19uk6.RTl8tSAcyEcXv3mjJyldbfPp2/ocete"
_GENERIC_AUTH_DETAIL = "Invalid authentication credentials"


def _network_identifier(request: Request) -> str:
    # Do not trust forwarding headers here. Deployments must configure the ASGI
    # server's trusted-proxy handling before a forwarded client IP is accepted.
    return request.client.host if request.client is not None else "unknown"


def _generic_auth_error() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, _GENERIC_AUTH_DETAIL)


def _throttled_error(retry_after: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Authentication temporarily unavailable",
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _private_capability_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"


async def _begin_auth_attempt(
    db: AsyncSession,
    *,
    email: str,
    request: Request,
) -> tuple[ThrottleKey, ThrottleKey]:
    try:
        return await ensure_login_allowed(
            db,
            email=email,
            network=_network_identifier(request),
        )
    except LoginThrottledError as exc:
        await db.rollback()
        raise _throttled_error(exc.retry_after_seconds) from exc


async def _reject_auth_attempt(
    db: AsyncSession,
    keys: tuple[ThrottleKey, ...],
) -> None:
    retry_after = await record_login_failure(db, keys)
    if retry_after is not None:
        raise _throttled_error(retry_after)
    raise _generic_auth_error()


async def _organization(db: AsyncSession, user: User) -> Organization:
    return (
        await db.execute(
            select(Organization).where(Organization.id == user.organization_id)
        )
    ).scalar_one()


def _session_response(
    user: User,
    org: Organization,
    refresh: IssuedRefreshToken,
) -> LoginResponse:
    return LoginResponse(
        access_token=encode_access_token(
            user.id,
            user.role,
            user.locale,
            token_version=user.token_version,
            mfa_verified=user.role == "admin",
            session_id=refresh.session_id,
        ),
        refresh_token=refresh.token,
        expires_in=get_settings().jwt_expires_minutes * 60,
        user_id=user.id,
        role=user.role,
        locale=user.locale,
        organization=OrganizationOut.model_validate(org),
    )


def _challenge_response(
    user: User,
    org: Organization,
    *,
    purpose: MfaChallengePurpose,
) -> LoginResponse:
    return LoginResponse(
        challenge_token=encode_mfa_challenge(
            user.id,
            user.token_version,
            purpose,
        ),
        mfa_required=purpose == "login",
        mfa_setup_required=purpose == "setup",
        user_id=user.id,
        role=user.role,
        locale=user.locale,
        organization=OrganizationOut.model_validate(org),
    )


async def _challenge_user(
    db: AsyncSession,
    token: str,
    *,
    purpose: MfaChallengePurpose,
    request: Request,
) -> tuple[User, tuple[ThrottleKey, ThrottleKey]]:
    try:
        claims = decode_mfa_challenge(token, purpose=purpose)
        user_id = UUID(str(claims["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise _generic_auth_error() from exc
    if forced_tenant_rls():
        bootstrap = await bootstrap_user_by_id(db, user_id)
        if bootstrap is None:
            raise _generic_auth_error()
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=bootstrap.identity_id,
            identity_kind="user",
        )
        user = (
            await db.execute(
                select(User).where(
                    User.id == bootstrap.identity_id,
                    User.organization_id == bootstrap.organization_id,
                )
            )
        ).scalar_one_or_none()
    else:
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
    if user is None:
        raise _generic_auth_error()
    keys = await _begin_auth_attempt(db, email=user.email, request=request)
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.role != "admin"
        or user.token_version != claims["ver"]
    ):
        await db.rollback()
        raise _generic_auth_error()
    if purpose == "login" and (
        not user.mfa_enabled or user.mfa_secret_ciphertext is None
    ):
        await db.rollback()
        raise _generic_auth_error()
    if purpose == "setup" and user.mfa_enabled:
        await db.rollback()
        raise _generic_auth_error()
    return user, keys


async def _verify_mfa(
    db: AsyncSession,
    user: User,
    *,
    code: str | None,
    recovery_code: str | None,
) -> bool:
    if code is not None:
        if user.mfa_secret_ciphertext is None:
            return False
        try:
            secret = decrypt_totp_secret(user.mfa_secret_ciphertext)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "MFA configuration is unavailable",
            ) from exc
        matched_step = match_totp(
            secret,
            code,
            last_used_step=user.mfa_last_used_step,
        )
        if matched_step is None:
            return False
        user.mfa_last_used_step = matched_step
        return True

    if recovery_code is None:
        return False
    digest = recovery_code_digest(recovery_code)
    recovery = (
        await db.execute(
            select(UserMfaRecoveryCode)
            .where(
                UserMfaRecoveryCode.user_id == user.id,
                UserMfaRecoveryCode.code_digest == digest,
                UserMfaRecoveryCode.used_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if recovery is None:
        return False
    recovery.used_at = datetime.now(UTC)
    # A recovery event is higher risk than ordinary TOTP and revokes all other
    # sessions before the replacement session is issued.
    await revoke_all_refresh_sessions(
        db,
        user_id=user.id,
        reason="mfa_recovery_used",
    )
    user.token_version += 1
    return True


def _user_access_preview(resolved) -> dict:
    return {
        "purpose": resolved.token.purpose,
        "email": resolved.user.email,
        "role": resolved.user.role,
        "locale": resolved.user.locale,
        "organization_name": resolved.organization.name,
        "expires_at": resolved.token.expires_at,
    }


@router.get("/invitations/{token}", response_model=UserAccessPreviewOut)
async def preview_user_invitation(
    token: str,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> UserAccessPreviewOut:
    _private_capability_headers(response)
    resolved = await resolve_user_access_token(
        db,
        raw_token=token,
        purpose="invitation",
    )
    if resolved.user.is_active:
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "invitation_already_accepted"}
        )
    return UserAccessPreviewOut(**_user_access_preview(resolved))


@router.post("/invitations/{token}/accept", status_code=status.HTTP_204_NO_CONTENT)
async def accept_user_invitation(
    token: str,
    body: InvitationAcceptRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> None:
    _private_capability_headers(response)
    resolved = await resolve_user_access_token(
        db,
        raw_token=token,
        purpose="invitation",
        lock=True,
    )
    if resolved.user.is_active:
        await db.rollback()
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "invitation_already_accepted"}
        )
    await record_usage(
        db,
        organization_id=resolved.user.organization_id,
        metric="seats",
        amount=1,
        kind="usage",
        idempotency_key=f"seat:invitation:{resolved.token.id}",
        resource_type="user",
        resource_id=resolved.user.id,
    )
    await revoke_all_refresh_sessions(
        db,
        user_id=resolved.user.id,
        reason="invitation_accepted",
    )
    set_user_password(resolved.user, body.password, activate=True)
    resolved.token.consumed_at = datetime.now(UTC)
    await db.commit()


@router.post(
    "/password-reset/request",
    response_model=PasswordResetAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> PasswordResetAcceptedOut:
    """Return the same accepted response for every address and provider state."""

    _private_capability_headers(response)
    email = body.email.lower()
    try:
        keys = await ensure_login_allowed(
            db,
            email=f"password-reset:{email}",
            network=f"password-reset:{_network_identifier(request)}",
        )
    except LoginThrottledError as exc:
        await db.rollback()
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        return PasswordResetAcceptedOut()
    retry_after = await record_login_failure(db, keys)
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
        return PasswordResetAcceptedOut()

    # Keep known and unknown-account work in the same expensive password-hash
    # class before the tenant lookup, reducing practical timing enumeration.
    verify_password("password-reset-dummy", _DUMMY_PASSWORD_HASH)

    if forced_tenant_rls():
        bootstrap = await bootstrap_user_by_email(db, email)
        if bootstrap is None:
            return PasswordResetAcceptedOut()
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=bootstrap.identity_id,
            identity_kind="public",
        )
        user = (
            await db.execute(
                select(User).where(
                    User.id == bootstrap.identity_id,
                    User.organization_id == bootstrap.organization_id,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    else:
        user = (
            await db.execute(
                select(User).where(
                    User.email == email,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    if user is None:
        return PasswordResetAcceptedOut()
    try:
        record, raw_token = await create_user_access_token(
            db,
            user=user,
            purpose="password_reset",
            created_by_user_id=None,
            lifetime=PASSWORD_RESET_LIFETIME,
        )
        if not await enqueue_user_access_email(
            db,
            user=user,
            record=record,
            raw_token=raw_token,
        ):
            # Do not leave a usable but undeliverable credential behind. Private
            # pilot administrators can still generate a copyable assisted reset.
            record.revoked_at = datetime.now(UTC)
        await db.commit()
    except Exception as exc:
        # Delivery/token persistence failures must not turn the generic public
        # endpoint into an account-existence oracle. Do not include the email,
        # raw token, provider response, or exception message in general logs.
        await db.rollback()
        logger.warning(
            "Password reset fulfillment failed",
            extra={"error_type": type(exc).__name__},
        )
    return PasswordResetAcceptedOut()


@router.get("/password-reset/{token}", response_model=UserAccessPreviewOut)
async def preview_password_reset(
    token: str,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> UserAccessPreviewOut:
    _private_capability_headers(response)
    resolved = await resolve_user_access_token(
        db,
        raw_token=token,
        purpose="password_reset",
    )
    if not resolved.user.is_active:
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "password_reset_unavailable"}
        )
    return UserAccessPreviewOut(**_user_access_preview(resolved))


@router.post("/password-reset/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    token: str,
    body: PasswordResetConfirmRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> None:
    _private_capability_headers(response)
    resolved = await resolve_user_access_token(
        db,
        raw_token=token,
        purpose="password_reset",
        lock=True,
    )
    if not resolved.user.is_active:
        await db.rollback()
        raise HTTPException(
            status.HTTP_410_GONE, {"code": "password_reset_unavailable"}
        )
    await revoke_all_refresh_sessions(
        db,
        user_id=resolved.user.id,
        reason="password_reset",
    )
    set_user_password(resolved.user, body.password, activate=False)
    resolved.token.consumed_at = datetime.now(UTC)
    # Any other reset/invitation capability becomes invalid after credential
    # recovery, preventing an older link from undoing the new password.
    await db.execute(
        update(UserAccessToken)
        .where(
            UserAccessToken.organization_id == resolved.user.organization_id,
            UserAccessToken.user_id == resolved.user.id,
            UserAccessToken.id != resolved.token.id,
            UserAccessToken.consumed_at.is_(None),
            UserAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> LoginResponse:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    email = body.email.lower()
    keys = await _begin_auth_attempt(db, email=email, request=request)
    if forced_tenant_rls():
        bootstrap = await bootstrap_user_by_email(db, email)
        if bootstrap is not None:
            await set_tenant_context(
                db,
                organization_id=bootstrap.organization_id,
                actor_id=bootstrap.identity_id,
                identity_kind="user",
            )
            user = (
                await db.execute(
                    select(User).where(
                        User.id == bootstrap.identity_id,
                        User.organization_id == bootstrap.organization_id,
                    )
                )
            ).scalar_one_or_none()
        else:
            user = None
    else:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
    password_matches = verify_password(
        body.password,
        user.password_hash if user is not None else _DUMMY_PASSWORD_HASH,
    )
    if user is None or not password_matches or not user.is_active:
        await _reject_auth_attempt(db, keys)

    org = await _organization(db, user)
    try:
        loopback_request = ip_address(_network_identifier(request)).is_loopback
    except ValueError:
        loopback_request = False
    local_preview_password_only = (
        user.role == "admin"
        and user.mfa_enabled
        and loopback_request
        and get_settings().auth_local_preview_admin_email == email
    )
    if local_preview_password_only:
        logger.warning(
            "Local preview administrator signed in with the explicit "
            "password-only override",
            extra={
                "user_id": str(user.id),
                "organization_id": str(user.organization_id),
            },
        )
        await clear_login_failures(db, keys)
        refresh = await create_refresh_session(db, user)
        await db.commit()
        return _session_response(user, org, refresh)
    if user.role == "admin":
        purpose: MfaChallengePurpose = "login" if user.mfa_enabled else "setup"
        if purpose == "login" and user.mfa_secret_ciphertext is None:
            await _reject_auth_attempt(db, keys)
        if purpose == "setup":
            # A new password-authenticated ceremony invalidates an abandoned
            # pending secret so setup can safely return a fresh one exactly once.
            user.mfa_pending_secret_ciphertext = None
        await db.commit()
        return _challenge_response(user, org, purpose=purpose)

    await clear_login_failures(db, keys)
    refresh = await create_refresh_session(db, user)
    await db.commit()
    return _session_response(user, org, refresh)


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def setup_mfa(
    body: MfaSetupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> MfaSetupResponse:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    user, _keys = await _challenge_user(
        db, body.challenge_token, purpose="setup", request=request
    )
    if user.mfa_pending_secret_ciphertext is not None:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "MFA setup has already been initialized; sign in to restart setup",
        )
    secret = generate_totp_secret()
    user.mfa_pending_secret_ciphertext = encrypt_totp_secret(secret)
    await db.commit()
    return MfaSetupResponse(
        secret=secret,
        provisioning_uri=provisioning_uri(secret=secret, email=user.email),
    )


@router.post("/mfa/confirm", response_model=MfaConfirmResponse)
async def confirm_mfa(
    body: MfaConfirmRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> MfaConfirmResponse:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    user, keys = await _challenge_user(
        db, body.challenge_token, purpose="setup", request=request
    )
    pending = user.mfa_pending_secret_ciphertext
    if pending is None:
        await _reject_auth_attempt(db, keys)
    try:
        secret = decrypt_totp_secret(pending)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "MFA configuration is unavailable",
        ) from exc
    matched_step = match_totp(secret, body.code)
    if matched_step is None:
        await _reject_auth_attempt(db, keys)

    recovery_codes = generate_recovery_codes()
    await db.execute(
        delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id)
    )
    db.add_all(
        UserMfaRecoveryCode(
            organization_id=user.organization_id,
            user_id=user.id,
            code_digest=recovery_code_digest(code),
        )
        for code in recovery_codes
    )
    user.mfa_secret_ciphertext = pending
    user.mfa_pending_secret_ciphertext = None
    user.mfa_enabled = True
    user.mfa_last_used_step = matched_step
    await revoke_all_refresh_sessions(
        db,
        user_id=user.id,
        reason="mfa_enrolled",
    )
    user.token_version += 1
    await clear_login_failures(db, keys)
    org = await _organization(db, user)
    refresh = await create_refresh_session(db, user)
    await db.commit()
    session = _session_response(user, org, refresh)
    return MfaConfirmResponse(
        **session.model_dump(),
        recovery_codes=recovery_codes,
    )


@router.post("/mfa/verify", response_model=LoginResponse)
async def verify_mfa(
    body: MfaVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> LoginResponse:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    user, keys = await _challenge_user(
        db, body.challenge_token, purpose="login", request=request
    )
    if not await _verify_mfa(
        db,
        user,
        code=body.code,
        recovery_code=body.recovery_code,
    ):
        await _reject_auth_attempt(db, keys)
    await clear_login_failures(db, keys)
    org = await _organization(db, user)
    refresh = await create_refresh_session(db, user)
    await db.commit()
    return _session_response(user, org, refresh)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_mfa(
    body: MfaDisableRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    authenticated: AuthenticatedRequest = Depends(authenticated_request),
) -> None:
    keys = await _begin_auth_attempt(
        db, email=authenticated.user.email, request=request
    )
    user = (
        await db.execute(
            select(User)
            .where(User.id == authenticated.user.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one()
    if user.role != "admin" or not user.mfa_enabled:
        await db.rollback()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "MFA is not enabled")
    if not verify_password(body.current_password, user.password_hash):
        await _reject_auth_attempt(db, keys)
    if not await _verify_mfa(
        db,
        user,
        code=body.code,
        recovery_code=body.recovery_code,
    ):
        await _reject_auth_attempt(db, keys)

    user.mfa_enabled = False
    user.mfa_secret_ciphertext = None
    user.mfa_pending_secret_ciphertext = None
    user.mfa_last_used_step = None
    await revoke_all_refresh_sessions(
        db,
        user_id=user.id,
        reason="mfa_disabled",
    )
    user.token_version += 1
    await db.execute(
        delete(UserMfaRecoveryCode).where(UserMfaRecoveryCode.user_id == user.id)
    )
    await clear_login_failures(db, keys)
    await db.commit()


@router.post("/refresh", response_model=LoginResponse)
async def refresh_session(
    body: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> LoginResponse:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    try:
        user, refresh = await rotate_refresh_token(db, body.refresh_token)
    except InvalidRefreshTokenError as exc:
        # Reuse and expiry detection may have revoked durable state. Commit that
        # state before returning the same generic response for every failure.
        await db.commit()
        raise _generic_auth_error() from exc
    org = await _organization(db, user)
    await db.commit()
    return _session_response(user, org, refresh)


@router.get("/sessions", response_model=CursorPage[RefreshSessionOut])
async def list_refresh_sessions(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    authenticated: AuthenticatedRequest = Depends(authenticated_request),
    db: AsyncSession = Depends(get_session),
) -> CursorPage[RefreshSessionOut]:
    current = datetime.now(UTC)
    namespace = "auth.sessions"
    scope = collection_cursor_scope(
        namespace,
        organization_id=authenticated.user.organization_id,
        filters={
            "user_id": str(authenticated.user.id),
            "token_version": authenticated.user.token_version,
        },
    )
    stmt = select(AuthRefreshSession).where(
        AuthRefreshSession.user_id == authenticated.user.id,
        AuthRefreshSession.token_version == authenticated.user.token_version,
        AuthRefreshSession.revoked_at.is_(None),
        AuthRefreshSession.expires_at > current,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid sessions cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid sessions cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid sessions cursor")
        stmt = stmt.where(
            or_(
                AuthRefreshSession.created_at < cursor_time,
                and_(
                    AuthRefreshSession.created_at == cursor_time,
                    AuthRefreshSession.id < cursor_id,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(
                AuthRefreshSession.created_at.desc(), AuthRefreshSession.id.desc()
            ).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    current_session_id = (
        UUID(str(authenticated.claims["sid"]))
        if authenticated.claims["sid"] is not None
        else None
    )
    items = [
        RefreshSessionOut(
            id=row.id,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            current=row.id == current_session_id,
        )
        for row in rows
    ]
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": rows[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(rows[-1].id),
                },
            )
            if has_more and rows
            else None
        ),
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: UUID,
    authenticated: AuthenticatedRequest = Depends(authenticated_request),
    db: AsyncSession = Depends(get_session),
) -> None:
    user = (
        await db.execute(
            select(User)
            .where(User.id == authenticated.user.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one()
    if not await revoke_refresh_session(
        db,
        user_id=user.id,
        session_id=session_id,
        reason="session_revoked",
    ):
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    if authenticated.claims["sid"] == str(session_id):
        await revoke_access_token(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            jti=str(authenticated.claims["jti"]),
            expires_at=datetime.fromtimestamp(int(authenticated.claims["exp"]), UTC),
        )
    await db.commit()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    authenticated: AuthenticatedRequest = Depends(authenticated_request),
    db: AsyncSession = Depends(get_session),
) -> None:
    claims = authenticated.claims
    if claims["sid"] is not None:
        user = (
            await db.execute(
                select(User)
                .where(User.id == authenticated.user.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one()
        await revoke_refresh_session(
            db,
            user_id=user.id,
            session_id=UUID(str(claims["sid"])),
            reason="logout",
        )
    await revoke_access_token(
        db,
        organization_id=authenticated.user.organization_id,
        user_id=authenticated.user.id,
        jti=str(claims["jti"]),
        expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
    )
    await db.commit()


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    authenticated: AuthenticatedRequest = Depends(authenticated_request),
    db: AsyncSession = Depends(get_session),
) -> None:
    user = (
        await db.execute(
            select(User)
            .where(User.id == authenticated.user.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one()
    await revoke_all_refresh_sessions(
        db,
        user_id=user.id,
        reason="logout_all",
    )
    user.token_version += 1
    await db.commit()


@router.get("/me", response_model=MeOut)
async def me(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
) -> MeOut:
    org = await _organization(db, user)
    return MeOut(
        id=user.id,
        email=user.email,
        role=user.role,
        locale=user.locale,
        is_active=user.is_active,
        mfa_enabled=user.mfa_enabled,
        organization_id=user.organization_id,
        created_at=user.created_at,
        organization=OrganizationOut.model_validate(org),
    )
