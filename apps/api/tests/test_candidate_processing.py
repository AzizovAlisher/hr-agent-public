"""Durable CV parsing/scoring queue contracts and crash recovery."""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from docx import Document
from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.llm import LLMConfigurationError
from app.models.candidate import Candidate
from app.models.candidate_processing import CandidateProcessingJob
from app.models.enterprise import OrganizationModelConfiguration
from app.models.organization import Organization
from app.models.position import Position
from app.models.user import User
from app.routers.candidates import (
    get_candidate_processing_job,
    score_all_open,
    score_candidate_endpoint,
)
from app.schemas.candidates import ScoreRequest
from app.services.bounded_document_parser import (
    BoundedDocumentParseError,
    extract_document_text,
    extract_spreadsheet_inventory,
    extract_spreadsheet_rows,
)
from app.services.candidate_processing import (
    CandidateProcessingError,
    enqueue_candidate_scoring_job,
    fail_candidate_processing_job,
    succeed_candidate_processing_job,
)
from app.services.candidate_processing_worker import (
    _parse_document,
    process_candidate_processing_jobs_once,
)
from app.services.enterprise import (
    MANAGED_PROCESSING_NOTICE_VERSION,
    organization_llm_client,
)
from app.services.operations import _queue_component

pytestmark = pytest.mark.asyncio


async def _seed_pair(db, label: str = "processing"):
    marker = uuid4().hex[:10]
    organization = Organization(name=label, slug=f"{label}-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"owner-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(user)
    await db.flush()
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        raw_cv=(
            "Senior Python engineer building reliable distributed services "
            "with PostgreSQL, observability, and measurable delivery impact."
        ),
    )
    position = Position(
        organization_id=organization.id,
        title="Platform Engineer",
        description="Build reliable hiring platform services.",
        status="open",
        created_by=user.id,
    )
    other_position = Position(
        organization_id=organization.id,
        title="Backend Engineer",
        description="Build backend services.",
        status="open",
        created_by=user.id,
    )
    db.add_all([candidate, position, other_position])
    await db.commit()
    return organization, user, candidate, position, other_position


async def test_enqueue_is_idempotent_and_deduplicates_active_pair(db):
    organization, user, candidate, position, other_position = await _seed_pair(db)
    first, first_created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="request-one",
        actor_user_id=user.id,
    )
    replay, replay_created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="request-one",
        actor_user_id=user.id,
    )
    duplicate, duplicate_created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="request-two",
        actor_user_id=user.id,
    )
    assert first_created is True
    assert replay_created is False
    assert duplicate_created is False
    assert first.id == replay.id == duplicate.id

    with pytest.raises(CandidateProcessingError) as conflict:
        await enqueue_candidate_scoring_job(
            db,
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=other_position.id,
            idempotency_key="request-one",
            actor_user_id=user.id,
        )
    assert conflict.value.code == "idempotency_conflict"


