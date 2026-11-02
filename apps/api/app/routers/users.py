"""User provisioning, invitations, account lifecycle, and self-service profile."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import ROLES
from app.auth.dependencies import current_user
from app.auth.passwords import hash_password, verify_password
from app.auth.refresh import revoke_all_refresh_sessions
from app.db import get_session
from app.models.user import User, UserAccessToken, UserMfaRecoveryCode
from app.schemas.auth import (
    AssignableUserOut,
    PasswordChangeRequest,
    UserAccessCreatedOut,
    UserCreate,
    UserInvitationCreate,
    UserOut,
    UserUpdate,
)
from app.schemas.pagination import CursorPage
from app.services.enterprise import has_permission, require_permission
from app.services.entitlements import ensure_entitlement_capacity, record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.user_access import (
    access_url,
    create_user_access_token,
    enqueue_user_access_email,
)

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(current_user)])


async def _require_user_management(db: AsyncSession, user: User) -> None:
    await require_permission(db, user, "enterprise.manage")


async def _protect_last_admin(db: AsyncSession, user: User) -> None:
    """Serialize admin-count changes and preserve one admin per organization."""

    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"organization-admins:{user.organization_id}", 0)
            )
        )
    )
    count = int(
        (
            await db.execute(
                select(func.count(User.id)).where(
                    User.organization_id == user.organization_id,
                    User.role == "admin",
                    User.is_active.is_(True),
                )
            )
        ).scalar_one()
    )
    if count <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The organization must retain at least one administrator.",
        )


@router.get("/assignable", response_model=CursorPage[AssignableUserOut])
async def list_assignable_users(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[AssignableUserOut]:
    """Minimal organization directory available to every signed-in teammate."""
    namespace = "users.assignable"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"active": True},
    )
    stmt = select(User).where(
        User.organization_id == user.organization_id,
        User.is_active.is_(True),
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"email", "id"},
            detail="Invalid assignable users cursor",
        )
        cursor_id = cursor_uuid(payload, detail="Invalid assignable users cursor")
        cursor_email = payload["email"]
        if not isinstance(cursor_email, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid assignable users cursor"},
            )
        stmt = stmt.where(
            or_(
                User.email > cursor_email,
                and_(User.email == cursor_email, User.id > cursor_id),
            )
        )
    rows = (
        await db.execute(stmt.order_by(User.email.asc(), User.id.asc()).limit(limit + 1))
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[AssignableUserOut(id=row.id, email=row.email, role=row.role) for row in rows],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"email": rows[-1].email, "id": str(rows[-1].id)},
            )
            if has_more and rows
            else None
        ),
    )


@router.get("", response_model=CursorPage[UserOut])
async def list_users(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[UserOut]:
    await _require_user_management(db, user)
    namespace = "users.directory"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    stmt = select(User).where(User.organization_id == user.organization_id)
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid users cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid users cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid users cursor")
        stmt = stmt.where(
            or_(
                User.created_at > cursor_time,
                and_(User.created_at == cursor_time, User.id > cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(User.created_at.asc(), User.id.asc()).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[UserOut.model_validate(row) for row in rows],
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


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> UserOut:
    await _require_user_management(db, user)
    if body.role not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role.")
    if body.locale not in {"ru", "en"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown locale.")

    # New users always inherit the caller's org; can't create users in other orgs.
    new_user_id = uuid4()
    await record_usage(
        db,
        organization_id=user.organization_id,
        metric="seats",
        amount=1,
        kind="usage",
        idempotency_key=f"seat:create:{new_user_id}",
        resource_type="user",
        resource_id=new_user_id,
    )
    new_user = User(
        id=new_user_id,
        organization_id=user.organization_id,
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        role=body.role,
        locale=body.locale,
    )
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A user with this email already exists."
        )
    await db.refresh(new_user)
    return UserOut.model_validate(new_user)


@router.post(
    "/invitations",
    response_model=UserAccessCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_invitation(
    body: UserInvitationCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> UserAccessCreatedOut:
    """Provision an inactive member and return one copyable invitation URL."""

    await _require_user_management(db, user)
    if body.role not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role.")
    if body.locale not in {"ru", "en"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown locale.")
    email = body.email.lower()
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"user-invitation:{email}", 0)
            )
        )
    )
    # Invitations remain inactive and therefore do not consume a seat.  A
    # serialized preflight avoids offering an unusable invitation when the
    # organization is already full; acceptance performs the metered change.
    await ensure_entitlement_capacity(
        db,
        organization_id=user.organization_id,
        metric="seats",
    )
    invited = (
        await db.execute(
            select(User)
            .where(
                User.organization_id == user.organization_id,
                func.lower(User.email) == email,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if invited is not None and invited.is_active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Unable to invite this address.",
        )
    if invited is None:
        invited = User(
            organization_id=user.organization_id,
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(48)),
            role=body.role,
            locale=body.locale,
            is_active=False,
        )
        db.add(invited)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Unable to invite this address.",
            ) from exc
    else:
        invited.role = body.role
        invited.locale = body.locale

    record, raw_token = await create_user_access_token(
        db,
        user=invited,
        purpose="invitation",
        created_by_user_id=user.id,
        lifetime=timedelta(days=body.expires_in_days),
    )
    delivery_enqueued = await enqueue_user_access_email(
        db,
        user=invited,
        record=record,
        raw_token=raw_token,
    )
    await db.commit()
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Referrer-Policy"] = "no-referrer"
    return UserAccessCreatedOut(
        id=record.id,
        user_id=invited.id,
        purpose=record.purpose,
        token_prefix=record.token_prefix,
        expires_at=record.expires_at,
        access_url=access_url(purpose=record.purpose, raw_token=raw_token),
        delivery_enqueued=delivery_enqueued,
    )


@router.post(
    "/{user_id}/password-reset",
    response_model=UserAccessCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_password_reset(
    user_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> UserAccessCreatedOut:
    """Create an admin-assisted reset URL when email delivery is unavailable."""

    await _require_user_management(db, user)
    target = (
        await db.execute(
            select(User)
            .where(
                User.id == user_id,
                User.organization_id == user.organization_id,
                User.is_active.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    record, raw_token = await create_user_access_token(
        db,
        user=target,
        purpose="password_reset",
        created_by_user_id=user.id,
        lifetime=timedelta(hours=1),
    )
    delivery_enqueued = await enqueue_user_access_email(
        db,
        user=target,
        record=record,
        raw_token=raw_token,
    )
    await db.commit()
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Referrer-Policy"] = "no-referrer"
    return UserAccessCreatedOut(
        id=record.id,
        user_id=target.id,
        purpose=record.purpose,
        token_prefix=record.token_prefix,
        expires_at=record.expires_at,
        access_url=access_url(purpose=record.purpose, raw_token=raw_token),
        delivery_enqueued=delivery_enqueued,
    )


@router.post(
    "/access-tokens/{token_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_user_access_token(
    token_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await _require_user_management(db, user)
    revoked = await db.execute(
        update(UserAccessToken)
        .where(
            UserAccessToken.id == token_id,
            UserAccessToken.organization_id == user.organization_id,
            UserAccessToken.consumed_at.is_(None),
            UserAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
        .returning(UserAccessToken.id)
    )
    if revoked.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Access token not found.")
    await db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await _require_user_management(db, user)
    if user_id == user.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot delete your own account."
        )
    target = (
        await db.execute(
            select(User)
            .where(
                User.id == user_id,
                User.organization_id == user.organization_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if target.role == "admin" and target.is_active:
        await _protect_last_admin(db, user)
    if target.is_active:
        await record_usage(
            db,
            organization_id=user.organization_id,
            metric="seats",
            amount=-1,
            kind="refund",
            idempotency_key=f"seat:delete:{target.id}",
            resource_type="user",
            resource_id=target.id,
            enforce_limit=False,
        )
    await db.delete(target)
    await db.commit()


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: UUID,
    body: UserUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> UserOut:
    """Admin can change any user's role/locale; users can change their own locale only."""
    target = (
        await db.execute(
            select(User)
            .where(
                User.id == user_id,
                User.organization_id == user.organization_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")

    can_manage_users = await has_permission(db, user, "enterprise.manage")
    if not can_manage_users and target.id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden.")
    if body.role is not None:
        if not can_manage_users:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only admins can change roles."
            )
        if body.role not in ROLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown role.")
    if body.locale is not None and body.locale not in {"ru", "en"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown locale.")
    if body.is_active is not None:
        if not can_manage_users:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only admins can change account status."
            )
        if target.id == user.id and body.is_active is False:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "You cannot disable your own account."
            )

    role_changes = body.role is not None and body.role != target.role
    active_changes = body.is_active is not None and body.is_active != target.is_active
    removes_active_admin = (
        target.role == "admin"
        and target.is_active
        and (
            (role_changes and body.role != "admin")
            or (active_changes and body.is_active is False)
        )
    )
    if removes_active_admin:
        await _protect_last_admin(db, user)

    if active_changes:
        activating = body.is_active is True
        await record_usage(
            db,
            organization_id=user.organization_id,
            metric="seats",
            amount=1 if activating else -1,
            kind="usage" if activating else "refund",
            idempotency_key=(
                f"seat:{'activate' if activating else 'deactivate'}:"
                f"{target.id}:v{target.token_version + 1}"
            ),
            resource_type="user",
            resource_id=target.id,
            enforce_limit=activating,
        )

    if body.role is not None:
        target.role = body.role
    if body.locale is not None:
        target.locale = body.locale
    if body.is_active is not None:
        target.is_active = body.is_active

    if role_changes or active_changes:
        await revoke_all_refresh_sessions(
            db,
            user_id=target.id,
            reason="role_or_status_changed",
        )
        target.token_version += 1
    if role_changes and target.role != "admin":
        target.mfa_enabled = False
        target.mfa_secret_ciphertext = None
        target.mfa_pending_secret_ciphertext = None
        target.mfa_last_used_step = None
        await db.execute(
            delete(UserMfaRecoveryCode).where(
                UserMfaRecoveryCode.user_id == target.id
            )
        )

    await db.commit()
    await db.refresh(target)
    return UserOut.model_validate(target)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    target = (
        await db.execute(
            select(User)
            .where(User.id == user.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one()
    if not verify_password(body.current_password, target.password_hash):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Current password is incorrect."
        )
    target.password_hash = hash_password(body.new_password)
    await revoke_all_refresh_sessions(
        db,
        user_id=target.id,
        reason="password_changed",
    )
    target.token_version += 1
    await db.commit()
