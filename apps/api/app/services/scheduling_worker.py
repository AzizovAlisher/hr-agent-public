"""Tenant-isolated poller for durable interview reminders."""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.services.scheduling import process_due_reminders
from app.services.worker_claims import (
    WorkClaim,
    WorkQueue,
    WorkerRlsMode,
    claim_work,
    process_one_claim,
)


logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:reminders"


async def process_scheduling_reminders_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    worker_id: str,
    limit: int,
    mode: WorkerRlsMode,
    lease_seconds: int = 120,
) -> dict[str, int]:
    async with session_factory() as discovery:
        claims = await claim_work(
            discovery,
            queue=WorkQueue.INTERVIEW_REMINDERS,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
        )
        await discovery.commit()
    counts = {"claimed": len(claims), "processed": 0, "abandoned": 0}

    async def process(db: AsyncSession, claim: WorkClaim) -> int:
        return await process_due_reminders(
            db,
            organization_id=claim.organization_id,
            reminder_ids=[claim.work_id],
            limit=1,
        )

    for claim in claims:
        try:
            counts["processed"] += int(await process_one_claim(claim, process))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Scheduling reminder work item abandoned",
                extra={"interview_reminder_id": str(claim.work_id)},
            )
            counts["abandoned"] += 1
    return counts


async def run_scheduling_worker(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    poll_seconds = max(1.0, min(settings.scheduling_worker_poll_seconds, 60.0))
    claim_limit = max(1, min(settings.scheduling_claim_limit, 100))
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    lease_seconds = 120
    identity = worker_id or _worker_id()
    while True:
        try:
            await process_scheduling_reminders_once(
                worker_id=identity,
                limit=claim_limit,
                mode=mode,
                lease_seconds=lease_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Scheduling URLs, location details, candidate contacts and
            # provider data never enter worker logs.
            logger.exception("Scheduling reminder iteration failed")
        await asyncio.sleep(poll_seconds)
