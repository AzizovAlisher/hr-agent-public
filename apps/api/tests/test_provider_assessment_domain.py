import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from app.auth.authorization import FieldClass, ResourceScope, operation_policy
from app.auth.jwt import encode_access_token
from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models.application import Application
from app.models.assessment import AssessmentTemplate, AssessmentTemplateVersion
from app.models.candidate import Candidate
from app.models.enterprise import PermissionOverride
from app.models.integration import (
    IntegrationJob,
    IntegrationWebhookDelivery,
    IntegrationWebhookDeliveryAttempt,
    IntegrationWebhookEvent,
)
from app.models.organization import Organization
from app.models.position import Position
from app.models.provider_assessment import (
    ProviderAssessment,
    ProviderAssessmentResult,
)
from app.models.user import User
from app.routers.integrations import ProviderRuntime, provider_runtime
from app.schemas.integrations import WebhookEnvelope
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import ProviderHttpClient
from app.services.integrations.lifecycle import (
    create_connection,
    set_connection_credentials,
)
from app.services.integrations.webhooks import canonical_envelope_bytes, sign_webhook
from app.services.integrations.worker import (
    process_integration_jobs_once,
    process_webhook_deliveries_once,
)
from app.services.private_object_retention import prepare_retention_erasure
from app.storage import LocalBlobStore
from sqlalchemy import func, select

pytestmark = pytest.mark.asyncio

TEST_CIPHER = IntegrationCipher(b"p" * 32, key_id="provider-assessment-test")


class AssessmentAdapter:
    def __init__(self) -> None:
        self.invitation_payloads: list[dict] = []
        self.erasure_payloads: list[dict] = []

    async def create_invitation(
        self,
        _credentials,
        _configuration,
        payload,
        *,
        idempotency_key,
    ):
        assert idempotency_key
        self.invitation_payloads.append(payload)
        return {
            "id": "vendor-invitation-1",
            "status": "invited",
            "launch_url": "https://assessment.vendor.test/launch/private-1",
            "expires_at": payload["expires_at"],
        }

    async def get_attempt(self, _credentials, _configuration, _attempt_id):
        now = datetime.now(UTC)
        return {
            "id": "vendor-attempt-1",
            "status": "submitted",
            "started_at": (now - timedelta(minutes=20)).isoformat(),
            "submitted_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    async def get_result(self, _credentials, _configuration, _attempt_id):
        return {
            "attempt_id": "vendor-attempt-1",
            "status": "graded",
            "score": 84,
            "recommendation": "review",
            "evidence": [{"criterion": "correctness", "score": 8}],
            "completed_at": datetime.now(UTC).isoformat(),
        }

    async def erase_candidate_data(
        self,
        _credentials,
        _configuration,
        payload,
        *,
        idempotency_key,
    ):
        assert idempotency_key.startswith("candidate-erasure-assessment:")
        self.erasure_payloads.append(payload)
        return {"status": "erased", "receipt_id": "vendor-erasure-receipt-private"}


def auth(user: User) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + encode_access_token(
            user.id,
            user.role,
            user.locale,
            token_version=user.token_version,
        )
    }


async def seed_domain(db, suffix: str = "main") -> dict:
    organization = Organization(
        name=f"Provider assessment {suffix}",
        slug=f"provider-assessment-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"provider-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"Candidate {suffix}",
        raw_cv="private candidate evidence",
    )
    db.add_all([admin, candidate])
    await db.flush()
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {suffix}",
        created_by=admin.id,
    )
    db.add(position)
    await db.flush()
    application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source="manual",
    )
    template = AssessmentTemplate(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        name=f"Specialist mapping {suffix}",
    )
    db.add_all([application, template])
    await db.flush()
    version = AssessmentTemplateVersion(
        organization_id=organization.id,
        template_id=template.id,
        version=1,
        title=f"Specialist mapping {suffix}",
        passing_percentage=0,
        created_by_user_id=admin.id,
    )
    db.add(version)
    await db.flush()
    connection, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        provider="assessment_generic",
        name=f"Assessment provider {suffix}",
        configuration={
            "base_url": "https://assessment.vendor.test",
            "provider_label": "Vendor test",
        },
        cipher=TEST_CIPHER,
    )
    await set_connection_credentials(
        db,
        connection=connection,
        credentials={
            "api_key": "vendor-api-key-private",
            "callback_signing_secret": "vendor-callback-secret-private",
        },
        cipher=TEST_CIPHER,
    )
    connection.status = "connected"
    await db.commit()
    return {
        "organization": organization,
        "admin": admin,
        "candidate": candidate,
        "application": application,
        "version": version,
        "connection": connection,
    }


