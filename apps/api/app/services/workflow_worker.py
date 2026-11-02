"""Tenant-isolated consumer for materialized recruiting workflow runs."""

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
from app.models.workflow import WorkflowRun
from app.services.worker_claims import (
    WorkClaim,
    WorkerRlsMode,
    WorkQueue,
    claim_work,
    process_one_claim,
)
from app.services.workflows import process_materialized_workflow_run

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:workflows"


async def process_workflow_runs_once(
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
            queue=WorkQueue.WORKFLOW_RUNS,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
        )
        await discovery.commit()
    counts = {
        "claimed": len(claims),
        "completed": 0,
        "skipped": 0,
        "cancelled": 0,
        "retried": 0,
        "dead_lettered": 0,
        "abandoned": 0,
    }

    async def process(db: AsyncSession, claim: WorkClaim) -> str | None:
        run = (
            await db.execute(
                select(WorkflowRun).where(
                    WorkflowRun.id == claim.work_id,
                    WorkflowRun.organization_id == claim.organization_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        # The explicit-off path mirrors the fixed claim function's transition.
        if run.status in {"queued", "retrying"}:
            current = datetime.now(UTC)
            if run.available_at > current or run.attempt_count >= run.max_attempts:
                return None
            run.status = "running"
            run.attempt_count += 1
            run.leased_at = current
            run.leased_by = worker_id
            run.lease_token = claim.lease_token
            run.lease_expires_at = claim.lease_expires_at
            run.error_code = None
            await db.flush()
        if run.status != "running" or run.lease_token != claim.lease_token:
            return None
        return await process_materialized_workflow_run(
            db,
            organization_id=claim.organization_id,
            run_id=claim.work_id,
        )

    for claim in claims:
        try:
            outcome = await process_one_claim(claim, process)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Workflow work item abandoned",
                extra={"workflow_run_id": str(claim.work_id)},
            )
            counts["abandoned"] += 1
            continue
        if outcome == "completed":
            counts["completed"] += 1
        elif outcome == "skipped":
            counts["skipped"] += 1
        elif outcome == "cancelled":
            counts["cancelled"] += 1
        elif outcome == "retrying":
            counts["retried"] += 1
        elif outcome == "dead_letter":
            counts["dead_lettered"] += 1
    return counts


async def run_workflow_worker(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    poll_seconds = max(1.0, min(settings.workflow_worker_poll_seconds, 300.0))
    claim_limit = max(1, min(settings.workflow_claim_limit, 100))
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    lease_seconds = 120
    identity = worker_id or _worker_id()
    while True:
        try:
            await process_workflow_runs_once(
                worker_id=identity,
                limit=claim_limit,
                mode=mode,
                lease_seconds=lease_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Workflow worker iteration failed")
        await asyncio.sleep(poll_seconds)