async def test_worker_completes_job_once_without_request_process(monkeypatch, db):
    organization, user, candidate, position, _other = await _seed_pair(db, "worker")
    job, _ = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="worker-score",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()
    calls: list[dict] = []

    async def fake_score_candidate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(overall_score=87, model_used="test/model")

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        fake_score_candidate,
    )
    first = await process_candidate_processing_jobs_once(
        worker_id="test:candidate-processing",
        limit=10,
        mode="off",
    )
    second = await process_candidate_processing_jobs_once(
        worker_id="test:candidate-processing",
        limit=10,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert first == {
        "claimed": 1,
        "succeeded": 1,
        "not_scored": 0,
        "retried": 0,
        "dead_lettered": 0,
        "abandoned": 0,
    }
    assert second["claimed"] == 0
    assert len(calls) == 1
    assert calls[0]["commit"] is False
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.attempt_count == 1
    assert persisted.result_summary == {
        "overall_score": 87,
        "model": "test/model",
    }


async def test_public_file_parse_survives_missing_managed_opt_in_without_dlq(
    monkeypatch,
    db,
    tmp_path,
    caplog,
):
    """Public-like intake parses locally and truthfully skips unavailable AI."""

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "blob_store_backend", "local")
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    organization, _user, candidate, position, _other = await _seed_pair(
        db, "public-no-ai"
    )
    document_path = tmp_path / "cvs" / "public-applicant.docx"
    document_path.parent.mkdir(parents=True)
    document = Document()
    document.add_paragraph(
        "Public applicant has substantial Python, PostgreSQL, distributed "
        "systems, observability, and delivery experience for local parsing."
    )
    document.save(document_path)
    candidate.raw_cv = None
    candidate.pdf_path = str(document_path)
    candidate_id = candidate.id
    await db.commit()

    score_job, _created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate_id,
        position_id=position.id,
        idempotency_key=f"public-application:{candidate_id}",
        actor_user_id=None,
    )
    score_job_id = score_job.id
    parse_job = (
        await db.execute(
            select(CandidateProcessingJob).where(
                CandidateProcessingJob.organization_id == organization.id,
                CandidateProcessingJob.candidate_id == candidate_id,
                CandidateProcessingJob.kind == "parse_cv",
            )
        )
    ).scalar_one()
    parse_job_id = parse_job.id
    await db.commit()

    parsed = await process_candidate_processing_jobs_once(
        worker_id="test:public-parse",
        limit=10,
        mode="off",
    )
    assert parsed == {
        "claimed": 1,
        "succeeded": 1,
        "not_scored": 0,
        "retried": 0,
        "dead_lettered": 0,
        "abandoned": 0,
    }
    db.expire_all()
    persisted_parse = await db.get(CandidateProcessingJob, parse_job_id)
    persisted_candidate = await db.get(Candidate, candidate_id)
    assert persisted_parse is not None and persisted_parse.status == "succeeded"
    assert persisted_candidate is not None
    assert "distributed systems" in (persisted_candidate.raw_cv or "")

    persisted_score = await db.get(CandidateProcessingJob, score_job_id)
    assert persisted_score is not None
    persisted_score.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    caplog.set_level(
        logging.WARNING,
        logger="app.services.candidate_processing_worker",
    )
    not_scored = await process_candidate_processing_jobs_once(
        worker_id="test:public-no-managed-opt-in",
        limit=10,
        mode="off",
    )
    assert not_scored == {
        "claimed": 1,
        "succeeded": 0,
        "not_scored": 1,
        "retried": 0,
        "dead_lettered": 0,
        "abandoned": 0,
    }

    db.expire_all()
    persisted_score = await db.get(CandidateProcessingJob, score_job_id)
    assert persisted_score is not None
    assert persisted_score.status == "cancelled"
    assert persisted_score.attempt_count == 1
    assert persisted_score.failure_code == "ai_managed_processing_opt_in_required"
    assert persisted_score.failure_detail == (
        "Managed AI processing is not enabled for this organization."
    )
    assert persisted_score.completed_at is not None
    assert persisted_score.cancelled_at == persisted_score.completed_at
    assert persisted_score.result_summary == {
        "outcome": "not_scored",
        "reason": "ai_managed_processing_opt_in_required",
        "provenance": {
            "component": "tenant_ai_gateway",
            "route": "managed",
            "provider_call_attempted": False,
        },
    }
    assert (
        await process_candidate_processing_jobs_once(
            worker_id="test:public-no-managed-opt-in-replay",
            limit=10,
            mode="off",
        )
    )["claimed"] == 0

    queue = await _queue_component(
        db,
        now=datetime.now(UTC),
        mode="off",
        required=True,
    )
    candidate_queue = next(
        item
        for item in queue["data"]["queues"]
        if item["queue"] == "candidate_processing_jobs"
    )
    assert queue["status"] == "ok"
    assert queue["data"]["dlq_total"] == 0
    assert candidate_queue["due_count"] == 0
    assert candidate_queue["dlq_count"] == 0

    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "candidate_processing_handled_failure"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.candidate_processing_job_id == str(score_job_id)
    assert record.failure_code == "ai_managed_processing_opt_in_required"
    assert record.processing_outcome == "not_scored"
    assert not hasattr(record, "candidate_id")
    assert "Public applicant" not in caplog.text