async def api_client(adapter: AssessmentAdapter):
    async def runtime_override():
        yield ProviderRuntime(
            cipher=TEST_CIPHER,
            http=None,  # type: ignore[arg-type]
            adapters={"assessment_generic": adapter},
            redirect_origins={"http://testserver"},
            outbound_hosts=set(),
        )

    app.dependency_overrides[provider_runtime] = runtime_override
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def dispatch_invitation(client, seeded: dict) -> tuple[dict, ProviderAssessment]:
    expires_at = datetime.now(UTC) + timedelta(days=7)
    response = await client.post(
        f"/integrations/{seeded['connection'].id}/assessment/invitations",
        headers=auth(seeded["admin"]),
        json={
            "application_id": str(seeded["application"].id),
            "template_version_id": str(seeded["version"].id),
            "external_template_ref": "vendor-template-engineering-v3",
            "expires_at": expires_at.isoformat(),
            "locale": "en",
            "accommodations": {"extra_time_percent": 25},
            "idempotency_key": "invite-application-once",
        },
    )
    assert response.status_code == 202, response.text
    return response.json(), expires_at


async def request_and_process_job(client, db, user, job_id, adapter):
    invitations_before = len(getattr(adapter, "invitation_payloads", []))
    queued = await client.post(f"/integrations/jobs/{job_id}/run", headers=auth(user))
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] in {"queued", "retrying"}
    assert len(getattr(adapter, "invitation_payloads", [])) == invitations_before
    await db.commit()
    counts = await process_integration_jobs_once(
        SessionLocal,
        cipher=TEST_CIPHER,
        adapters={"assessment_generic": adapter},
        worker_id=f"provider-assessment-test-{uuid4()}",
        mode="off",
    )
    assert counts["claimed"] >= 1
    job = await db.scalar(
        select(IntegrationJob)
        .where(IntegrationJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    assert job is not None
    return job


async def test_invitation_poll_result_and_reviewer_evidence_are_one_vertical_slice(db):
    seeded = await seed_domain(db)
    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        job_out, _expiry = await dispatch_invitation(client, seeded)
        mapping = await db.scalar(select(ProviderAssessment))
        assert mapping is not None
        assert mapping.application_id == seeded["application"].id
        assert mapping.candidate_id == seeded["candidate"].id
        assert mapping.template_version_id == seeded["version"].id

        run = await request_and_process_job(
            client, db, seeded["admin"], job_out["id"], adapter
        )
        assert run.status == "succeeded"
        assert "private-1" not in json.dumps(run.result_summary).lower()
        await db.refresh(mapping)
        assert mapping.status == "invited"
        assert mapping.launch_url_ciphertext is not None
        assert b"private-1" not in mapping.launch_url_ciphertext
        assert adapter.invitation_payloads[0]["candidate_ref"] == str(
            seeded["candidate"].id
        )
        assert adapter.invitation_payloads[0]["callback_url"].endswith(
            f"/integrations/webhooks/{seeded['connection'].id}"
        )
        active_evidence = await client.get(
            f"/integrations/assessment/applications/{seeded['application'].id}/evidence",
            headers=auth(seeded["admin"]),
        )
        assert active_evidence.json()["items"][0]["candidate_launch_url"].endswith(
            "/launch/private-1"
        )

        status_job = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/attempts/status",
            headers=auth(seeded["admin"]),
            json={
                "provider_assessment_id": str(mapping.id),
                "idempotency_key": "poll-status-1",
            },
        )
        assert status_job.status_code == 202, status_job.text
        status_run = await request_and_process_job(
            client,
            db,
            seeded["admin"],
            status_job.json()["id"],
            adapter,
        )
        assert status_run.status == "succeeded"
        await db.refresh(mapping)
        assert mapping.started_at is not None
        assert mapping.submitted_at is not None
        assert mapping.started_at < mapping.submitted_at

        result_job = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/attempts/result",
            headers=auth(seeded["admin"]),
            json={
                "provider_assessment_id": str(mapping.id),
                "idempotency_key": "poll-result-1",
            },
        )
        assert result_job.status_code == 202, result_job.text
        result_run = await request_and_process_job(
            client,
            db,
            seeded["admin"],
            result_job.json()["id"],
            adapter,
        )
        assert result_run.status == "succeeded"
        stored_result = await db.scalar(select(ProviderAssessmentResult))
        assert stored_result is not None
        assert stored_result.score == 84
        assert b"correctness" not in stored_result.evidence_ciphertext

        evidence = await client.get(
            f"/integrations/assessment/applications/{seeded['application'].id}/evidence",
            headers=auth(seeded["admin"]),
        )
        assert evidence.status_code == 200, evidence.text
        assert evidence.headers["cache-control"] == "private, no-store"
        item = evidence.json()["items"][0]
        assert item["candidate_launch_url"] is None
        assert item["result"]["score"] == 84
        assert item["result"]["recommendation"] == "review"
        assert item["result"]["provenance"]["human_review_required"] is True
        assert item["result"]["provenance"]["automated_stage_change"] is False
        assert item["started_at"] is not None
        assert item["submitted_at"] is not None
        await db.refresh(seeded["application"])
        assert seeded["application"].stage == "new"
    finally:
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def test_erasure_waits_for_provider_confirmation_and_scrubs_terminal_payload(
    db, tmp_path, monkeypatch
):
    seeded = await seed_domain(db, "privacy-erasure")
    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        invitation_job, _expiry = await dispatch_invitation(client, seeded)
        invitation_run = await request_and_process_job(
            client,
            db,
            seeded["admin"],
            invitation_job["id"],
            adapter,
        )
        assert invitation_run.status == "succeeded"
        mapping = await db.scalar(select(ProviderAssessment))
        assert mapping is not None

        now = datetime.now(UTC)
        seeded["candidate"].deleted_at = now - timedelta(days=60)
        encoded_key = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
        settings = get_settings()
        monkeypatch.setattr(
            settings, "integration_credential_encryption_key", encoded_key
        )
        monkeypatch.setattr(
            settings, "integration_credential_key_id", TEST_CIPHER.key_id
        )
        await db.commit()

        first_plan = await prepare_retention_erasure(
            db,
            organization_id=seeded["organization"].id,
            deleted_cutoff=now - timedelta(days=30),
            active_cutoff=now - timedelta(days=30),
            include_active_candidates=False,
            store=LocalBlobStore(tmp_path),
        )
        assert seeded["candidate"].id not in first_plan.candidate_ids
        delete_job = await db.scalar(
            select(IntegrationJob).where(
                IntegrationJob.organization_id == seeded["organization"].id,
                IntegrationJob.operation == "assessment.delete",
            )
        )
        assert delete_job is not None and delete_job.status == "queued"
        assert delete_job.payload_ciphertext is not None
        assert b"vendor-invitation-1" not in delete_job.payload_ciphertext
        visible_job = json.dumps(delete_job.reference_payload)
        assert str(seeded["candidate"].id) not in visible_job
        assert "vendor-invitation-1" not in visible_job
        await db.commit()

        counts = await process_integration_jobs_once(
            SessionLocal,
            cipher=TEST_CIPHER,
            adapters={"assessment_generic": adapter},
            worker_id=f"provider-erasure-{uuid4()}",
            mode="off",
        )
        assert counts == {"claimed": 1, "succeeded": 1, "failed": 0}
        delete_job = await db.scalar(
            select(IntegrationJob)
            .where(IntegrationJob.id == delete_job.id)
            .execution_options(populate_existing=True)
        )
        assert delete_job is not None and delete_job.status == "succeeded"
        assert delete_job.payload_ciphertext is None
        assert delete_job.payload_nonce is None
        assert delete_job.payload_key_id is None
        assert delete_job.result_summary["deletion_confirmed"] is True
        assert "vendor-erasure-receipt-private" not in json.dumps(
            delete_job.result_summary
        )
        assert adapter.erasure_payloads == [
            {
                "subject_ref": str(seeded["candidate"].id),
                "provider_invitation_id": "vendor-invitation-1",
            }
        ]

        second_plan = await prepare_retention_erasure(
            db,
            organization_id=seeded["organization"].id,
            deleted_cutoff=now - timedelta(days=30),
            active_cutoff=now - timedelta(days=30),
            include_active_candidates=False,
            store=LocalBlobStore(tmp_path),
        )
        assert seeded["candidate"].id in second_plan.candidate_ids
    finally:
        await db.rollback()
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def _post_callback(client, connection_id, envelope: WebhookEnvelope):
    body = canonical_envelope_bytes(envelope)
    timestamp = str(int(datetime.now(UTC).timestamp()))
    return await client.post(
        f"/integrations/webhooks/{connection_id}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-HR-Webhook-Timestamp": timestamp,
            "X-HR-Webhook-Signature": sign_webhook(
                "vendor-callback-secret-private", int(timestamp), body
            ),
        },
    )


