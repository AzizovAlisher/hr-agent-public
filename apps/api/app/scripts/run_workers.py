"""Run all durable workers outside the FastAPI process.

Deploy this module as a separate replica set. A small heartbeat loop makes
worker availability observable without making Valkey authoritative.
"""

import asyncio
import logging
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime

from app.config import get_settings
from app.db import SessionLocal, session_scope
from app.observability.metrics import worker_telemetry_snapshot
from app.observability.telemetry import (
    build_telemetry_runtime,
    install_correlation_log_record_factory,
)
from app.services.ai_hiring_worker import run_ai_hiring_worker
from app.services.candidate_processing_worker import (
    run_candidate_processing_worker,
)
from app.services.communication_worker import run_communication_worker
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import (
    ProviderHttpClient,
    create_integration_http_client,
    validate_operator_approved_url,
)
from app.services.integrations.providers import default_adapters
from app.services.integrations.worker import (
    process_integration_jobs_once,
    process_webhook_deliveries_once,
)
from app.services.legacy_private_object_worker import (
    run_legacy_private_object_worker,
)
from app.services.maintenance import run_maintenance_loop
from app.services.operations import touch_worker_heartbeat
from app.services.private_object_worker import run_private_object_worker
from app.services.scheduling_worker import run_scheduling_worker
from app.services.workflow_worker import run_workflow_worker

logger = logging.getLogger(__name__)


async def _heartbeat(
    worker_id: str,
    started_at: datetime,
    queues: tuple[str, ...],
) -> None:
    while True:
        async with session_scope() as db:
            await touch_worker_heartbeat(
                db,
                worker_name="durable-work",
                worker_id=worker_id,
                process_started_at=started_at,
                metadata={
                    "pid": os.getpid(),
                    "queues": list(queues),
                    "telemetry": worker_telemetry_snapshot(),
                },
            )
        await asyncio.sleep(20)


async def _run_integration_worker(worker_id: str) -> None:
    settings = get_settings()
    key = settings.integration_credential_encryption_key
    if key is None:
        raise RuntimeError(
            "Integration worker requires INTEGRATION_CREDENTIAL_ENCRYPTION_KEY"
        )
    cipher = IntegrationCipher.from_base64(
        key, key_id=settings.integration_credential_key_id
    )
    allowed_hosts = {
        value.strip().lower().rstrip(".")
        for value in settings.integration_outbound_host_allowlist.split(",")
        if value.strip()
    }

    async def validate_endpoint(value: str) -> str:
        return await validate_operator_approved_url(value, allowed_hosts=allowed_hosts)

    timeout = settings.integration_http_timeout_seconds
    async with create_integration_http_client(
        allowed_hosts=allowed_hosts,
        timeout=timeout,
        max_connections=50,
        max_keepalive_connections=20,
    ) as client:
        provider_http = ProviderHttpClient(client)
        adapters = default_adapters(
            provider_http,
            hh_user_agent=settings.hh_user_agent,
            outbound_hosts=allowed_hosts,
        )
        while True:
            try:
                await process_integration_jobs_once(
                    SessionLocal,
                    cipher=cipher,
                    adapters=adapters,
                    worker_id=worker_id,
                    limit=settings.integration_worker_claim_limit,
                )
                await process_webhook_deliveries_once(
                    SessionLocal,
                    cipher=cipher,
                    http=provider_http,
                    validate_endpoint=validate_endpoint,
                    worker_id=worker_id,
                    limit=settings.integration_worker_claim_limit,
                )
            except Exception:
                # A provider/runtime defect must not terminate unrelated queue
                # workers. Leases and retry state make the next cycle safe.
                logger.exception("Integration worker cycle failed")
            await asyncio.sleep(settings.integration_worker_poll_seconds)


async def main() -> None:
    settings = get_settings()
    telemetry_runtime = build_telemetry_runtime(settings)
    install_correlation_log_record_factory()
    started_at = datetime.now(UTC)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    tasks: list[asyncio.Task[None]] = []
    queues = [
        "legacy-private-object-deletions",
        "maintenance",
        "private-object-deletions",
    ]
    tasks.append(
        asyncio.create_task(
            run_maintenance_loop(worker_id=f"{worker_id}:maintenance"),
            name="maintenance",
        )
    )
    tasks.append(
        asyncio.create_task(
            run_legacy_private_object_worker(
                worker_id=f"{worker_id}:legacy-private-object-deletions"
            ),
            name="legacy-private-object-deletions",
        )
    )
    tasks.append(
        asyncio.create_task(
            run_private_object_worker(
                worker_id=f"{worker_id}:private-object-deletions"
            ),
            name="private-object-deletions",
        )
    )
    if settings.communications_worker_enabled:
        queues.append("communications")
        tasks.append(
            asyncio.create_task(
                run_communication_worker(worker_id=f"{worker_id}:communications"),
                name="communications",
            )
        )
    if (
        settings.integration_worker_enabled
        and settings.integration_credential_encryption_key is not None
    ):
        queues.append("integrations")
        tasks.append(
            asyncio.create_task(_run_integration_worker(worker_id), name="integrations")
        )
    if settings.scheduling_worker_enabled:
        queues.append("reminders")
        tasks.append(
            asyncio.create_task(
                run_scheduling_worker(worker_id=f"{worker_id}:reminders"),
                name="scheduling",
            )
        )
    if settings.workflow_worker_enabled:
        queues.append("workflows")
        tasks.append(
            asyncio.create_task(
                run_workflow_worker(worker_id=f"{worker_id}:workflows"),
                name="workflows",
            )
        )
    if settings.ai_worker_enabled:
        queues.append("ai-hiring")
        tasks.append(
            asyncio.create_task(
                run_ai_hiring_worker(worker_id=f"{worker_id}:ai-hiring"),
                name="ai-hiring",
            )
        )
    if settings.candidate_processing_worker_enabled:
        queues.append("candidate-processing")
        tasks.append(
            asyncio.create_task(
                run_candidate_processing_worker(
                    worker_id=f"{worker_id}:candidate-processing"
                ),
                name="candidate-processing",
            )
        )
    tasks.insert(
        0,
        asyncio.create_task(
            _heartbeat(worker_id, started_at, tuple(sorted(queues))),
            name="heartbeat",
        ),
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        telemetry_runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
