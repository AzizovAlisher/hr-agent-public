"""Enqueue and state transitions for durable candidate processing work."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.candidate_processing import CandidateProcessingJob
from app.services.structured_hiring import resolve_application_for_write

ACTIVE_STATES = ("queued", "running", "retrying")
TERMINAL_STATES = ("succeeded", "dead_letter", "cancelled")


class CandidateProcessingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def external_idempotency_key(namespace: str, value: str) -> str:
    """Create a bounded, non-sensitive key from an external request token."""

    clean_namespace = namespace.strip().casefold()[:64]
    if not clean_namespace or not value or len(value) > 512:
        raise CandidateProcessingError("invalid_idempotency_key")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{clean_namespace}:{digest}"


async def _enqueue(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    candidate_id: uuid.UUID,
    position_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
    kind: str,
    idempotency_key: str,
    actor_user_id: uuid.UUID | None,
    max_attempts: int,
    available_at: datetime | None = None,
) -> tuple[CandidateProcessingJob, bool]:
    if kind not in {"parse_cv", "score"}:
        raise CandidateProcessingError("invalid_processing_kind")
    if (kind == "score") != (position_id is not None):
        raise CandidateProcessingError("invalid_processing_target")
    if (kind == "score") != (application_id is not None):
        raise CandidateProcessingError("invalid_processing_application")
    if not 1 <= len(idempotency_key) <= 160:
        raise CandidateProcessingError("invalid_idempotency_key")
    if not 1 <= max_attempts <= 10:
        raise CandidateProcessingError("invalid_max_attempts")
    current = available_at or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CandidateProcessingError("invalid_available_at")

    # One transaction-scoped lock serializes both idempotency replay and the
    # partial active-job uniqueness checks without exception-driven rollback.
    lock_key = (
        f"candidate-processing:{organization_id}:{kind}:{candidate_id}:"
        f"{position_id or 'none'}:{application_id or 'none'}"
    )
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    replay = (
        await db.execute(
            select(CandidateProcessingJob).where(
                CandidateProcessingJob.organization_id == organization_id,
                CandidateProcessingJob.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if replay is not None:
        if (
            replay.kind != kind
            or replay.candidate_id != candidate_id
            or replay.position_id != position_id
            or replay.application_id != application_id
        ):
            raise CandidateProcessingError("idempotency_conflict")
        return replay, False

    active_statement = select(CandidateProcessingJob).where(
        CandidateProcessingJob.organization_id == organization_id,
        CandidateProcessingJob.candidate_id == candidate_id,
        CandidateProcessingJob.kind == kind,
        CandidateProcessingJob.status.in_(ACTIVE_STATES),
    )
    if kind == "score":
        active_statement = active_statement.where(
            CandidateProcessingJob.application_id == application_id
        )
    active = (await db.execute(active_statement)).scalar_one_or_none()
    if active is not None:
        return active, False

    job = CandidateProcessingJob(
        id=uuid.uuid4(),
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        application_id=application_id,
        created_by_user_id=actor_user_id,
        kind=kind,
        status="queued",
        idempotency_key=idempotency_key,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=current,
        result_summary={},
    )
    db.add(job)
    await db.flush()
    return job, True


async def enqueue_candidate_parse_job(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    candidate_id: uuid.UUID,
    idempotency_key: str,
    actor_user_id: uuid.UUID | None,
    available_at: datetime | None = None,
) -> tuple[CandidateProcessingJob, bool]:
    return await _enqueue(
        db,
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=None,
        application_id=None,
        kind="parse_cv",
        idempotency_key=idempotency_key,
        actor_user_id=actor_user_id,
        max_attempts=3,
        available_at=available_at,
    )


async def enqueue_candidate_scoring_job(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    candidate_id: uuid.UUID,
    position_id: uuid.UUID,
    application_id: uuid.UUID | None = None,
    idempotency_key: str,
    actor_user_id: uuid.UUID | None,
    available_at: datetime | None = None,
) -> tuple[CandidateProcessingJob, bool]:
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == candidate_id,
                Candidate.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise CandidateProcessingError("candidate_not_found")

    application = await resolve_application_for_write(
        db,
        organization_id=organization_id,
        candidate_id=candidate.id,
        position_id=position_id,
        application_id=application_id,
        source=candidate.source or "scoring",
        actor_user_id=actor_user_id,
        actor_kind="human" if actor_user_id is not None else "automation",
        reason="candidate_scoring_queued",
        occurred_at=available_at,
    )

    current = available_at or datetime.now(UTC)
    # Legacy file-only candidates and freshly accepted uploads always get a
    # parser job first. The scoring worker defers without consuming an attempt
    # while that prerequisite remains active.
    if len((candidate.raw_cv or "").strip()) < 50 and candidate.pdf_path:
        await enqueue_candidate_parse_job(
            db,
            organization_id=organization_id,
            candidate_id=candidate_id,
            idempotency_key=f"parse-before-score:{candidate_id}:{candidate.private_object_id or 'legacy'}",
            actor_user_id=actor_user_id,
            available_at=current,
        )
        current = current + timedelta(seconds=2)

    return await _enqueue(
        db,
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position_id,
        application_id=application.id,
        kind="score",
        idempotency_key=idempotency_key,
        actor_user_id=actor_user_id,
        max_attempts=3,
        available_at=current,
    )


async def succeed_candidate_processing_job(
    db: AsyncSession,
    job: CandidateProcessingJob,
    *,
    result_summary: dict,
    now: datetime | None = None,
) -> bool:
    current_job = (
        await db.execute(
            select(CandidateProcessingJob)
            .where(
                CandidateProcessingJob.id == job.id,
                CandidateProcessingJob.organization_id == job.organization_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current_job is None:
        raise CandidateProcessingError("job_not_found")
    if current_job.status == "cancelled":
        # Application closure is authoritative. A worker that was already in
        # flight must acknowledge its lease without reviving the cancelled
        # job from stale ORM state.
        return False
    if current_job.status != "running":
        raise CandidateProcessingError("job_not_running")
    current = now or datetime.now(UTC)
    current_job.status = "succeeded"
    current_job.failure_code = None
    current_job.failure_detail = None
    current_job.result_summary = result_summary
    current_job.completed_at = current
    current_job.leased_at = None
    current_job.leased_by = None
    current_job.lease_expires_at = None
    await db.flush()
    return True


async def cancel_candidate_scoring_for_ai_unavailability(
    db: AsyncSession,
    job: CandidateProcessingJob,
    *,
    failure_code: str,
    detail: str,
    route: str,
    now: datetime | None = None,
) -> bool:
    """Finish one scoring job when tenant AI policy forbids provider I/O.

    Configuration and policy failures are not delivery failures: retrying the
    same immutable job cannot fix them, and placing them in the dead-letter
    queue would falsely report an operational outage.  ``cancelled`` is the
    existing non-DLQ terminal state.  The bounded result summary makes the
    outcome explicit to API clients while recording that no provider call was
    attempted.
    """

    current_job = (
        await db.execute(
            select(CandidateProcessingJob)
            .where(
                CandidateProcessingJob.id == job.id,
                CandidateProcessingJob.organization_id == job.organization_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current_job is None:
        raise CandidateProcessingError("job_not_found")
    if current_job.status == "cancelled":
        return False
    if current_job.status != "running":
        raise CandidateProcessingError("job_not_running")
    if current_job.kind != "score":
        raise CandidateProcessingError("job_not_scoring")

    current = now or datetime.now(UTC)
    stable_code = failure_code[:64]
    current_job.status = "cancelled"
    current_job.failure_code = stable_code
    current_job.failure_detail = detail[:500]
    current_job.result_summary = {
        "outcome": "not_scored",
        "reason": stable_code,
        "provenance": {
            "component": "tenant_ai_gateway",
            "route": route[:32],
            "provider_call_attempted": False,
        },
    }
    current_job.completed_at = current
    current_job.cancelled_at = current
    current_job.leased_at = None
    current_job.leased_by = None
    current_job.lease_expires_at = None
    await db.flush()
    return True


async def fail_candidate_processing_job(
    db: AsyncSession,
    job: CandidateProcessingJob,
    *,
    failure_code: str,
    retryable: bool,
    detail: str,
    preserve_attempt: bool = False,
    now: datetime | None = None,
) -> str | None:
    current_job = (
        await db.execute(
            select(CandidateProcessingJob)
            .where(
                CandidateProcessingJob.id == job.id,
                CandidateProcessingJob.organization_id == job.organization_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current_job is None:
        raise CandidateProcessingError("job_not_found")
    if current_job.status == "cancelled":
        return None
    if current_job.status != "running":
        raise CandidateProcessingError("job_not_running")
    current = now or datetime.now(UTC)
    current_job.failure_code = failure_code[:64]
    current_job.failure_detail = detail[:500]
    current_job.result_summary = {}
    current_job.leased_at = None
    current_job.leased_by = None
    current_job.lease_expires_at = None
    if preserve_attempt and current_job.attempt_count > 0:
        current_job.attempt_count -= 1
    if retryable and current_job.attempt_count < current_job.max_attempts:
        current_job.status = "retrying"
        current_job.available_at = current + timedelta(
            seconds=min(5 * (2 ** max(current_job.attempt_count - 1, 0)), 300)
        )
        await db.flush()
        return "retrying"
    current_job.status = "dead_letter"
    current_job.completed_at = current
    await db.flush()
    return "dead_letter"