async def _run_callback_job(client, db, user, event_id: str, adapter):
    job = await db.scalar(
        select(IntegrationJob).where(
            IntegrationJob.operation == "assessment.callback",
            IntegrationJob.idempotency_key == event_id,
        )
    )
    assert job is not None
    processed = await request_and_process_job(client, db, user, job.id, adapter)
    return processed


async def test_signed_callbacks_are_idempotent_out_of_order_safe_and_reviewable(db):
    seeded = await seed_domain(db, "callback")
    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        job_out, _expiry = await dispatch_invitation(client, seeded)
        await request_and_process_job(
            client, db, seeded["admin"], job_out["id"], adapter
        )
        mapping = await db.scalar(select(ProviderAssessment))
        assert mapping is not None
        now = datetime.now(UTC)
        result_envelope = WebhookEnvelope(
            id="assessment-result-ready-1",
            type="assessment.result.ready",
            created_at=now,
            data={
                "provider_invitation_id": "vendor-invitation-1",
                "provider_attempt_id": "callback-attempt-1",
                "status": "graded",
                "score": 91,
                "recommendation": "advance",
                "evidence": [{"criterion": "quality", "score": 9}],
                "completed_at": now.isoformat(),
            },
        )
        accepted = await _post_callback(
            client, seeded["connection"].id, result_envelope
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["replay"] is False
        run = await _run_callback_job(
            client, db, seeded["admin"], result_envelope.id, adapter
        )
        assert run.status == "succeeded"

        repeated = await _post_callback(
            client, seeded["connection"].id, result_envelope
        )
        assert repeated.status_code == 202
        assert repeated.json()["replay"] is True
        assert await db.scalar(select(func.count(ProviderAssessmentResult.id))) == 1

        older = now - timedelta(hours=1)
        old_attempt = WebhookEnvelope(
            id="assessment-attempt-old-1",
            type="assessment.attempt.updated",
            created_at=older,
            data={
                "provider_invitation_id": "vendor-invitation-1",
                "provider_attempt_id": "callback-attempt-1",
                "status": "started",
                "started_at": older.isoformat(),
                "submitted_at": None,
                "updated_at": older.isoformat(),
            },
        )
        accepted_old = await _post_callback(
            client, seeded["connection"].id, old_attempt
        )
        assert accepted_old.status_code == 202
        old_run = await _run_callback_job(
            client, db, seeded["admin"], old_attempt.id, adapter
        )
        assert old_run.status == "succeeded"
        await db.refresh(mapping)
        assert mapping.status == "graded"
        old_event = await db.scalar(
            select(IntegrationWebhookEvent).where(
                IntegrationWebhookEvent.event_id == old_attempt.id
            )
        )
        assert old_event is not None and old_event.status == "processed"
        assert old_event.provider_assessment_id == mapping.id
        assert mapping.started_at == older
        assert mapping.status == "graded"

        evidence = await client.get(
            f"/integrations/assessment/applications/{seeded['application'].id}/evidence",
            headers=auth(seeded["admin"]),
        )
        assert evidence.json()["items"][0]["result"]["score"] == 91
        assert evidence.json()["items"][0]["status"] == "graded"
    finally:
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def test_cross_tenant_targets_and_provider_mapping_ids_are_hidden(db):
    seeded = await seed_domain(db, "tenant-a")
    other = await seed_domain(db, "tenant-b")
    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        response = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/invitations",
            headers=auth(seeded["admin"]),
            json={
                "application_id": str(other["application"].id),
                "external_template_ref": "vendor-template",
                "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                "idempotency_key": "cross-tenant-invite",
            },
        )
        assert response.status_code == 404
        assert await db.scalar(select(func.count(ProviderAssessment.id))) == 0

        other_job, _expiry = await dispatch_invitation(client, other)
        await request_and_process_job(
            client, db, other["admin"], other_job["id"], adapter
        )
        other_mapping = await db.scalar(
            select(ProviderAssessment).where(
                ProviderAssessment.organization_id == other["organization"].id
            )
        )
        assert other_mapping is not None
        poll = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/attempts/result",
            headers=auth(seeded["admin"]),
            json={
                "provider_assessment_id": str(other_mapping.id),
                "idempotency_key": "cross-tenant-poll",
            },
        )
        assert poll.status_code == 404
        hidden = await client.get(
            f"/integrations/assessment/applications/{other['application'].id}/evidence",
            headers=auth(seeded["admin"]),
        )
        assert hidden.status_code == 404
    finally:
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def test_callback_failure_retries_then_dead_letters_without_mutating_stage(db):
    seeded = await seed_domain(db, "failure")
    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        job_out, _expiry = await dispatch_invitation(client, seeded)
        await request_and_process_job(
            client, db, seeded["admin"], job_out["id"], adapter
        )
        now = datetime.now(UTC)
        unknown = WebhookEnvelope(
            id="assessment-result-unknown-mapping",
            type="assessment.result.ready",
            created_at=now,
            data={
                "provider_invitation_id": "unknown-provider-invitation",
                "provider_attempt_id": "unknown-attempt",
                "status": "graded",
                "score": 75,
                "recommendation": "review",
                "evidence": [],
                "completed_at": now.isoformat(),
            },
        )
        accepted = await _post_callback(client, seeded["connection"].id, unknown)
        assert accepted.status_code == 202
        job = await db.scalar(
            select(IntegrationJob).where(IntegrationJob.idempotency_key == unknown.id)
        )
        assert job is not None
        last = None
        for _ in range(5):
            last = await request_and_process_job(
                client, db, seeded["admin"], job.id, adapter
            )
            if last.status == "dead_letter":
                break
        assert last is not None and last.status == "dead_letter"
        assert last.failure_code == "assessment_mapping_not_ready"
        event = await db.scalar(
            select(IntegrationWebhookEvent).where(
                IntegrationWebhookEvent.event_id == unknown.id
            )
        )
        assert event is not None
        assert event.status == "failed"
        assert event.failure_code == "assessment_mapping_not_ready"
        await db.refresh(seeded["application"])
        assert seeded["application"].stage == "new"
    finally:
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def test_provider_evidence_operation_is_exactly_classified():
    policy = operation_policy(
        "GET", "/integrations/assessment/applications/{application_id}/evidence"
    )
    assert policy is not None
    assert policy.actions == ("assessments.results",)
    assert policy.scope is ResourceScope.HIRING_ASSIGNMENT
    assert policy.fields == frozenset({FieldClass.ASSESSMENT_RESULT})