@pytest.mark.parametrize(
    ("provider", "expected_managed", "expected_model"),
    [
        ("vllm", False, "private/qwen"),
        ("openrouter", True, "managed/gemini"),
    ],
)
async def test_available_private_and_managed_routes_still_complete_scoring(
    monkeypatch,
    db,
    provider,
    expected_managed,
    expected_model,
):
    organization, user, candidate, position, _other = await _seed_pair(
        db, f"available-{provider}"
    )
    now = datetime.now(UTC)
    config = OrganizationModelConfiguration(
        organization_id=organization.id,
        provider=provider,
        base_url=("http://model.internal:8001/v1" if provider == "vllm" else None),
        model=expected_model,
        status="ready",
        data_residency="on_prem" if provider == "vllm" else "provider",
        managed_processing_accepted_at=now if provider == "openrouter" else None,
        managed_processing_accepted_by_user_id=(
            user.id if provider == "openrouter" else None
        ),
        managed_processing_notice_version=(
            MANAGED_PROCESSING_NOTICE_VERSION if provider == "openrouter" else None
        ),
    )
    db.add(config)
    await db.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key-never-sent")

    async def resolved_private_endpoint(_value):
        return SimpleNamespace(base_url="http://model.internal:8001/v1")

    monkeypatch.setattr(
        "app.services.enterprise.resolve_model_endpoint",
        resolved_private_endpoint,
    )
    observed_routes = []

    async def routed_score(**kwargs):
        gateway = await organization_llm_client(
            kwargs["db"],
            kwargs["organization_id"],
        )
        observed_routes.append((gateway._provider, gateway._managed))
        return SimpleNamespace(overall_score=91, model_used=expected_model)

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        routed_score,
    )
    job, _created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key=f"available-route:{provider}",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()

    result = await process_candidate_processing_jobs_once(
        worker_id=f"test:available-{provider}",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert result["succeeded"] == 1
    assert result["not_scored"] == 0
    assert observed_routes == [(provider, expected_managed)]
    assert persisted is not None and persisted.status == "succeeded"
    assert persisted.result_summary == {
        "overall_score": 91,
        "model": expected_model,
    }


async def test_transient_private_model_dns_failure_retries_before_provider_call(
    monkeypatch,
    db,
    caplog,
):
    organization, user, candidate, position, _other = await _seed_pair(
        db, "private-dns-retry"
    )
    endpoint = "http://model.internal:8001/v1"
    db.add(
        OrganizationModelConfiguration(
            organization_id=organization.id,
            provider="vllm",
            base_url=endpoint,
            model="private/qwen",
            status="ready",
            data_residency="on_prem",
        )
    )
    job, _created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="private-dns-retry",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_private_base_url_allowlist", endpoint)
    resolutions = 0
    provider_calls = 0

    async def resolving_once(_hostname: str, _port: int) -> frozenset[str]:
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            raise socket.gaierror("candidate@example.test resolver detail")
        return frozenset({"10.0.0.8"})

    class FakePrivateClient:
        def __init__(self, **_kwargs):
            pass

    async def routed_score(**kwargs):
        nonlocal provider_calls
        await organization_llm_client(kwargs["db"], kwargs["organization_id"])
        provider_calls += 1
        return SimpleNamespace(overall_score=88, model_used="private/qwen")

    monkeypatch.setattr(
        "app.llm.vllm.resolve_host_addresses",
        resolving_once,
    )
    monkeypatch.setattr("app.llm.vllm.VLLMClient", FakePrivateClient)
    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        routed_score,
    )
    caplog.set_level(
        logging.WARNING,
        logger="app.services.candidate_processing_worker",
    )

    first = await process_candidate_processing_jobs_once(
        worker_id="test:private-dns:first",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert first["retried"] == 1
    assert first["dead_lettered"] == first["not_scored"] == 0
    assert resolutions == 1
    assert provider_calls == 0
    assert persisted is not None and persisted.status == "retrying"
    assert persisted.attempt_count == 1
    assert persisted.failure_code == "ai_private_provider_resolution_unavailable"
    assert persisted.failure_detail == (
        "Private AI endpoint resolution is temporarily unavailable."
    )
    assert "candidate@example.test" not in caplog.text

    persisted.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    second = await process_candidate_processing_jobs_once(
        worker_id="test:private-dns:second",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert second["succeeded"] == 1
    assert second["retried"] == second["dead_lettered"] == 0
    assert resolutions == 2
    assert provider_calls == 1
    assert persisted is not None and persisted.status == "succeeded"
    assert persisted.attempt_count == 2


@pytest.mark.parametrize(
    ("status", "base_url"),
    [
        ("ready", "http://unapproved.internal:8001/v1"),
        ("unreachable", "http://model.internal:8001/v1"),
    ],
    ids=("non-allowlisted", "permanently-not-ready"),
)
async def test_private_model_policy_or_configuration_failure_is_terminal(
    monkeypatch,
    db,
    status,
    base_url,
):
    organization, user, candidate, position, _other = await _seed_pair(
        db, f"private-terminal-{status}"
    )
    approved_endpoint = "http://model.internal:8001/v1"
    db.add(
        OrganizationModelConfiguration(
            organization_id=organization.id,
            provider="vllm",
            base_url=base_url,
            model="private/qwen",
            status=status,
            data_residency="on_prem",
        )
    )
    job, _created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key=f"private-terminal:{status}",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()
    monkeypatch.setattr(
        get_settings(),
        "llm_private_base_url_allowlist",
        approved_endpoint,
    )
    provider_calls = 0

    async def routed_score(**kwargs):
        nonlocal provider_calls
        await organization_llm_client(kwargs["db"], kwargs["organization_id"])
        provider_calls += 1
        return SimpleNamespace(overall_score=88, model_used="private/qwen")

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        routed_score,
    )
    result = await process_candidate_processing_jobs_once(
        worker_id=f"test:private-terminal:{status}",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert result["not_scored"] == 1
    assert result["retried"] == result["dead_lettered"] == 0
    assert provider_calls == 0
    assert persisted is not None and persisted.status == "cancelled"
    assert persisted.attempt_count == 1
    assert persisted.failure_code == "ai_private_provider_unavailable"
    assert persisted.result_summary == {
        "outcome": "not_scored",
        "reason": "ai_private_provider_unavailable",
        "provenance": {
            "component": "tenant_ai_gateway",
            "route": "private",
            "provider_call_attempted": False,
        },
    }


async def test_unknown_configuration_code_is_sanitized_and_non_retrying(
    monkeypatch,
    db,
):
    organization, user, candidate, position, _other = await _seed_pair(
        db, "unknown-ai-code"
    )
    job, _created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="unknown-ai-code",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()

    async def unsafe_configuration_error(**_kwargs):
        raise LLMConfigurationError(
            "tenant-secret-marker",
            "candidate@example.test and provider internals",
        )

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        unsafe_configuration_error,
    )
    result = await process_candidate_processing_jobs_once(
        worker_id="test:unknown-ai-code",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert result["not_scored"] == 1
    assert result["retried"] == result["dead_lettered"] == 0
    assert persisted is not None and persisted.status == "cancelled"
    assert persisted.failure_code == "ai_provider_unavailable"
    assert "tenant-secret-marker" not in str(persisted.result_summary)
    assert "candidate@example.test" not in (persisted.failure_detail or "")


async def test_retry_releases_lease_and_recovers_without_duplicate_effect(
    monkeypatch, db
):
    organization, user, candidate, position, _other = await _seed_pair(db, "retry")
    job, _ = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="retry-score",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()

    async def unavailable(**_kwargs):
        raise OSError("provider details must not be persisted")

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        unavailable,
    )
    failed = await process_candidate_processing_jobs_once(
        worker_id="test:retry-one",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert failed["retried"] == 1
    assert persisted is not None
    assert persisted.status == "retrying"
    assert persisted.failure_code == "scoring_failed"
    assert "provider details" not in (persisted.failure_detail or "")

    persisted.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    async def recovered(**_kwargs):
        return SimpleNamespace(overall_score=72, model_used="test/recovered")

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        recovered,
    )
    recovered_counts = await process_candidate_processing_jobs_once(
        worker_id="test:retry-two",
        limit=1,
        mode="off",
    )
    db.expire_all()
    persisted = await db.get(CandidateProcessingJob, job_id)
    assert recovered_counts["succeeded"] == 1
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.attempt_count == 2


async def test_transient_provider_errors_retry_to_boundary_and_degrade_readiness(
    monkeypatch,
    db,
    caplog,
):
    organization, user, candidate, position, _other = await _seed_pair(
        db, "transient-boundary"
    )
    job, _created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="transient-boundary",
        actor_user_id=user.id,
    )
    job_id = job.id
    await db.commit()

    async def transient_provider_failure(**_kwargs):
        raise OSError("candidate@example.test must never reach logs")

    monkeypatch.setattr(
        "app.services.candidate_processing_worker.score_candidate",
        transient_provider_failure,
    )
    caplog.set_level(
        logging.WARNING,
        logger="app.services.candidate_processing_worker",
    )
    outcomes = []
    for attempt in range(1, 4):
        counts = await process_candidate_processing_jobs_once(
            worker_id=f"test:transient-boundary:{attempt}",
            limit=1,
            mode="off",
        )
        db.expire_all()
        persisted = await db.get(CandidateProcessingJob, job_id)
        assert persisted is not None and persisted.attempt_count == attempt
        outcomes.append((counts["retried"], counts["dead_lettered"]))
        if attempt < 3:
            assert persisted.status == "retrying"
            persisted.available_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

    assert outcomes == [(1, 0), (1, 0), (0, 1)]
    assert persisted.status == "dead_letter"
    assert persisted.failure_code == "scoring_failed"
    assert "candidate@example.test" not in (persisted.failure_detail or "")

    queue = await _queue_component(
        db,
        now=datetime.now(UTC),
        mode="off",
        required=True,
    )
    candidate_queue = next(
        item
        for item in queue["data"]["queues"]
        if item["queue"] == "candidate_processing_jobs"
    )
    assert queue["status"] == "degraded"
    assert queue["data"]["dlq_total"] == 1
    assert candidate_queue["dlq_count"] == 1
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "candidate_processing_handled_failure"
    ]
    assert [record.processing_outcome for record in records] == [
        "retrying",
        "retrying",
        "dead_letter",
    ]
    assert all(record.failure_code == "scoring_failed" for record in records)
    assert "candidate@example.test" not in caplog.text


