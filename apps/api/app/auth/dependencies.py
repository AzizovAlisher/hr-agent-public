from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import decode_access_token
from app.auth.authorization import AuthMode, PERMISSIONS, ResourceScope, operation_policy
from app.auth.scope import enforce_hiring_assignment_scope
from app.auth.security import access_token_jti_digest
from app.db import (
    bootstrap_user_by_id,
    forced_tenant_rls,
    get_session,
    set_tenant_context,
)
from app.models.user import AuthRefreshSession, RevokedAccessToken, User
from app.services.enterprise import require_field_access, require_permission

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedRequest:
    user: User
    claims: dict


def _invalid_bearer_token() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticated_request(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_session),
) -> AuthenticatedRequest:
    if creds is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(creds.credentials)
        user_id = UUID(str(payload["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        # Decoder/provider details are server-side diagnostics. Returning them
        # lets malformed tokens probe implementation and configuration state.
        raise _invalid_bearer_token() from exc

    if forced_tenant_rls():
        bootstrap = await bootstrap_user_by_id(db, user_id)
        if bootstrap is None:
            raise _invalid_bearer_token()
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
    if user is None or not user.is_active:
        raise _invalid_bearer_token()
    if payload["ver"] != user.token_version:
        raise _invalid_bearer_token()
    if user.role == "admin" and payload["mfa"] is not True:
        raise _invalid_bearer_token()
    # Rebind with the verified role before any permission or assignment query.
    # The organization came only from the broker claim/database user, never a
    # caller-controlled header, query, body, or JWT organization claim.
    await set_tenant_context(
        db,
        organization_id=user.organization_id,
        actor_id=user.id,
        identity_kind="user",
        role=user.role,
    )
    if payload["sid"] is not None:
        session_id = UUID(str(payload["sid"]))
        active_session = (
            await db.execute(
                select(AuthRefreshSession.id).where(
                    AuthRefreshSession.id == session_id,
                    AuthRefreshSession.user_id == user.id,
                    AuthRefreshSession.token_version == user.token_version,
                    AuthRefreshSession.revoked_at.is_(None),
                    AuthRefreshSession.expires_at > datetime.now(UTC),
                )
            )
        ).scalar_one_or_none()
        if active_session is None:
            raise _invalid_bearer_token()
    revoked = (
        await db.execute(
            select(RevokedAccessToken.id).where(
                RevokedAccessToken.user_id == user.id,
                RevokedAccessToken.jti_digest
                == access_token_jti_digest(str(payload["jti"])),
            )
        )
    ).scalar_one_or_none()
    if revoked is not None:
        raise _invalid_bearer_token()
    return AuthenticatedRequest(user=user, claims=payload)


async def current_user(
    request: Request,
    authenticated: AuthenticatedRequest = Depends(authenticated_request),
    db: AsyncSession = Depends(get_session),
) -> User:
    route = request.scope.get("route")
    path_template = getattr(route, "path", None)
    policy = (
        operation_policy(request.method, path_template)
        if isinstance(path_template, str)
        else None
    )
    if policy is None or policy.auth_mode is not AuthMode.BEARER:
        # Configuration defects are denied at the boundary; the route-catalog
        # coverage test makes them release-blocking rather than discoverable by
        # an attacker in production.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Operation is not authorized by policy",
        )
    user = authenticated.user
    for permission in policy.actions:
        await require_permission(db, user, permission)
    for field_class in policy.fields:
        await require_field_access(db, user, field_class)
    if policy.scope is ResourceScope.HIRING_ASSIGNMENT:
        await enforce_hiring_assignment_scope(db, user, request)
    return user


PermissionDependency = Callable[..., Awaitable[User]]


def user_with_permissions(*permissions: str) -> PermissionDependency:
    """Return a FastAPI dependency requiring every named permission.

    Route modules bind the returned callable once at import time and use it in
    place of ``current_user``. Centralizing the check makes permission
    overrides effective at the actual HTTP boundary instead of relying on UI
    visibility or ad-hoc checks inside individual handlers.
    """

    if not permissions:
        raise ValueError("At least one permission is required")
    unknown = set(permissions) - PERMISSIONS
    if unknown:
        raise ValueError(f"Unknown permissions: {', '.join(sorted(unknown))}")

    async def dependency(
        user: User = Depends(current_user),
        db: AsyncSession = Depends(get_session),
    ) -> User:
        for permission in permissions:
            await require_permission(db, user, permission)
        return user

    dependency.__name__ = "require_" + "_and_".join(
        permission.replace(".", "_") for permission in permissions
    )
    return dependency
