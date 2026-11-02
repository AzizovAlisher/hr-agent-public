"""Minimal, tenant-safe work discovery and lease validation.

Production workers discover work through one fixed ``SECURITY DEFINER``
function per queue.  The functions return only opaque work identity and a
rotating lease token; all payload access happens later in a tenant-bound
session.  A direct SQL implementation exists solely for local/test deployments
that explicitly set worker RLS mode to ``off``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import tenant_session_scope
from app.observability.telemetry import job_observation, job_outcome_for_result

WorkerRlsMode = Literal["forced", "off"]


class WorkQueue(StrEnum):
    INTEGRATION_JOBS = "integration_jobs"
    WEBHOOK_DELIVERIES = "integration_webhook_deliveries"
    COMMUNICATION_MESSAGES = "communication_messages"
    INTERVIEW_REMINDERS = "interview_reminders"
    WORKFLOW_RUNS = "workflow_runs"
    MISSION_STEPS = "mission_steps"
    CANDIDATE_PROCESSING = "candidate_processing_jobs"
    PRIVATE_OBJECT_DELETIONS = "private_object_deletions"
    LEGACY_PRIVATE_OBJECT_DELETIONS = "legacy_private_object_deletions"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True, slots=True)
class WorkClaim:
    queue: WorkQueue
    work_id: UUID
    organization_id: UUID
    lease_token: UUID
    lease_expires_at: datetime


class WorkerClaimError(RuntimeError):
    """Base error for a malformed or unsafe claim operation."""


class StaleWorkClaim(WorkerClaimError):
    """The lease expired, was released, or has since been assigned again."""


ClaimProcessor = Callable[[AsyncSession, WorkClaim], Awaitable[Any]]


_CLAIM_FUNCTIONS: dict[WorkQueue, str] = {
    WorkQueue.INTEGRATION_JOBS: "security.claim_integration_jobs",
    WorkQueue.WEBHOOK_DELIVERIES: "security.claim_integration_webhook_deliveries",
    WorkQueue.COMMUNICATION_MESSAGES: "security.claim_communication_messages",
    WorkQueue.INTERVIEW_REMINDERS: "security.claim_interview_reminders",
    WorkQueue.WORKFLOW_RUNS: "security.claim_workflow_runs",
    WorkQueue.MISSION_STEPS: "security.claim_mission_steps",
    WorkQueue.CANDIDATE_PROCESSING: ("security.claim_candidate_processing_jobs"),
    WorkQueue.PRIVATE_OBJECT_DELETIONS: ("security.claim_private_object_deletions"),
    WorkQueue.LEGACY_PRIVATE_OBJECT_DELETIONS: (
        "security.claim_legacy_private_object_deletions"
    ),
    WorkQueue.MAINTENANCE: "security.claim_maintenance_organizations",
}


# Each statement is deliberately static. Never interpolate a caller-provided
# relation, predicate, status, or ordering expression into worker discovery.
_DIRECT_CANDIDATES: dict[WorkQueue, str] = {
    WorkQueue.INTEGRATION_JOBS: """
        SELECT source.id AS work_id, source.organization_id
        FROM integration_jobs AS source
        WHERE source.cancelled_at IS NULL
          AND source.next_attempt_at <= :now
          AND source.attempt_count < source.max_attempts
          AND (
            source.status IN ('queued', 'retrying')
            OR (
              source.status = 'running'
              AND source.lease_expires_at IS NOT NULL
              AND source.lease_expires_at <= :now
            )
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.next_attempt_at, source.created_at, source.id
    """,
    WorkQueue.WEBHOOK_DELIVERIES: """
        SELECT source.id AS work_id, source.organization_id
        FROM integration_webhook_deliveries AS source
        WHERE source.status IN ('queued', 'retrying')
          AND source.next_attempt_at <= :now
          AND source.attempt_count < source.max_attempts
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.next_attempt_at, source.created_at, source.id
    """,
    WorkQueue.COMMUNICATION_MESSAGES: """
        SELECT source.id AS work_id, source.organization_id
        FROM communication_messages AS source
        WHERE source.direction = 'outbound'
          AND (
            (
              source.status = 'queued'
              AND source.attempt_count < 3
              AND source.scheduled_for <= :now
            )
            OR (
              source.status = 'processing'
              AND source.attempt_count <= 3
              AND source.claimed_at < :communication_stale_before
            )
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.scheduled_for, source.created_at, source.id
    """,
    WorkQueue.INTERVIEW_REMINDERS: """
        SELECT source.id AS work_id, source.organization_id
        FROM interview_reminders AS source
        WHERE source.status = 'pending'
          AND source.due_at <= :now
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.due_at, source.id
    """,
    WorkQueue.WORKFLOW_RUNS: """
        SELECT source.id AS work_id, source.organization_id
        FROM workflow_runs AS source
        WHERE source.available_at <= :now
          AND source.attempt_count < source.max_attempts
          AND (
            source.status IN ('queued', 'retrying')
            OR (
              source.status = 'running'
              AND source.lease_expires_at IS NOT NULL
              AND source.lease_expires_at <= :now
            )
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.available_at, source.created_at, source.id
    """,
    WorkQueue.MISSION_STEPS: """
        SELECT source.id AS work_id, source.organization_id
        FROM mission_steps AS source
        JOIN hiring_missions AS mission ON mission.id = source.mission_id
          AND mission.organization_id = source.organization_id
        JOIN ai_organization_settings AS settings
          ON settings.organization_id = source.organization_id
        WHERE settings.feature_enabled IS TRUE
          AND mission.status IN ('planning', 'running')
          AND (
            SELECT count(*)
            FROM mission_step_attempts AS attempt
            WHERE attempt.step_id = source.id
              AND attempt.organization_id = source.organization_id
          ) < source.max_attempts
          AND (
            (source.status = 'ready' AND source.available_at <= :now)
            OR (
              source.status = 'running'
              AND source.lease_expires_at IS NOT NULL
              AND source.lease_expires_at <= :now
            )
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.available_at, source.created_at, source.id
    """,
    WorkQueue.CANDIDATE_PROCESSING: """
        SELECT source.id AS work_id, source.organization_id
        FROM candidate_processing_jobs AS source
        WHERE source.cancelled_at IS NULL
          AND source.available_at <= :now
          AND source.attempt_count < source.max_attempts
          AND (
            source.status IN ('queued', 'retrying')
            OR (
              source.status = 'running'
              AND source.lease_expires_at IS NOT NULL
              AND source.lease_expires_at <= :now
            )
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.available_at, source.created_at, source.id
    """,
    WorkQueue.PRIVATE_OBJECT_DELETIONS: """
        SELECT source.id AS work_id, source.organization_id
        FROM private_object_deletions AS source
        JOIN private_objects AS object
          ON object.id = source.private_object_id
         AND object.organization_id = source.organization_id
        WHERE source.completed_at IS NULL
          AND source.not_before <= :now
          AND source.next_attempt_at <= :now
          AND source.attempt_count < 10
          AND object.state = 'delete_pending'
          AND object.legal_hold_at IS NULL
          AND (
            source.claimed_at IS NULL
            OR source.claimed_at < :private_deletion_stale_before
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.next_attempt_at, source.requested_at, source.id
    """,
    WorkQueue.LEGACY_PRIVATE_OBJECT_DELETIONS: """
        SELECT source.id AS work_id, source.organization_id
        FROM legacy_private_object_deletions AS source
        WHERE source.completed_at IS NULL
          AND source.not_before <= :now
          AND source.next_attempt_at <= :now
          AND source.attempt_count < 10
          AND (
            source.claimed_at IS NULL
            OR source.claimed_at < :private_deletion_stale_before
          )
          AND NOT EXISTS (
            SELECT 1 FROM worker_queue_leases AS active_lease
            WHERE active_lease.queue_name = :queue_name
              AND active_lease.work_id = source.id
              AND active_lease.lease_expires_at > :now
          )
        ORDER BY source.next_attempt_at, source.requested_at, source.id
    """,
    WorkQueue.MAINTENANCE: """
        SELECT source.id AS work_id, source.id AS organization_id
        FROM organizations AS source
        WHERE NOT EXISTS (
          SELECT 1 FROM worker_queue_leases AS active_lease
          WHERE active_lease.queue_name = :queue_name
            AND active_lease.work_id = source.id
            AND active_lease.lease_expires_at > :now
        )
        ORDER BY source.created_at, source.id
    """,
}


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise WorkerClaimError("Worker claim time must include a timezone")
    return current.astimezone(UTC)


def _bounded_inputs(*, worker_id: str, limit: int, lease_seconds: int) -> None:
    if not worker_id or len(worker_id) > 255:
        raise WorkerClaimError("Worker ID must contain 1-255 characters")
    if not 1 <= limit <= 100:
        raise WorkerClaimError("Worker claim limit must be between 1 and 100")
    if not 15 <= lease_seconds <= 900:
        raise WorkerClaimError("Worker lease must be between 15 and 900 seconds")


async def claim_work(
    db: AsyncSession,
    *,
    queue: WorkQueue,
    worker_id: str,
    limit: int,
    mode: WorkerRlsMode,
    lease_seconds: int = 120,
    now: datetime | None = None,
) -> list[WorkClaim]:
    """Atomically claim a bounded batch and return no queue payload fields."""

    _bounded_inputs(worker_id=worker_id, limit=limit, lease_seconds=lease_seconds)
    current = _aware_utc(now)
    if mode == "forced":
        # Function identifiers come exclusively from the closed enum mapping.
        function = _CLAIM_FUNCTIONS[queue]
        statement = text(
            f"""SELECT work_id, organization_id, lease_token, lease_expires_at
                FROM {function}(:worker_id, :limit, :now)"""
        )
        rows = (
            await db.execute(
                statement,
                {"worker_id": worker_id, "limit": limit, "now": current},
            )
        ).mappings()
    elif mode == "off":
        rows = (
            await db.execute(
                _direct_claim_statement(queue),
                {
                    "queue_name": queue.value,
                    "worker_id": worker_id,
                    "limit": limit,
                    "lease_seconds": lease_seconds,
                    "now": current,
                    "communication_stale_before": current
                    - _COMMUNICATION_STALE_INTERVAL,
                    "private_deletion_stale_before": current
                    - _PRIVATE_DELETION_STALE_INTERVAL,
                },
            )
        ).mappings()
    else:  # pragma: no cover - protected by Settings and the Literal contract
        raise WorkerClaimError("Worker RLS mode must be 'forced' or 'off'")
    claims: list[WorkClaim] = []
    for row in rows:
        expires = row["lease_expires_at"]
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise WorkerClaimError("Database returned a naive lease expiry")
        claims.append(
            WorkClaim(
                queue=queue,
                work_id=row["work_id"],
                organization_id=row["organization_id"],
                lease_token=row["lease_token"],
                lease_expires_at=expires.astimezone(UTC),
            )
        )
    return claims


_COMMUNICATION_STALE_INTERVAL = timedelta(minutes=5)
_PRIVATE_DELETION_STALE_INTERVAL = timedelta(seconds=120)


def _direct_claim_statement(queue: WorkQueue):
    candidates = _DIRECT_CANDIDATES[queue].strip()
    return text(
        f"""
        WITH candidates AS MATERIALIZED (
            {candidates}
            FOR UPDATE OF source SKIP LOCKED
            LIMIT :limit
        ), claimed AS (
            INSERT INTO worker_queue_leases (
                queue_name,
                work_id,
                organization_id,
                lease_token,
                worker_id,
                leased_at,
                lease_expires_at
            )
            SELECT
                :queue_name,
                candidate.work_id,
                candidate.organization_id,
                gen_random_uuid(),
                :worker_id,
                :now,
                :now + make_interval(secs => :lease_seconds)
            FROM candidates AS candidate
            ON CONFLICT ON CONSTRAINT worker_queue_leases_pkey DO UPDATE
              SET organization_id = EXCLUDED.organization_id,
                  lease_token = gen_random_uuid(),
                  worker_id = EXCLUDED.worker_id,
                  leased_at = EXCLUDED.leased_at,
                  lease_expires_at = EXCLUDED.lease_expires_at
              WHERE worker_queue_leases.lease_expires_at <= :now
            RETURNING work_id, organization_id, lease_token, lease_expires_at
        )
        SELECT work_id, organization_id, lease_token, lease_expires_at
        FROM claimed
        """
    )


async def assert_active_claim(
    db: AsyncSession,
    claim: WorkClaim,
    *,
    now: datetime | None = None,
) -> None:
    """Lock and validate a lease inside the tenant processing transaction."""

    current = _aware_utc(now)
    active = (
        await db.execute(
            text(
                """
                SELECT 1
                FROM worker_queue_leases
                WHERE queue_name = :queue_name
                  AND work_id = :work_id
                  AND organization_id = :organization_id
                  AND lease_token = :lease_token
                  AND lease_expires_at > :now
                FOR UPDATE
                """
            ),
            {
                "queue_name": claim.queue.value,
                "work_id": claim.work_id,
                "organization_id": claim.organization_id,
                "lease_token": claim.lease_token,
                "now": current,
            },
        )
    ).scalar_one_or_none()
    if active is None:
        raise StaleWorkClaim("Worker claim is no longer active")


async def complete_claim(
    db: AsyncSession,
    claim: WorkClaim,
    *,
    now: datetime | None = None,
) -> None:
    """Acknowledge only the exact, unexpired token or fail the transaction."""

    current = _aware_utc(now)
    removed = (
        await db.execute(
            text(
                """
                DELETE FROM worker_queue_leases
                WHERE queue_name = :queue_name
                  AND work_id = :work_id
                  AND organization_id = :organization_id
                  AND lease_token = :lease_token
                  AND lease_expires_at > :now
                RETURNING 1
                """
            ),
            {
                "queue_name": claim.queue.value,
                "work_id": claim.work_id,
                "organization_id": claim.organization_id,
                "lease_token": claim.lease_token,
                "now": current,
            },
        )
    ).scalar_one_or_none()
    if removed is None:
        raise StaleWorkClaim("Stale worker cannot acknowledge this work item")


async def defer_recurring_claim(
    db: AsyncSession,
    claim: WorkClaim,
    *,
    available_at: datetime,
    now: datetime | None = None,
) -> None:
    """Acknowledge recurring work while retaining its next-due cursor.

    Organization maintenance has no terminal source row. Keeping the exact
    lease until the next due time prevents a bounded batch from repeatedly
    selecting the same earliest organizations and starving later tenants.
    """

    current = _aware_utc(now)
    next_available = _aware_utc(available_at)
    if next_available <= current:
        raise WorkerClaimError("Recurring work must be deferred into the future")
    updated = (
        await db.execute(
            text(
                """
                UPDATE worker_queue_leases
                SET leased_at = :now,
                    lease_expires_at = :available_at
                WHERE queue_name = :queue_name
                  AND work_id = :work_id
                  AND organization_id = :organization_id
                  AND lease_token = :lease_token
                  AND lease_expires_at > :now
                RETURNING 1
                """
            ),
            {
                "queue_name": claim.queue.value,
                "work_id": claim.work_id,
                "organization_id": claim.organization_id,
                "lease_token": claim.lease_token,
                "now": current,
                "available_at": next_available,
            },
        )
    ).scalar_one_or_none()
    if updated is None:
        raise StaleWorkClaim("Stale worker cannot defer this recurring work item")


async def release_claim(db: AsyncSession, claim: WorkClaim) -> bool:
    """Release an exact claim for prompt graceful-shutdown recovery."""

    removed = (
        await db.execute(
            text(
                """
                DELETE FROM worker_queue_leases
                WHERE queue_name = :queue_name
                  AND work_id = :work_id
                  AND organization_id = :organization_id
                  AND lease_token = :lease_token
                RETURNING 1
                """
            ),
            {
                "queue_name": claim.queue.value,
                "work_id": claim.work_id,
                "organization_id": claim.organization_id,
                "lease_token": claim.lease_token,
            },
        )
    ).scalar_one_or_none()
    return removed is not None


async def process_one_claim(
    claim: WorkClaim,
    processor: ClaimProcessor,
) -> Any:
    """Run one work item in its own tenant transaction and acknowledge it.

    If the processor crashes, the tenant transaction rolls back and the lease
    remains until expiry. If ownership changes or the lease expires while the
    processor is running, acknowledgement raises ``StaleWorkClaim`` and rolls
    back every database mutation made by that stale worker.
    """

    with job_observation(queue=claim.queue.value, job_id=claim.work_id) as observation:
        async with tenant_session_scope(
            organization_id=claim.organization_id,
            identity_kind="background",
            role="worker",
        ) as db:
            await assert_active_claim(db, claim)
            result = await processor(db, claim)
            await complete_claim(db, claim)
            observation.outcome = job_outcome_for_result(result)
            return result


async def process_recurring_claim(
    claim: WorkClaim,
    processor: ClaimProcessor,
    *,
    cooldown: timedelta,
    now: datetime | None = None,
) -> Any:
    """Process recurring work atomically and retain its next-due cursor."""

    if cooldown <= timedelta(0):
        raise WorkerClaimError("Recurring work cooldown must be positive")
    started_at = _aware_utc(now)
    with job_observation(queue=claim.queue.value, job_id=claim.work_id) as observation:
        async with tenant_session_scope(
            organization_id=claim.organization_id,
            identity_kind="background",
            role="worker",
        ) as db:
            await assert_active_claim(db, claim, now=started_at)
            result = await processor(db, claim)
            completed_at = started_at if now is not None else datetime.now(UTC)
            await defer_recurring_claim(
                db,
                claim,
                available_at=completed_at + cooldown,
                now=completed_at,
            )
            observation.outcome = job_outcome_for_result(result)
            return result