async def test_cancelled_cycle_jobs_cannot_be_revived_by_stale_workers(db):
    organization, user, candidate, position, other_position = await _seed_pair(
        db, "cancel-race"
    )
    succeeding_job, _ = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        idempotency_key="cancel-before-success",
        actor_user_id=user.id,
    )
    failing_job, _ = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=other_position.id,
        idempotency_key="cancel-before-failure",
        actor_user_id=user.id,
    )
    for job in (succeeding_job, failing_job):
        job.status = "running"
        job.attempt_count = 1
    await db.commit()

    cancelled_at = datetime.now(UTC)
    async with SessionLocal() as closer:
        await closer.execute(
            update(CandidateProcessingJob)
            .where(CandidateProcessingJob.id.in_((succeeding_job.id, failing_job.id)))
            .values(
                status="cancelled",
                cancelled_at=cancelled_at,
                completed_at=cancelled_at,
                failure_code="application_closed",
            )
        )
        await closer.commit()

    # Both objects still carry the worker's stale in-memory `running` state.
    assert succeeding_job.status == "running"
    assert failing_job.status == "running"
    assert (
        await succeed_candidate_processing_job(
            db,
            succeeding_job,
            result_summary={"overall_score": 99},
        )
        is False
    )
    assert (
        await fail_candidate_processing_job(
            db,
            failing_job,
            failure_code="provider_timeout",
            retryable=True,
            detail="Provider timed out.",
        )
        is None
    )
    await db.commit()

    persisted = list(
        (
            await db.execute(
                select(CandidateProcessingJob).where(
                    CandidateProcessingJob.id.in_((succeeding_job.id, failing_job.id))
                )
            )
        ).scalars()
    )
    assert {job.status for job in persisted} == {"cancelled"}
    assert all(job.failure_code == "application_closed" for job in persisted)


