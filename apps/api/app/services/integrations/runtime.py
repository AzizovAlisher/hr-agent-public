"""Execute encrypted provider jobs through the canonical adapter boundary."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.communication import CandidateContactPoint
from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.scheduling import (
    InterviewAppointment,
    InterviewAppointmentInterviewer,
)
from app.models.user import User
from app.services.integrations.assessments import (
    consume_assessment_callback,
    execute_provider_assessment_job,
    reconcile_provider_assessment_job,
)
from app.services.integrations.crypto import IntegrationCipher, IntegrationCryptoError
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.jobs import (
    IntegrationJobError,
    fail_job,
    load_job_payload,
    succeed_job,
)
from app.services.integrations.lifecycle import (
    IntegrationLifecycleError,
    ensure_fresh_credentials,
)
from app.services.integrations.providers import (
    CalendarEventRequest,
    EmailRequest,
    VacancyPublishRequest,
)
from app.services.integrations.sourcing import (
    execute_sourcing_job,
    reconcile_sourcing_job,
)

logger = logging.getLogger(__name__)


async def _fail_and_reconcile(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    failure_code: str,
    retryable: bool,
) -> None:
    await fail_job(
        db,
        job=job,
        failure_code=failure_code,
        retryable=retryable,
    )
    await reconcile_sourcing_job(db, job=job)
    await reconcile_provider_assessment_job(db, job=job)


async def execute_provider_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    cipher: IntegrationCipher,
    adapters: dict[str, Any],
    prepared_credentials: dict[str, Any] | None = None,
) -> None:
    if job.status != "running":
        raise IntegrationJobError("job_not_running")
    if job.operation == "calendar.sync":
        await _execute_calendar_sync_job(
            db,
            job=job,
            cipher=cipher,
            adapters=adapters,
            prepared_credentials=prepared_credentials,
        )
        return
    connection_statement = select(IntegrationConnection).where(
        IntegrationConnection.id == job.connection_id,
        IntegrationConnection.organization_id == job.organization_id,
    )
    # Dedicated workers refresh credentials in a short, separately committed
    # preparation transaction and pass the decrypted snapshot here. Legacy
    # synchronous callers retain the serialized refresh path.
    if prepared_credentials is None:
        connection_statement = connection_statement.with_for_update()
    connection = (await db.execute(connection_statement)).scalar_one_or_none()
    if connection is None:
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code="connection_not_found",
            retryable=False,
        )
        return
    if connection.status not in {"connected", "degraded"}:
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code="connection_not_ready",
            retryable=False,
        )
        return
    is_local_sourcing = (job.reference_payload or {}).get("source_kind") in {
        "local_candidate_import",
        "local_candidate_file_import",
    }
    adapter = adapters.get(connection.provider)
    if adapter is None and not is_local_sourcing:
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code="provider_adapter_unavailable",
            retryable=False,
        )
        return
    try:
        credentials = prepared_credentials
        if credentials is None and not is_local_sourcing:
            credentials = await ensure_fresh_credentials(
                db, connection=connection, cipher=cipher, adapter=adapter
            )
        if credentials is None:
            credentials = {}
        payload = load_job_payload(job, cipher=cipher)
        result = await execute_sourcing_job(
            db,
            job=job,
            connection=connection,
            adapter=adapter,
            credentials=credentials,
            cipher=cipher,
        )
        if result is None and job.operation == "assessment.callback":
            result = await consume_assessment_callback(
                db,
                job=job,
                connection=connection,
                cipher=cipher,
            )
        if result is None:
            result = await execute_provider_assessment_job(
                db,
                job=job,
                connection=connection,
                adapter=adapter,
                credentials=credentials,
                payload=payload,
                cipher=cipher,
            )
        if result is None:
            result = await _execute(
                adapter,
                connection=connection,
                credentials=credentials,
                operation=job.operation,
                payload=payload,
                idempotency_key=job.idempotency_key,
            )
    except (ProviderRequestError, IntegrationLifecycleError) as exc:
        code = getattr(exc, "code", "provider_job_failed")
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code=code,
            retryable=bool(getattr(exc, "retryable", False)),
        )
        connection.last_error_code = code
        connection.consecutive_failures += 1
        connection.status = (
            "expired"
            if code in {"provider_auth_rejected", "provider_token_expired"}
            else "degraded"
        )
        return
    except IntegrationJobError as exc:
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code=exc.code,
            retryable=False,
        )
        return
    except IntegrationCryptoError:
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code="job_payload_unavailable",
            retryable=False,
        )
        return
    except (KeyError, TypeError, ValueError):
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code="job_payload_invalid",
            retryable=False,
        )
        return
    except Exception:
        # A malformed provider implementation or unexpected SDK exception must
        # dead-letter this unit of work, not poison every later job in the
        # worker cycle. Details belong in sanitized server logs, never the row.
        logger.exception(
            "Integration provider job failed unexpectedly",
            extra={
                "integration_job_id": str(job.id),
                "integration_provider": connection.provider,
                "integration_operation": job.operation,
            },
        )
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code="integration_job_failed",
            retryable=False,
        )
        return
    try:
        await succeed_job(db, job=job, result_summary=result)
    except IntegrationJobError as exc:
        await _fail_and_reconcile(
            db,
            job=job,
            failure_code=exc.code,
            retryable=False,
        )
        return
    await reconcile_sourcing_job(db, job=job)
    await reconcile_provider_assessment_job(db, job=job)
    connection.status = "connected"
    connection.last_success_at = datetime.now(UTC)
    connection.last_error_code = None
    connection.consecutive_failures = 0


async def _calendar_sync_failure(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    appointment: InterviewAppointment | None,
    failure_code: str,
    retryable: bool,
) -> None:
    await _fail_and_reconcile(
        db,
        job=job,
        failure_code=failure_code,
        retryable=retryable,
    )
    if appointment is not None and appointment.version == int(
        job.reference_payload.get("appointment_version", -1)
    ):
        appointment.sync_status = "pending" if job.status == "retrying" else "failed"


async def _execute_calendar_sync_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    cipher: IntegrationCipher,
    adapters: dict[str, Any],
    prepared_credentials: dict[str, Any] | None,
) -> None:
    """Reconcile one appointment version from reference-only durable work."""

    try:
        appointment_id = UUID(str(job.reference_payload["appointment_id"]))
        requested_version = int(job.reference_payload["appointment_version"])
        if requested_version < 1 or set(job.reference_payload) != {
            "appointment_id",
            "appointment_version",
        }:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=None,
            failure_code="job_reference_invalid",
            retryable=False,
        )
        return

    # Integration replicas serialize only work for the same appointment. The
    # lock spans provider I/O so an older create cannot overtake a newer cancel.
    lock_key = f"calendar-sync:{job.organization_id}:{appointment_id}"
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    await db.refresh(job)
    if job.status != "running":
        return
    appointment = (
        await db.execute(
            select(InterviewAppointment).where(
                InterviewAppointment.id == appointment_id,
                InterviewAppointment.organization_id == job.organization_id,
                InterviewAppointment.calendar_connection_id == job.connection_id,
            )
        )
    ).scalar_one_or_none()
    if appointment is None:
        await succeed_job(
            db,
            job=job,
            result_summary={"reconciled": False, "appointment_missing": True},
        )
        await reconcile_sourcing_job(db, job=job)
        return
    if appointment.version != requested_version:
        await succeed_job(
            db,
            job=job,
            result_summary={
                "reconciled": False,
                "stale": True,
                "appointment_version": appointment.version,
            },
        )
        await reconcile_sourcing_job(db, job=job)
        return
    # A cancelled appointment that never reached the provider has no external
    # side effect to undo and can settle even if the connection was revoked.
    if appointment.status == "cancelled" and not appointment.external_event_id:
        appointment.sync_status = "synced"
        appointment.external_meeting_url = None
        await succeed_job(
            db,
            job=job,
            result_summary={"reconciled": True, "event_deleted": False},
        )
        await reconcile_sourcing_job(db, job=job)
        return

    connection = (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.id == job.connection_id,
                IntegrationConnection.organization_id == job.organization_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=appointment,
            failure_code="connection_not_found",
            retryable=False,
        )
        return
    if connection.status not in {"connected", "degraded"}:
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=appointment,
            failure_code="connection_not_ready",
            retryable=False,
        )
        return
    adapter = adapters.get(connection.provider)
    if adapter is None:
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=appointment,
            failure_code="provider_adapter_unavailable",
            retryable=False,
        )
        return
    try:
        credentials = prepared_credentials
        if credentials is None:
            credentials = await ensure_fresh_credentials(
                db,
                connection=connection,
                cipher=cipher,
                adapter=adapter,
            )
        result = None
        action = "delete"
        if appointment.status == "cancelled":
            await adapter.delete_event(
                credentials,
                connection.configuration or {},
                str(appointment.external_event_id),
            )
        else:
            request = await _appointment_calendar_request(db, appointment, job)
            conflict_check = getattr(adapter, "has_calendar_conflict", None)
            if conflict_check is not None and await conflict_check(
                credentials,
                connection.configuration or {},
                starts_at=request.starts_at,
                ends_at=request.ends_at,
                exclude_event_id=appointment.external_event_id,
            ):
                raise ProviderRequestError("provider_calendar_conflict")
            if appointment.external_event_id:
                action = "update"
                result = await adapter.update_event(
                    credentials,
                    connection.configuration or {},
                    appointment.external_event_id,
                    request,
                )
            else:
                action = "create"
                result = await adapter.create_event(
                    credentials,
                    connection.configuration or {},
                    request,
                )
    except (ProviderRequestError, IntegrationLifecycleError) as exc:
        code = getattr(exc, "code", "provider_job_failed")
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=appointment,
            failure_code=code,
            retryable=bool(getattr(exc, "retryable", False)),
        )
        if code != "provider_calendar_conflict":
            connection.last_error_code = code
            connection.consecutive_failures += 1
            connection.status = (
                "expired"
                if code in {"provider_auth_rejected", "provider_token_expired"}
                else "degraded"
            )
        return
    except (KeyError, TypeError, ValueError):
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=appointment,
            failure_code="calendar_data_unavailable",
            retryable=False,
        )
        return
    except Exception:
        logger.exception(
            "Calendar reconciliation failed unexpectedly",
            extra={"integration_job_id": str(job.id)},
        )
        await _calendar_sync_failure(
            db,
            job=job,
            appointment=appointment,
            failure_code="integration_job_failed",
            retryable=False,
        )
        return

    # A public mutation may have committed while the provider call was in
    # flight. Keep a newly-created event ID so the latest queued version can
    # update/delete it, but only mark the exact current version as synced.
    try:
        await db.refresh(appointment)
    except InvalidRequestError:
        appointment = None
    if appointment is not None:
        if action == "delete":
            if appointment.version == requested_version:
                appointment.external_event_id = None
                appointment.external_meeting_url = None
        elif result is not None:
            appointment.external_event_id = result.event_id
            if result.meeting_url is not None:
                appointment.external_meeting_url = result.meeting_url
        if appointment.version == requested_version:
            appointment.sync_status = "synced"
    await succeed_job(
        db,
        job=job,
        result_summary={
            "reconciled": appointment is not None,
            "event_created": action == "create",
            "event_updated": action == "update",
            "event_deleted": action == "delete",
        },
    )
    await reconcile_sourcing_job(db, job=job)
    connection.status = "connected"
    connection.last_success_at = datetime.now(UTC)
    connection.last_error_code = None
    connection.consecutive_failures = 0


async def _appointment_calendar_request(
    db: AsyncSession,
    appointment: InterviewAppointment,
    job: IntegrationJob,
) -> CalendarEventRequest:
    contact = (
        await db.execute(
            select(CandidateContactPoint)
            .where(
                CandidateContactPoint.organization_id == appointment.organization_id,
                CandidateContactPoint.candidate_id == appointment.candidate_id,
                CandidateContactPoint.channel == "email",
                CandidateContactPoint.is_opted_out.is_(False),
            )
            .order_by(
                CandidateContactPoint.is_primary.desc(),
                CandidateContactPoint.created_at,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.id == appointment.candidate_id,
                Candidate.organization_id == appointment.organization_id,
            )
        )
    ).scalar_one_or_none()
    candidate_email = (
        contact.value
        if contact is not None
        else candidate.email_normalized
        if candidate is not None
        else None
    )
    if not candidate_email:
        raise ValueError("candidate email unavailable")
    participant_ids = list(
        (
            await db.execute(
                select(InterviewAppointmentInterviewer.user_id).where(
                    InterviewAppointmentInterviewer.organization_id
                    == appointment.organization_id,
                    InterviewAppointmentInterviewer.appointment_id == appointment.id,
                    InterviewAppointmentInterviewer.user_id.is_not(None),
                )
            )
        ).scalars()
    )
    if appointment.organizer_user_id is not None:
        participant_ids.append(appointment.organizer_user_id)
    user_emails = list(
        (
            await db.execute(
                select(User.email).where(
                    User.organization_id == appointment.organization_id,
                    User.id.in_(set(participant_ids)),
                    User.is_active.is_(True),
                )
            )
        ).scalars()
    )
    attendees = tuple(dict.fromkeys([candidate_email, *user_emails]))
    return CalendarEventRequest(
        title=f"Interview: {appointment.position_title_snapshot}",
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        timezone=appointment.candidate_timezone,
        attendees=attendees,
        location=(
            appointment.location_details
            if appointment.location_mode != "video"
            else None
        ),
        video_conference=appointment.location_mode == "video",
        idempotency_key=f"appointment:{appointment.id}:v{job.reference_payload['appointment_version']}",
    )


async def _execute(
    adapter: Any,
    *,
    connection: IntegrationConnection,
    credentials: dict[str, Any],
    operation: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    configuration = connection.configuration or {}
    if operation == "email.send":
        external_id = await adapter.send_email(
            credentials,
            EmailRequest(
                to=str(payload["to"]),
                subject=str(payload["subject"]),
                body_text=str(payload["body_text"]),
                from_email=payload.get("from_email"),
            ),
        )
        return {"provider_accepted": True, "external_ref": external_id}
    if operation == "calendar.create":
        event = await adapter.create_event(
            credentials,
            configuration,
            _calendar_request(payload, idempotency_key),
        )
        return {
            "provider_accepted": True,
            "external_ref": event.event_id,
            "meeting_created": event.meeting_url is not None,
        }
    if operation == "calendar.update":
        event = await adapter.update_event(
            credentials,
            configuration,
            str(payload["event_id"]),
            _calendar_request(payload, idempotency_key),
        )
        return {"provider_accepted": True, "external_ref": event.event_id}
    if operation == "calendar.delete":
        await adapter.delete_event(credentials, configuration, str(payload["event_id"]))
        return {"provider_accepted": True}
    if (
        operation in {"telegram.send", "vacancy.publish"}
        and connection.provider == "telegram"
    ):
        chat_id = str(payload.get("chat_id") or configuration.get("chat_id") or "")
        if not chat_id:
            raise ProviderRequestError("provider_configuration_missing")
        external_id = await adapter.send_message(
            credentials,
            chat_id=chat_id,
            text=str(payload["text"]),
            parse_mode=configuration.get("parse_mode"),
        )
        return {"provider_accepted": True, "external_ref": external_id}
    if operation == "vacancy.publish" and connection.provider == "hh_ru":
        published = await adapter.publish_vacancy(
            credentials,
            VacancyPublishRequest(payload=payload, idempotency_key=idempotency_key),
        )
        return {"provider_accepted": True, "external_ref": published.external_id}
    if operation == "candidate.import" and connection.provider == "hh_ru":
        raw = await adapter.list_negotiation_collections(
            credentials, vacancy_id=str(payload["vacancy_id"])
        )
        collections = raw.get("collections")
        return {
            "provider_accepted": True,
            "collection_count": len(collections)
            if isinstance(collections, list)
            else 0,
        }
    if operation == "health":
        await adapter.health(credentials, configuration)
        return {"provider_accepted": True}
    raise IntegrationJobError("operation_not_supported")


def _calendar_request(
    payload: dict[str, Any], idempotency_key: str
) -> CalendarEventRequest:
    try:
        starts_at = datetime.fromisoformat(str(payload["starts_at"]))
        ends_at = datetime.fromisoformat(str(payload["ends_at"]))
        attendees = tuple(str(value) for value in payload.get("attendees") or [])
        return CalendarEventRequest(
            title=str(payload["title"]),
            starts_at=starts_at,
            ends_at=ends_at,
            timezone=str(payload["timezone"]),
            attendees=attendees,
            description=payload.get("description"),
            location=payload.get("location"),
            video_conference=bool(payload.get("video_conference", False)),
            idempotency_key=idempotency_key,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrationJobError("job_payload_invalid") from exc
