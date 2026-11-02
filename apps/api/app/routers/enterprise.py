"""Enterprise policy, audit, recovery, API, SSO, and model controls."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.db import get_session
from app.models.candidate import Candidate
from app.models.enterprise import (
    AuditLog,
    OrganizationApiKey,
    OrganizationModelConfiguration,
    PermissionOverride,
    SsoConfiguration,
)
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.schemas.enterprise import (
    ApiKeyCreate,
    ApiKeyCreatedOut,
    ApiKeyOut,
    AuditActorOut,
    AuditLogOut,
    AuditLogPage,
    EffectivePermissionsOut,
    EnterpriseApiCandidateOut,
    EnterpriseApiPositionOut,
    EnterprisePolicyOut,
    EnterprisePolicyUpdate,
    ModelConfigurationIn,
    ModelConfigurationOut,
    PermissionOverrideIn,
    PermissionOverrideOut,
    RetentionPreviewOut,
    RetentionRunOut,
    RetentionRunRequest,
    SsoConfigurationIn,
    SsoConfigurationOut,
    TrashItemOut,
    TrashPage,
)
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.enterprise import (
    authenticate_api_key,
    effective_permissions,
    get_or_create_policy,
    new_api_secret,
    require_permission,
)
from app.services.entitlements import record_usage
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.private_object_retention import (
    RetentionStorageError,
    prepare_retention_erasure,
)

router = APIRouter(
    prefix="/enterprise",
    tags=["enterprise"],
    dependencies=[Depends(current_user)],
)
api_router = APIRouter(prefix="/enterprise/api/v1", tags=["enterprise-api"])


async def _require_admin(db: AsyncSession, user: User) -> None:
    """Compatibility name backed by the authoritative permission engine."""

    await require_permission(db, user, "enterprise.manage")


@router.get("/policy", response_model=EnterprisePolicyOut)
async def get_policy(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> EnterprisePolicyOut:
    policy = await get_or_create_policy(db, user.organization_id)
    await db.commit()
    await db.refresh(policy)
    return EnterprisePolicyOut.model_validate(policy)


@router.patch("/policy", response_model=EnterprisePolicyOut)
async def update_policy(
    body: EnterprisePolicyUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> EnterprisePolicyOut:
    await _require_admin(db, user)
    policy = await get_or_create_policy(db, user.organization_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(policy, field, value)
    await db.commit()
    await db.refresh(policy)
    return EnterprisePolicyOut.model_validate(policy)


@router.get("/permissions/me", response_model=EffectivePermissionsOut)
async def my_permissions(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> EffectivePermissionsOut:
    values = await effective_permissions(
        db, organization_id=user.organization_id, role=user.role
    )
    return EffectivePermissionsOut(role=user.role, permissions=values)


@router.get("/permissions", response_model=CursorPage[PermissionOverrideOut])
async def list_permission_overrides(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[PermissionOverrideOut]:
    await _require_admin(db, user)
    namespace = "enterprise.permission-overrides"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    stmt = select(PermissionOverride).where(
        PermissionOverride.organization_id == user.organization_id
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"role", "permission", "id"},
            detail="Invalid permission overrides cursor",
        )
        role = payload["role"]
        permission = payload["permission"]
        if not isinstance(role, str) or not isinstance(permission, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid permission overrides cursor"},
            )
        cursor_id = cursor_uuid(payload, detail="Invalid permission overrides cursor")
        stmt = stmt.where(
            or_(
                PermissionOverride.role > role,
                and_(
                    PermissionOverride.role == role,
                    PermissionOverride.permission > permission,
                ),
                and_(
                    PermissionOverride.role == role,
                    PermissionOverride.permission == permission,
                    PermissionOverride.id > cursor_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    PermissionOverride.role,
                    PermissionOverride.permission,
                    PermissionOverride.id,
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[PermissionOverrideOut.model_validate(row) for row in rows],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "role": rows[-1].role,
                    "permission": rows[-1].permission,
                    "id": str(rows[-1].id),
                },
            )
            if has_more and rows
            else None
        ),
    )


@router.put("/permissions", response_model=PermissionOverrideOut)
async def put_permission_override(
    body: PermissionOverrideIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> PermissionOverrideOut:
    await _require_admin(db, user)
    row = (
        await db.execute(
            select(PermissionOverride).where(
                PermissionOverride.organization_id == user.organization_id,
                PermissionOverride.role == body.role,
                PermissionOverride.permission == body.permission,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PermissionOverride(
            organization_id=user.organization_id,
            role=body.role,
            permission=body.permission,
            allowed=body.allowed,
        )
        db.add(row)
    else:
        row.allowed = body.allowed
    await db.commit()
    await db.refresh(row)
    return PermissionOverrideOut.model_validate(row)


@router.delete("/permissions/{override_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_permission_override(
    override_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await _require_admin(db, user)
    result = await db.execute(
        delete(PermissionOverride).where(
            PermissionOverride.id == override_id,
            PermissionOverride.organization_id == user.organization_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Permission override not found")
    await db.commit()


@router.get("/audit", response_model=AuditLogPage)
async def list_audit_log(
    action: str | None = Query(default=None, max_length=160),
    actor_user_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> AuditLogPage:
    await _require_admin(db, user)
    conditions = [AuditLog.organization_id == user.organization_id]
    if action:
        conditions.append(AuditLog.action == action)
    if actor_user_id:
        conditions.append(AuditLog.actor_user_id == actor_user_id)
    namespace = "enterprise.audit-log"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={
            "action": action,
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
        },
    )
    if cursor:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"occurred_at", "id"},
            detail="Invalid audit cursor",
        )
        cursor_time = cursor_datetime(payload, "occurred_at", detail="Invalid audit cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid audit cursor")
        conditions.append(
            or_(
                AuditLog.occurred_at < cursor_time,
                and_(
                    AuditLog.occurred_at == cursor_time,
                    AuditLog.id < cursor_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
                .limit(limit + 1)
            )
        ).scalars()
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        next_cursor = encode_collection_cursor(
            namespace,
            scope=scope,
            boundary={
                "occurred_at": page[-1].occurred_at.astimezone(UTC).isoformat(),
                "id": str(page[-1].id),
            },
        )
    return AuditLogPage(
        items=[
            AuditLogOut(
                id=row.id,
                actor=AuditActorOut(
                    id=row.actor_user_id, email=row.actor_email_snapshot
                ),
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                path_template=row.path_template,
                method=row.method,
                status_code=row.status_code,
                request_id=row.request_id,
                metadata=row.metadata_json,
                occurred_at=row.occurred_at,
            )
            for row in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/trash", response_model=TrashPage)
async def list_trash(
    resource_type: str = Query(default="all", pattern="^(all|candidate|position)$"),
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> TrashPage:
    await _require_admin(db, user)
    items: list[TrashItemOut] = []
    if resource_type in {"all", "candidate"}:
        candidates = list(
            (
                await db.execute(
                    select(Candidate)
                    .where(
                        Candidate.organization_id == user.organization_id,
                        Candidate.deleted_at.is_not(None),
                    )
                    .execution_options(include_deleted=True)
                )
            ).scalars()
        )
        items.extend(
            TrashItemOut(
                id=row.id,
                resource_type="candidate",
                label=row.source_ref or f"{row.source} candidate",
                deleted_at=row.deleted_at,
                deleted_by_user_id=row.deleted_by_user_id,
            )
            for row in candidates
            if row.deleted_at is not None
        )
    if resource_type in {"all", "position"}:
        positions = list(
            (
                await db.execute(
                    select(Position)
                    .where(
                        Position.organization_id == user.organization_id,
                        Position.deleted_at.is_not(None),
                    )
                    .execution_options(include_deleted=True)
                )
            ).scalars()
        )
        items.extend(
            TrashItemOut(
                id=row.id,
                resource_type="position",
                label=row.title,
                deleted_at=row.deleted_at,
                deleted_by_user_id=row.deleted_by_user_id,
            )
            for row in positions
            if row.deleted_at is not None
        )
    items.sort(
        key=lambda item: (item.deleted_at, item.id, item.resource_type), reverse=True
    )
    namespace = "enterprise.trash"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"resource_type": resource_type},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"deleted_at", "id", "resource_type"},
            detail="Invalid trash cursor",
        )
        cursor_time = cursor_datetime(payload, "deleted_at", detail="Invalid trash cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid trash cursor")
        cursor_type = payload["resource_type"]
        if cursor_type not in {"candidate", "position"}:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "cursor_invalid", "detail": "Invalid trash cursor"},
            )
        items = [
            item
            for item in items
            if (item.deleted_at, item.id, item.resource_type)
            < (cursor_time, cursor_id, cursor_type)
        ]
    has_more = len(items) > limit
    items = items[:limit]
    return TrashPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "deleted_at": items[-1].deleted_at.astimezone(UTC).isoformat(),
                    "id": str(items[-1].id),
                    "resource_type": items[-1].resource_type,
                },
            )
            if has_more and items
            else None
        ),
    )


@router.post("/trash/{resource_type}/{resource_id}/restore", status_code=204)
async def restore_from_trash(
    resource_type: str,
    resource_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> None:
    await _require_admin(db, user)
    if resource_type == "candidate":
        row = (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.id == resource_id,
                    Candidate.organization_id == user.organization_id,
                    Candidate.deleted_at.is_not(None),
                )
                .execution_options(include_deleted=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None:
            if row.merged_at is None:
                await record_usage(
                    db,
                    organization_id=user.organization_id,
                    metric="candidates",
                    amount=1,
                    kind="usage",
                    idempotency_key=(
                        f"candidate:restore:{row.id}:{row.deleted_at.isoformat()}"
                    ),
                    resource_type="candidate",
                    resource_id=row.id,
                )
            row.deleted_at = None
            row.deleted_by_user_id = None
            append_candidate_activity(
                db,
                organization_id=user.organization_id,
                candidate_id=row.id,
                actor_user_id=user.id,
                event_type="candidate_restored",
                subject_type="candidate",
                subject_id=row.id,
                payload={"source": row.source},
            )
    elif resource_type == "position":
        row = (
            await db.execute(
                select(Position)
                .where(
                    Position.id == resource_id,
                    Position.organization_id == user.organization_id,
                    Position.deleted_at.is_not(None),
                )
                .execution_options(include_deleted=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None:
            if row.status == "open":
                await record_usage(
                    db,
                    organization_id=user.organization_id,
                    metric="active_jobs",
                    amount=1,
                    kind="usage",
                    idempotency_key=(
                        f"active-job:restore:{row.id}:{row.deleted_at.isoformat()}"
                    ),
                    resource_type="position",
                    resource_id=row.id,
                )
            row.deleted_at = None
            row.deleted_by_user_id = None
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trash item not found")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trash item not found")
    await db.commit()


async def _retention_counts(
    db: AsyncSession, *, organization_id: UUID
) -> tuple[RetentionPreviewOut, datetime, datetime, datetime]:
    policy = await get_or_create_policy(db, organization_id)
    now = datetime.now(UTC)
    deleted_cutoff = now - timedelta(days=policy.deleted_retention_days)
    active_cutoff = now - timedelta(days=policy.candidate_retention_days)
    audit_cutoff = now - timedelta(days=policy.audit_retention_days)
    deleted_candidates = (
        await db.execute(
            select(func.count(Candidate.id))
            .where(
                Candidate.organization_id == organization_id,
                Candidate.deleted_at.is_not(None),
                Candidate.deleted_at < deleted_cutoff,
            )
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    deleted_positions = (
        await db.execute(
            select(func.count(Position.id))
            .where(
                Position.organization_id == organization_id,
                Position.deleted_at.is_not(None),
                Position.deleted_at < deleted_cutoff,
            )
            .execution_options(include_deleted=True)
        )
    ).scalar_one()
    old_active = (
        await db.execute(
            select(func.count(Candidate.id)).where(
                Candidate.organization_id == organization_id,
                Candidate.deleted_at.is_(None),
                Candidate.created_at < active_cutoff,
            )
        )
    ).scalar_one()
    old_audits = (
        await db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.organization_id == organization_id,
                AuditLog.occurred_at < audit_cutoff,
            )
        )
    ).scalar_one()
    return (
        RetentionPreviewOut(
            deleted_candidates=deleted_candidates,
            deleted_positions=deleted_positions,
            old_active_candidates=old_active,
            expired_audit_logs=old_audits,
        ),
        deleted_cutoff,
        active_cutoff,
        audit_cutoff,
    )


@router.get("/retention/preview", response_model=RetentionPreviewOut)
async def preview_retention(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RetentionPreviewOut:
    await _require_admin(db, user)
    preview, *_cutoffs = await _retention_counts(
        db, organization_id=user.organization_id
    )
    await db.commit()
    return preview


@router.post("/retention/run", response_model=RetentionRunOut)
async def run_retention(
    body: RetentionRunRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> RetentionRunOut:
    await _require_admin(db, user)
    preview, deleted_cutoff, active_cutoff, audit_cutoff = await _retention_counts(
        db, organization_id=user.organization_id
    )
    try:
        plan = await prepare_retention_erasure(
            db,
            organization_id=user.organization_id,
            deleted_cutoff=deleted_cutoff,
            active_cutoff=active_cutoff,
            include_active_candidates=body.include_active_candidates,
        )
        # A normal soft delete already writes its release event. Retention may
        # also erase an old, still-live candidate directly, so append exactly
        # one transaction-bound release for only those canonical live rows.
        # ``prepare_retention_erasure`` holds row locks for this purge set.
        live_canonical_candidate_ids = tuple(
            (
                await db.execute(
                    select(Candidate.id).where(
                        Candidate.organization_id == user.organization_id,
                        Candidate.id.in_(plan.candidate_ids),
                        Candidate.deleted_at.is_(None),
                        Candidate.merged_at.is_(None),
                    )
                )
            ).scalars()
        )
        if live_canonical_candidate_ids:
            retention_usage_id = uuid4()
            await record_usage(
                db,
                organization_id=user.organization_id,
                metric="candidates",
                amount=-len(live_canonical_candidate_ids),
                kind="refund",
                idempotency_key=f"candidate:retention:{retention_usage_id}",
                resource_type="retention_run",
                resource_id=retention_usage_id,
                metadata={"reason": "retention"},
                enforce_limit=False,
            )
        # The append-only application-stage trigger permits deletion only inside
        # this explicitly audited privacy-erasure transaction. UPDATE remains
        # impossible even with the flag set.
        await db.execute(text("SET LOCAL app.allow_erasure = 'on'"))
        await db.execute(
            delete(Candidate).where(
                Candidate.organization_id == user.organization_id,
                Candidate.id.in_(plan.candidate_ids),
            )
        )
        await db.execute(
            delete(Position).where(
                Position.organization_id == user.organization_id,
                Position.id.in_(plan.position_ids),
            )
        )
        await db.execute(
            delete(AuditLog).where(
                AuditLog.organization_id == user.organization_id,
                AuditLog.occurred_at < audit_cutoff,
            )
        )
        await db.commit()
    except RetentionStorageError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Retention cannot prove complete private-object erasure.",
        ) from exc
    result = preview.model_dump()
    result.update(
        deleted_candidates=plan.soft_deleted_candidates,
        deleted_positions=plan.soft_deleted_positions,
        old_active_candidates=plan.active_candidates,
    )
    return RetentionRunOut(**result, purged_at=datetime.now(UTC))


@router.get("/backup")
async def export_backup(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> JSONResponse:
    await _require_admin(db, user)
    policy = await get_or_create_policy(db, user.organization_id)
    if not policy.backup_export_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Core-data exports are disabled")
    organization = (
        await db.execute(
            select(Organization).where(Organization.id == user.organization_id)
        )
    ).scalar_one()
    users = list(
        (
            await db.execute(
                select(User).where(User.organization_id == user.organization_id)
            )
        ).scalars()
    )
    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(Candidate.organization_id == user.organization_id)
                .execution_options(include_deleted=True)
            )
        ).scalars()
    )
    positions = list(
        (
            await db.execute(
                select(Position)
                .where(Position.organization_id == user.organization_id)
                .execution_options(include_deleted=True)
            )
        ).scalars()
    )
    scores = list(
        (
            await db.execute(
                select(Score).where(Score.organization_id == user.organization_id)
            )
        ).scalars()
    )
    payload = {
        # Compatibility route: this is deliberately a bounded portability
        # export, not a disaster-recovery archive. Full recovery is performed
        # by ops/recovery/hr_recovery.py so PostgreSQL, every tenant table, and
        # private-object bytes are restored as one integrity-checked snapshot.
        "format": "hr-agent-core-data-export-v1",
        "purpose": "data_portability",
        "restorable": False,
        "scope": ["organization", "users", "positions", "candidates", "legacy_scores"],
        "excluded": [
            "applications_and_stage_history",
            "assessments_and_interviews",
            "communications_and_integrations",
            "audit_and_security_state",
            "private_object_bytes",
            "workflow_and_ai_state",
        ],
        "exported_at": datetime.now(UTC),
        "organization": {
            "id": organization.id,
            "name": organization.name,
            "slug": organization.slug,
        },
        "users": [
            {"id": row.id, "email": row.email, "role": row.role, "locale": row.locale}
            for row in users
        ],
        "positions": [
            {
                "id": row.id,
                "title": row.title,
                "description": row.description,
                "requirements": row.requirements,
                "status": row.status,
                # Keep sensitivity semantics explicit for approved downstream
                # portability processors; this payload is still not restorable.
                "sensitivity": row.sensitivity,
                "candidate_profile": row.candidate_profile,
                "public_description": row.public_description,
                "deleted_at": row.deleted_at,
                "created_at": row.created_at,
            }
            for row in positions
        ],
        "candidates": [
            {
                "id": row.id,
                "source": row.source,
                "source_ref": row.source_ref,
                "raw_cv": row.raw_cv,
                "parsed_cv": row.parsed_cv,
                "deleted_at": row.deleted_at,
                "created_at": row.created_at,
            }
            for row in candidates
        ],
        "scores": [
            {
                "id": row.id,
                "candidate_id": row.candidate_id,
                "position_id": row.position_id,
                "overall_score": row.overall_score,
                "stage": row.stage,
                "scored_at": row.scored_at,
            }
            for row in scores
        ],
    }
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="export_core_data",
            resource_type="organization",
            resource_id=user.organization_id,
            path_template="/enterprise/backup",
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={
                "sensitive_read": True,
                "export_kind": "core_data_portability",
                "restorable": False,
                "user_count": len(users),
                "position_count": len(positions),
                "candidate_count": len(candidates),
                "legacy_score_count": len(scores),
            },
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    filename = (
        f'attachment; filename="hr-agent-core-data-export-'
        f'{datetime.now(UTC).date()}.json"'
    )
    return JSONResponse(
        content=jsonable_encoder(payload),
        headers={
            "Content-Disposition": filename,
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-HR-Export-Restorable": "false",
        },
    )


@router.get("/api-keys", response_model=CursorPage[ApiKeyOut])
async def list_api_keys(
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[ApiKeyOut]:
    await _require_admin(db, user)
    namespace = "enterprise.api-keys"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    stmt = select(OrganizationApiKey).where(
        OrganizationApiKey.organization_id == user.organization_id
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid API keys cursor",
        )
        cursor_time = cursor_datetime(payload, "created_at", detail="Invalid API keys cursor")
        cursor_id = cursor_uuid(payload, detail="Invalid API keys cursor")
        stmt = stmt.where(
            or_(
                OrganizationApiKey.created_at < cursor_time,
                and_(OrganizationApiKey.created_at == cursor_time, OrganizationApiKey.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(
                    OrganizationApiKey.created_at.desc(), OrganizationApiKey.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CursorPage(
        items=[ApiKeyOut.model_validate(row) for row in rows],
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


@router.post("/api-keys", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    body: ApiKeyCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApiKeyCreatedOut:
    await _require_admin(db, user)
    now = datetime.now(UTC)
    if body.expires_at is not None and body.expires_at <= now:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Expiry must be future"
        )
    secret, prefix, digest = new_api_secret()
    row = OrganizationApiKey(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        name=body.name,
        key_prefix=prefix,
        key_digest=digest,
        scopes=body.scopes,
        expires_at=body.expires_at,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "API key name already exists"
        ) from exc
    await db.refresh(row)
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(row).model_dump(), secret=secret)


@router.post("/api-keys/{api_key_id}/revoke", response_model=ApiKeyOut)
async def revoke_api_key(
    api_key_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ApiKeyOut:
    await _require_admin(db, user)
    row = (
        await db.execute(
            select(OrganizationApiKey).where(
                OrganizationApiKey.id == api_key_id,
                OrganizationApiKey.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
    return ApiKeyOut.model_validate(row)


@router.get("/sso", response_model=SsoConfigurationOut | None)
async def get_sso(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SsoConfigurationOut | None:
    await _require_admin(db, user)
    row = (
        await db.execute(
            select(SsoConfiguration).where(
                SsoConfiguration.organization_id == user.organization_id
            )
        )
    ).scalar_one_or_none()
    return SsoConfigurationOut.model_validate(row) if row else None


@router.put("/sso", response_model=SsoConfigurationOut)
async def put_sso(
    body: SsoConfigurationIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SsoConfigurationOut:
    await _require_admin(db, user)
    # Production SSO is explicitly outside the private-pilot boundary. Keep
    # GET available so tenants can inspect metadata imported before this
    # boundary was frozen, but fail closed for every mutation. This prevents a
    # write-only API surface from contradicting the read-only capability state
    # shown in the UI.
    del body
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "code": "sso_configuration_unavailable",
            "detail": (
                "Production SSO configuration is outside the private-pilot "
                "scope; existing metadata is read-only."
            ),
        },
    )


@router.get("/model-configuration", response_model=ModelConfigurationOut | None)
async def get_model_configuration(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ModelConfigurationOut | None:
    await _require_admin(db, user)
    row = (
        await db.execute(
            select(OrganizationModelConfiguration).where(
                OrganizationModelConfiguration.organization_id == user.organization_id
            )
        )
    ).scalar_one_or_none()
    return ModelConfigurationOut.model_validate(row) if row else None


@router.put("/model-configuration", response_model=ModelConfigurationOut)
async def put_model_configuration(
    body: ModelConfigurationIn,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ModelConfigurationOut:
    await _require_admin(db, user)
    from app.config import get_settings
    from app.services.enterprise import MANAGED_PROCESSING_NOTICE_VERSION

    values = body.model_dump(exclude={"managed_processing_accepted"})
    if body.base_url:
        from app.services.enterprise import (
            UnsafeModelEndpoint,
            validate_model_base_url,
        )

        try:
            values["base_url"] = await validate_model_base_url(body.base_url)
        except UnsafeModelEndpoint as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
            ) from exc
    row = (
        await db.execute(
            select(OrganizationModelConfiguration)
            .where(
                OrganizationModelConfiguration.organization_id == user.organization_id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if body.managed_processing_accepted:
        active_current_acceptance = bool(
            row is not None
            and row.managed_processing_accepted_at is not None
            and row.managed_processing_revoked_at is None
            and row.managed_processing_notice_version
            == MANAGED_PROCESSING_NOTICE_VERSION
        )
        if not active_current_acceptance:
            values.update(
                managed_processing_accepted_at=now,
                managed_processing_accepted_by_user_id=user.id,
                managed_processing_notice_version=MANAGED_PROCESSING_NOTICE_VERSION,
                managed_processing_revoked_at=None,
            )
    elif row is not None and row.managed_processing_accepted_at is not None:
        values["managed_processing_revoked_at"] = now

    settings = get_settings()
    if (
        body.provider == "inherit"
        and body.managed_processing_accepted
        and settings.llm_provider.lower() != "openrouter"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Managed-processing acceptance applies only to managed OpenRouter routing",
        )
    if body.provider == "inherit":
        if settings.llm_provider.lower() == "openrouter":
            status_value = (
                "ready"
                if body.managed_processing_accepted and settings.openrouter_api_key
                else "credentials_not_configured"
                if body.managed_processing_accepted
                else "disabled"
            )
        else:
            status_value = "ready"
    elif body.provider == "openrouter":
        status_value = (
            "ready" if settings.openrouter_api_key else "credentials_not_configured"
        )
    elif body.base_url and body.model:
        status_value = "ready"
    else:
        status_value = "credentials_not_configured"
    values["status"] = status_value
    if row is None:
        row = OrganizationModelConfiguration(
            organization_id=user.organization_id, **values
        )
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await db.commit()
    await db.refresh(row)
    return ModelConfigurationOut.model_validate(row)


async def _api_key(
    required_scope: str,
    x_api_key: str | None,
    db: AsyncSession,
) -> OrganizationApiKey:
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing API key")
    return await authenticate_api_key(db, x_api_key, required_scope)


def _api_key_audit_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        {
            "code": "audit_unavailable",
            "detail": "The audit service is temporarily unavailable.",
        },
    )


async def _start_api_key_read_audit(
    db: AsyncSession,
    *,
    key: OrganizationApiKey,
    request: Request,
    action: str,
    path_template: str,
    required_scope: str,
    sensitive_fields: tuple[str, ...] = (),
) -> UUID:
    """Persist body-free API-key read intent before releasing tenant data."""

    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, UUID):
        request_id = uuid4()
        request.state.request_id = request_id
    db.add(
        AuditLog(
            organization_id=key.organization_id,
            actor_user_id=key.created_by_user_id,
            actor_email_snapshot=None,
            action=action,
            resource_type="api_key",
            resource_id=key.id,
            path_template=path_template,
            method="GET",
            status_code=102,
            request_id=request_id,
            metadata_json={
                "audit_state": "started",
                "identity_kind": "api_key",
                "api_key_id": str(key.id),
                "required_scope": required_scope,
                "query_keys": sorted(set(request.query_params.keys())),
                "sensitive_read": True,
                **(
                    {
                        "field_classes": list(sensitive_fields),
                        "required_scopes": [required_scope, "candidates:contact"],
                    }
                    if sensitive_fields
                    else {}
                ),
            },
            occurred_at=datetime.now(UTC),
        )
    )
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise _api_key_audit_unavailable() from exc
    return request_id


async def _finish_api_key_read_audit(
    db: AsyncSession,
    *,
    key: OrganizationApiKey,
    request_id: UUID,
    required_scope: str,
    query_keys: list[str],
    sensitive_fields: tuple[str, ...] = (),
) -> None:
    """Finalize the intent; retain status 102 if finalization is unavailable."""

    try:
        result = await db.execute(
            update(AuditLog)
            .where(
                AuditLog.organization_id == key.organization_id,
                AuditLog.request_id == request_id,
                AuditLog.resource_type == "api_key",
                AuditLog.resource_id == key.id,
            )
            .values(
                status_code=200,
                metadata_json={
                    "identity_kind": "api_key",
                    "api_key_id": str(key.id),
                    "required_scope": required_scope,
                    "query_keys": query_keys,
                    "sensitive_read": True,
                    **(
                        {
                            "field_classes": list(sensitive_fields),
                            "required_scopes": [
                                required_scope,
                                "candidates:contact",
                            ],
                        }
                        if sensitive_fields
                        else {}
                    ),
                },
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("API-key audit intent is missing")
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise _api_key_audit_unavailable() from exc


@api_router.get(
    "/candidates",
    response_model=CursorPage[EnterpriseApiCandidateOut],
    response_model_exclude_none=True,
)
async def api_list_candidates(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_session),
) -> CursorPage[EnterpriseApiCandidateOut]:
    key = await _api_key("candidates:read", x_api_key, db)
    include_contact = "candidates:contact" in (key.scopes or [])
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    audit_request_id = await _start_api_key_read_audit(
        db,
        key=key,
        request=request,
        action="api_key_read.candidates",
        path_template="/enterprise/api/v1/candidates",
        required_scope="candidates:read",
        sensitive_fields=("candidate_contacts",) if include_contact else (),
    )
    namespace = "enterprise-api.candidates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=key.organization_id,
        filters={"api_key_id": str(key.id), "merged": False},
    )
    stmt = select(Candidate).where(
        Candidate.organization_id == key.organization_id,
        Candidate.merged_at.is_(None),
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid enterprise candidates cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid enterprise candidates cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid enterprise candidates cursor")
        stmt = stmt.where(
            or_(
                Candidate.created_at < cursor_time,
                and_(Candidate.created_at == cursor_time, Candidate.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(Candidate.created_at.desc(), Candidate.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    def project_candidate(row: Candidate) -> EnterpriseApiCandidateOut:
        data: dict[str, object] = {
            "id": row.id,
            "source": row.source,
            "created_at": row.created_at,
        }
        if include_contact:
            parsed = row.parsed_cv if isinstance(row.parsed_cv, dict) else {}
            parsed_name = parsed.get("applicant_name")
            parsed_email = parsed.get("applicant_email")
            parsed_phone = parsed.get("applicant_phone")
            data.update(
                {
                    "source_ref": row.source_ref,
                    "name": (
                        parsed_name.strip()
                        if isinstance(parsed_name, str) and parsed_name.strip()
                        else None
                    ),
                    "email": row.email_normalized
                    or (
                        parsed_email.strip()
                        if isinstance(parsed_email, str) and parsed_email.strip()
                        else None
                    ),
                    "phone": row.phone_normalized
                    or (
                        parsed_phone.strip()
                        if isinstance(parsed_phone, str) and parsed_phone.strip()
                        else None
                    ),
                }
            )
        return EnterpriseApiCandidateOut.model_validate(data)

    page = CursorPage(
        items=[project_candidate(row) for row in rows],
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
    await _finish_api_key_read_audit(
        db,
        key=key,
        request_id=audit_request_id,
        required_scope="candidates:read",
        query_keys=sorted(set(request.query_params.keys())),
        sensitive_fields=("candidate_contacts",) if include_contact else (),
    )
    return page


@api_router.get("/positions", response_model=CursorPage[EnterpriseApiPositionOut])
async def api_list_positions(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_session),
) -> CursorPage[EnterpriseApiPositionOut]:
    key = await _api_key("positions:read", x_api_key, db)
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    audit_request_id = await _start_api_key_read_audit(
        db,
        key=key,
        request=request,
        action="api_key_read.positions",
        path_template="/enterprise/api/v1/positions",
        required_scope="positions:read",
    )
    namespace = "enterprise-api.positions"
    scope = collection_cursor_scope(
        namespace,
        organization_id=key.organization_id,
        filters={"api_key_id": str(key.id)},
    )
    stmt = select(Position).where(Position.organization_id == key.organization_id)
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid enterprise positions cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid enterprise positions cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid enterprise positions cursor")
        stmt = stmt.where(
            or_(
                Position.created_at < cursor_time,
                and_(Position.created_at == cursor_time, Position.id < cursor_id),
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(Position.created_at.desc(), Position.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    page = CursorPage(
        items=[EnterpriseApiPositionOut.model_validate(row) for row in rows],
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
    await _finish_api_key_read_audit(
        db,
        key=key,
        request_id=audit_request_id,
        required_scope="positions:read",
        query_keys=sorted(set(request.query_params.keys())),
    )
    return page