async def test_document_parser_runs_in_bounded_child_process(tmp_path):
    path = tmp_path / "candidate.docx"
    document = Document()
    document.add_paragraph("Durable parser isolates candidate document extraction.")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Table-based CV evidence"
    document.save(path)

    extracted = await _parse_document(path, ".docx")

    assert "Durable parser isolates" in extracted
    assert "Table-based CV evidence" in extracted


async def test_spreadsheet_parser_runs_in_bounded_child_process(tmp_path):
    path = tmp_path / "candidates.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    sheet.append(["Ada", "Python and PostgreSQL delivery experience"])
    workbook.save(path)

    rows = await extract_spreadsheet_rows(path)

    assert rows == [
        {"name": "Ada", "cv_text": "Python and PostgreSQL delivery experience"}
    ]


async def test_spreadsheet_parser_rejects_formula_cells(tmp_path):
    path = tmp_path / "formula-candidate.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    sheet.append(["Ada", '="candidate@example.test"'])
    workbook.save(path)
    workbook.close()

    with pytest.raises(
        BoundedDocumentParseError,
        match="spreadsheet_formula_unsupported",
    ):
        await extract_spreadsheet_rows(path)


@pytest.mark.parametrize("hidden", [False, True], ids=("visible", "hidden"))
async def test_spreadsheet_parser_rejects_additional_worksheet(tmp_path, hidden):
    path = tmp_path / f"extra-sheet-{hidden}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    sheet.append(["Ada", "Python"])
    extra = workbook.create_sheet("Candidate private data")
    extra.append(["candidate@example.test"])
    if hidden:
        extra.sheet_state = "hidden"
    workbook.save(path)
    workbook.close()

    with pytest.raises(
        BoundedDocumentParseError,
        match="spreadsheet_sheet_count_unsupported",
    ):
        await extract_spreadsheet_rows(path)


