"""Inventory-driven candidate erasure for non-relational live stores.

Candidate-owned domain rows normally disappear through tenant-scoped foreign
keys.  This module covers stores that intentionally have no candidate foreign
key: encrypted integration work, provider cursors and webhooks, operational
usage history, notifications, audit metadata, and queue leases.

The scan is deliberately fail-closed.  If an encrypted live artifact cannot be
opened in its original tenant/purpose context, retention cannot prove that it
does not contain the candidate and the surrounding transaction must roll back.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
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
from app.models.enterprise import AuditLog
from app.models.integration import (
    IntegrationConnection,
    IntegrationCursor,
    IntegrationJob,
    IntegrationWebhookDelivery,
    IntegrationWebhookEvent,
)
from app.models.notification import Notification
from app.models.operations import UsageLedgerEntry, WorkerQueueLease
from app.models.provider_assessment import ProviderAssessment
from app.models.interview_recording import InterviewRecording
from app.models.scheduling import InterviewAppointment, SchedulingLink
from app.models.sourcing import SourceCandidateReceipt
from app.models.task_submission import TaskSubmission
from app.models.workflow import WorkflowRun
from app.services.integrations.crypto import (
    IntegrationCipher,
    IntegrationCryptoError,
    connection_aad,
)

BACKUP_MAX_AGE_DAYS = 35
_ACTIVE_INTEGRATION_STATES = frozenset({"queued", "running", "retrying"})
_EXACT_DOCUMENT_MARKER_MIN_CHARS = 256
_UUID_MARKER_RE = re.compile(
    r"(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)
_EMAIL_MARKER_RE = re.compile(
    r"(?<![a-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
    r"(?![a-z0-9-])",
    re.IGNORECASE,
)
_HEX_TOKEN_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32,128}(?![0-9a-f])", re.I)
_OPAQUE_TOKEN_RE = re.compile(r"(?<![a-zA-Z0-9])[a-zA-Z0-9_~+/=-]{24,128}(?![a-zA-Z0-9])")
_PHONE_MARKER_RE = re.compile(r"^\+[1-9][0-9]{9,14}$")
_PHONE_IN_TEXT_RE = re.compile(r"(?<![0-9])\+[1-9][0-9\s().-]{8,24}[0-9](?![0-9])")


class PrivacyErasureError(RuntimeError):
    """A live-store traversal could not prove candidate erasure."""


@dataclass(frozen=True, slots=True)
class CandidatePrivacyContext:
    candidate_ids: frozenset[UUID]
    related_ids: frozenset[UUID]
    candidate_job_ids: frozenset[UUID]
    workflow_run_ids: frozenset[UUID]
    provider_assessment_ids: frozenset[UUID]
    markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrivacyErasureSummary:
    integration_jobs_scrubbed: int
    webhook_events_erased: int
    webhook_deliveries_erased: int
    provider_cursors_erased: int
    usage_rows_pseudonymized: int
    audit_rows_erased: int
    notifications_erased: int
    communication_batch_entries_erased: int
    queue_leases_erased: int
    backup_expires_at: datetime


def _json_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise PrivacyErasureError("candidate_erasure_value_invalid") from exc


def contains_candidate_marker(value: Any, markers: tuple[str, ...]) -> bool:
    """Return whether a structured value contains an inventory marker."""

    if value is None or not markers:
        return False
    try:
        for raw in _iter_searchable_strings(value):
            haystack = raw.strip().casefold()
            email_tokens = {
                match.group(0).casefold() for match in _EMAIL_MARKER_RE.finditer(raw)
            }
            uuid_tokens = {
                match.group(0).casefold() for match in _UUID_MARKER_RE.finditer(raw)
            }
            phone_tokens = {
                re.sub(r"[\s().-]", "", match.group(0)).casefold()
                for match in _PHONE_IN_TEXT_RE.finditer(raw)
            }
            opaque_tokens = _high_entropy_embedded_markers(raw)
            for marker in markers:
                if _EMAIL_MARKER_RE.fullmatch(marker):
                    matched = marker in email_tokens
                elif _UUID_MARKER_RE.fullmatch(marker):
                    matched = marker in uuid_tokens
                elif _PHONE_MARKER_RE.fullmatch(marker):
                    matched = marker in phone_tokens
                elif len(marker) >= _EXACT_DOCUMENT_MARKER_MIN_CHARS:
                    matched = marker in haystack
                else:
                    # All remaining inventory markers are validated opaque
                    # tokens. Compare token equality, not substrings (for
                    # example ann@example.com must not match joann@example.com).
                    matched = marker in opaque_tokens
                if matched:
                    return True
    except (TypeError, ValueError) as exc:
        raise PrivacyErasureError("candidate_erasure_value_invalid") from exc
    return False


def _iter_searchable_strings(value: Any):
    """Yield structured text without JSON escaping changing exact documents.

    Dictionary keys are searchable because provider/user-controlled payloads
    can place identifiers in either keys or values.  Traversing raw strings
    also means a multiline CV marker containing quotes/newlines is compared to
    the original text instead of an escaped JSON representation.
    """

    if value is None:
        return
    if isinstance(value, UUID):
        yield str(value)
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_searchable_strings(key)
            yield from _iter_searchable_strings(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_searchable_strings(item)
        return
    if isinstance(value, (bool, int, float)):
        return
    # Preserve the previous fail-closed contract for values that cannot be
    # represented by the supported JSON-like privacy inventory.
    _json_text(value)


def _iter_string_values(value: Any):
    if value is None:
        return
    if isinstance(value, UUID):
        yield str(value)
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_string_values(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_string_values(item)


def _high_entropy_embedded_markers(raw: str) -> set[str]:
    """Extract only opaque canaries whose collision risk is acceptably low.

    This deliberately rejects ordinary names, job titles, skills, and prose.
    It also rejects embedded emails and UUIDs: a CV or prompt may mention a
    referee, recruiter, requisition, or other third party.  Canonical typed
    candidate identifiers are promoted separately via ``identifier=True``.
    """

    found: set[str] = set()
    identifier_spans = [
        match.span()
        for pattern in (_EMAIL_MARKER_RE, _UUID_MARKER_RE)
        for match in pattern.finditer(raw)
    ]

    def is_identifier_fragment(start: int, end: int) -> bool:
        return any(
            start >= span_start and end <= span_end
            for span_start, span_end in identifier_spans
        )

    for match in _HEX_TOKEN_RE.finditer(raw):
        token = match.group(0)
        if (
            not is_identifier_fragment(*match.span())
            and len(set(token.casefold())) >= 10
        ):
            found.add(token.casefold())
    for match in _OPAQUE_TOKEN_RE.finditer(raw):
        token = match.group(0)
        letters = [character for character in token if character.isalpha()]
        digits = [character for character in token if character.isdigit()]
        # Require substantial character diversity plus mixed case. Human
        # phrases/slugs and dates fail this test; random opaque canaries pass.
        if (
            not is_identifier_fragment(*match.span())
            and len(letters) >= 8
            and len(digits) >= 4
            and any(character.islower() for character in letters)
            and any(character.isupper() for character in letters)
            and len(set(token.casefold())) >= 16
        ):
            found.add(token.casefold())
    return found


def _add_marker(
    markers: set[str],
    value: Any,
    *,
    identifier: bool = False,
    exact_document: bool = False,
) -> None:
    for raw in _iter_string_values(value):
        normalized = raw.strip().casefold()
        if not normalized:
            continue
        compact_phone = re.sub(r"[\s().-]", "", normalized)
        if identifier and (
            _UUID_MARKER_RE.fullmatch(normalized)
            or _EMAIL_MARKER_RE.fullmatch(normalized)
            or _PHONE_MARKER_RE.fullmatch(compact_phone)
        ):
            markers.add(normalized)
            if _PHONE_MARKER_RE.fullmatch(compact_phone):
                markers.add(compact_phone)
            continue
        embedded = _high_entropy_embedded_markers(raw)
        markers.update(embedded)
        if identifier and normalized in embedded:
            markers.add(normalized)
        if exact_document and len(normalized) >= _EXACT_DOCUMENT_MARKER_MIN_CHARS:
            # A whole CV/transcript can be retained as one exact marker. We do
            # not split it into phrases: matching a common role/name fragment
            # would make erasure destructive to unrelated tenant records.
            markers.add(normalized)


async def build_candidate_privacy_context(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: set[UUID],
) -> CandidatePrivacyContext:
    """Snapshot candidate identifiers before cascades remove their linkage."""

    if not candidate_ids:
        return CandidatePrivacyContext(
            frozenset(), frozenset(), frozenset(), frozenset(), frozenset(), ()
        )
    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.organization_id == organization_id,
                    Candidate.id.in_(candidate_ids),
                )
                .execution_options(include_deleted=True)
            )
        ).scalars()
    )
    if {item.id for item in candidates} != candidate_ids:
        raise PrivacyErasureError("candidate_erasure_inventory_incomplete")

    contacts = list(
        (
            await db.execute(
                select(CandidateContactPoint).where(
                    CandidateContactPoint.organization_id == organization_id,
                    CandidateContactPoint.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    applications = list(
        (
            await db.execute(
                select(Application).where(
                    Application.organization_id == organization_id,
                    Application.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    receipts = list(
        (
            await db.execute(
                select(SourceCandidateReceipt).where(
                    SourceCandidateReceipt.organization_id == organization_id,
                    SourceCandidateReceipt.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    appointments = list(
        (
            await db.execute(
                select(InterviewAppointment).where(
                    InterviewAppointment.organization_id == organization_id,
                    InterviewAppointment.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    provider_assessments = list(
        (
            await db.execute(
                select(ProviderAssessment).where(
                    ProviderAssessment.organization_id == organization_id,
                    ProviderAssessment.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    candidate_jobs = list(
        (
            await db.execute(
                select(CandidateProcessingJob).where(
                    CandidateProcessingJob.organization_id == organization_id,
                    CandidateProcessingJob.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    workflow_runs = list(
        (
            await db.execute(
                select(WorkflowRun).where(
                    WorkflowRun.organization_id == organization_id,
                    WorkflowRun.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.organization_id == organization_id,
                    CandidateActivity.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    chats = list(
        (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.organization_id == organization_id,
                    ChatSession.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    threads = list(
        (
            await db.execute(
                select(CommunicationThread).where(
                    CommunicationThread.organization_id == organization_id,
                    CommunicationThread.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    recordings = list(
        (
            await db.execute(
                select(InterviewRecording).where(
                    InterviewRecording.organization_id == organization_id,
                    InterviewRecording.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    scheduling_links = list(
        (
            await db.execute(
                select(SchedulingLink).where(
                    SchedulingLink.organization_id == organization_id,
                    SchedulingLink.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    tasks = list(
        (
            await db.execute(
                select(TaskSubmission).where(
                    TaskSubmission.organization_id == organization_id,
                    TaskSubmission.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )

    markers: set[str] = set()
    related_ids: set[UUID] = set(candidate_ids)
    for identifier in candidate_ids:
        _add_marker(markers, identifier, identifier=True)
    for item in candidates:
        _add_marker(markers, item.source_ref)
        _add_marker(markers, item.raw_cv, exact_document=True)
        _add_marker(markers, item.parsed_cv)
        _add_marker(markers, item.github_login)
        _add_marker(markers, item.github_data)
        _add_marker(markers, item.email_normalized, identifier=True)
        _add_marker(markers, item.phone_normalized, identifier=True)
        _add_marker(markers, item.pdf_path, identifier=True)
        if item.private_object_id is not None:
            related_ids.add(item.private_object_id)
    for item in contacts:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.value, identifier=True)
        _add_marker(markers, item.normalized_value, identifier=True)
    for item in applications:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.external_ref, identifier=True)
        _add_marker(markers, item.application_metadata)
    for item in receipts:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.external_id, identifier=True)
        _add_marker(markers, item.source_url, identifier=True)
    for item in appointments:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.external_event_id, identifier=True)
        _add_marker(markers, item.external_meeting_url, identifier=True)
        _add_marker(markers, item.location_details)
    for item in provider_assessments:
        related_ids.update((item.id, item.invitation_job_id))
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.invitation_job_id, identifier=True)
        _add_marker(markers, item.provider_invitation_id, identifier=True)
        _add_marker(markers, item.provider_attempt_id, identifier=True)
    for item in candidate_jobs:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.idempotency_key, identifier=True)
        _add_marker(markers, item.result_summary)
    for item in workflow_runs:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.dedupe_key, identifier=True)
        _add_marker(markers, item.action_results)
    for item in activities:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.payload)
    for item in chats:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.title)
    for item in threads:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
    for item in recordings:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.title)
        _add_marker(markers, item.media_url, identifier=True)
        _add_marker(markers, item.transcript, exact_document=True)
        _add_marker(markers, item.interview_summary)
    for item in scheduling_links:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
    for item in tasks:
        related_ids.add(item.id)
        _add_marker(markers, item.id, identifier=True)
        _add_marker(markers, item.title)
        _add_marker(markers, item.prompt)
        _add_marker(markers, item.repo_url, identifier=True)
        _add_marker(markers, item.content)

    return CandidatePrivacyContext(
        candidate_ids=frozenset(candidate_ids),
        related_ids=frozenset(related_ids),
        candidate_job_ids=frozenset(item.id for item in candidate_jobs),
        workflow_run_ids=frozenset(item.id for item in workflow_runs),
        provider_assessment_ids=frozenset(item.id for item in provider_assessments),
        markers=tuple(sorted(markers, key=lambda value: (-len(value), value))),
    )


def _cipher() -> IntegrationCipher:
    settings = get_settings()
    key = settings.integration_credential_encryption_key
    if key is None:
        raise PrivacyErasureError("candidate_erasure_cipher_unavailable")
    try:
        return IntegrationCipher.from_base64(
            key,
            key_id=settings.integration_credential_key_id,
        )
    except IntegrationCryptoError as exc:
        raise PrivacyErasureError("candidate_erasure_cipher_unavailable") from exc


def _connection(
    connections: dict[UUID, IntegrationConnection], connection_id: UUID
) -> IntegrationConnection:
    value = connections.get(connection_id)
    if value is None:
        raise PrivacyErasureError("candidate_erasure_connection_missing")
    return value


def _decrypt_json(
    cipher: IntegrationCipher,
    *,
    ciphertext: bytes,
    nonce: bytes,
    key_id: str,
    aad: bytes,
) -> Any:
    try:
        raw = cipher.decrypt_bytes(ciphertext, nonce, aad=aad, key_id=key_id)
        return json.loads(raw)
    except (IntegrationCryptoError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivacyErasureError("candidate_erasure_ciphertext_unavailable") from exc


def _is_calendar_delete_tombstone(job: IntegrationJob) -> bool:
    return (
        job.operation == "calendar.delete"
        and (job.reference_payload or {}).get("kind")
        == "candidate_erasure_calendar_cleanup"
    )


def _is_assessment_delete_tombstone(job: IntegrationJob) -> bool:
    return (
        job.operation == "assessment.delete"
        and (job.reference_payload or {}).get("kind")
        == "candidate_erasure_assessment_cleanup"
    )


def _scrub_job(job: IntegrationJob, *, now: datetime) -> None:
    if job.status in _ACTIVE_INTEGRATION_STATES:
        job.status = "cancelled"
        job.cancelled_at = now
        job.completed_at = now
    job.idempotency_key = f"privacy-erased:{job.id}"
    job.request_fingerprint = hashlib.sha256(
        f"candidate-erased:{job.id}".encode()
    ).hexdigest()
    job.reference_payload = {"kind": "candidate_erased"}
    job.payload_ciphertext = None
    job.payload_nonce = None
    job.payload_key_id = None
    job.result_summary = {"privacy_erased": True}
    job.failure_code = None
    job.leased_at = None
    job.leased_by = None
    job.lease_expires_at = None


def _manifest_digest(organization_id: UUID, candidate_id: UUID) -> str:
    return hmac.new(
        get_settings().jwt_secret.encode(),
        f"candidate-erasure:v1:{organization_id}:{candidate_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def erase_candidate_live_store_copies(
    db: AsyncSession,
    *,
    organization_id: UUID,
    context: CandidatePrivacyContext,
    now: datetime | None = None,
) -> PrivacyErasureSummary:
    """Scrub non-FK live copies and record the backup-expiry obligation."""

    current = now or datetime.now(UTC)
    backup_expires_at = current + timedelta(days=BACKUP_MAX_AGE_DAYS)
    if not context.candidate_ids:
        return PrivacyErasureSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, backup_expires_at)

    connections = {
        item.id: item
        for item in (
            (
                await db.execute(
                    select(IntegrationConnection).where(
                        IntegrationConnection.organization_id == organization_id
                    )
                )
            ).scalars()
        )
    }
    events = list(
        (
            await db.execute(
                select(IntegrationWebhookEvent)
                .where(IntegrationWebhookEvent.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    deliveries = list(
        (
            await db.execute(
                select(IntegrationWebhookDelivery)
                .where(IntegrationWebhookDelivery.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    cursors = list(
        (
            await db.execute(
                select(IntegrationCursor)
                .where(IntegrationCursor.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    jobs = list(
        (
            await db.execute(
                select(IntegrationJob)
                .where(IntegrationJob.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    cipher = (
        _cipher()
        if events
        or deliveries
        or cursors
        or any(item.payload_ciphertext is not None for item in jobs)
        else None
    )

    related_ids = set(context.related_ids)
    erased_events = 0
    for event in events:
        matched = event.provider_assessment_id in context.provider_assessment_ids
        if not matched:
            if cipher is None:
                raise PrivacyErasureError("candidate_erasure_cipher_unavailable")
            connection = _connection(connections, event.connection_id)
            value = _decrypt_json(
                cipher,
                ciphertext=event.body_ciphertext,
                nonce=event.body_nonce,
                key_id=event.body_key_id,
                aad=connection_aad(
                    organization_id,
                    connection.id,
                    f"webhook-inbound:{event.event_id}",
                ),
            )
            matched = contains_candidate_marker(value, context.markers)
        if matched:
            related_ids.add(event.id)
            await db.delete(event)
            erased_events += 1

    erased_deliveries = 0
    for delivery in deliveries:
        if cipher is None:
            raise PrivacyErasureError("candidate_erasure_cipher_unavailable")
        connection = _connection(connections, delivery.connection_id)
        value = _decrypt_json(
            cipher,
            ciphertext=delivery.body_ciphertext,
            nonce=delivery.body_nonce,
            key_id=delivery.body_key_id,
            aad=connection_aad(
                organization_id,
                connection.id,
                f"webhook-outbound:{delivery.event_id}",
            ),
        )
        if contains_candidate_marker(value, context.markers):
            related_ids.add(delivery.id)
            await db.delete(delivery)
            erased_deliveries += 1

    erased_cursors = 0
    for cursor in cursors:
        if cipher is None:
            raise PrivacyErasureError("candidate_erasure_cipher_unavailable")
        connection = _connection(connections, cursor.connection_id)
        value = _decrypt_json(
            cipher,
            ciphertext=cursor.value_ciphertext,
            nonce=cursor.value_nonce,
            key_id=cursor.value_key_id,
            aad=connection_aad(
                organization_id,
                connection.id,
                f"cursor:{cursor.cursor_name}",
            ),
        )
        if contains_candidate_marker(value, context.markers):
            related_ids.add(cursor.id)
            await db.delete(cursor)
            erased_cursors += 1

    scrubbed_jobs = 0
    scrubbed_job_ids: set[UUID] = set()
    for job in jobs:
        direct = contains_candidate_marker(
            {
                "idempotency_key": job.idempotency_key,
                "references": job.reference_payload,
                "result": job.result_summary,
            },
            context.markers,
        ) or any(
            str(identifier).casefold() in _json_text(job.reference_payload).casefold()
            for identifier in related_ids
        )
        payload_match = False
        if job.payload_ciphertext is not None and not direct:
            if _is_calendar_delete_tombstone(job):
                # The encrypted event identifier is the minimum data required
                # to perform provider deletion.  It is purpose-bound and may
                # remain only while this erasure job is runnable.
                if job.status not in _ACTIVE_INTEGRATION_STATES:
                    direct = True
            elif _is_assessment_delete_tombstone(job):
                if (
                    cipher is None
                    or job.payload_nonce is None
                    or job.payload_key_id is None
                ):
                    raise PrivacyErasureError(
                        "candidate_erasure_ciphertext_unavailable"
                    )
                value = _decrypt_json(
                    cipher,
                    ciphertext=job.payload_ciphertext,
                    nonce=job.payload_nonce,
                    key_id=job.payload_key_id,
                    aad=connection_aad(
                        organization_id,
                        job.id,
                        "job-payload",
                    ),
                )
                if contains_candidate_marker(value, context.markers):
                    if job.status != "succeeded":
                        raise PrivacyErasureError(
                            "candidate_erasure_provider_deletion_unconfirmed"
                        )
                    # Compatibility cleanup for a success written before
                    # terminal payload scrubbing was introduced.
                    job.payload_ciphertext = None
                    job.payload_nonce = None
                    job.payload_key_id = None
            else:
                if (
                    cipher is None
                    or job.payload_nonce is None
                    or job.payload_key_id is None
                ):
                    raise PrivacyErasureError(
                        "candidate_erasure_ciphertext_unavailable"
                    )
                value = _decrypt_json(
                    cipher,
                    ciphertext=job.payload_ciphertext,
                    nonce=job.payload_nonce,
                    key_id=job.payload_key_id,
                    aad=connection_aad(
                        organization_id,
                        job.id,
                        "job-payload",
                    ),
                )
                payload_match = contains_candidate_marker(value, context.markers)
        if direct or payload_match:
            _scrub_job(job, now=current)
            scrubbed_jobs += 1
            scrubbed_job_ids.add(job.id)

    usage_rows = list(
        (
            await db.execute(
                select(UsageLedgerEntry)
                .where(UsageLedgerEntry.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    usage_pseudonymized = 0
    for row in usage_rows:
        direct = row.resource_type == "candidate" and row.resource_id in (
            context.candidate_ids
        )
        if not direct:
            direct = contains_candidate_marker(
                {
                    "idempotency_key": row.idempotency_key,
                    "resource_id": row.resource_id,
                    "metadata": row.metadata_json,
                },
                context.markers,
            )
        if not direct:
            continue
        # Preserve the accounting delta and timestamp while severing the
        # subject link.  Deleting usage rows would silently corrupt limits.
        row.idempotency_key = f"privacy-erased:{row.id}"
        row.resource_type = "erased_subject"
        row.resource_id = None
        row.metadata_json = {"privacy_erased": True}
        usage_pseudonymized += 1

    audits = list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    audit_erased = 0
    for row in audits:
        direct = row.resource_type == "candidate" and row.resource_id in (
            context.candidate_ids
        )
        if direct or contains_candidate_marker(row.metadata_json, context.markers):
            await db.delete(row)
            audit_erased += 1

    notifications = list(
        (
            await db.execute(
                select(Notification)
                .where(Notification.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    notifications_erased = 0
    for row in notifications:
        if contains_candidate_marker(row.payload, context.markers):
            await db.delete(row)
            notifications_erased += 1

    batches = list(
        (
            await db.execute(
                select(CommunicationBatch)
                .where(CommunicationBatch.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    batch_entries_erased = 0
    for batch in batches:
        current_entries = list(batch.skip_summary or [])
        retained = [
            item
            for item in current_entries
            if not contains_candidate_marker(item, context.markers)
        ]
        removed = len(current_entries) - len(retained)
        if not removed:
            continue
        batch.skip_summary = retained
        batch.skipped_count = max(0, batch.skipped_count - removed)
        batch.requested_count = max(0, batch.requested_count - removed)
        batch_entries_erased += removed

    lease_ids = (
        set(context.candidate_job_ids)
        | set(context.workflow_run_ids)
        | scrubbed_job_ids
    )
    queue_leases_erased = 0
    if lease_ids:
        queue_leases_erased = int(
            (
                await db.execute(
                    delete(WorkerQueueLease).where(
                        WorkerQueueLease.organization_id == organization_id,
                        WorkerQueueLease.work_id.in_(lease_ids),
                    )
                )
            ).rowcount
            or 0
        )

    candidate_digests = sorted(
        _manifest_digest(organization_id, candidate_id)
        for candidate_id in context.candidate_ids
    )
    db.add(
        AuditLog(
            organization_id=organization_id,
            actor_user_id=None,
            actor_email_snapshot=None,
            action="candidate_erasure.backup_expiry_manifest",
            resource_type="organization",
            resource_id=organization_id,
            path_template="/enterprise/retention/run",
            method="SYSTEM",
            status_code=202,
            request_id=uuid4(),
            metadata_json={
                "format": "candidate-erasure-backup-expiry-v1",
                "candidate_count": len(candidate_digests),
                "candidate_digests": candidate_digests,
                "live_erasure_at": current.isoformat(),
                "backups_not_after": backup_expires_at.isoformat(),
                "maximum_backup_age_days": BACKUP_MAX_AGE_DAYS,
                "physical_deletion_verified": False,
                "required_expiry_evidence": [
                    "encrypted_archive_prune_receipt",
                    "object_store_lifecycle_evidence",
                ],
            },
            occurred_at=current,
        )
    )
    return PrivacyErasureSummary(
        integration_jobs_scrubbed=scrubbed_jobs,
        webhook_events_erased=erased_events,
        webhook_deliveries_erased=erased_deliveries,
        provider_cursors_erased=erased_cursors,
        usage_rows_pseudonymized=usage_pseudonymized,
        audit_rows_erased=audit_erased,
        notifications_erased=notifications_erased,
        communication_batch_entries_erased=batch_entries_erased,
        queue_leases_erased=queue_leases_erased,
        backup_expires_at=backup_expires_at,
    )


__all__ = [
    "BACKUP_MAX_AGE_DAYS",
    "CandidatePrivacyContext",
    "PrivacyErasureError",
    "PrivacyErasureSummary",
    "build_candidate_privacy_context",
    "contains_candidate_marker",
    "erase_candidate_live_store_copies",
]