async def test_recruiter_uses_provider_assessments_without_connector_administration(db):
    seeded = await seed_domain(db, "recruiter-separation")
    recruiter = User(
        organization_id=seeded["organization"].id,
        email=f"provider-recruiter-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(recruiter)
    await db.flush()
    # Prove the hiring surface does not inherit even connector read access.
    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="recruiter",
            permission="integrations.read",
            allowed=False,
        )
    )
    await db.commit()

    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        denied_control_plane = await client.get(
            "/integrations", headers=auth(recruiter)
        )
        assert denied_control_plane.status_code == 403
        assessment_connections = await client.get(
            "/integrations/assessment/connections", headers=auth(recruiter)
        )
        assert assessment_connections.status_code == 200, assessment_connections.text
        assert [item["id"] for item in assessment_connections.json()["items"]] == [
            str(seeded["connection"].id)
        ]

        denied_credentials = await client.put(
            f"/integrations/{seeded['connection'].id}/credentials",
            headers=auth(recruiter),
            json={"credentials": {"api_key": "cannot-change"}},
        )
        assert denied_credentials.status_code == 403

        invitation = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/invitations",
            headers=auth(recruiter),
            json={
                "application_id": str(seeded["application"].id),
                "template_version_id": str(seeded["version"].id),
                "external_template_ref": "vendor-template-recruiter",
                "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
                "locale": "en",
                "accommodations": {},
                "idempotency_key": "recruiter-assessment-invitation",
            },
        )
        assert invitation.status_code == 202, invitation.text
        mapping = await db.scalar(
            select(ProviderAssessment).where(
                ProviderAssessment.organization_id == seeded["organization"].id
            )
        )
        assert mapping is not None

        status_poll = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/attempts/status",
            headers=auth(recruiter),
            json={
                "provider_assessment_id": str(mapping.id),
                "idempotency_key": "recruiter-assessment-status",
            },
        )
        result_poll = await client.post(
            f"/integrations/{seeded['connection'].id}/assessment/attempts/result",
            headers=auth(recruiter),
            json={
                "provider_assessment_id": str(mapping.id),
                "idempotency_key": "recruiter-assessment-result",
            },
        )
        evidence = await client.get(
            f"/integrations/assessment/applications/{seeded['application'].id}/evidence",
            headers=auth(recruiter),
        )
        assert status_poll.status_code == 202, status_poll.text
        assert result_poll.status_code == 202, result_poll.text
        assert evidence.status_code == 200, evidence.text
        assert evidence.json()["items"][0]["id"] == str(mapping.id)
    finally:
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def test_provider_hiring_operation_policies_do_not_require_connector_management():
    expected = {
        ("GET", "/integrations/assessment/connections"): ("assessments.read",),
        (
            "POST",
            "/integrations/{connection_id}/assessment/invitations",
        ): ("assessments.manage",),
        (
            "POST",
            "/integrations/{connection_id}/assessment/attempts/status",
        ): ("assessments.manage",),
        (
            "POST",
            "/integrations/{connection_id}/assessment/attempts/result",
        ): ("assessments.results",),
    }
    for (method, path), actions in expected.items():
        policy = operation_policy(method, path)
        assert policy is not None
        assert policy.actions == actions
        assert "integrations.manage" not in policy.actions