async def test_spreadsheet_inventory_includes_allowed_core_metadata(tmp_path):
    marker = f"metadata-{uuid4().hex}@candidate.example"
    path = tmp_path / "metadata.xlsx"
    workbook = Workbook()
    workbook.properties.title = marker
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    sheet.append(["Ada", "Python"])
    workbook.save(path)
    workbook.close()

    inventory = await extract_spreadsheet_inventory(path)

    assert inventory.rows == ({"name": "Ada", "cv_text": "Python"},)
    assert marker in inventory.metadata_text


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("comment", "spreadsheet_package_unsupported"),
        ("hyperlink", "spreadsheet_package_unsupported"),
        ("defined-name", "spreadsheet_feature_unsupported"),
        ("custom-property", "spreadsheet_package_unsupported"),
    ],
)
async def test_spreadsheet_parser_rejects_uninventoried_ooxml_surfaces(
    tmp_path,
    case,
    expected_code,
):
    path = tmp_path / f"unsupported-{case}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "cv_text"])
    sheet.append(["Ada", "Python"])
    if case == "comment":
        sheet["B2"].comment = Comment("candidate@example.test", "Candidate")
    elif case == "hyperlink":
        sheet["B2"].hyperlink = "mailto:candidate@example.test"
    elif case == "defined-name":
        workbook.defined_names.add(
            DefinedName("CandidateMarker", attr_text="Sheet!$B$2")
        )
    workbook.save(path)
    workbook.close()
    if case == "custom-property":
        with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "docProps/custom.xml",
                "<properties><marker>candidate@example.test</marker></properties>",
            )

    with pytest.raises(BoundedDocumentParseError, match=expected_code):
        await extract_spreadsheet_rows(path)


