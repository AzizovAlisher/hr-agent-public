"""Vendor-neutral specialist assessment adapter and durable domain wiring."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application import Application
from app.models.assessment import AssessmentTemplateVersion
from app.models.integration import (
    IntegrationConnection,
    IntegrationJob,
    IntegrationWebhookEvent,
)
from app.models.provider_assessment import (
    ProviderAssessment,
    ProviderAssessmentResult,
)
from app.schemas.integrations import (
    AssessmentProviderAttemptCallback,
    AssessmentProviderAttemptOut,
    AssessmentProviderErasureRequest,
    AssessmentProviderInvitationCreate,
    AssessmentProviderInvitationOut,
    AssessmentProviderResultCallback,
    AssessmentProviderResultOut,
    ProviderAssessmentApplicationEvidenceOut,
    ProviderAssessmentEvidenceOut,
    ProviderAssessmentResultEvidenceOut,
    WebhookEnvelope,
)
from app.services.integrations.crypto import IntegrationCipher, IntegrationCryptoError
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.jobs import IntegrationJobError
from app.services.integrations.webhooks import load_webhook_envelope

_STATUSES = {"invited", "started", "submitted", "graded", "expired", "cancelled"}
_STATUS_RANK = {
    "queued": 0,
    "invited": 1,
    "started": 2,
    "submitted": 3,
    "graded": 4,
    "expired": 5,
    "cancelled": 5,
    "failed": 5,
}


def _https_url(value: Any, *, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderRequestError(code)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ProviderRequestError(code)
    return value


def _status(value: Any) -> str:
    if not isinstance(value, str) or value not in _STATUSES:
        raise ProviderRequestError("assessment_provider_status_invalid")
    return value


def _mapping_aad(record: ProviderAssessment, purpose: str) -> bytes:
    return (
        f"hr-agent:provider-assessment:v1:{record.organization_id}:"
        f"{record.id}:{purpose}"
    ).encode()


def _result_aad(record: ProviderAssessmentResult) -> bytes:
    return (
        f"hr-agent:provider-assessment-result:v1:{record.organization_id}:"
        f"{record.id}:evidence"
    ).encode()


async def create_provider_invitation(
    adapter: Any,
    *,
    credentials: dict[str, Any],
    configuration: dict,
    request: AssessmentProviderInvitationCreate,
    candidate_ref: str,
    callback_url: str,
) -> AssessmentProviderInvitationOut:
    payload = {
        "candidate_ref": candidate_ref,
        "template_ref": request.external_template_ref,
        "callback_url": callback_url,
        "expires_at": request.expires_at.isoformat(),
        "locale": request.locale,
        "accommodations": request.accommodations,
    }
    raw = await adapter.create_invitation(
        credentials,
        configuration,
        payload,
        idempotency_key=request.idempotency_key,
    )
    provider_id = raw.get("id")
    expires_at = raw.get("expires_at")
    if not isinstance(provider_id, str) or not provider_id:
        raise ProviderRequestError("assessment_provider_response_invalid")
    try:
        parsed_expiry = (
            datetime.fromisoformat(expires_at)
            if isinstance(expires_at, str)
            else request.expires_at
        )
        return AssessmentProviderInvitationOut(
            provider_invitation_id=provider_id,
            status=_status(raw.get("status", "invited")),
            launch_url=_https_url(
                raw.get("launch_url"), code="assessment_launch_url_invalid"
            ),
            expires_at=parsed_expiry,
        )
    except (ValueError, ValidationError) as exc:
        raise ProviderRequestError("assessment_provider_response_invalid") from exc


async def get_provider_attempt(
    adapter: Any,
    *,
    credentials: dict[str, Any],
    configuration: dict,
    attempt_id: str,
) -> AssessmentProviderAttemptOut:
    raw = await adapter.get_attempt(credentials, configuration, attempt_id)
    try:
        return AssessmentProviderAttemptOut(
            provider_attempt_id=str(raw["id"]),
            status=_status(raw["status"]),
            started_at=raw.get("started_at"),
            submitted_at=raw.get("submitted_at"),
            updated_at=raw["updated_at"],
        )
    except (KeyError, ValueError, ValidationError) as exc:
        raise ProviderRequestError("assessment_provider_response_invalid") from exc


async def get_provider_result(
    adapter: Any,
    *,
    credentials: dict[str, Any],
    configuration: dict,
    attempt_id: str,
) -> AssessmentProviderResultOut:
    raw = await adapter.get_result(credentials, configuration, attempt_id)
    try:
        result = AssessmentProviderResultOut(
            provider_attempt_id=str(raw["attempt_id"]),
            status=_status(raw["status"]),
            score=raw.get("score"),
            recommendation=raw.get("recommendation"),
            evidence=raw.get("evidence") or [],
            completed_at=raw["completed_at"],
        )
    except (KeyError, ValueError, ValidationError) as exc:
        raise ProviderRequestError("assessment_provider_response_invalid") from exc
    if result.status not in {"submitted", "graded"}:
        raise ProviderRequestError("assessment_result_not_ready", retryable=True)
    return result


async def erase_provider_candidate_data(
    adapter: Any,
    *,
    credentials: dict[str, Any],
    configuration: dict,
    request: AssessmentProviderErasureRequest,
    idempotency_key: str,
) -> str:
    """Require a synchronous provider confirmation and return an opaque proof.

    A mere HTTP acceptance is insufficient: the normalized contract requires
    the simulator/vendor to report completed erasure and a receipt.  Only the
    receipt digest is retained in the job summary.
    """

    raw = await adapter.erase_candidate_data(
        credentials,
        configuration,
        request.model_dump(mode="json", exclude_none=True),
        idempotency_key=idempotency_key,
    )
    receipt_id = raw.get("receipt_id") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("status") != "erased"
        or not isinstance(receipt_id, str)
        or not receipt_id.strip()
        or len(receipt_id) > 512
    ):
        raise ProviderRequestError("assessment_erasure_unconfirmed", retryable=True)
    return hashlib.sha256(receipt_id.encode()).hexdigest()


def normalize_assessment_callback(
    envelope: WebhookEnvelope,
) -> AssessmentProviderAttemptCallback | AssessmentProviderResultCallback:
    """Normalize a signed callback without applying a hiring decision."""

    try:
        if envelope.type == "assessment.attempt.updated":
            return AssessmentProviderAttemptCallback.model_validate(envelope.data)
        if envelope.type == "assessment.result.ready":
            return AssessmentProviderResultCallback.model_validate(envelope.data)
    except ValidationError as exc:
        raise ProviderRequestError("assessment_callback_invalid") from exc
    raise ProviderRequestError("assessment_callback_type_unsupported")


async def bind_provider_invitation_job(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    job: IntegrationJob,
    actor_user_id: uuid.UUID | None,
    request: AssessmentProviderInvitationCreate,
    now: datetime | None = None,
) -> ProviderAssessment:
    """Bind one idempotent invitation job to an existing tenant application."""

    if (
        connection.provider != "assessment_generic"
        or job.organization_id != connection.organization_id
        or job.connection_id != connection.id
        or job.operation != "assessment.invite"
    ):
        raise IntegrationJobError("assessment_binding_invalid")
    existing = (
        await db.execute(
            select(ProviderAssessment).where(
                ProviderAssessment.organization_id == connection.organization_id,
                ProviderAssessment.invitation_job_id == job.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    application = (
        await db.execute(
            select(Application).where(
                Application.id == request.application_id,
                Application.organization_id == connection.organization_id,
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise IntegrationJobError("assessment_target_not_found")
    if request.template_version_id is not None:
        template_exists = (
            await db.execute(
                select(AssessmentTemplateVersion.id).where(
                    AssessmentTemplateVersion.id == request.template_version_id,
                    AssessmentTemplateVersion.organization_id
                    == connection.organization_id,
                )
            )
        ).scalar_one_or_none()
        if template_exists is None:
            raise IntegrationJobError("assessment_target_not_found")
    current = now or datetime.now(UTC)
    if request.expires_at <= current or request.expires_at > current + timedelta(
        days=30
    ):
        raise IntegrationJobError("assessment_expiry_invalid")
    record = ProviderAssessment(
        organization_id=connection.organization_id,
        connection_id=connection.id,
        application_id=application.id,
        candidate_id=application.candidate_id,
        template_version_id=request.template_version_id,
        invitation_job_id=job.id,
        created_by_user_id=actor_user_id,
        external_template_ref=request.external_template_ref,
        status="queued",
        expires_at=request.expires_at,
        last_job_status=job.status,
    )
    db.add(record)
    await db.flush()
    return record


async def _mapping_for_job(
    db: AsyncSession, job: IntegrationJob, *, lock: bool = True
) -> ProviderAssessment:
    statement = select(ProviderAssessment).where(
        ProviderAssessment.organization_id == job.organization_id,
        ProviderAssessment.connection_id == job.connection_id,
    )
    if job.operation == "assessment.invite":
        statement = statement.where(ProviderAssessment.invitation_job_id == job.id)
    else:
        try:
            mapping_id = uuid.UUID(str(job.reference_payload["provider_assessment_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrationJobError("job_reference_invalid") from exc
        statement = statement.where(ProviderAssessment.id == mapping_id)
    if lock:
        statement = statement.with_for_update()
    record = (await db.execute(statement)).scalar_one_or_none()
    if record is None:
        raise IntegrationJobError("assessment_mapping_not_found")
    return record


async def require_provider_assessment(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    provider_assessment_id: uuid.UUID,
) -> ProviderAssessment:
    record = (
        await db.execute(
            select(ProviderAssessment)
            .join(
                Application,
                Application.id == ProviderAssessment.application_id,
            )
            .where(
                ProviderAssessment.id == provider_assessment_id,
                ProviderAssessment.organization_id == organization_id,
                ProviderAssessment.connection_id == connection_id,
                Application.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise IntegrationJobError("assessment_mapping_not_found")
    return record


async def _assert_attempt_available(
    db: AsyncSession,
    record: ProviderAssessment,
    provider_attempt_id: str,
) -> None:
    conflict = (
        await db.execute(
            select(ProviderAssessment.id).where(
                ProviderAssessment.connection_id == record.connection_id,
                ProviderAssessment.provider_attempt_id == provider_attempt_id,
                ProviderAssessment.id != record.id,
            )
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise ProviderRequestError("assessment_attempt_conflict")


def _advance_status(record: ProviderAssessment, status: str) -> bool:
    if _STATUS_RANK[status] < _STATUS_RANK[record.status]:
        return False
    changed = status != record.status
    record.status = status
    return changed


def _earliest_timestamp(
    current: datetime | None, observed: datetime | None
) -> datetime | None:
    """Keep the earliest provider timestamp so retries cannot rewrite history."""

    if observed is None:
        return current
    return observed if current is None or observed < current else current


def _record_attempt_timestamps(
    record: ProviderAssessment,
    *,
    started_at: datetime | None,
    submitted_at: datetime | None,
) -> bool:
    """Persist monotonic milestone timestamps reported by the provider."""

    prior_started = record.started_at
    prior_submitted = record.submitted_at
    record.started_at = _earliest_timestamp(record.started_at, started_at)
    record.submitted_at = _earliest_timestamp(record.submitted_at, submitted_at)
    return record.started_at != prior_started or record.submitted_at != prior_submitted


async def _store_result(
    db: AsyncSession,
    *,
    record: ProviderAssessment,
    result: AssessmentProviderResultOut,
    cipher: IntegrationCipher,
    provider_occurred_at: datetime,
    source_kind: str,
    source_id: uuid.UUID,
    source_event_id: uuid.UUID | None = None,
) -> tuple[ProviderAssessmentResult, bool]:
    existing = (
        await db.execute(
            select(ProviderAssessmentResult)
            .where(
                ProviderAssessmentResult.organization_id == record.organization_id,
                ProviderAssessmentResult.provider_assessment_id == record.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None and (
        result.completed_at < existing.completed_at
        or provider_occurred_at < existing.provider_occurred_at
    ):
        return existing, False
    await _assert_attempt_available(db, record, result.provider_attempt_id)
    if existing is None:
        existing = ProviderAssessmentResult(
            id=uuid.uuid4(),
            organization_id=record.organization_id,
            provider_assessment_id=record.id,
            provider_attempt_id=result.provider_attempt_id,
            status=result.status,
            completed_at=result.completed_at,
            provider_occurred_at=provider_occurred_at,
            provenance={},
            version=1,
            evidence_ciphertext=b"",
            evidence_nonce=b"",
            evidence_key_id=cipher.key_id,
        )
        db.add(existing)
    else:
        existing.version += 1
    encrypted = cipher.encrypt_json(result.evidence, aad=_result_aad(existing))
    existing.provider_attempt_id = result.provider_attempt_id
    existing.status = result.status
    existing.score = Decimal(str(result.score)) if result.score is not None else None
    existing.recommendation = result.recommendation
    existing.evidence_ciphertext = encrypted.ciphertext
    existing.evidence_nonce = encrypted.nonce
    existing.evidence_key_id = encrypted.key_id
    existing.completed_at = result.completed_at
    existing.provider_occurred_at = provider_occurred_at
    existing.source_event_id = source_event_id
    existing.source_job_id = source_id if source_kind == "poll" else None
    existing.provenance = {
        "provider": "assessment_generic",
        "connection_id": str(record.connection_id),
        "source": source_kind,
        "source_id": str(source_id),
        "observed_at": provider_occurred_at.isoformat(),
        "human_review_required": True,
        "automated_stage_change": False,
    }
    record.provider_attempt_id = result.provider_attempt_id
    _advance_status(record, result.status)
    _record_attempt_timestamps(
        record,
        started_at=None,
        submitted_at=result.completed_at,
    )
    record.provider_updated_at = result.completed_at
    await db.flush()
    return existing, True


async def execute_provider_assessment_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    connection: IntegrationConnection,
    adapter: Any,
    credentials: dict[str, Any],
    payload: dict[str, Any],
    cipher: IntegrationCipher,
) -> dict[str, Any] | None:
    """Execute and persist one specialist-assessment operation."""

    if job.operation not in {
        "assessment.invite",
        "assessment.status",
        "assessment.result",
        "assessment.delete",
    }:
        return None
    if job.operation == "assessment.delete":
        try:
            request = AssessmentProviderErasureRequest.model_validate(payload)
        except ValidationError as exc:
            raise IntegrationJobError("job_payload_invalid") from exc
        receipt_digest = await erase_provider_candidate_data(
            adapter,
            credentials=credentials,
            configuration=connection.configuration or {},
            request=request,
            idempotency_key=job.idempotency_key,
        )
        return {
            "provider_accepted": True,
            "deletion_confirmed": True,
            "receipt_sha256": receipt_digest,
        }
    record = await _mapping_for_job(db, job)
    if job.operation == "assessment.invite":
        request = AssessmentProviderInvitationCreate.model_validate(payload)
        callback_url = payload.get("callback_url")
        if not isinstance(callback_url, str) or not callback_url:
            raise IntegrationJobError("job_payload_invalid")
        invitation = await create_provider_invitation(
            adapter,
            credentials=credentials,
            configuration=connection.configuration or {},
            request=request,
            candidate_ref=str(record.candidate_id),
            callback_url=callback_url,
        )
        if invitation.expires_at <= datetime.now(UTC):
            raise ProviderRequestError("assessment_provider_response_invalid")
        encrypted = cipher.encrypt_bytes(
            invitation.launch_url.encode(), aad=_mapping_aad(record, "launch-url")
        )
        observed_at = datetime.now(UTC)
        record.provider_invitation_id = invitation.provider_invitation_id
        record.status = invitation.status
        record.launch_url_ciphertext = encrypted.ciphertext
        record.launch_url_nonce = encrypted.nonce
        record.launch_url_key_id = encrypted.key_id
        record.expires_at = min(record.expires_at, invitation.expires_at)
        record.provider_updated_at = observed_at
        # The normalized invitation response has no milestone timestamps. A
        # provider that already reports a progressed state is therefore
        # recorded at the first observation time instead of inventing an
        # earlier candidate event.
        if invitation.status in {"started", "submitted", "graded"}:
            record.started_at = _earliest_timestamp(record.started_at, observed_at)
        if invitation.status in {"submitted", "graded"}:
            record.submitted_at = _earliest_timestamp(record.submitted_at, observed_at)
        return {
            "provider_accepted": True,
            "provider_assessment_id": str(record.id),
            "external_ref": invitation.provider_invitation_id,
            "provider_status": invitation.status,
            "launch_ready": True,
        }
    attempt_id = record.provider_attempt_id or record.provider_invitation_id
    if not attempt_id:
        raise ProviderRequestError("assessment_mapping_not_ready", retryable=True)
    if job.operation == "assessment.status":
        attempt = await get_provider_attempt(
            adapter,
            credentials=credentials,
            configuration=connection.configuration or {},
            attempt_id=attempt_id,
        )
        await _assert_attempt_available(db, record, attempt.provider_attempt_id)
        record.provider_attempt_id = attempt.provider_attempt_id
        _advance_status(record, attempt.status)
        _record_attempt_timestamps(
            record,
            started_at=attempt.started_at,
            submitted_at=attempt.submitted_at,
        )
        record.provider_updated_at = attempt.updated_at
        return {
            "provider_accepted": True,
            "provider_assessment_id": str(record.id),
            "external_ref": attempt.provider_attempt_id,
            "provider_status": record.status,
        }
    result = await get_provider_result(
        adapter,
        credentials=credentials,
        configuration=connection.configuration or {},
        attempt_id=attempt_id,
    )
    _stored, changed = await _store_result(
        db,
        record=record,
        result=result,
        cipher=cipher,
        provider_occurred_at=datetime.now(UTC),
        source_kind="poll",
        source_id=job.id,
    )
    return {
        "provider_accepted": True,
        "provider_assessment_id": str(record.id),
        "external_ref": result.provider_attempt_id,
        "provider_status": record.status,
        "score_available": result.score is not None,
        "result_updated": changed,
    }


async def consume_assessment_callback(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
) -> dict[str, Any]:
    try:
        event_id = uuid.UUID(str(job.reference_payload["webhook_event_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrationJobError("job_reference_invalid") from exc
    event = (
        await db.execute(
            select(IntegrationWebhookEvent)
            .where(
                IntegrationWebhookEvent.id == event_id,
                IntegrationWebhookEvent.organization_id == job.organization_id,
                IntegrationWebhookEvent.connection_id == job.connection_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if event is None:
        raise IntegrationJobError("webhook_event_not_found")
    if event.status in {"processed", "ignored"}:
        return {
            "callback_processed": event.status == "processed",
            "callback_ignored": event.status == "ignored",
        }
    event.status = "processing"
    envelope = load_webhook_envelope(event, connection=connection, cipher=cipher)
    normalized = normalize_assessment_callback(envelope)
    record = (
        await db.execute(
            select(ProviderAssessment)
            .where(
                ProviderAssessment.organization_id == job.organization_id,
                ProviderAssessment.connection_id == job.connection_id,
                ProviderAssessment.provider_invitation_id
                == normalized.provider_invitation_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise ProviderRequestError("assessment_mapping_not_ready", retryable=True)
    event.provider_assessment_id = record.id
    changed = False
    if isinstance(normalized, AssessmentProviderAttemptCallback):
        await _assert_attempt_available(db, record, normalized.provider_attempt_id)
        if record.provider_attempt_id in {None, normalized.provider_attempt_id}:
            record.provider_attempt_id = normalized.provider_attempt_id
        changed = _advance_status(record, normalized.status)
        changed = (
            _record_attempt_timestamps(
                record,
                started_at=normalized.started_at,
                submitted_at=normalized.submitted_at,
            )
            or changed
        )
        if (
            record.provider_updated_at is None
            or normalized.updated_at > record.provider_updated_at
        ):
            record.provider_updated_at = normalized.updated_at
            changed = True
    else:
        _stored, changed = await _store_result(
            db,
            record=record,
            result=normalized,
            cipher=cipher,
            provider_occurred_at=envelope.created_at,
            source_kind="callback",
            source_id=event.id,
            source_event_id=event.id,
        )
    if record.last_event_at is None or envelope.created_at > record.last_event_at:
        record.last_event_at = envelope.created_at
    event.status = "processed" if changed else "ignored"
    event.failure_code = None
    event.processed_at = datetime.now(UTC)
    await db.flush()
    return {
        "callback_processed": changed,
        "callback_ignored": not changed,
        "provider_assessment_id": str(record.id),
        "provider_status": record.status,
    }


async def reconcile_provider_assessment_job(
    db: AsyncSession, *, job: IntegrationJob
) -> None:
    """Mirror durable job outcome onto its mapping and inbound event."""

    if not job.operation.startswith("assessment."):
        return
    record: ProviderAssessment | None = None
    if job.operation == "assessment.callback":
        try:
            event_id = uuid.UUID(str(job.reference_payload["webhook_event_id"]))
        except (KeyError, TypeError, ValueError):
            return
        event = (
            await db.execute(
                select(IntegrationWebhookEvent).where(
                    IntegrationWebhookEvent.id == event_id,
                    IntegrationWebhookEvent.organization_id == job.organization_id,
                    IntegrationWebhookEvent.connection_id == job.connection_id,
                )
            )
        ).scalar_one_or_none()
        if event is not None:
            if job.status == "retrying":
                event.status = "accepted"
                event.failure_code = job.failure_code
            elif job.status == "dead_letter":
                event.status = "failed"
                event.failure_code = job.failure_code
                event.processed_at = datetime.now(UTC)
            elif job.status == "cancelled":
                event.status = "ignored"
                event.failure_code = "job_cancelled"
                event.processed_at = datetime.now(UTC)
            if event.provider_assessment_id is not None:
                record = await db.get(ProviderAssessment, event.provider_assessment_id)
    else:
        try:
            record = await _mapping_for_job(db, job, lock=False)
        except IntegrationJobError:
            record = None
    if record is not None:
        record.last_job_status = job.status
        record.failure_code = job.failure_code
        if job.operation == "assessment.invite" and job.status == "dead_letter":
            record.status = "failed"
    await db.flush()


async def application_provider_evidence(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    application_id: uuid.UUID,
    cipher: IntegrationCipher,
) -> ProviderAssessmentApplicationEvidenceOut:
    application = (
        await db.execute(
            select(Application.id).where(
                Application.id == application_id,
                Application.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if application is None:
        raise IntegrationJobError("assessment_target_not_found")
    rows = list(
        (
            await db.execute(
                select(ProviderAssessment, ProviderAssessmentResult)
                .outerjoin(
                    ProviderAssessmentResult,
                    ProviderAssessmentResult.provider_assessment_id
                    == ProviderAssessment.id,
                )
                .where(
                    ProviderAssessment.organization_id == organization_id,
                    ProviderAssessment.application_id == application_id,
                )
                .order_by(
                    ProviderAssessment.created_at.desc(), ProviderAssessment.id.desc()
                )
            )
        ).all()
    )
    items: list[ProviderAssessmentEvidenceOut] = []
    now = datetime.now(UTC)
    for record, result in rows:
        launch_url = None
        if (
            record.status in {"invited", "started"}
            and record.expires_at > now
            and record.launch_url_ciphertext is not None
            and record.launch_url_nonce is not None
            and record.launch_url_key_id is not None
        ):
            try:
                launch_url = cipher.decrypt_bytes(
                    record.launch_url_ciphertext,
                    record.launch_url_nonce,
                    aad=_mapping_aad(record, "launch-url"),
                    key_id=record.launch_url_key_id,
                ).decode()
            except (IntegrationCryptoError, UnicodeDecodeError) as exc:
                raise IntegrationJobError("assessment_launch_unavailable") from exc
        result_out = None
        if result is not None:
            try:
                evidence = cipher.decrypt_json(
                    result.evidence_ciphertext,
                    result.evidence_nonce,
                    aad=_result_aad(result),
                    key_id=result.evidence_key_id,
                )
            except IntegrationCryptoError as exc:
                raise IntegrationJobError("assessment_evidence_unavailable") from exc
            if not isinstance(evidence, list) or any(
                not isinstance(item, dict) for item in evidence
            ):
                raise IntegrationJobError("assessment_evidence_unavailable")
            result_out = ProviderAssessmentResultEvidenceOut(
                id=result.id,
                provider_attempt_id=result.provider_attempt_id,
                status=result.status,
                score=float(result.score) if result.score is not None else None,
                recommendation=result.recommendation,
                evidence=evidence,
                provenance=result.provenance,
                completed_at=result.completed_at,
                provider_occurred_at=result.provider_occurred_at,
                version=result.version,
            )
        items.append(
            ProviderAssessmentEvidenceOut(
                id=record.id,
                connection_id=record.connection_id,
                application_id=record.application_id,
                candidate_id=record.candidate_id,
                template_version_id=record.template_version_id,
                external_template_ref=record.external_template_ref,
                provider_invitation_id=record.provider_invitation_id,
                provider_attempt_id=record.provider_attempt_id,
                status=record.status,
                candidate_launch_url=launch_url,
                expires_at=record.expires_at,
                provider_updated_at=record.provider_updated_at,
                started_at=record.started_at,
                submitted_at=record.submitted_at,
                last_event_at=record.last_event_at,
                last_job_status=record.last_job_status,
                failure_code=record.failure_code,
                result=result_out,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )
    return ProviderAssessmentApplicationEvidenceOut(
        application_id=application_id, items=items
    )
