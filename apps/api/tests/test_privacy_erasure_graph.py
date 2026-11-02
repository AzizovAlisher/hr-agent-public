"""PRIV-001 live-store canaries and fail-closed encrypted traversal."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.config import get_settings
from app.db import Base
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_processing import CandidateProcessingJob
from app.models.chat_session import ChatSession
from app.models.communication import (
    CandidateContactPoint,
    CommunicationBatch,
    CommunicationThread,
)
from app.models.enterprise import AuditLog, EnterprisePolicy
from app.models.integration import (
    IntegrationConnection,
    IntegrationCursor,
    IntegrationJob,
    IntegrationWebhookDelivery,
    IntegrationWebhookEvent,
)
from app.models.notification import Notification
from app.models.operations import UsageLedgerEntry, WorkerQueueLease
from app.models.organization import Organization
from app.models.position import Position
from app.models.sourcing import SourceCandidateReceipt
from app.models.task_submission import TaskSubmission
from app.models.interview_recording import InterviewRecording
from app.models.user import User
from app.models.workflow import WorkflowRun
from app.routers.enterprise import run_retention
from app.schemas.enterprise import RetentionRunRequest
from app.services.integrations.crypto import IntegrationCipher, connection_aad
from fastapi import HTTPException
from sqlalchemy import select, text

pytestmark = pytest.mark.asyncio
_KEY = b"p" * 32
_KEY_ID = "privacy-erasure-test"
_CIPHER = IntegrationCipher(_KEY, key_id=_KEY_ID)


def _configure_cipher(monkeypatch) -> None:
    encoded = base64.urlsafe_b64encode(_KEY).decode().rstrip("=")
    settings = get_settings()
    monkeypatch.setattr(settings, "integration_credential_encryption_key", encoded)
    monkeypatch.setattr(settings, "integration_credential_key_id", _KEY_ID)


async def _seed_subject(db, suffix: str):
    organization = Organization(
        name=f"Privacy {suffix}", slug=f"privacy-{suffix}-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"privacy-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    canary = f"PRIV-001-CANARY-{uuid4()}"
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=canary,
        raw_cv=f"Candidate private evidence {canary}",
        parsed_cv={"identity_canary": canary},
        email_normalized=f"{canary.casefold()}@example.test",
        deleted_at=datetime.now(UTC) - timedelta(days=60),
    )
    db.add(candidate)
    await db.flush()
    db.add(
        CandidateContactPoint(
            organization_id=organization.id,
            candidate_id=candidate.id,
            channel="email",
            value=f"{canary}@example.test",
            normalized_value=f"{canary.casefold()}@example.test",
            source="manual",
            is_primary=True,
        )
    )
    await db.commit()
    return organization, admin, candidate, canary


def _encrypted_json(value, *, aad: bytes, cipher: IntegrationCipher = _CIPHER):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    encrypted = cipher.encrypt_bytes(encoded, aad=aad)
    return encoded, encrypted


async def _tables_containing(db, needle: str) -> list[str]:
    found: list[str] = []
    for table_name in sorted(Base.metadata.tables):
        # Names come from SQLAlchemy metadata, never from request input.  Quoted
        # identifiers keep this whole-schema canary proof deterministic.
        statement = text(
            f'SELECT 1 FROM "{table_name}" AS candidate_row '
            "WHERE to_jsonb(candidate_row)::text LIKE :needle LIMIT 1"
        )
        if (await db.execute(statement, {"needle": f"%{needle}%"})).first():
            found.append(table_name)
    return found


async def test_retention_erases_complete_non_fk_live_store_canary_graph(
    db, monkeypatch
):
    _configure_cipher(monkeypatch)
    organization, admin, candidate, canary = await _seed_subject(db, "graph")
    organization_id = organization.id
    candidate_id = candidate.id
    now = datetime.now(UTC)

    # Exact-cycle artifacts exercise both direct candidate FKs and the new
    # deferred (org, application, candidate, position) identity. Retention must
    # remove the whole cycle without leaving candidate IDs or private canaries
    # behind in tables whose direct candidate FK is SET NULL.
    position = Position(
        organization_id=organization_id,
        title="Privacy exact-cycle position",
        status="open",
        created_by=admin.id,
    )
    db.add(position)
    await db.flush()
    application = Application(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position.id,
        stage="new",
        source="manual",
        cycle_number=1,
    )
    db.add(application)
    await db.flush()
    cycle_activity = CandidateActivity(
        organization_id=organization_id,
        candidate_id=candidate_id,
        actor_user_id=admin.id,
        application_id=application.id,
        position_id=position.id,
        event_type="privacy_cycle_fixture",
        subject_type="application",
        subject_id=application.id,
        payload={"marker": canary},
        occurred_at=now,
    )
    cycle_chat = ChatSession(
        organization_id=organization_id,
        user_id=admin.id,
        scope="candidate",
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        title=canary,
    )
    cycle_thread = CommunicationThread(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        channel="email",
        status="open",
    )
    cycle_recording = InterviewRecording(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        created_by=admin.id,
        title="Privacy recording",
        status="transcribed",
        transcript=canary,
    )
    cycle_task = TaskSubmission(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        created_by=admin.id,
        title="Privacy task",
        submission_type="text",
        content=canary,
        status="submitted",
    )
    cycle_receipt = SourceCandidateReceipt(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        external_id=f"privacy-{uuid4()}",
        provider_snapshot="csv",
        source_url="https://example.test/privacy",
        received_at=now,
    )
    cycle_run = WorkflowRun(
        organization_id=organization_id,
        rule_id_snapshot=uuid4(),
        rule_version_snapshot=1,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        rule_name_snapshot="Privacy cycle",
        trigger_snapshot="candidate_added",
        conditions_snapshot={"position_id": str(position.id), "marker": canary},
        actions_snapshot=[],
        dedupe_key=f"privacy-cycle:{candidate_id}",
        status="queued",
        action_results=[],
        started_at=now,
    )
    cycle_processing = CandidateProcessingJob(
        organization_id=organization_id,
        candidate_id=candidate_id,
        position_id=position.id,
        application_id=application.id,
        created_by_user_id=admin.id,
        kind="score",
        status="queued",
        idempotency_key=f"privacy-score:{application.id}",
        attempt_count=0,
        max_attempts=3,
        available_at=now,
        result_summary={"marker": canary},
    )
    db.add_all(
        [
            cycle_activity,
            cycle_chat,
            cycle_thread,
            cycle_recording,
            cycle_task,
            cycle_receipt,
            cycle_run,
            cycle_processing,
        ]
    )

    connection = IntegrationConnection(
        organization_id=organization_id,
        created_by_user_id=admin.id,
        provider="generic_webhook",
        name=f"Privacy webhook {uuid4().hex[:8]}",
        status="connected",
        capabilities=["webhook.deliver"],
        configuration={"target_url": "https://example.test/webhook"},
        granted_scopes=[],
    )
    db.add(connection)
    await db.flush()

    job_id = uuid4()
    _job_raw, job_payload = _encrypted_json(
        {"candidate_private_value": canary},
        aad=connection_aad(organization_id, job_id, "job-payload"),
    )
    job = IntegrationJob(
        id=job_id,
        organization_id=organization_id,
        connection_id=connection.id,
        created_by_user_id=admin.id,
        operation="health",
        status="queued",
        idempotency_key=f"candidate:{candidate_id}",
        request_fingerprint=hashlib.sha256(b"privacy-job").hexdigest(),
        reference_payload={"candidate_id": str(candidate_id)},
        payload_ciphertext=job_payload.ciphertext,
        payload_nonce=job_payload.nonce,
        payload_key_id=job_payload.key_id,
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=now,
        result_summary={"canary": canary},
    )
    db.add(job)
    artifact_reference_job = IntegrationJob(
        organization_id=organization_id,
        connection_id=connection.id,
        created_by_user_id=admin.id,
        operation="health",
        status="queued",
        idempotency_key=f"task-reference:{cycle_task.id}",
        request_fingerprint=hashlib.sha256(b"privacy-task-reference").hexdigest(),
        reference_payload={"task_submission_id": str(cycle_task.id)},
        attempt_count=0,
        max_attempts=3,
        next_attempt_at=now,
        result_summary={},
    )
    db.add(artifact_reference_job)

    cursor_raw, cursor_payload = _encrypted_json(
        {"candidate_id": str(candidate_id), "canary": canary},
        aad=connection_aad(
            organization_id, connection.id, "cursor:candidate_import.next"
        ),
    )
    cursor = IntegrationCursor(
        organization_id=organization_id,
        connection_id=connection.id,
        cursor_name="candidate_import.next",
        value_ciphertext=cursor_payload.ciphertext,
        value_nonce=cursor_payload.nonce,
        value_key_id=cursor_payload.key_id,
    )
    db.add(cursor)

    inbound_id = f"evt_{uuid4().hex}"
    inbound_raw, inbound_payload = _encrypted_json(
        {
            "version": "2026-08-01",
            "id": inbound_id,
            "type": "candidate.updated",
            "created_at": now.isoformat(),
            "data": {"candidate_id": str(candidate_id), "canary": canary},
        },
        aad=connection_aad(
            organization_id, connection.id, f"webhook-inbound:{inbound_id}"
        ),
    )
    inbound = IntegrationWebhookEvent(
        organization_id=organization_id,
        connection_id=connection.id,
        event_id=inbound_id,
        envelope_version="2026-08-01",
        event_type="candidate.updated",
        occurred_at=now,
        payload_sha256=hashlib.sha256(inbound_raw).hexdigest(),
        body_ciphertext=inbound_payload.ciphertext,
        body_nonce=inbound_payload.nonce,
        body_key_id=inbound_payload.key_id,
        status="accepted",
        received_at=now,
    )
    db.add(inbound)

    outbound_id = f"evt_{uuid4().hex}"
    outbound_raw, outbound_payload = _encrypted_json(
        {
            "version": "2026-08-01",
            "id": outbound_id,
            "type": "candidate.exported",
            "created_at": now.isoformat(),
            "data": {"candidate_id": str(candidate_id), "canary": canary},
        },
        aad=connection_aad(
            organization_id, connection.id, f"webhook-outbound:{outbound_id}"
        ),
    )
    delivery = IntegrationWebhookDelivery(
        organization_id=organization_id,
        connection_id=connection.id,
        event_id=outbound_id,
        envelope_version="2026-08-01",
        event_type="candidate.exported",
        target_url="https://example.test/webhook",
        payload_sha256=hashlib.sha256(outbound_raw).hexdigest(),
        body_ciphertext=outbound_payload.ciphertext,
        body_nonce=outbound_payload.nonce,
        body_key_id=outbound_payload.key_id,
        status="queued",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now,
    )
    db.add(delivery)

    processing_job = CandidateProcessingJob(
        organization_id=organization_id,
        candidate_id=candidate_id,
        created_by_user_id=admin.id,
        kind="parse_cv",
        status="queued",
        idempotency_key=f"privacy-parse:{candidate_id}",
        attempt_count=0,
        max_attempts=3,
        available_at=now,
        result_summary={"canary": canary},
    )
    db.add(processing_job)
    await db.flush()
    lease = WorkerQueueLease(
        queue_name="candidate_processing",
        work_id=processing_job.id,
        organization_id=organization_id,
        lease_token=uuid4(),
        worker_id="privacy-test-worker",
        leased_at=now,
        lease_expires_at=now + timedelta(minutes=1),
    )
    ledger = UsageLedgerEntry(
        organization_id=organization_id,
        metric="candidates",
        amount=1,
        kind="usage",
        idempotency_key=f"candidate:create:{candidate_id}",
        resource_type="candidate",
        resource_id=candidate_id,
        metadata_json={"canary": canary},
        occurred_at=now,
    )
    audit = AuditLog(
        organization_id=organization_id,
        actor_user_id=admin.id,
        actor_email_snapshot=admin.email,
        action="privacy_nested_fixture",
        resource_type="task_submission",
        resource_id=uuid4(),
        path_template="/privacy/test",
        method="GET",
        status_code=200,
        request_id=uuid4(),
        metadata_json={"nested": {"candidate_id": str(candidate_id), "canary": canary}},
        occurred_at=now,
    )
    notification = Notification(
        organization_id=organization_id,
        user_id=admin.id,
        type="candidate_applied",
        payload={"nested": {"candidate_id": str(candidate_id), "canary": canary}},
    )
    batch = CommunicationBatch(
        organization_id=organization_id,
        created_by_user_id=admin.id,
        channel="email",
        status="completed",
        idempotency_key=f"privacy-batch-{uuid4()}",
        total_count=0,
        requested_count=1,
        skipped_count=1,
        skip_summary=[
            {"nested": {"candidate_id": str(candidate_id)}, "canary": canary}
        ],
    )
    db.add_all([lease, ledger, audit, notification, batch])
    await db.commit()
    identifiers = {
        "job": job.id,
        "artifact_reference_job": artifact_reference_job.id,
        "cursor": cursor.id,
        "inbound": inbound.id,
        "delivery": delivery.id,
        "processing_job": processing_job.id,
        "ledger": ledger.id,
        "audit": audit.id,
        "notification": notification.id,
        "batch": batch.id,
        "application": application.id,
        "cycle_activity": cycle_activity.id,
        "cycle_chat": cycle_chat.id,
        "cycle_thread": cycle_thread.id,
        "cycle_recording": cycle_recording.id,
        "cycle_task": cycle_task.id,
        "cycle_receipt": cycle_receipt.id,
        "cycle_run": cycle_run.id,
        "cycle_processing": cycle_processing.id,
    }

    result = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )
    assert result.deleted_candidates == 1
    db.expunge_all()

    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is None
    sanitized_job = await db.get(IntegrationJob, identifiers["job"])
    assert sanitized_job is not None
    assert sanitized_job.status == "cancelled"
    assert sanitized_job.reference_payload == {"kind": "candidate_erased"}
    assert sanitized_job.payload_ciphertext is None
    assert sanitized_job.result_summary == {"privacy_erased": True}
    sanitized_artifact_job = await db.get(
        IntegrationJob, identifiers["artifact_reference_job"]
    )
    assert sanitized_artifact_job is not None
    assert sanitized_artifact_job.status == "cancelled"
    assert sanitized_artifact_job.reference_payload == {"kind": "candidate_erased"}
    assert await db.get(IntegrationCursor, identifiers["cursor"]) is None
    assert await db.get(IntegrationWebhookEvent, identifiers["inbound"]) is None
    assert await db.get(IntegrationWebhookDelivery, identifiers["delivery"]) is None
    assert await db.get(CandidateProcessingJob, identifiers["processing_job"]) is None
    assert await db.get(Application, identifiers["application"]) is None
    assert await db.get(CandidateActivity, identifiers["cycle_activity"]) is None
    assert await db.get(ChatSession, identifiers["cycle_chat"]) is None
    assert await db.get(CommunicationThread, identifiers["cycle_thread"]) is None
    assert await db.get(InterviewRecording, identifiers["cycle_recording"]) is None
    assert await db.get(TaskSubmission, identifiers["cycle_task"]) is None
    assert await db.get(SourceCandidateReceipt, identifiers["cycle_receipt"]) is None
    assert await db.get(WorkflowRun, identifiers["cycle_run"]) is None
    assert await db.get(CandidateProcessingJob, identifiers["cycle_processing"]) is None
    assert (
        await db.execute(
            select(WorkerQueueLease).where(
                WorkerQueueLease.work_id == identifiers["processing_job"]
            )
        )
    ).scalar_one_or_none() is None
    sanitized_ledger = await db.get(UsageLedgerEntry, identifiers["ledger"])
    assert sanitized_ledger is not None
    assert sanitized_ledger.amount == 1
    assert sanitized_ledger.resource_id is None
    assert sanitized_ledger.resource_type == "erased_subject"
    assert sanitized_ledger.metadata_json == {"privacy_erased": True}
    assert await db.get(AuditLog, identifiers["audit"]) is None
    assert await db.get(Notification, identifiers["notification"]) is None
    sanitized_batch = await db.get(CommunicationBatch, identifiers["batch"])
    assert sanitized_batch is not None
    assert sanitized_batch.skip_summary == []
    assert sanitized_batch.requested_count == 0
    assert sanitized_batch.skipped_count == 0

    manifest = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == "candidate_erasure.backup_expiry_manifest",
            )
        )
    ).scalar_one()
    manifest_data = manifest.metadata_json
    assert manifest_data["maximum_backup_age_days"] == 35
    assert manifest_data["physical_deletion_verified"] is False
    assert manifest_data["candidate_count"] == 1
    assert str(candidate_id) not in json.dumps(manifest_data)
    assert canary not in json.dumps(manifest_data)
    expires_at = datetime.fromisoformat(manifest_data["backups_not_after"])
    erased_at = datetime.fromisoformat(manifest_data["live_erasure_at"])
    assert expires_at - erased_at == timedelta(days=35)

    retained_serialized = json.dumps(
        {
            "job": {
                "idempotency": sanitized_job.idempotency_key,
                "references": sanitized_job.reference_payload,
                "result": sanitized_job.result_summary,
            },
            "ledger": {
                "idempotency": sanitized_ledger.idempotency_key,
                "metadata": sanitized_ledger.metadata_json,
            },
            "batch": sanitized_batch.skip_summary,
            "manifest": manifest_data,
        },
        default=str,
    )
    assert str(candidate_id) not in retained_serialized
    assert canary not in retained_serialized
    assert cursor_raw.decode() != retained_serialized
    assert await _tables_containing(db, canary) == []
    assert await _tables_containing(db, str(candidate_id)) == []


async def test_retention_rolls_back_when_encrypted_store_cannot_be_proven(
    db, monkeypatch
):
    _configure_cipher(monkeypatch)
    organization, admin, candidate, canary = await _seed_subject(db, "fail-closed")
    organization_id = organization.id
    candidate_id = candidate.id
    now = datetime.now(UTC)
    connection = IntegrationConnection(
        organization_id=organization_id,
        created_by_user_id=admin.id,
        provider="generic_webhook",
        name=f"Broken privacy webhook {uuid4().hex[:8]}",
        status="connected",
        capabilities=["webhook.deliver"],
        configuration={"target_url": "https://example.test/webhook"},
        granted_scopes=[],
    )
    db.add(connection)
    await db.flush()
    event_id = f"evt_{uuid4().hex}"
    wrong_cipher = IntegrationCipher(b"x" * 32, key_id="retired-unknown-key")
    raw, encrypted = _encrypted_json(
        {
            "version": "2026-08-01",
            "id": event_id,
            "type": "candidate.updated",
            "created_at": now.isoformat(),
            "data": {"candidate_id": str(candidate_id), "canary": canary},
        },
        aad=connection_aad(
            organization_id, connection.id, f"webhook-inbound:{event_id}"
        ),
        cipher=wrong_cipher,
    )
    event = IntegrationWebhookEvent(
        organization_id=organization_id,
        connection_id=connection.id,
        event_id=event_id,
        envelope_version="2026-08-01",
        event_type="candidate.updated",
        occurred_at=now,
        payload_sha256=hashlib.sha256(raw).hexdigest(),
        body_ciphertext=encrypted.ciphertext,
        body_nonce=encrypted.nonce,
        body_key_id=encrypted.key_id,
        status="accepted",
        received_at=now,
    )
    db.add(event)
    await db.commit()
    event_pk = event.id

    with pytest.raises(HTTPException) as blocked:
        await run_retention(
            RetentionRunRequest(
                confirmation="PURGE",
                include_active_candidates=False,
            ),
            db=db,
            user=admin,
        )
    assert blocked.value.status_code == 409
    assert "cannot prove" in str(blocked.value.detail).casefold()
    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is not None
    assert await db.get(IntegrationWebhookEvent, event_pk) is not None
    assert (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == "candidate_erasure.backup_expiry_manifest",
            )
        )
    ).scalar_one_or_none() is None