async def test_spreadsheet_parser_rejects_more_than_synchronous_limit(tmp_path):
    path = tmp_path / "too-many-candidates.xlsx"
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(["name", "cv_text"])
    for index in range(501):
        sheet.append([f"Candidate {index}", "Bounded candidate evidence"])
    workbook.save(path)

    with pytest.raises(
        BoundedDocumentParseError,
        match="spreadsheet_row_limit_exceeded",
    ):
        await extract_spreadsheet_rows(path)


async def test_document_parser_rejects_instead_of_silently_truncating(tmp_path):
    path = tmp_path / "oversized-text.docx"
    document = Document()
    document.add_paragraph("x" * 500_001)
    document.save(path)

    with pytest.raises(
        BoundedDocumentParseError,
        match="document_text_limit_exceeded",
    ):
        await extract_document_text(path)


@pytest.mark.parametrize(
    ("headers", "row", "expected_code"),
    [
        (["name", ""], ["Ada", "CV"], "spreadsheet_header_blank"),
        (["Name", " name "], ["Ada", "CV"], "spreadsheet_header_duplicate"),
        (
            [f"column-{index}" for index in range(129)],
            ["value"] * 129,
            "spreadsheet_column_limit_exceeded",
        ),
        (
            ["name", "cv_text"],
            ["Ada", "x" * 20_001],
            "spreadsheet_cell_limit_exceeded",
        ),
    ],
)
async def test_spreadsheet_parser_rejects_ambiguous_or_oversized_cells(
    tmp_path,
    headers,
    row,
    expected_code,
):
    path = tmp_path / f"{expected_code}.xlsx"
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(headers)
    sheet.append(row)
    workbook.save(path)

    with pytest.raises(BoundedDocumentParseError, match=expected_code):
        await extract_spreadsheet_rows(path)


async def test_spreadsheet_parser_rejects_total_text_limit(tmp_path):
    path = tmp_path / "total-text-limit.xlsx"
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(["left", "right"])
    random_source = random.Random(7)
    for _index in range(51):
        sheet.append(
            [
                random_source.randbytes(10_000).hex(),
                random_source.randbytes(10_000).hex(),
            ]
        )
    workbook.save(path)

    with pytest.raises(
        BoundedDocumentParseError,
        match="spreadsheet_total_text_limit_exceeded",
    ):
        await extract_spreadsheet_rows(path)


async def test_parser_child_does_not_block_api_event_loop(tmp_path):
    path = tmp_path / "responsive.xlsx"
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet()
    sheet.append(["name", "cv_text"])
    for index in range(200):
        sheet.append([f"Candidate {index}", "bounded evidence"])
    workbook.save(path)
    ticks = 0
    finished = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not finished.is_set():
            ticks += 1
            await asyncio.sleep(0)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        rows = await extract_spreadsheet_rows(path)
    finally:
        finished.set()
        await heartbeat_task

    assert len(rows) == 200
    assert ticks > 1


async def test_scoring_api_returns_real_receipts_and_hides_unknown_jobs(db):
    _organization, user, candidate, position, other_position = await _seed_pair(
        db, "receipt"
    )
    first = await score_candidate_endpoint(
        candidate.id,
        ScoreRequest(position_id=position.id),
        idempotency_key="stable-browser-request",
        db=db,
        user=user,
    )
    replay = await score_candidate_endpoint(
        candidate.id,
        ScoreRequest(position_id=position.id),
        idempotency_key="stable-browser-request",
        db=db,
        user=user,
    )
    assert first.id == replay.id
    assert first.status == "queued"
    assert first.kind == "score"

    visible = await get_candidate_processing_job(
        candidate.id,
        first.id,
        db=db,
        user=user,
    )
    assert visible.id == first.id
    with pytest.raises(HTTPException) as hidden:
        await get_candidate_processing_job(
            candidate.id,
            uuid4(),
            db=db,
            user=user,
        )
    assert hidden.value.status_code == 404

    batch = await score_all_open(
        candidate.id,
        db=db,
        user=user,
    )
    # The already-active first pair is accepted once and the second position
    # receives a distinct durable job; no synchronous LLM result is fabricated.
    assert batch.queued == 2
    assert str(other_position.id) not in batch.message
