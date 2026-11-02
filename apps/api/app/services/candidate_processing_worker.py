"""Tenant-isolated worker for CV parsing and AI scoring jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionLocal
from app.llm import LLMConfigurationError, LLMModelBudgetExceeded
from app.models.candidate import Candidate
from app.models.candidate_processing import CandidateProcessingJob
from app.services.bounded_document_parser import (
    BoundedDocumentParseError,
    BoundedDocumentParseTimeout,
    extract_document_text,
)
from app.services.candidate_processing import (
    ACTIVE_STATES,
    cancel_candidate_scoring_for_ai_unavailability,
    fail_candidate_processing_job,
    succeed_candidate_processing_job,
)
from app.services.enterprise import TransientModelEndpointResolutionError
from app.services.scoring import ScoringEngineError, score_candidate
from app.services.uploads import resolve_resource_private_blob_reference
from app.services.worker_claims import (
    WorkClaim,
    WorkerRlsMode,
    WorkQueue,
    claim_work,
    process_one_claim,
)
from app.storage import BlobStoreError, get_blob_store

logger = logging.getLogger(__name__)


class RetryableCandidateProcessingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:candidate-processing"


async def _parse_document(path: Path, suffix: str) -> str:
    try:
        return await extract_document_text(path, suffix)
    except BoundedDocumentParseTimeout:
        raise RetryableCandidateProcessingError("document_parse_timeout") from None
    except BoundedDocumentParseError as exc:
        raise ValueError(str(exc)) from None


async def _parse_candidate_cv(
    db: AsyncSession,
    *,
    job: CandidateProcessingJob,
) -> dict:
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == job.candidate_id,
                Candidate.organization_id == job.organization_id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise ValueError("candidate_not_found")
    if not candidate.pdf_path:
        raise ValueError("candidate_document_missing")
    key = await resolve_resource_private_blob_reference(
        db,
        organization_id=job.organization_id,
        category="candidates",
        resource_type="candidate",
        resource_id=candidate.id,
        private_object_id=candidate.private_object_id,
        legacy_reference=candidate.pdf_path,
    )
    if key is None:
        raise ValueError("candidate_document_unavailable")
    suffix = Path(key).suffix.casefold()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("candidate_document_unsupported")
    try:
        async with get_blob_store().materialize(key) as path:
            extracted = await _parse_document(path, suffix)
    except BlobStoreError:
        raise RetryableCandidateProcessingError("private_storage_unavailable") from None
    if not extracted:
        raise ValueError("candidate_document_empty")
    candidate.raw_cv = extracted
    await db.flush()
    return {"characters": len(extracted), "document_type": suffix[1:]}


async def _score_candidate_job(
    db: AsyncSession,
    *,
    job: CandidateProcessingJob,
) -> dict:
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == job.candidate_id,
                Candidate.organization_id == job.organization_id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise ValueError("candidate_not_found")
    if len((candidate.raw_cv or "").strip()) < 50:
        parser = (
            await db.execute(
                select(CandidateProcessingJob)
                .where(
                    CandidateProcessingJob.organization_id == job.organization_id,
                    CandidateProcessingJob.candidate_id == job.candidate_id,
                    CandidateProcessingJob.kind == "parse_cv",
                )
                .order_by(CandidateProcessingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if parser is not None and parser.status in ACTIVE_STATES:
            raise RetryableCandidateProcessingError("candidate_parsing_pending")
        if parser is not None and parser.status == "dead_letter":
            raise ValueError("candidate_parse_failed")
        raise ValueError("insufficient_cv_text")
    if job.position_id is None or job.application_id is None:
        # Guarded by the exact-cycle database constraint; defensive for
        # metadata-only/legacy test rows.
        raise ValueError("position_not_found")
    try:
        async with asyncio.timeout(240):
            result = await score_candidate(
                candidate_id=job.candidate_id,
                position_id=job.position_id,
                application_id=job.application_id,
                db=db,
                organization_id=job.organization_id,
                actor_user_id=job.created_by_user_id,
                commit=False,
            )
    except TimeoutError:
        raise RetryableCandidateProcessingError("scoring_timeout") from None
    return {
        "overall_score": result.overall_score,
        "model": result.model_used,
    }


_SAFE_FAILURE_DETAILS = {
    "candidate_not_found": "Candidate is no longer available.",
    "position_not_found": "Position is no longer available.",
    "candidate_document_missing": "Candidate has no document to parse.",
    "candidate_document_unavailable": "Candidate document is unavailable.",
    "candidate_document_unsupported": "Candidate document type is unsupported.",
    "candidate_document_empty": "No text could be extracted from the document.",
    "document_parse_failed": "Candidate document could not be parsed safely.",
    "document_text_too_large": "Extracted document text exceeds the processing limit.",
    "insufficient_cv_text": "Candidate CV text is too short to score.",
    "candidate_parse_failed": "Candidate document parsing did not complete.",
    "candidate_parsing_pending": "Candidate document parsing is still in progress.",
    "private_storage_unavailable": "Private storage is temporarily unavailable.",
    "document_parse_timeout": "Candidate document parsing timed out.",
    "scoring_failed": "Candidate scoring could not be completed.",
    "scoring_timeout": "Candidate scoring timed out.",
    "ai_private_provider_resolution_unavailable": (
        "Private AI endpoint resolution is temporarily unavailable."
    ),
}

_AI_UNAVAILABLE_DETAILS = {
    "ai_managed_processing_opt_in_required": (
        "Managed AI processing is not enabled for this organization."
    ),
    "ai_managed_provider_unavailable": (
        "Managed AI scoring is unavailable for this organization."
    ),
    "ai_private_provider_unavailable": (
        "Private AI scoring is unavailable for this organization."
    ),
    "ai_provider_unavailable": "AI scoring is unavailable for this organization.",
    "ai_private_budget_accounting_unavailable": (
        "Private AI budget accounting is unavailable for this organization."
    ),
    "ai_budget_exceeded": "The AI scoring budget does not allow this operation.",
}


def _ai_route_for_failure(code: str) -> str:
    if code.startswith("ai_managed_"):
        return "managed"
    if code.startswith("ai_private_"):
        return "private"
    return "tenant_policy"


def _stable_ai_unavailability_code(code: object) -> str:
    value = code if isinstance(code, str) else ""
    return value if value in _AI_UNAVAILABLE_DETAILS else "ai_provider_unavailable"


def _log_handled_failure(
    job: CandidateProcessingJob,
    *,
    failure_code: str,
    outcome: str,
) -> None:
    """Log only stable queue metadata; never exception text or candidate data."""

    logger.warning(
        "Candidate processing handled failure",
        extra={
            "event": "candidate_processing_handled_failure",
            "candidate_processing_job_id": str(job.id),
            "organization_id": str(job.organization_id),
            "processing_kind": job.kind,
            "failure_code": failure_code,
            "processing_outcome": outcome,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
        },
    )


async def process_candidate_processing_jobs_once(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    worker_id: str,
    limit: int,
    mode: WorkerRlsMode,
    lease_seconds: int = 300,
) -> dict[str, int]:
    async with session_factory() as discovery:
        claims = await claim_work(
            discovery,
            queue=WorkQueue.CANDIDATE_PROCESSING,
            worker_id=worker_id,
            limit=max(1, min(limit, 100)),
            mode=mode,
            lease_seconds=lease_seconds,
        )
        await discovery.commit()
    counts = {
        "claimed": len(claims),
        "succeeded": 0,
        "not_scored": 0,
        "retried": 0,
        "dead_lettered": 0,
        "abandoned": 0,
    }

    async def process(db: AsyncSession, claim: WorkClaim) -> str | None:
        job = (
            await db.execute(
                select(CandidateProcessingJob).where(
                    CandidateProcessingJob.id == claim.work_id,
                    CandidateProcessingJob.organization_id == claim.organization_id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        if job.status in {"queued", "retrying"}:
            # Explicit-off discovery leaves source state unchanged; mirror the
            # fixed broker function's transition in the tenant transaction.
            job.status = "running"
            job.attempt_count += 1
            job.leased_at = datetime.now(UTC)
            job.leased_by = worker_id
            job.lease_expires_at = claim.lease_expires_at
            job.failure_code = None
            job.failure_detail = None
            await db.flush()
        if job.status != "running":
            return None
        try:
            summary = (
                await _parse_candidate_cv(db, job=job)
                if job.kind == "parse_cv"
                else await _score_candidate_job(db, job=job)
            )
        except TransientModelEndpointResolutionError:
            code = "ai_private_provider_resolution_unavailable"
            outcome = await fail_candidate_processing_job(
                db,
                job,
                failure_code=code,
                retryable=True,
                detail=_SAFE_FAILURE_DETAILS[code],
            )
            if outcome is not None:
                _log_handled_failure(job, failure_code=code, outcome=outcome)
            return outcome
        except LLMConfigurationError as exc:
            code = _stable_ai_unavailability_code(exc.code)
            completed = await cancel_candidate_scoring_for_ai_unavailability(
                db,
                job,
                failure_code=code,
                detail=_AI_UNAVAILABLE_DETAILS[code],
                route=_ai_route_for_failure(code),
            )
            if completed:
                _log_handled_failure(
                    job,
                    failure_code=code,
                    outcome="not_scored",
                )
                return "cancelled"
            return None
        except LLMModelBudgetExceeded:
            code = "ai_budget_exceeded"
            completed = await cancel_candidate_scoring_for_ai_unavailability(
                db,
                job,
                failure_code=code,
                detail=_AI_UNAVAILABLE_DETAILS[code],
                route=_ai_route_for_failure(code),
            )
            if completed:
                _log_handled_failure(
                    job,
                    failure_code=code,
                    outcome="not_scored",
                )
                return "cancelled"
            return None
        except ValueError as exc:
            reported_code = str(exc)
            code = (
                reported_code
                if reported_code in _SAFE_FAILURE_DETAILS
                else (
                    "scoring_failed" if job.kind == "score" else "document_parse_failed"
                )
            )
            outcome = await fail_candidate_processing_job(
                db,
                job,
                failure_code=code,
                retryable=code == "scoring_failed",
                detail=_SAFE_FAILURE_DETAILS.get(
                    code, "Candidate processing could not be completed."
                ),
            )
            if outcome is not None:
                _log_handled_failure(job, failure_code=code, outcome=outcome)
            return outcome
        except ScoringEngineError as exc:
            code = (
                "insufficient_cv_text"
                if str(exc).startswith("Insufficient CV text")
                else "scoring_failed"
            )
            outcome = await fail_candidate_processing_job(
                db,
                job,
                failure_code=code,
                retryable=code == "scoring_failed",
                detail=_SAFE_FAILURE_DETAILS[code],
            )
            if outcome is not None:
                _log_handled_failure(job, failure_code=code, outcome=outcome)
            return outcome
        except RetryableCandidateProcessingError as exc:
            code = exc.code
            outcome = await fail_candidate_processing_job(
                db,
                job,
                failure_code=code,
                retryable=True,
                preserve_attempt=code == "candidate_parsing_pending",
                detail=_SAFE_FAILURE_DETAILS.get(
                    code, "Candidate processing is temporarily unavailable."
                ),
            )
            if outcome is not None:
                _log_handled_failure(job, failure_code=code, outcome=outcome)
            return outcome
        except Exception:  # noqa: BLE001 - worker boundary sanitizes provider errors
            code = "scoring_failed" if job.kind == "score" else "document_parse_failed"
            outcome = await fail_candidate_processing_job(
                db,
                job,
                failure_code=code,
                retryable=True,
                detail=(
                    _SAFE_FAILURE_DETAILS["scoring_failed"]
                    if job.kind == "score"
                    else _SAFE_FAILURE_DETAILS["document_parse_failed"]
                ),
            )
            if outcome is not None:
                _log_handled_failure(job, failure_code=code, outcome=outcome)
            return outcome
        succeeded = await succeed_candidate_processing_job(
            db, job, result_summary=summary
        )
        return "succeeded" if succeeded else None

    for claim in claims:
        try:
            outcome = await process_one_claim(claim, process)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Candidate processing item abandoned",
                extra={"candidate_processing_job_id": str(claim.work_id)},
            )
            counts["abandoned"] += 1
            continue
        if outcome == "succeeded":
            counts["succeeded"] += 1
        elif outcome == "cancelled":
            counts["not_scored"] += 1
        elif outcome == "retrying":
            counts["retried"] += 1
        elif outcome == "dead_letter":
            counts["dead_lettered"] += 1
    return counts


async def run_candidate_processing_worker(*, worker_id: str | None = None) -> None:
    settings = get_settings()
    poll_seconds = max(
        0.5, min(settings.candidate_processing_worker_poll_seconds, 60.0)
    )
    claim_limit = max(1, min(settings.candidate_processing_claim_limit, 100))
    mode = getattr(settings, "tenant_rls_mode", None)
    if mode not in {"forced", "off"}:
        raise RuntimeError("TENANT_RLS_MODE must be explicitly 'forced' or 'off'")
    identity = worker_id or _worker_id()
    while True:
        try:
            await process_candidate_processing_jobs_once(
                worker_id=identity,
                limit=claim_limit,
                mode=mode,
                lease_seconds=300,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Candidate processing worker iteration failed")
        await asyncio.sleep(poll_seconds)
