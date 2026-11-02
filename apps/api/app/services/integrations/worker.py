"""Tenant-isolated worker cycles for provider jobs and webhook deliveries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import tenant_session_scope
from app.models.integration import (
    IntegrationConnection,
    IntegrationJob,
    IntegrationWebhookDelivery,
)
from app.observability.context import job_correlation, provider_operation_group
from app.observability.metrics import record_provider_connection_state
from app.observability.telemetry import provider_observation
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import ProviderHttpClient
from app.services.integrations.lifecycle import (
    IntegrationLifecycleError,
    ensure_fresh_credentials,
)
from app.services.integrations.runtime import execute_provider_job
from app.services.integrations.webhooks import deliver_webhook
from app.services.worker_claims import (
    WorkClaim,
    WorkerRlsMode,
    WorkQueue,
    assert_active_claim,
    claim_work,
    process_one_claim,
)

logger = logging.getLogger(__name__)
EndpointValidator = Callable[[str], Awaitable[str]]


def _provider_outcome(
    *,
    work_status: str,
    failure_code: str | None,
    connection_status: str,
) -> str:
    if work_status == "cancelled":
        return "cancelled"
    if failure_code == "provider_rate_limited":
        return "rate_limited"
    if work_status in {"retrying"} or connection_status in {"degraded", "expired"}:
        return "degraded"
    if work_status in {"dead_letter", "failed"}:
        return "failed"
    if work_status in {"succeeded", "delivered"}:
        return "succeeded"
    return "other"


def _worker_runtime() -> tuple[WorkerRlsMode, int]:
    settings = get_settings()
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    lease_seconds = 120
    return mode, lease_seconds


async def _claim_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    queue: WorkQueue,
    worker_id: str,
    limit: int,
    mode: WorkerRlsMode,
    lease_seconds: int,
) -> list[WorkClaim]:
    async with session_factory() as discovery:
        claims = await claim_work(
            discovery,
            queue=queue,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
        )
        await discovery.commit()
        return claims


async def process_integration_jobs_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cipher: IntegrationCipher,
    adapters: dict[str, Any],
    worker_id: str,
    limit: int = 25,
    mode: WorkerRlsMode | None = None,
    lease_seconds: int | None = None,
) -> dict[str, int]:
    configured_mode, configured_lease = _worker_runtime()
    claims = await _claim_batch(
        session_factory,
        queue=WorkQueue.INTEGRATION_JOBS,
        worker_id=worker_id,
        limit=limit,
        mode=mode or configured_mode,
        lease_seconds=lease_seconds or configured_lease,
    )
    counts = {"claimed": len(claims), "succeeded": 0, "failed": 0}

    async def prepare_credentials(claim: WorkClaim) -> dict[str, Any] | None:
        """Refresh OAuth credentials in a short transaction before I/O.

        The subsequent provider operation therefore holds neither the job row
        nor the connection row lock. Refresh itself remains serialized on the
        connection so rotating refresh tokens cannot race across jobs.
        """

        async with tenant_session_scope(
            organization_id=claim.organization_id,
            identity_kind="background",
            role="worker",
        ) as preparation:
            await assert_active_claim(preparation, claim)
            job = (
                await preparation.execute(
                    select(IntegrationJob).where(
                        IntegrationJob.id == claim.work_id,
                        IntegrationJob.organization_id == claim.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            connection = (
                await preparation.execute(
                    select(IntegrationConnection)
                    .where(
                        IntegrationConnection.id == job.connection_id,
                        IntegrationConnection.organization_id == claim.organization_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if connection is None or connection.status not in {
                "connected",
                "degraded",
            }:
                return None
            is_local_sourcing = (job.reference_payload or {}).get(
                "source_kind"
            ) in {
                "local_candidate_import",
                "local_candidate_file_import",
            }
            if is_local_sourcing:
                # CSV/XLSX and Somon link/import jobs execute entirely inside
                # our validated sourcing boundary. They still require a live,
                # tenant-bound connection record for audit and health, but no
                # third-party adapter credentials.
                return {}
            adapter = adapters.get(connection.provider)
            if adapter is None:
                return None
            with job_correlation(
                queue=WorkQueue.INTEGRATION_JOBS.value,
                job_id=claim.work_id,
            ):
                try:
                    return await ensure_fresh_credentials(
                        preparation,
                        connection=connection,
                        cipher=cipher,
                        adapter=adapter,
                    )
                except IntegrationLifecycleError:
                    # The connection's normalized expired/degraded state commits;
                    # job execution records the matching durable failure below.
                    return None
                finally:
                    record_provider_connection_state(
                        connection_id=connection.id,
                        provider=connection.provider,
                        state=connection.status,
                    )

    async def process(
        db: AsyncSession,
        claim: WorkClaim,
        prepared_credentials: dict[str, Any] | None,
    ) -> str:
        job = (
            await db.execute(
                select(IntegrationJob).where(
                    IntegrationJob.id == claim.work_id,
                    IntegrationJob.organization_id == claim.organization_id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            return "missing"
        now = datetime.now(UTC)
        # Forced-RLS claim functions already perform this transition. The
        # explicit-off path mirrors it here for local/test compatibility.
        if job.status in {"queued", "retrying"}:
            if job.next_attempt_at > now or job.attempt_count >= job.max_attempts:
                return "not_due"
            job.status = "running"
            job.attempt_count += 1
            job.leased_at = now
            job.leased_by = worker_id
            job.lease_expires_at = claim.lease_expires_at
            job.failure_code = None
        if (
            job.status != "running"
            or job.leased_by != worker_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            return "stale"
        connection = (
            await db.execute(
                select(IntegrationConnection).where(
                    IntegrationConnection.id == job.connection_id,
                    IntegrationConnection.organization_id == job.organization_id,
                )
            )
        ).scalar_one_or_none()
        provider = connection.provider if connection is not None else "other"
        with provider_observation(
            provider=provider,
            operation=provider_operation_group(job.operation),
        ) as observation:
            try:
                await execute_provider_job(
                    db,
                    job=job,
                    cipher=cipher,
                    adapters=adapters,
                    prepared_credentials=prepared_credentials,
                )
            finally:
                if connection is not None:
                    record_provider_connection_state(
                        connection_id=connection.id,
                        provider=connection.provider,
                        state=connection.status,
                    )
            observation.outcome = _provider_outcome(
                work_status=job.status,
                failure_code=job.failure_code,
                connection_status=(
                    connection.status if connection is not None else "unavailable"
                ),
            )
        return job.status

    for claim in claims:
        try:
            credentials = await prepare_credentials(claim)

            async def prepared_process(
                db: AsyncSession,
                item: WorkClaim,
                prepared: dict[str, Any] | None = credentials,
            ) -> str:
                return await process(db, item, prepared)

            outcome = await process_one_claim(claim, prepared_process)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The lease is deliberately left for expiry/reclaim; never expose
            # provider payloads or credentials in worker logs.
            logger.exception(
                "Integration job processing abandoned",
                extra={"integration_job_id": str(claim.work_id)},
            )
            counts["failed"] += 1
            continue
        if outcome == "succeeded":
            counts["succeeded"] += 1
        else:
            counts["failed"] += 1
    return counts


async def process_webhook_deliveries_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cipher: IntegrationCipher,
    http: ProviderHttpClient,
    validate_endpoint: EndpointValidator,
    worker_id: str,
    limit: int = 25,
    mode: WorkerRlsMode | None = None,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    configured_mode, configured_lease = _worker_runtime()
    claims = await _claim_batch(
        session_factory,
        queue=WorkQueue.WEBHOOK_DELIVERIES,
        worker_id=worker_id,
        limit=limit,
        mode=mode or configured_mode,
        lease_seconds=lease_seconds or configured_lease,
    )
    counts = {"claimed": len(claims), "delivered": 0, "failed": 0}

    async def process(db: AsyncSession, claim: WorkClaim) -> str:
        delivery = (
            await db.execute(
                select(IntegrationWebhookDelivery).where(
                    IntegrationWebhookDelivery.id == claim.work_id,
                    IntegrationWebhookDelivery.organization_id == claim.organization_id,
                )
            )
        ).scalar_one_or_none()
        if delivery is None:
            return "missing"
        current = now or datetime.now(UTC)
        if (
            delivery.status not in {"queued", "retrying"}
            or delivery.next_attempt_at > current
        ):
            return "not_due"
        connection = (
            await db.execute(
                select(IntegrationConnection).where(
                    IntegrationConnection.id == delivery.connection_id,
                    IntegrationConnection.organization_id == delivery.organization_id,
                )
            )
        ).scalar_one_or_none()
        if connection is None:
            delivery.status = "dead_letter"
            delivery.failure_code = "connection_not_found"
            return delivery.status
        with provider_observation(
            provider="generic_webhook",
            operation="webhook_delivery",
        ) as observation:
            try:
                await deliver_webhook(
                    db,
                    delivery=delivery,
                    connection=connection,
                    cipher=cipher,
                    http=http,
                    validate_endpoint=validate_endpoint,
                    now=current,
                )
            finally:
                record_provider_connection_state(
                    connection_id=connection.id,
                    provider=connection.provider,
                    state=connection.status,
                )
            observation.outcome = _provider_outcome(
                work_status=delivery.status,
                failure_code=delivery.failure_code,
                connection_status=connection.status,
            )
        return delivery.status

    for claim in claims:
        try:
            outcome = await process_one_claim(claim, process)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Webhook delivery processing abandoned",
                extra={"webhook_delivery_id": str(claim.work_id)},
            )
            counts["failed"] += 1
            continue
        if outcome == "delivered":
            counts["delivered"] += 1
        else:
            counts["failed"] += 1
    return counts
