"""PostgreSQL-backed integration jobs with leases, retry, DLQ, and replay controls."""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationConnection, IntegrationJob
from app.services.entitlements import record_usage
from app.services.integrations.catalog import PROVIDER_OPERATIONS
from app.services.integrations.crypto import IntegrationCipher, connection_aad


class IntegrationJobError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


DEFAULT_ENCRYPTED_JOB_PAYLOAD_BYTES = 512_000
MAX_ENCRYPTED_JOB_PAYLOAD_BYTES = 40 * 1024 * 1024


def request_fingerprint(operation: str, references: dict) -> str:
    canonical = json.dumps(
        {"operation": operation, "references": references},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def enqueue_job(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    actor_user_id: uuid.UUID | None,
    operation: str,
    idempotency_key: str,
    references: dict,
    sensitive_payload: dict[str, Any] | None = None,
    cipher: IntegrationCipher | None = None,
    max_attempts: int = 3,
    max_payload_bytes: int = DEFAULT_ENCRYPTED_JOB_PAYLOAD_BYTES,
    now: datetime | None = None,
    allow_unready: bool = False,
) -> tuple[IntegrationJob, bool]:
    if not 1 <= max_payload_bytes <= MAX_ENCRYPTED_JOB_PAYLOAD_BYTES:
        raise IntegrationJobError("job_payload_limit_invalid")
    allowed = PROVIDER_OPERATIONS.get(connection.provider, frozenset())
    durable_assessment_callback = (
        connection.provider == "assessment_generic"
        and operation == "assessment.callback"
        and "webhook.receive" in set(connection.capabilities or [])
    )
    durable_local_candidate_import = (
        operation == "candidate.import"
        and references.get("source_kind")
        in {"local_candidate_import", "local_candidate_file_import"}
        and connection.provider
        in {"csv_xlsx", "somon", "generic_webhook", "hh_ru"}
    )
    if (
        operation not in allowed
        and not durable_assessment_callback
        and not durable_local_candidate_import
    ):
        raise IntegrationJobError("operation_not_supported")
    if connection.status != "connected" and not allow_unready:
        raise IntegrationJobError("connection_not_ready")
    now = now or datetime.now(UTC)
    fingerprint_payload = dict(references)
    if sensitive_payload is not None:
        encoded_payload = json.dumps(
            sensitive_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(encoded_payload) > max_payload_bytes:
            raise IntegrationJobError("job_payload_too_large")
        fingerprint_payload["_payload_sha256"] = hashlib.sha256(
            encoded_payload
        ).hexdigest()
    fingerprint = request_fingerprint(operation, fingerprint_payload)
    # Serialize one idempotency key without relying on an exception/rollback,
    # which keeps the caller's wider transaction usable.
    lock_key = f"integration-job:{connection.organization_id}:{connection.id}:{operation}:{idempotency_key}"
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    existing = (
        await db.execute(
            select(IntegrationJob).where(
                IntegrationJob.organization_id == connection.organization_id,
                IntegrationJob.connection_id == connection.id,
                IntegrationJob.operation == operation,
                IntegrationJob.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise IntegrationJobError("idempotency_conflict")
        return existing, False
    job = IntegrationJob(
        id=uuid.uuid4(),
        organization_id=connection.organization_id,
        connection_id=connection.id,
        created_by_user_id=actor_user_id,
        operation=operation,
        status="queued",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        reference_payload=references,
        attempt_count=0,
        max_attempts=max_attempts,
        next_attempt_at=now,
        result_summary={},
    )
    if sensitive_payload is not None:
        if cipher is None:
            raise IntegrationJobError("credential_encryption_unavailable")
        encrypted = cipher.encrypt_json(
            sensitive_payload,
            aad=connection_aad(
                connection.organization_id,
                job.id,
                "job-payload",
            ),
        )
        job.payload_ciphertext = encrypted.ciphertext
        job.payload_nonce = encrypted.nonce
        job.payload_key_id = encrypted.key_id
    await record_usage(
        db,
        organization_id=connection.organization_id,
        metric="integration_runs",
        amount=1,
        kind="usage",
        idempotency_key=f"integration-job:{job.id}",
        resource_type="integration_job",
        resource_id=job.id,
        metadata={"provider": connection.provider, "operation": operation},
    )
    db.add(job)
    await db.flush()
    return job, True


def load_job_payload(
    job: IntegrationJob, *, cipher: IntegrationCipher
) -> dict[str, Any]:
    if job.payload_ciphertext is None:
        return {}
    if job.payload_nonce is None or job.payload_key_id is None:
        raise IntegrationJobError("job_payload_unavailable")
    value = cipher.decrypt_json(
        job.payload_ciphertext,
        job.payload_nonce,
        aad=connection_aad(job.organization_id, job.id, "job-payload"),
        key_id=job.payload_key_id,
    )
    if not isinstance(value, dict):
        raise IntegrationJobError("job_payload_unavailable")
    return value


async def claim_jobs(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int = 25,
    lease_seconds: int = 60,
    organization_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[IntegrationJob]:
    now = now or datetime.now(UTC)
    statement = (
        select(IntegrationJob)
        .where(
            or_(
                IntegrationJob.status.in_(["queued", "retrying"]),
                (
                    (IntegrationJob.status == "running")
                    & (IntegrationJob.lease_expires_at <= now)
                ),
            ),
            IntegrationJob.next_attempt_at <= now,
            IntegrationJob.attempt_count < IntegrationJob.max_attempts,
            IntegrationJob.cancelled_at.is_(None),
        )
        .order_by(
            IntegrationJob.next_attempt_at, IntegrationJob.created_at, IntegrationJob.id
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if organization_id is not None:
        statement = statement.where(IntegrationJob.organization_id == organization_id)
    jobs = list((await db.execute(statement)).scalars())
    for job in jobs:
        job.status = "running"
        job.attempt_count += 1
        job.leased_at = now
        job.leased_by = worker_id
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.failure_code = None
    await db.flush()
    return jobs


def _safe_summary(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 8_000:
        raise IntegrationJobError("result_summary_too_large")
    prohibited = {
        "body",
        "contact",
        "cv",
        "email",
        "name",
        "phone",
        "prompt",
        "secret",
        "token",
    }
    if any(set(key.lower().replace("-", "_").split("_")) & prohibited for key in value):
        raise IntegrationJobError("result_summary_contains_sensitive_field")
    return value


def _discard_payload_on_success(job: IntegrationJob) -> bool:
    """Return whether terminal success makes the encrypted input unnecessary.

    These payloads remain available through retries and dead-letter replay, but
    are removed atomically with terminal success. Visible references contain
    only tenant-local identifiers and bounded counters.
    """

    references = job.reference_payload or {}
    return references.get("kind") in {
        "candidate_erasure_calendar_cleanup",
        "candidate_erasure_assessment_cleanup",
    } or references.get("source_kind") in {
        "local_candidate_import",
        "local_candidate_file_import",
    }


async def succeed_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    result_summary: dict[str, Any],
    now: datetime | None = None,
) -> None:
    if job.status != "running":
        raise IntegrationJobError("job_not_running")
    now = now or datetime.now(UTC)
    job.status = "succeeded"
    job.result_summary = _safe_summary(result_summary)
    job.failure_code = None
    job.completed_at = now
    job.lease_expires_at = None
    job.leased_by = None
    if _discard_payload_on_success(job):
        # Clear all AES-GCM parts in the same transaction that records terminal
        # success.  Retry/dead-letter paths intentionally retain the encrypted
        # input so an operator can safely replay the deletion obligation.
        job.payload_ciphertext = None
        job.payload_nonce = None
        job.payload_key_id = None
    await db.flush()


async def fail_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    failure_code: str,
    retryable: bool,
    now: datetime | None = None,
) -> None:
    if job.status != "running":
        raise IntegrationJobError("job_not_running")
    now = now or datetime.now(UTC)
    job.failure_code = failure_code[:64]
    job.lease_expires_at = None
    job.leased_by = None
    if retryable and job.attempt_count < job.max_attempts:
        job.status = "retrying"
        job.next_attempt_at = now + timedelta(
            seconds=min(5 * (2 ** max(job.attempt_count - 1, 0)), 300)
        )
    else:
        job.status = "dead_letter"
        job.completed_at = now
    await db.flush()


async def cancel_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    now: datetime | None = None,
) -> None:
    if job.status in {"succeeded", "dead_letter", "cancelled"}:
        raise IntegrationJobError("job_terminal")
    now = now or datetime.now(UTC)
    job.status = "cancelled"
    job.cancelled_at = now
    job.completed_at = now
    job.lease_expires_at = None
    job.leased_by = None
    await db.flush()


async def replay_dead_letter(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    now: datetime | None = None,
) -> None:
    if job.status != "dead_letter":
        raise IntegrationJobError("job_not_dead_letter")
    now = now or datetime.now(UTC)
    job.status = "queued"
    job.attempt_count = 0
    job.failure_code = None
    job.result_summary = {}
    job.next_attempt_at = now
    job.completed_at = None
    await db.flush()


async def request_job_execution(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    now: datetime | None = None,
) -> None:
    """Make queued work immediately claimable without executing provider I/O."""

    if job.status not in {"queued", "retrying"}:
        raise IntegrationJobError("job_not_runnable")
    now = now or datetime.now(UTC)
    job.next_attempt_at = now
    job.leased_at = None
    job.leased_by = None
    job.lease_expires_at = None
    await db.flush()


JobHandler = Callable[[IntegrationJob], Awaitable[dict[str, Any]]]


async def run_claimed_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    handlers: dict[str, JobHandler],
) -> None:
    handler = handlers.get(job.operation)
    if handler is None:
        await fail_job(
            db,
            job=job,
            failure_code="operation_handler_missing",
            retryable=False,
        )
        return
    try:
        result = await handler(job)
    except IntegrationJobError as exc:
        await fail_job(db, job=job, failure_code=exc.code, retryable=False)
    except Exception as exc:  # provider errors are normalized by handlers
        code = getattr(exc, "code", "integration_job_failed")
        retryable = bool(getattr(exc, "retryable", False))
        await fail_job(db, job=job, failure_code=code, retryable=retryable)
    else:
        await succeed_job(db, job=job, result_summary=result)
