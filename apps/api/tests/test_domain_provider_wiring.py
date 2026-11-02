"""Connected communication and calendar providers are durable and tenant safe."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text

from app.config import get_settings
from app.models.candidate import Candidate
from app.models.communication import CandidateContactPoint, CommunicationMessage
from app.models.enterprise import AuditLog
from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.scheduling import InterviewAppointment, InterviewReminder
from app.models.score import Score
from app.models.user import User
from app.routers.communications import (
    create_communication_account,
    send_candidate_communication,
)
from app.schemas.communications import (
    CandidateCommunicationSend,
    CommunicationAccountCreate,
)
from app.schemas.scheduling import AvailabilityInterval, SchedulingLinkCreate
from app.schemas.talent import TalentNurtureSequenceCreate
from app.schemas.workflows import WorkflowRuleCreate
from app.services.communication_worker import process_communication_messages_once
from app.services.communications import process_claimed_message
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.lifecycle import (
    create_connection,
    set_connection_credentials,
)
from app.services.integrations.providers import CalendarEventResult
from app.services.integrations.runtime import execute_provider_job
from app.services.private_object_retention import (
    RetentionStorageError,
    _stage_calendar_cleanup_for_erasure,
    prepare_retention_erasure,
)
from app.services.scheduling import (
    SchedulingError,
    appointment_manage_token,
    appointment_out,
    available_slots,
    book_appointment,
    cancel_appointment,
    create_scheduling_link,
    process_due_reminders,
    replace_user_availability,
    reschedule_appointment,
)
from app.storage import LocalBlobStore, private_object_key

pytestmark = pytest.mark.asyncio
TEST_CIPHER = IntegrationCipher(b"w" * 32, key_id="domain-provider-test")


class MessageAdapter:
    def __init__(self, outcomes: list[str | Exception] | None = None) -> None:
        self.outcomes = list(outcomes or ["provider-message-1"])
        self.calls: list[tuple[str, str]] = []

    def _next(self) -> str:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def send_email(self, _credentials, request) -> str:
        self.calls.append(("email", request.to))
        return self._next()

    async def send_message(
        self, _credentials, *, chat_id: str, text: str, parse_mode=None
    ) -> str:
        del text, parse_mode
        self.calls.append(("telegram", chat_id))
        return self._next()


class CalendarAdapter:
    def __init__(self, outcomes: list[str | Exception] | None = None) -> None:
        self.outcomes = list(outcomes or ["ok"])
        self.calls: list[tuple[str, str | None, str]] = []

    def _next(self) -> str:
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def create_event(self, _credentials, _configuration, request):
        self.calls.append(("create", None, request.timezone))
        self._next()
        return CalendarEventResult(
            event_id="calendar-event-1",
            meeting_url="https://meet.example.test/interview-1",
        )

    async def update_event(self, _credentials, _configuration, event_id: str, request):
        self.calls.append(("update", event_id, request.timezone))
        self._next()
        return CalendarEventResult(
            event_id=event_id,
            meeting_url="https://meet.example.test/interview-1",
        )

    async def delete_event(self, _credentials, _configuration, event_id: str):
        self.calls.append(("delete", event_id, ""))
        self._next()


async def _seed_people(db, suffix: str):
    organization = Organization(
        name=f"Provider domain {suffix}",
        slug=f"provider-domain-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"provider-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"provider-{suffix}",
        parsed_cv={"name": "Ada Candidate"},
    )
    db.add_all([user, candidate])
    await db.flush()
    return organization, user, candidate


async def _connected(
    db,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    provider: str,
) -> IntegrationConnection:
    connection, _secret = await create_connection(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        provider=provider,
        name=f"{provider}-{uuid4().hex[:8]}",
        configuration={"calendar_id": "primary"},
        cipher=TEST_CIPHER,
    )
    credentials = (
        {"bot_token": "123456:test-bot-token"}
        if provider == "telegram"
        else {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }
    )
    await set_connection_credentials(
        db,
        connection=connection,
        credentials=credentials,
        cipher=TEST_CIPHER,
    )
    connection.status = "connected"
    await db.flush()
    return connection


async def _add_contact(db, *, organization_id, user_id, candidate_id, channel):
    value = "candidate@example.test" if channel == "email" else "candidate_test"
    normalized = value if channel == "email" else value.casefold()
    contact = CandidateContactPoint(
        organization_id=organization_id,
        candidate_id=candidate_id,
        created_by_user_id=user_id,
        channel=channel,
        value=value,
        normalized_value=normalized,
        is_primary=True,
    )
    db.add(contact)
    await db.flush()
    return contact


@pytest.mark.parametrize(
    ("provider", "channel", "expected_target"),
    [
        ("google_workspace", "email", "candidate@example.test"),
        ("microsoft_365", "email", "candidate@example.test"),
        ("telegram", "telegram", "@candidate_test"),
    ],
)
async def test_connected_accounts_deliver_through_encrypted_runtime(
    db, provider, channel, expected_target
):
    organization, user, candidate = await _seed_people(db, provider)
    await _add_contact(
        db,
        organization_id=organization.id,
        user_id=user.id,
        candidate_id=candidate.id,
        channel=channel,
    )
    connection = await _connected(
        db,
        organization_id=organization.id,
        actor_user_id=user.id,
        provider=provider,
    )
    await db.commit()
    created = await create_communication_account(
        CommunicationAccountCreate(
            channel=channel,
            provider=provider,
            integration_connection_id=connection.id,
            name=f"Connected {provider}",
            configuration={"label": "discarded"},
        ),
        db=db,
        user=user,
    )
    assert created.webhook_secret is None
    assert created.account.connection_status == "connected"
    assert created.account.delivery_available is True
    assert created.account.configuration == {}

    queued = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel=channel,
            account_id=created.account.id,
            body="Provider delivery",
            idempotency_key=f"provider-delivery-{provider}",
        ),
        db=db,
        user=user,
    )
    adapter = MessageAdapter()
    counts = await process_communication_messages_once(
        worker_id=f"message-{provider}",
        limit=1,
        mode="off",
        cipher=TEST_CIPHER,
        adapters={provider: adapter},
    )
    assert counts == {
        "claimed": 1,
        "sent": 1,
        "retrying": 0,
        "failed": 0,
        "abandoned": 0,
    }
    db.expire_all()
    persisted = await db.get(CommunicationMessage, queued.id)
    assert persisted.status == "sent"
    assert persisted.provider_message_id == "provider-message-1"
    assert adapter.calls == [(channel, expected_target)]


async def test_account_binding_and_deferred_whatsapp_fail_closed(db):
    organization, user, candidate = await _seed_people(db, "binding")
    connection = await _connected(
        db,
        organization_id=organization.id,
        actor_user_id=user.id,
        provider="google_workspace",
    )
    other, other_user, _other_candidate = await _seed_people(db, "binding-other")
    other_connection = await _connected(
        db,
        organization_id=other.id,
        actor_user_id=other_user.id,
        provider="google_workspace",
    )
    await db.commit()

    with pytest.raises(HTTPException) as hidden:
        await create_communication_account(
            CommunicationAccountCreate(
                channel="email",
                provider="google_workspace",
                integration_connection_id=other_connection.id,
                name="Cross tenant",
            ),
            db=db,
            user=user,
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail["code"] == "connection_not_found"

    with pytest.raises(HTTPException) as mismatch:
        await create_communication_account(
            CommunicationAccountCreate(
                channel="email",
                provider="microsoft_365",
                integration_connection_id=connection.id,
                name="Wrong provider",
            ),
            db=db,
            user=user,
        )
    assert mismatch.value.detail["code"] == "connection_capability_mismatch"

    connection.status = "expired"
    await db.commit()
    with pytest.raises(HTTPException) as expired:
        await create_communication_account(
            CommunicationAccountCreate(
                channel="email",
                provider="google_workspace",
                integration_connection_id=connection.id,
                name="Expired provider",
            ),
            db=db,
            user=user,
        )
    assert expired.value.detail["code"] == "connection_capability_mismatch"

    with pytest.raises(ValidationError):
        CommunicationAccountCreate(channel="email", provider="smtp", name="Legacy SMTP")
    with pytest.raises(HTTPException) as whatsapp:
        await send_candidate_communication(
            candidate.id,
            CandidateCommunicationSend(
                channel="whatsapp",
                body="Unavailable",
                idempotency_key="whatsapp-disabled",
            ),
            db=db,
            user=user,
        )
    assert whatsapp.value.detail["code"] == "capability_unavailable"
    with pytest.raises(ValidationError, match="capability_unavailable"):
        TalentNurtureSequenceCreate(
            name="Legacy WhatsApp",
            steps=[
                {
                    "channel": "whatsapp",
                    "template_id": uuid4(),
                    "delay_days": 0,
                }
            ],
        )
    with pytest.raises(ValidationError, match="capability_unavailable"):
        WorkflowRuleCreate(
            name="Legacy WhatsApp",
            trigger="candidate_added",
            actions=[
                {
                    "type": "send_template",
                    "channel": "whatsapp",
                    "template_id": uuid4(),
                }
            ],
        )


async def test_message_retry_is_bounded_and_stale_claim_is_not_replayed(db):
    organization, user, candidate = await _seed_people(db, "retry")
    await _add_contact(
        db,
        organization_id=organization.id,
        user_id=user.id,
        candidate_id=candidate.id,
        channel="email",
    )
    connection = await _connected(
        db,
        organization_id=organization.id,
        actor_user_id=user.id,
        provider="google_workspace",
    )
    await db.commit()
    account = (
        await create_communication_account(
            CommunicationAccountCreate(
                channel="email",
                provider="google_workspace",
                integration_connection_id=connection.id,
                name="Retry provider",
            ),
            db=db,
            user=user,
        )
    ).account
    queued = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            account_id=account.id,
            body="Retry once",
            idempotency_key="retry-once",
        ),
        db=db,
        user=user,
    )
    adapter = MessageAdapter(
        [ProviderRequestError("provider_rate_limited", retryable=True), "accepted"]
    )
    first = await process_communication_messages_once(
        worker_id="retry-1",
        limit=1,
        mode="off",
        cipher=TEST_CIPHER,
        adapters={"google_workspace": adapter},
    )
    assert first["retrying"] == 1
    retry_row = await db.get(CommunicationMessage, queued.id)
    retry_row.scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    second = await process_communication_messages_once(
        worker_id="retry-2",
        limit=1,
        mode="off",
        cipher=TEST_CIPHER,
        adapters={"google_workspace": adapter},
    )
    assert second["sent"] == 1
    assert len(adapter.calls) == 2

    exhausted = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            account_id=account.id,
            body="Bounded retry",
            idempotency_key="retry-exhausted",
        ),
        db=db,
        user=user,
    )
    exhausted_adapter = MessageAdapter(
        [
            ProviderRequestError("provider_rate_limited", retryable=True),
            ProviderRequestError("provider_rate_limited", retryable=True),
            ProviderRequestError("provider_rate_limited", retryable=True),
        ]
    )
    for attempt in range(3):
        cycle = await process_communication_messages_once(
            worker_id=f"retry-exhausted-{attempt}",
            limit=1,
            mode="off",
            cipher=TEST_CIPHER,
            adapters={"google_workspace": exhausted_adapter},
        )
        if attempt < 2:
            assert cycle["retrying"] == 1
            exhausted_row = await db.get(CommunicationMessage, exhausted.id)
            exhausted_row.scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        else:
            assert cycle["failed"] == 1
    exhausted_row = await db.get(CommunicationMessage, exhausted.id)
    await db.refresh(exhausted_row)
    assert exhausted_row.status == "failed"
    assert exhausted_row.attempt_count == 3
    assert exhausted_row.failure_code == "provider_rate_limited"
    assert len(exhausted_adapter.calls) == 3
    await db.refresh(connection)
    assert connection.status == "degraded"
    connection.status = "connected"
    connection.last_error_code = None
    connection.consecutive_failures = 0
    await db.commit()

    ambiguous = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            account_id=account.id,
            body="Do not replay after crash",
            idempotency_key="ambiguous-crash",
        ),
        db=db,
        user=user,
    )
    ambiguous_row = await db.get(CommunicationMessage, ambiguous.id)
    ambiguous_row.status = "processing"
    ambiguous_row.attempt_count = 1
    ambiguous_row.claimed_at = datetime.now(UTC) - timedelta(minutes=6)
    await db.commit()
    never_called = MessageAdapter()
    recovered = await process_communication_messages_once(
        worker_id="stale-recovery",
        limit=1,
        mode="off",
        cipher=TEST_CIPHER,
        adapters={"google_workspace": never_called},
    )
    assert recovered["failed"] == 1
    db.expire_all()
    ambiguous_row = await db.get(CommunicationMessage, ambiguous.id)
    assert ambiguous_row.failure_code == "delivery_outcome_unknown"
    assert never_called.calls == []


async def test_forced_claim_preserves_only_authorized_retry_marker(db):
    function_installed = await db.scalar(
        text(
            "SELECT to_regprocedure("
            "'security.claim_communication_messages(text,integer,timestamptz)'"
            ") IS NOT NULL"
        )
    )
    if not function_installed:
        pytest.skip("migration-backed forced-claim function is not installed")
    organization, user, candidate = await _seed_people(db, "claim-marker")
    await _add_contact(
        db,
        organization_id=organization.id,
        user_id=user.id,
        candidate_id=candidate.id,
        channel="email",
    )
    queued = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            body="Claim marker",
            idempotency_key="claim-marker",
        ),
        db=db,
        user=user,
    )
    row = await db.get(CommunicationMessage, queued.id)
    row.failure_code = "provider_rate_limited"
    row.attempt_count = 1
    row.scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    await db.execute(
        text("SELECT * FROM security.claim_communication_messages(:worker, 1, :now)"),
        {"worker": "claim-marker-1", "now": datetime.now(UTC)},
    )
    await db.commit()
    db.expire_all()
    row = await db.get(CommunicationMessage, queued.id)
    assert (row.status, row.attempt_count, row.failure_code) == (
        "processing",
        2,
        "provider_rate_limited",
    )

    await db.execute(
        text(
            "DELETE FROM worker_queue_leases "
            "WHERE queue_name = 'communication_messages' AND work_id = :work_id"
        ),
        {"work_id": queued.id},
    )
    row.claimed_at = datetime.now(UTC) - timedelta(minutes=6)
    await db.commit()
    await db.execute(
        text("SELECT * FROM security.claim_communication_messages(:worker, 1, :now)"),
        {"worker": "claim-marker-2", "now": datetime.now(UTC)},
    )
    await db.commit()
    db.expire_all()
    row = await db.get(CommunicationMessage, queued.id)
    assert row.failure_code is None


async def _seed_scheduling(
    db, suffix: str, *, reminder_offsets: list[int] | None = None
):
    organization, recruiter, candidate = await _seed_people(db, suffix)
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {suffix}",
        status="open",
    )
    db.add(position)
    await db.flush()
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            overall_score=80,
            stage="interview",
        )
    )
    await _add_contact(
        db,
        organization_id=organization.id,
        user_id=recruiter.id,
        candidate_id=candidate.id,
        channel="email",
    )
    await replace_user_availability(
        db,
        organization_id=organization.id,
        user_id=recruiter.id,
        timezone="UTC",
        intervals=[
            AvailabilityInterval(
                weekday=weekday,
                start_time=time(0, 0),
                end_time=time(23, 59),
            )
            for weekday in range(7)
        ],
    )
    connection = await _connected(
        db,
        organization_id=organization.id,
        actor_user_id=recruiter.id,
        provider="google_workspace",
    )
    await db.commit()
    issue = await create_scheduling_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        actor_user_id=recruiter.id,
        body=SchedulingLinkCreate(
            position_id=position.id,
            interviewer_user_ids=[recruiter.id],
            calendar_connection_id=connection.id,
            min_notice_minutes=0,
            booking_window_days=14,
            expires_in_days=14,
            reminder_offsets=reminder_offsets or [],
        ),
    )
    await db.commit()
    link = issue.link
    link._test_public_token = issue.public_url.rsplit("/", 1)[-1]
    return organization, recruiter, candidate, position, connection, link


async def _book_connected(db, link, *, key: str, now: datetime):
    slots = await available_slots(
        db,
        link=link,
        timezone="Europe/Berlin",
        from_date=now.date(),
        days=4,
        now=now,
    )
    appointment, _url = await book_appointment(
        db,
        token=link._test_public_token,
        starts_at=slots[0].starts_at,
        timezone="Europe/Berlin",
        idempotency_key=key,
        now=now,
    )
    await db.commit()
    return appointment, slots


async def _calendar_job(db, appointment_id: UUID, version: int) -> IntegrationJob:
    return (
        await db.execute(
            select(IntegrationJob).where(
                IntegrationJob.operation == "calendar.sync",
                IntegrationJob.reference_payload["appointment_id"].as_string()
                == str(appointment_id),
                IntegrationJob.reference_payload["appointment_version"].as_integer()
                == version,
            )
        )
    ).scalar_one()


async def _run_calendar_job(db, job, adapter, *, attempt: int | None = None):
    job.status = "running"
    job.attempt_count = attempt or (job.attempt_count + 1)
    await db.flush()
    await execute_provider_job(
        db,
        job=job,
        cipher=TEST_CIPHER,
        adapters={"google_workspace": adapter},
        prepared_credentials={"access_token": "prepared"},
    )
    await db.commit()


async def test_calendar_create_update_delete_and_latest_version_reconciliation(db):
    (
        _organization,
        _recruiter,
        _candidate,
        _position,
        _connection,
        link,
    ) = await _seed_scheduling(db, "calendar-flow")
    now = datetime.now(UTC)
    appointment, _slots = await _book_connected(db, link, key="calendar-flow", now=now)
    first_job = await _calendar_job(db, appointment.id, 1)
    assert first_job.reference_payload == {
        "appointment_id": str(appointment.id),
        "appointment_version": 1,
    }
    assert first_job.payload_ciphertext is None
    appointment_id = appointment.id
    adapter = CalendarAdapter(["create", "update", "delete"])
    await _run_calendar_job(db, first_job, adapter)
    appointment = await db.get(InterviewAppointment, appointment_id)
    await db.refresh(appointment)
    assert appointment.sync_status == "synced"
    assert appointment.external_event_id == "calendar-event-1"
    assert appointment.external_meeting_url.endswith("interview-1")

    later = (
        await available_slots(
            db,
            link=link,
            timezone="Europe/Berlin",
            from_date=now.date(),
            days=4,
            now=now,
            exclude_appointment_id=appointment.id,
        )
    )[1]
    appointment = await reschedule_appointment(
        db,
        manage_token=appointment_manage_token(appointment.id),
        starts_at=later.starts_at,
        timezone="Europe/Berlin",
        expected_version=1,
        now=now,
    )
    await db.commit()
    await _run_calendar_job(db, await _calendar_job(db, appointment.id, 2), adapter)
    appointment = await cancel_appointment(
        db,
        manage_token=appointment_manage_token(appointment.id),
        expected_version=2,
        now=now,
    )
    await db.commit()
    await _run_calendar_job(db, await _calendar_job(db, appointment.id, 3), adapter)
    await db.refresh(appointment)
    assert appointment.sync_status == "synced"
    assert appointment.external_event_id is None
    assert appointment.external_meeting_url is None
    assert [item[0] for item in adapter.calls] == ["create", "update", "delete"]
    assert adapter.calls[0][2] == adapter.calls[1][2] == "Europe/Berlin"
    summaries = list(
        (
            await db.execute(
                select(IntegrationJob.result_summary).where(
                    IntegrationJob.operation == "calendar.sync"
                )
            )
        ).scalars()
    )
    serialized = str(summaries).lower()
    assert "candidate@example.test" not in serialized
    assert "calendar-event-1" not in serialized


async def test_calendar_pending_changes_collapse_and_outage_recovers(db):
    (
        _organization,
        _recruiter,
        _candidate,
        _position,
        connection,
        link,
    ) = await _seed_scheduling(db, "calendar-race")
    now = datetime.now(UTC)
    appointment, slots = await _book_connected(db, link, key="calendar-race", now=now)
    first_job = await _calendar_job(db, appointment.id, 1)
    appointment = await reschedule_appointment(
        db,
        manage_token=appointment_manage_token(appointment.id),
        starts_at=slots[1].starts_at,
        timezone="Europe/Berlin",
        expected_version=1,
        now=now,
    )
    await db.commit()
    stale_adapter = CalendarAdapter()
    await _run_calendar_job(db, first_job, stale_adapter)
    assert stale_adapter.calls == []
    second_job = await _calendar_job(db, appointment.id, 2)
    outage = CalendarAdapter(
        [ProviderRequestError("provider_unavailable", retryable=True), "ok"]
    )
    await _run_calendar_job(db, second_job, outage, attempt=1)
    assert second_job.status == "retrying"
    assert appointment.sync_status == "pending"
    assert connection.status == "degraded"
    await _run_calendar_job(db, second_job, outage, attempt=2)
    await db.refresh(appointment)
    assert appointment.sync_status == "synced"
    assert appointment.external_event_id == "calendar-event-1"
    assert [item[0] for item in outage.calls] == ["create", "create"]

    appointment = await cancel_appointment(
        db,
        manage_token=appointment_manage_token(appointment.id),
        expected_version=2,
        now=now,
    )
    connection.status = "revoked"
    await db.commit()
    cancelled_job = await _calendar_job(db, appointment.id, 3)
    await _run_calendar_job(db, cancelled_job, CalendarAdapter(), attempt=1)
    assert cancelled_job.status == "dead_letter"
    assert cancelled_job.failure_code == "connection_not_ready"
    assert appointment.sync_status == "failed"


async def test_provider_busy_window_blocks_calendar_create_without_degrading_connection(
    db,
):
    (
        _organization,
        _recruiter,
        _candidate,
        _position,
        connection,
        link,
    ) = await _seed_scheduling(db, "calendar-busy")
    now = datetime.now(UTC)
    appointment, _slots = await _book_connected(
        db, link, key="calendar-busy", now=now
    )
    job = await _calendar_job(db, appointment.id, 1)

    class BusyCalendarAdapter(CalendarAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.windows = []

        async def has_calendar_conflict(
            self,
            _credentials,
            _configuration,
            *,
            starts_at,
            ends_at,
            exclude_event_id,
        ):
            self.windows.append((starts_at, ends_at, exclude_event_id))
            return True

    adapter = BusyCalendarAdapter()
    await _run_calendar_job(db, job, adapter)
    await db.refresh(appointment)
    await db.refresh(connection)
    assert job.status == "dead_letter"
    assert job.failure_code == "provider_calendar_conflict"
    assert appointment.sync_status == "failed"
    assert appointment.external_event_id is None
    assert connection.status == "connected"
    assert connection.consecutive_failures == 0
    assert adapter.calls == []
    assert adapter.windows == [
        (appointment.starts_at, appointment.ends_at, None)
    ]


async def test_connected_candidate_reminders_reuse_durable_message_retry_and_dlq(db):
    (
        organization,
        recruiter,
        _candidate,
        _position,
        connection,
        link,
    ) = await _seed_scheduling(
        db,
        "reminder-delivery",
        reminder_offsets=[1, 0],
    )
    account = await create_communication_account(
        CommunicationAccountCreate(
            channel="email",
            provider="google_workspace",
            integration_connection_id=connection.id,
            name="Reminder delivery",
        ),
        db=db,
        user=recruiter,
    )
    assert account.account.delivery_available is True
    now = datetime.now(UTC)
    appointment, _slots = await _book_connected(
        db, link, key="reminder-delivery", now=now
    )

    await process_due_reminders(db, now=appointment.starts_at)
    await db.commit()
    candidate_reminders = list(
        (
            await db.execute(
                select(InterviewReminder)
                .where(
                    InterviewReminder.organization_id == organization.id,
                    InterviewReminder.appointment_id == appointment.id,
                    InterviewReminder.audience == "candidate",
                )
                .order_by(InterviewReminder.offset_minutes.desc())
            )
        ).scalars()
    )
    assert len(candidate_reminders) == 2
    assert all(reminder.status == "pending" for reminder in candidate_reminders)
    assert all(reminder.channel == "email" for reminder in candidate_reminders)
    assert all(
        reminder.communication_message_id is not None
        for reminder in candidate_reminders
    )
    message_ids = {
        reminder.communication_message_id for reminder in candidate_reminders
    }
    assert len(message_ids) == 2

    # Reconciliation can be replayed while messages are queued without
    # creating a second external effect.
    await process_due_reminders(
        db,
        now=appointment.starts_at,
        reminder_ids=[reminder.id for reminder in candidate_reminders],
    )
    assert (
        await db.scalar(
            select(func.count(CommunicationMessage.id)).where(
                CommunicationMessage.id.in_(message_ids)
            )
        )
        == 2
    )

    async def deliver_three_times(message, adapter):
        outcomes = []
        for _attempt in range(3):
            message.scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
            await db.flush()
            outcomes.append(
                await process_claimed_message(
                    db,
                    organization_id=organization.id,
                    message_id=message.id,
                    now=datetime.now(UTC),
                    cipher=TEST_CIPHER,
                    adapters={"google_workspace": adapter},
                )
            )
            await db.commit()
            await db.refresh(message)
        return outcomes

    recovered_message = await db.get(
        CommunicationMessage, candidate_reminders[0].communication_message_id
    )
    failed_message = await db.get(
        CommunicationMessage, candidate_reminders[1].communication_message_id
    )
    assert recovered_message is not None and failed_message is not None
    rate_limit = ProviderRequestError("provider_rate_limited", retryable=True)
    recovered = await deliver_three_times(
        recovered_message,
        MessageAdapter(
            [
                rate_limit,
                ProviderRequestError("provider_rate_limited", retryable=True),
                "reminder-provider-message",
            ]
        ),
    )
    assert recovered == ["retrying", "retrying", "sent"]
    exhausted = await deliver_three_times(
        failed_message,
        MessageAdapter(
            [
                ProviderRequestError("provider_rate_limited", retryable=True),
                ProviderRequestError("provider_rate_limited", retryable=True),
                ProviderRequestError("provider_rate_limited", retryable=True),
            ]
        ),
    )
    assert exhausted == ["retrying", "retrying", "failed"]

    await process_due_reminders(
        db,
        now=appointment.starts_at + timedelta(minutes=1),
        reminder_ids=[reminder.id for reminder in candidate_reminders],
    )
    await db.commit()
    view = await appointment_out(db, appointment)
    candidate_delivery = [
        reminder for reminder in view.reminders if reminder.audience == "candidate"
    ]
    assert [reminder.status for reminder in candidate_delivery] == ["sent", "failed"]
    assert [reminder.delivery_status for reminder in candidate_delivery] == [
        "sent",
        "failed",
    ]
    assert [reminder.delivery_attempts for reminder in candidate_delivery] == [3, 3]
    assert candidate_delivery[0].failure_code is None
    assert candidate_delivery[1].failure_code == "provider_rate_limited"


async def test_calendar_connection_is_tenant_scoped_and_requires_candidate_email(db):
    organization, recruiter, candidate = await _seed_people(db, "calendar-scope")
    position = Position(
        organization_id=organization.id,
        title="Scoped position",
        status="open",
    )
    db.add(position)
    await db.flush()
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            overall_score=70,
            stage="interview",
        )
    )
    await replace_user_availability(
        db,
        organization_id=organization.id,
        user_id=recruiter.id,
        timezone="UTC",
        intervals=[
            AvailabilityInterval(
                weekday=weekday,
                start_time=time(9),
                end_time=time(17),
            )
            for weekday in range(7)
        ],
    )
    other, other_user, _ = await _seed_people(db, "calendar-scope-other")
    other_connection = await _connected(
        db,
        organization_id=other.id,
        actor_user_id=other_user.id,
        provider="google_workspace",
    )
    own_connection = await _connected(
        db,
        organization_id=organization.id,
        actor_user_id=recruiter.id,
        provider="google_workspace",
    )
    await db.commit()
    common = {
        "position_id": position.id,
        "interviewer_user_ids": [recruiter.id],
        "min_notice_minutes": 0,
        "reminder_offsets": [],
    }
    with pytest.raises(SchedulingError) as hidden:
        await create_scheduling_link(
            db,
            organization_id=organization.id,
            candidate_id=candidate.id,
            actor_user_id=recruiter.id,
            body=SchedulingLinkCreate(
                **common, calendar_connection_id=other_connection.id
            ),
        )
    assert hidden.value.code == "calendar_connection_not_found"
    with pytest.raises(SchedulingError) as missing_email:
        await create_scheduling_link(
            db,
            organization_id=organization.id,
            candidate_id=candidate.id,
            actor_user_id=recruiter.id,
            body=SchedulingLinkCreate(
                **common, calendar_connection_id=own_connection.id
            ),
        )
    assert missing_email.value.code == "candidate_email_required"


async def test_candidate_erasure_keeps_encrypted_provider_delete_after_cascade(
    db, monkeypatch
):
    (
        organization,
        _recruiter,
        candidate,
        _position,
        connection,
        link,
    ) = await _seed_scheduling(db, "calendar-erasure")
    now = datetime.now(UTC)
    appointment, _slots = await _book_connected(
        db, link, key="calendar-erasure", now=now
    )
    appointment.external_event_id = "private-provider-event-id"
    appointment.external_meeting_url = "https://meet.example.test/private"
    appointment.sync_status = "synced"
    await db.commit()
    existing_job = await _calendar_job(db, appointment.id, 1)

    encoded_key = base64.urlsafe_b64encode(b"w" * 32).decode().rstrip("=")
    settings = get_settings()
    monkeypatch.setattr(settings, "integration_credential_encryption_key", encoded_key)
    monkeypatch.setattr(settings, "integration_credential_key_id", TEST_CIPHER.key_id)
    await _stage_calendar_cleanup_for_erasure(
        db,
        organization_id=organization.id,
        candidate_ids={candidate.id},
    )
    await db.flush()
    assert existing_job.status == "cancelled"
    assert existing_job.reference_payload["kind"] == "appointment_erased"
    assert str(appointment.id) not in str(existing_job.reference_payload)
    delete_job = (
        await db.execute(
            select(IntegrationJob).where(
                IntegrationJob.operation == "calendar.delete",
                IntegrationJob.connection_id == connection.id,
            )
        )
    ).scalar_one()
    assert delete_job.payload_ciphertext is not None
    assert b"private-provider-event-id" not in delete_job.payload_ciphertext
    assert "private-provider-event-id" not in str(delete_job.reference_payload)
    encrypted_payload_parts = (
        delete_job.payload_ciphertext,
        delete_job.payload_nonce,
        delete_job.payload_key_id,
    )

    candidate_id = candidate.id
    appointment_id = appointment.id
    await db.delete(candidate)
    await db.commit()
    assert await db.get(Candidate, candidate_id) is None
    db.expire_all()
    assert await db.get(InterviewAppointment, appointment_id) is None
    adapter = CalendarAdapter(
        [ProviderRequestError("provider_unavailable", retryable=True), "delete"]
    )
    await _run_calendar_job(db, delete_job, adapter, attempt=1)
    assert delete_job.status == "retrying"
    assert (
        delete_job.payload_ciphertext,
        delete_job.payload_nonce,
        delete_job.payload_key_id,
    ) == encrypted_payload_parts
    await _run_calendar_job(db, delete_job, adapter, attempt=2)
    assert delete_job.status == "succeeded"
    assert delete_job.payload_ciphertext is None
    assert delete_job.payload_nonce is None
    assert delete_job.payload_key_id is None
    assert adapter.calls == [
        ("delete", "private-provider-event-id", ""),
        ("delete", "private-provider-event-id", ""),
    ]


async def test_erasure_fails_closed_for_ambiguous_inflight_calendar_create(db):
    (
        organization,
        _recruiter,
        candidate,
        _position,
        connection,
        link,
    ) = await _seed_scheduling(db, "calendar-erasure-ambiguous")
    appointment, _slots = await _book_connected(
        db,
        link,
        key="calendar-erasure-ambiguous",
        now=datetime.now(UTC),
    )
    job = await _calendar_job(db, appointment.id, 1)
    job.status = "running"
    job.attempt_count = 1
    await db.commit()
    candidate_id = candidate.id
    organization_id = organization.id
    connection_id = connection.id
    with pytest.raises(RetentionStorageError, match="outcome is unknown"):
        await _stage_calendar_cleanup_for_erasure(
            db,
            organization_id=organization_id,
            candidate_ids={candidate_id},
        )
    # The retention router rolls the entire transaction back.  Prove the
    # provider operation and candidate remain unchanged rather than accepting
    # an unprovable remote orphan.
    await db.rollback()
    await db.refresh(job)
    assert job.status == "running"
    assert await db.get(Candidate, candidate_id) is not None
    assert (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == "candidate_erasure.calendar_cleanup_failed",
                AuditLog.resource_id == connection_id,
            )
        )
    ).scalar_one_or_none() is None


async def test_erasure_fails_closed_for_terminal_failed_calendar_delete(
    db, monkeypatch
):
    (
        organization,
        _recruiter,
        candidate,
        _position,
        connection,
        link,
    ) = await _seed_scheduling(db, "calendar-erasure-dead-letter")
    appointment, _slots = await _book_connected(
        db,
        link,
        key="calendar-erasure-dead-letter",
        now=datetime.now(UTC),
    )
    appointment.external_event_id = "provider-event-that-must-be-deleted"
    appointment.sync_status = "synced"
    await db.commit()
    encoded_key = base64.urlsafe_b64encode(b"w" * 32).decode().rstrip("=")
    settings = get_settings()
    monkeypatch.setattr(settings, "integration_credential_encryption_key", encoded_key)
    monkeypatch.setattr(settings, "integration_credential_key_id", TEST_CIPHER.key_id)

    await _stage_calendar_cleanup_for_erasure(
        db,
        organization_id=organization.id,
        candidate_ids={candidate.id},
    )
    delete_job = (
        await db.execute(
            select(IntegrationJob).where(
                IntegrationJob.organization_id == organization.id,
                IntegrationJob.connection_id == connection.id,
                IntegrationJob.operation == "calendar.delete",
            )
        )
    ).scalar_one()
    delete_job.status = "dead_letter"
    delete_job.completed_at = datetime.now(UTC)
    await db.commit()

    with pytest.raises(RetentionStorageError, match="not runnable"):
        await _stage_calendar_cleanup_for_erasure(
            db,
            organization_id=organization.id,
            candidate_ids={candidate.id},
        )


async def test_legacy_erasure_adoption_bypasses_quota_only_as_delete_pending(
    db, tmp_path
):
    organization, _user, candidate = await _seed_people(db, "quota-erasure")
    old = datetime.now(UTC) - timedelta(days=60)
    candidate.deleted_at = old
    key = private_object_key(
        organization.id,
        "candidates",
        uuid4(),
        ".pdf",
    )
    candidate.pdf_path = key
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="storage_bytes",
            limit_value=0,
        )
    )
    await db.commit()

    source = tmp_path / "legacy.pdf"
    source.write_bytes(b"%PDF-1.4\nlegacy object already in private storage\n")
    store = LocalBlobStore(tmp_path / "objects")
    await store.put_file(key, source, content_type="application/pdf")
    plan = await prepare_retention_erasure(
        db,
        organization_id=organization.id,
        deleted_cutoff=datetime.now(UTC) - timedelta(days=30),
        active_cutoff=datetime.now(UTC) - timedelta(days=365),
        include_active_candidates=False,
        store=store,
    )
    await db.flush()
    assert plan.candidate_ids == frozenset({candidate.id})
    assert plan.adopted_legacy_objects == 1
    record = (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.organization_id == organization.id,
                PrivateObject.object_key == key,
            )
        )
    ).scalar_one()
    assert record.state == "delete_pending"
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id == record.id
            )
        )
    ).scalar_one()
    assert deletion.reason == "candidate_erasure"
    usage = (
        await db.execute(
            select(UsageLedgerEntry).where(
                UsageLedgerEntry.resource_id == record.id,
                UsageLedgerEntry.metric == "storage_bytes",
            )
        )
    ).scalar_one()
    assert usage.metadata_json == {
        "category": "candidates",
        "purpose": "legacy_erasure_adoption",
    }
