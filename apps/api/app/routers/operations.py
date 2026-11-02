"""Operational health and read-only contract usage APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.models.user import User
from app.schemas.operations import (
    ComponentHealthOut,
    EntitlementsOut,
    EntitlementUsageOut,
    PublicReadinessOut,
    ReadinessOut,
    UsageEventOut,
    UsagePage,
)
from app.services.enterprise import require_permission
from app.services.entitlements import (
    ENTITLEMENT_METRICS,
    all_entitlement_snapshots,
    effective_technical_limits,
    list_usage,
)
from app.services.operations import (
    ReadinessSnapshot,
    readiness_components,
    readiness_probe_cache,
)

router = APIRouter(tags=["operations"])
enterprise_router = APIRouter(
    prefix="/enterprise", tags=["enterprise"], dependencies=[Depends(current_user)]
)
settings = get_settings()


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


async def _readiness_snapshot() -> ReadinessSnapshot:
    async def probe() -> dict[str, dict]:
        async with SessionLocal() as session:
            return await readiness_components(session)

    return await readiness_probe_cache.get(probe)


def _readiness_status(snapshot: ReadinessSnapshot) -> tuple[str, bool]:
    failed = any(
        value["status"] == "failed" for value in snapshot.components.values()
    )
    return ("not_ready" if failed else "ready"), failed


@router.get("/health/ready", response_model=PublicReadinessOut)
async def readiness() -> JSONResponse:
    snapshot = await _readiness_snapshot()
    readiness_status, failed = _readiness_status(snapshot)
    payload = PublicReadinessOut(
        status=readiness_status,
        checked_at=snapshot.checked_at,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        if failed
        else status.HTTP_200_OK,
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@enterprise_router.get("/operations/readiness", response_model=ReadinessOut)
async def detailed_readiness(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> JSONResponse:
    await require_permission(db, user, "enterprise.manage")
    snapshot = await _readiness_snapshot()
    readiness_status, failed = _readiness_status(snapshot)
    payload = ReadinessOut(
        status=readiness_status,
        checked_at=snapshot.checked_at,
        components={
            key: ComponentHealthOut.model_validate(value)
            for key, value in snapshot.components.items()
        },
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE
        if failed
        else status.HTTP_200_OK,
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@enterprise_router.get("/entitlements", response_model=EntitlementsOut)
async def entitlements(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> EntitlementsOut:
    await require_permission(db, user, "enterprise.read")
    snapshots = await all_entitlement_snapshots(db, user.organization_id)
    return EntitlementsOut(
        entitlements=[
            EntitlementUsageOut(
                metric=item.metric,
                limit=item.limit,
                used=item.used,
                remaining=item.remaining,
                period=item.period,
                effective_from=item.effective_from,
                effective_until=item.effective_until,
                basis=item.basis,
            )
            for item in snapshots
        ],
        technical_limits=effective_technical_limits(
            communications_max_bulk_candidates=(
                settings.communications_max_bulk_candidates
            )
        ),
    )


@enterprise_router.get("/usage", response_model=UsagePage)
async def usage(
    metric: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> UsagePage:
    await require_permission(db, user, "enterprise.manage")
    if metric is not None and metric not in ENTITLEMENT_METRICS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown metric")
    try:
        rows, next_cursor = await list_usage(
            db,
            organization_id=user.organization_id,
            metric=metric,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return UsagePage(
        items=[
            UsageEventOut(
                id=str(row.id),
                metric=row.metric,
                amount=row.amount,
                kind=row.kind,
                resource_type=row.resource_type,
                resource_id=str(row.resource_id) if row.resource_id else None,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ],
        next_cursor=next_cursor,
    )
