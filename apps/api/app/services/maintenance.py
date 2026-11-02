"""Bounded, RLS-safe maintenance for security and worker records."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, or_, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.models.operations import WorkerHeartbeat
from app.models.user import (
    AuthLoginThrottle,
    AuthRefreshSession,
    RevokedAccessToken,
    UserAccessToken,
)
from app.services.worker_claims import (
    WorkClaim,
    WorkQueue,
    WorkerRlsMode,
    claim_work,
    process_recurring_claim,
)
from app.services.workflows import materialize_scheduled_workflow_runs
from app.storage import reconcile_private_storage


logger = logging.getLogger(__name__)
MAINTENANCE_INTERVAL = timedelta(hours=1)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:maintenance"


async def cleanup_expired_tenant_records(
    db: AsyncSession,
    *,
    organization_id: UUID,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete only one organization's expired authorization records."""

    current = now or datetime.now(UTC)
    refresh_cutoff = current - timedelta(days=30)
    revoked = await db.execute(
        delete(RevokedAccessToken).where(
            RevokedAccessToken.organization_id == organization_id,
            RevokedAccessToken.expires_at < current,
        )
    )
    refresh_sessions = await db.execute(
        delete(AuthRefreshSession).where(
            AuthRefreshSession.organization_id == organization_id,
            AuthRefreshSession.expires_at < refresh_cutoff,
        )
    )
    workflow_runs = await materialize_scheduled_workflow_runs(
        db,
        organization_id=organization_id,
        limit=100,
        now=current,
    )
    access_cutoff = current - timedelta(days=30)
    user_access_tokens = await db.execute(
        delete(UserAccessToken).where(
            UserAccessToken.organization_id == organization_id,
            or_(
                UserAccessToken.expires_at < access_cutoff,
                UserAccessToken.consumed_at < access_cutoff,
                UserAccessToken.revoked_at < access_cutoff,
            ),
        )
    )
    reconciliation = await reconcile_private_storage(
        db,
        organization_id=organization_id,
        dry_run=False,
        now=current,
    )
    reconciliation_counts = {
        f"storage_reconciliation_{key}": int(value)
        for key, value in reconciliation.as_dict().items()
        if key != "dry_run"
    }
    return {
        "revoked_access_tokens": revoked.rowcount or 0,
        "auth_refresh_sessions": refresh_sessions.rowcount or 0,
        "workflow_runs_materialized": workflow_runs,
        "user_access_tokens": user_access_tokens.rowcount or 0,
        **reconciliation_counts,
    }


async def _cleanup_global_records_direct(
    db: AsyncSession,
    *,
    now: datetime,
) -> dict[str, int]:
    """Compatibility cleanup used only when worker RLS mode is exactly off."""

    settings = get_settings()
    throttle_cutoff = now - timedelta(
        seconds=settings.auth_login_window_seconds
        + settings.auth_login_lockout_seconds
    )
    heartbeat_cutoff = now - timedelta(days=7)
    throttles = await db.execute(
        delete(AuthLoginThrottle).where(AuthLoginThrottle.updated_at < throttle_cutoff)
    )
    heartbeats = await db.execute(
        delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < heartbeat_cutoff)
    )
    leases = await db.execute(
        text(
            "DELETE FROM worker_queue_leases "
            "WHERE lease_expires_at < :now RETURNING work_id"
        ),
        {"now": now},
    )
    return {
        "auth_login_throttles": throttles.rowcount or 0,
        "worker_heartbeats": heartbeats.rowcount or 0,
        "expired_worker_leases": len(leases.all()),
    }