async def test_manual_delivery_attempt_only_queues_and_worker_performs_http(db):
    seeded = await seed_domain(db, "delivery-topology")
    connection, _ = await create_connection(
        db,
        organization_id=seeded["organization"].id,
        actor_user_id=seeded["admin"].id,
        provider="generic_webhook",
        name="Durable outbound webhook",
        configuration={
            "target_url": "https://hooks.vendor.test/hr",
            "event_types": ["candidate.updated"],
        },
        cipher=TEST_CIPHER,
    )
    await set_connection_credentials(
        db,
        connection=connection,
        credentials={"signing_secret": "outbound-signing-secret-private"},
        cipher=TEST_CIPHER,
    )
    connection.status = "connected"
    await db.commit()
    adapter = AssessmentAdapter()
    client = await api_client(adapter)
    try:
        created = await client.post(
            f"/integrations/{connection.id}/deliveries",
            headers=auth(seeded["admin"]),
            json={
                "idempotency_key": "delivery-topology-1",
                "type": "candidate.updated",
                "data": {"candidate_id": str(seeded["candidate"].id)},
            },
        )
        assert created.status_code == 202, created.text
        requested = await client.post(
            f"/integrations/deliveries/{created.json()['id']}/attempt",
            headers=auth(seeded["admin"]),
        )
        assert requested.status_code == 202, requested.text
        assert requested.json()["status"] == "queued"
        assert (
            await db.scalar(select(func.count(IntegrationWebhookDeliveryAttempt.id)))
            == 0
        )

        outbound_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            outbound_calls.append(request)
            return httpx.Response(204)

        await db.commit()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as outbound:
            counts = await process_webhook_deliveries_once(
                SessionLocal,
                cipher=TEST_CIPHER,
                http=ProviderHttpClient(outbound),
                validate_endpoint=lambda value: _approved_endpoint(value),
                worker_id=f"delivery-topology-{uuid4()}",
                mode="off",
            )
        assert counts == {"claimed": 1, "delivered": 1, "failed": 0}
        assert len(outbound_calls) == 1
        delivery = await db.scalar(
            select(IntegrationWebhookDelivery)
            .where(IntegrationWebhookDelivery.id == created.json()["id"])
            .execution_options(populate_existing=True)
        )
        assert delivery is not None and delivery.status == "delivered"
        assert (
            await db.scalar(select(func.count(IntegrationWebhookDeliveryAttempt.id)))
            == 1
        )
    finally:
        await client.aclose()
        app.dependency_overrides.pop(provider_runtime, None)


async def _approved_endpoint(value: str) -> str:
    assert value == "https://hooks.vendor.test/hr"
    return value
