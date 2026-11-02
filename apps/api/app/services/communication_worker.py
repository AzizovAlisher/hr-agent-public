"""Tenant-isolated poller for scheduled candidate communications."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.models.communication import CommunicationMessage
from app.services.communications import process_claimed_message
from app.services.integrations.crypto import IntegrationCipher, IntegrationCryptoError
from app.services.integrations.http import (
    ProviderHttpClient,
    create_integration_http_client,
)
from app.services.integrations.providers import default_adapters
from app.services.worker_claims import (
    WorkClaim,
    WorkerRlsMode,
    WorkQueue,
    claim_work,
    process_one_claim,
)

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:communications"


async def process_communication_messages_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    worker_id: str,
    limit: int,
    mode: WorkerRlsMode,
    lease_seconds: int = 120,
    cipher: IntegrationCipher | None = None,
    adapters: dict[str, Any] | None = None,
) -> dict[str, int]:
    async with session_factory() as discovery:
        claims = await claim_work(
            discovery,
            queue=WorkQueue.COMMUNICATION_MESSAGES,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
        )
        await discovery.commit()
    counts = {
        "claimed": len(claims),
        "sent": 0,
        "retrying": 0,
        "failed": 0,
        "abandoned": 0,
    }

    async def process(db: AsyncSession, claim: WorkClaim) -> str | None:
        status_value = (
            await db.execute(
                select(
                    CommunicationMessage.status,
                    CommunicationMessage.attempt_count,
                    CommunicationMessage.failure_code,
                ).where(
                    CommunicationMessage.id == claim.work_id,
                    CommunicationMessage.organization_id == claim.organization_id,
                )
            )
        ).one_or_none()
        if status_value is None:
            return None
        status_name, attempt_count, failure_code = status_value
        recovered = (
            mode == "forced"
            and status_name == "processing"
            and attempt_count > 1
            and failure_code is None
        ) or (mode == "off" and status_name == "processing")
        return await process_claimed_message(
            db,
            organization_id=claim.organization_id,
            message_id=claim.work_id,
            # Forced claims transition to processing; explicit-off discovery
            # leaves a queued record untouched and the single-item executor
            # performs the equivalent transition tenant-side.
            already_claimed=status_name == "processing",
            recovered_claim=recovered,
            cipher=cipher,
            adapters=adapters,
        )

    for claim in claims:
        try:
            outcome = await process_one_claim(claim, process)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "Communication work item abandoned",
                extra={"communication_message_id": str(claim.work_id)},
            )
            counts["abandoned"] += 1
            continue
        if outcome == "sent":
            counts["sent"] += 1
        elif outcome == "retrying":
            counts["retrying"] += 1
        elif outcome == "failed":
            counts["failed"] += 1
    return counts


async def run_communication_worker(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    poll_seconds = max(0.5, min(settings.communications_worker_poll_seconds, 60.0))
    claim_limit = max(1, min(settings.communications_claim_limit, 100))
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    lease_seconds = 120
    identity = worker_id or _worker_id()
    cipher = None
    if settings.integration_credential_encryption_key is not None:
        try:
            cipher = IntegrationCipher.from_base64(
                settings.integration_credential_encryption_key,
                key_id=settings.integration_credential_key_id,
            )
        except IntegrationCryptoError:
            logger.error("Communication credential encryption is unavailable")
    allowed_hosts = {
        value.strip().lower().rstrip(".")
        for value in settings.integration_outbound_host_allowlist.split(",")
        if value.strip()
    }
    timeout = settings.integration_http_timeout_seconds
    async with create_integration_http_client(
        allowed_hosts=allowed_hosts,
        timeout=timeout,
        max_connections=25,
        max_keepalive_connections=10,
    ) as client:
        adapters = default_adapters(
            ProviderHttpClient(client),
            hh_user_agent=settings.hh_user_agent,
            outbound_hosts=allowed_hosts,
        )
        while True:
            try:
                await process_communication_messages_once(
                    worker_id=identity,
                    limit=claim_limit,
                    mode=mode,
                    lease_seconds=lease_seconds,
                    cipher=cipher,
                    adapters=adapters,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log provider/contact/message data. The queue records a
                # stable failure code when a delivery itself fails.
                logger.error("Communication queue iteration failed")
            await asyncio.sleep(poll_seconds)