async def cleanup_expired_operational_records(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Legacy explicit-off/test helper preserving the public service contract.

    The dedicated forced-RLS worker never calls this global tenant-table scan.
    """

    current = now or datetime.now(UTC)
    settings = get_settings()
    throttle_cutoff = current - timedelta(
        seconds=settings.auth_login_window_seconds
        + settings.auth_login_lockout_seconds
    )
    refresh_cutoff = current - timedelta(days=30)
    heartbeat_cutoff = current - timedelta(days=7)
    throttles = await db.execute(
        delete(AuthLoginThrottle).where(AuthLoginThrottle.updated_at < throttle_cutoff)
    )
    revoked = await db.execute(
        delete(RevokedAccessToken).where(RevokedAccessToken.expires_at < current)
    )
    refresh_sessions = await db.execute(
        delete(AuthRefreshSession).where(AuthRefreshSession.expires_at < refresh_cutoff)
    )
    access_cutoff = current - timedelta(days=30)
    user_access_tokens = await db.execute(
        delete(UserAccessToken).where(
            or_(
                UserAccessToken.expires_at < access_cutoff,
                UserAccessToken.consumed_at < access_cutoff,
                UserAccessToken.revoked_at < access_cutoff,
            )
        )
    )
    heartbeats = await db.execute(
        delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < heartbeat_cutoff)
    )
    return {
        "auth_login_throttles": throttles.rowcount or 0,
        "revoked_access_tokens": revoked.rowcount or 0,
        "auth_refresh_sessions": refresh_sessions.rowcount or 0,
        "worker_heartbeats": heartbeats.rowcount or 0,
        "user_access_tokens": user_access_tokens.rowcount or 0,
    }


async def process_maintenance_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    worker_id: str,
    mode: WorkerRlsMode,
    limit: int = 100,
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    async with session_factory() as discovery:
        claims = await claim_work(
            discovery,
            queue=WorkQueue.MAINTENANCE,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
            now=current,
        )
        await discovery.commit()
    counts = {
        "organizations_claimed": len(claims),
        "organizations_abandoned": 0,
        "revoked_access_tokens": 0,
        "auth_refresh_sessions": 0,
        "auth_login_throttles": 0,
        "worker_heartbeats": 0,
        "expired_worker_leases": 0,
        "workflow_runs_materialized": 0,
        "user_access_tokens": 0,
    }

    async def process(db: AsyncSession, claim: WorkClaim) -> dict[str, int]:
        return await cleanup_expired_tenant_records(
            db,
            organization_id=claim.organization_id,
            now=current,
        )

    for claim in claims:
        try:
            removed = await process_recurring_claim(
                claim,
                process,
                cooldown=MAINTENANCE_INTERVAL,
                now=current if now is not None else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Tenant maintenance item abandoned",
                extra={"maintenance_work_id": str(claim.work_id)},
            )
            counts["organizations_abandoned"] += 1
            continue
        for key, value in removed.items():
            counts[key] = counts.get(key, 0) + value

    async with session_factory() as global_session:
        if mode == "forced":
            row = (
                await global_session.execute(
                    text(
                        "SELECT auth_login_throttles, worker_heartbeats, "
                        "expired_worker_leases "
                        "FROM security.run_global_maintenance(:now)"
                    ),
                    {"now": current},
                )
            ).mappings().one()
            global_counts = {key: int(value or 0) for key, value in row.items()}
        elif mode == "off":
            global_counts = await _cleanup_global_records_direct(
                global_session,
                now=current,
            )
        else:  # pragma: no cover - guarded by Settings
            raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
        await global_session.commit()
    for key, value in global_counts.items():
        counts[key] += value
    return counts


async def run_maintenance_loop(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    lease_seconds = 120
    identity = worker_id or _worker_id()
    while True:
        try:
            cycle_counts: dict[str, int] = {}
            while True:
                removed = await process_maintenance_once(
                    worker_id=identity,
                    mode=mode,
                    lease_seconds=lease_seconds,
                )
                for key, value in removed.items():
                    cycle_counts[key] = cycle_counts.get(key, 0) + value
                if removed["organizations_claimed"] < 100:
                    break
                # Yield between bounded batches so API/worker peers are not
                # starved while every due tenant receives a maintenance pass.
                await asyncio.sleep(0)
            if any(cycle_counts.values()):
                logger.info("Operational cleanup counts=%s", cycle_counts)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Operational cleanup iteration failed")
        await asyncio.sleep(3600)
