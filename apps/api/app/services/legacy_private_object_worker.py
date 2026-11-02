"""Tenant-isolated erasure worker for verified pre-registry local files."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.models.private_object import LegacyPrivateObjectDeletion
from app.services.worker_claims import (
    StaleWorkClaim,
    WorkClaim,
    WorkerRlsMode,
    WorkQueue,
    claim_work,
    process_one_claim,
)
from app.storage.legacy import settle_legacy_private_object_deletion
from app.storage.local import LocalBlobStore

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 10


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:legacy-private-object-deletions"


async def _process_legacy_private_object_deletion(
    db: AsyncSession,
    claim: WorkClaim,
    *,
    store: LocalBlobStore | None,
    now: datetime,
) -> str:
    deletion = (
        await db.execute(
            select(LegacyPrivateObjectDeletion).where(
                LegacyPrivateObjectDeletion.id == claim.work_id,
                LegacyPrivateObjectDeletion.organization_id == claim.organization_id,
            )
        )
    ).scalar_one_or_none()
    if deletion is None:
        return "missing"
    if deletion.completed_at is not None:
        return "already_deleted"

    # The forced broker owns the native attempt/token transition. Explicit-off
    # tests mirror it after the generic lease excludes competing workers.
    if deletion.claim_token is None and deletion.claimed_at is None:
        if deletion.attempt_count >= _MAX_ATTEMPTS:
            return "dead_letter"
        deletion.attempt_count += 1
        deletion.claimed_at = now
        deletion.claim_token = claim.lease_token
        await db.flush()
    elif deletion.claim_token != claim.lease_token:
        raise StaleWorkClaim("Legacy deletion token no longer belongs to worker")

    settlement = await settle_legacy_private_object_deletion(
        db,
        deletion_id=claim.work_id,
        organization_id=claim.organization_id,
        claim_token=claim.lease_token,
        store=store,
        now=now,
    )
    await db.flush()
    if settlement == "retry_scheduled" and deletion.attempt_count >= _MAX_ATTEMPTS:
        return "dead_letter"
    return settlement


async def process_legacy_private_object_deletions_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    worker_id: str,
    mode: WorkerRlsMode,
    limit: int = 25,
    lease_seconds: int = 120,
    store: LocalBlobStore | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)
    async with session_factory() as discovery:
        claims = await claim_work(
            discovery,
            queue=WorkQueue.LEGACY_PRIVATE_OBJECT_DELETIONS,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
            now=current,
        )
        await discovery.commit()

    counts = {
        "claimed": len(claims),
        "deleted": 0,
        "retried": 0,
        "dead_lettered": 0,
        "abandoned": 0,
    }

    async def process(db: AsyncSession, claim: WorkClaim) -> str:
        return await _process_legacy_private_object_deletion(
            db,
            claim,
            store=store,
            now=current,
        )

    for claim in claims:
        try:
            outcome = await process_one_claim(claim, process)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Legacy private-object deletion work item abandoned",
                extra={"legacy_private_object_deletion_id": str(claim.work_id)},
            )
            counts["abandoned"] += 1
            continue
        if outcome in {"deleted", "already_deleted"}:
            counts["deleted"] += 1
        elif outcome == "retry_scheduled":
            counts["retried"] += 1
        elif outcome == "dead_letter":
            counts["dead_lettered"] += 1
    return counts


async def run_legacy_private_object_worker(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    identity = worker_id or _worker_id()
    while True:
        try:
            await process_legacy_private_object_deletions_once(
                worker_id=identity,
                mode=mode,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Legacy private-object deletion queue iteration failed")
        await asyncio.sleep(5.0)


__all__ = [
    "process_legacy_private_object_deletions_once",
    "run_legacy_private_object_worker",
]
