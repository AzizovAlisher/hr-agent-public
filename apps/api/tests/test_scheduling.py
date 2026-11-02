import asyncio
import hashlib
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.auth.jwt import encode_access_token
from app.db import SessionLocal
from app.main import app
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.integration import IntegrationConnection
from app.models.interview import Interview, InterviewAssignment
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.scheduling import (
    InterviewAppointment,
    InterviewReminder,
    SchedulingLink,
)
from app.models.score import Score
from app.models.user import User
from app.routers.scheduling import (
    list_calendar_providers,
    list_candidate_scheduling_links,
)
from app.schemas.scheduling import (
    AvailabilityInterval,
    SchedulingLinkCreate,
)
from app.services.applications import ensure_application_link
from app.services.candidate_merge import merge_candidates
from app.services.scheduling import (
    SchedulingError,
    appointment_manage_token,
    available_slots,
    book_appointment,
    cancel_appointment,
    create_scheduling_link,
    get_appointment_by_manage_token,
    get_link_by_token,
    process_due_reminders,
    public_appointment_out,
    replace_user_availability,
    reschedule_appointment,
    revoke_appointment_manage_token,
    rotate_appointment_manage_token,
    rotate_scheduling_link,
)
from app.services.scheduling_providers import (
    CalendarProviderUnavailable,
    create_external_event,
    provider_statuses,
)

pytestmark = pytest.mark.asyncio


async def seed_application(db, suffix: str = "schedule"):
    organization = Organization(
        name=f"Org {suffix}",
        slug=f"org-{suffix}-{uuid4().hex[:8]}",
        accent_color="#3366FF",
    )
    db.add(organization)
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"recruiter-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{suffix}",
        raw_cv="A sufficiently detailed CV for scheduling integration tests. " * 2,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Engineer {suffix}",
        status="open",
    )
    db.add_all([recruiter, candidate, position])
    await db.flush()
    score = Score(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=82,
        stage="interview",
    )
    db.add(score)
    await db.flush()
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
    await db.commit()
    return organization, recruiter, candidate, position


async def create_link(db, organization, recruiter, candidate, position, **changes):
    values = {
        "position_id": position.id,
        "interviewer_user_ids": [recruiter.id],
        "duration_minutes": 60,
        "slot_interval_minutes": 30,
        "min_notice_minutes": 0,
        "booking_window_days": 14,
        "expires_in_days": 14,
        "reminder_offsets": [60],
    }
    values.update(changes)
    issue = await create_scheduling_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        actor_user_id=recruiter.id,
        body=SchedulingLinkCreate(**values),
    )
    await db.commit()
    return issue


def issued_token(issue) -> str:
    return issue.public_url.rsplit("/", 1)[-1]


def auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_local_provider_is_explicit_and_external_providers_are_safe_stubs(db):
    statuses = provider_statuses()
    assert [(item.provider, item.available) for item in statuses] == [
        ("local", True),
    ]
    assert await create_external_event("local") is None
    with pytest.raises(CalendarProviderUnavailable):
        await create_external_event("google")


async def test_availability_is_deterministic_across_dst_gap_and_fold(db):
    organization, recruiter, candidate, position = await seed_application(
        db, "dst-boundaries"
    )
    await replace_user_availability(
        db,
        organization_id=organization.id,
        user_id=recruiter.id,
        timezone="Europe/Berlin",
        intervals=[
            AvailabilityInterval(
                weekday=6,
                start_time=time(1, 30),
                end_time=time(3, 30),
            )
        ],
    )
    issue = await create_link(
        db,
        organization,
        recruiter,
        candidate,
        position,
        duration_minutes=30,
        slot_interval_minutes=30,
        min_notice_minutes=0,
        booking_window_days=90,
        expires_in_days=90,
    )
    zone = ZoneInfo("Europe/Berlin")

    spring = await available_slots(
        db,
        link=issue.link,
        timezone="Europe/Berlin",
        from_date=date(2026, 3, 29),
        days=1,
        now=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
    )
    spring_starts = [item.starts_at.astimezone(zone) for item in spring]
    assert spring_starts
    assert all(item.date() == date(2026, 3, 29) for item in spring_starts)
    assert not any(item.hour == 2 for item in spring_starts)
    assert len({item.astimezone(UTC) for item in spring_starts}) == len(spring_starts)

    autumn = await available_slots(
        db,
        link=issue.link,
        timezone="Europe/Berlin",
        from_date=date(2026, 10, 25),
        days=1,
        now=datetime(2026, 10, 24, 0, 0, tzinfo=UTC),
    )
    autumn_starts = [item.starts_at.astimezone(zone) for item in autumn]
    ambiguous_two_oclock = [item for item in autumn_starts if item.hour == 2]
    assert {item.fold for item in ambiguous_two_oclock} == {0, 1}
    assert len({item.astimezone(UTC) for item in autumn_starts}) == len(autumn_starts)


async def test_calendar_provider_status_uses_only_current_tenant_connections(db):
    organization, recruiter, _candidate, _position = await seed_application(
        db, "provider-state"
    )
    other = Organization(
        name="Other provider tenant",
        slug=f"other-provider-{uuid4().hex[:8]}",
    )
    db.add(other)
    await db.flush()
    db.add_all(
        [
            IntegrationConnection(
                organization_id=organization.id,
                created_by_user_id=recruiter.id,
                provider="google_workspace",
                name="Recruiting Google",
                status="connected",
                capabilities=["calendar.write"],
            ),
            IntegrationConnection(
                organization_id=organization.id,
                created_by_user_id=recruiter.id,
                provider="microsoft_365",
                name="Recruiting Microsoft",
                status="expired",
                capabilities=["calendar.write"],
            ),
            IntegrationConnection(
                organization_id=other.id,
                provider="microsoft_365",
                name="Other Microsoft",
                status="connected",
                capabilities=["calendar.write"],
            ),
        ]
    )
    await db.commit()

    statuses = await list_calendar_providers(db=db, user=recruiter)
    assert [(item.provider, item.available, item.reason) for item in statuses.items] == [
        ("local", True, None),
        ("google", True, None),
        ("outlook", False, "expired"),
    ]


async def test_public_http_contract_books_without_leaking_internal_ids(db):
    organization, recruiter, candidate, position = await seed_application(db, "http")
    issue = await create_link(db, organization, recruiter, candidate, position)
    token = issued_token(issue)
    with pytest.raises(SchedulingError) as expired:
        await get_link_by_token(
            db,
            token,
            now=issue.link.expires_at + timedelta(seconds=1),
        )
    assert expired.value.code == "link_expired"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.example.test",
    ) as client:
        details = await client.get(f"/public/scheduling/{token}")
        assert details.status_code == 200
        assert details.json()["position_title"] == position.title
        assert details.headers["cache-control"] == "private, no-store"
        assert details.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in details.headers["x-robots-tag"]

        slots = await client.get(
            f"/public/scheduling/{token}/slots",
            params={
                "timezone": "UTC",
                "from_date": datetime.now(UTC).date().isoformat(),
                "days": 3,
            },
        )
        assert slots.status_code == 200
        assert slots.headers["cache-control"] == "private, no-store"
        selected = slots.json()["items"][0]["starts_at"]
        booked = await client.post(
            f"/public/scheduling/{token}/book",
            json={
                "starts_at": selected,
                "timezone": "UTC",
                "idempotency_key": "public-http-booking",
            },
        )
        assert booked.status_code == 201
        assert booked.headers["cache-control"] == "private, no-store"
        payload = booked.json()
        assert payload["manage_url"].startswith("/public/scheduling/manage/")
        assert payload["appointment"]["position_title"] == position.title
        assert "candidate_id" not in payload["appointment"]
        assert "interviewer_user_ids" not in payload["appointment"]
        assert "organizer_user_id" not in payload["appointment"]


async def test_scheduling_link_capability_is_one_time_rotatable_and_permission_scoped(
    db,
):
    organization, recruiter, candidate, position = await seed_application(
        db, "link-capability"
    )
    interviewer = User(
        organization_id=organization.id,
        email=f"interviewer-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    unassigned_manager = User(
        organization_id=organization.id,
        email=f"manager-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="hiring_manager",
        locale="en",
    )
    other_organization = Organization(
        name="Other scheduling tenant",
        slug=f"other-scheduling-{uuid4().hex[:8]}",
    )
    db.add_all([interviewer, unassigned_manager, other_organization])
    await db.flush()
    outsider = User(
        organization_id=other_organization.id,
        email=f"outsider-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    interview_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=candidate.source,
        owner_user_id=None,
        actor_user_id=recruiter.id,
        actor_kind="human",
        reason="scheduling_permission_test_setup",
    )
    interview = Interview(
        organization_id=organization.id,
        application_id=interview_link.application.id,
        candidate_id=candidate.id,
        position_id=position.id,
        created_by=recruiter.id,
    )
    db.add_all([outsider, interview])
    await db.flush()
    db.add(
        InterviewAssignment(
            organization_id=organization.id,
            interview_id=interview.id,
            interviewer_user_id=interviewer.id,
            assigned_by_user_id=recruiter.id,
        )
    )
    await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://api.example.test",
    ) as client:
        created = await client.post(
            f"/candidates/{candidate.id}/scheduling-links",
            headers=auth(recruiter),
            json={
                "position_id": str(position.id),
                "interview_id": str(interview.id),
                "interviewer_user_ids": [str(recruiter.id)],
                "min_notice_minutes": 0,
            },
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "private, no-store"
        created_body = created.json()
        original_url = created_body["public_url"]
        assert original_url.startswith("/public/scheduling/")
        original_token = original_url.rsplit("/", 1)[-1]
        assert len(original_token) >= 43

        link_id = created_body["id"]
        stored = await db.get(SchedulingLink, UUID(link_id))
        assert stored is not None
        assert (
            stored.token_digest == hashlib.sha256(original_token.encode()).hexdigest()
        )
        assert original_token not in stored.token_digest

        recruiter_list = await client.get(
            f"/candidates/{candidate.id}/scheduling-links",
            headers=auth(recruiter),
        )
        interviewer_list = await client.get(
            f"/candidates/{candidate.id}/scheduling-links",
            headers=auth(interviewer),
        )
        assert recruiter_list.status_code == 200
        assert interviewer_list.status_code == 200
        assert recruiter_list.json()["items"][0]["public_url"] is None
        assert interviewer_list.json()["items"][0]["public_url"] is None

        read_only_rotate = await client.post(
            f"/scheduling-links/{link_id}/rotate",
            headers=auth(interviewer),
        )
        unassigned_rotate = await client.post(
            f"/scheduling-links/{link_id}/rotate",
            headers=auth(unassigned_manager),
        )
        cross_tenant_rotate = await client.post(
            f"/scheduling-links/{link_id}/rotate",
            headers=auth(outsider),
        )
        assert read_only_rotate.status_code == 403
        assert unassigned_rotate.status_code == 404
        assert cross_tenant_rotate.status_code == 404

        rotated = await client.post(
            f"/scheduling-links/{link_id}/rotate",
            headers=auth(recruiter),
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.headers["cache-control"] == "private, no-store"
        rotated_url = rotated.json()["public_url"]
        rotated_token = rotated_url.rsplit("/", 1)[-1]
        assert rotated_token != original_token

        old_replay = await client.get(f"/public/scheduling/{original_token}")
        current = await client.get(f"/public/scheduling/{rotated_token}")
        assert old_replay.status_code == 404
        assert current.status_code == 200

        after_rotate_list = await client.get(
            f"/candidates/{candidate.id}/scheduling-links",
            headers=auth(recruiter),
        )
        assert after_rotate_list.json()["items"][0]["public_url"] is None

        revoked = await client.delete(
            f"/scheduling-links/{link_id}",
            headers=auth(recruiter),
        )
        revoked_replay = await client.get(f"/public/scheduling/{rotated_token}")
        repeated_revoke = await client.delete(
            f"/scheduling-links/{link_id}",
            headers=auth(recruiter),
        )
        rotate_revoked = await client.post(
            f"/scheduling-links/{link_id}/rotate",
            headers=auth(recruiter),
        )
        assert revoked.status_code == 204
        assert revoked_replay.status_code == 410
        assert repeated_revoke.status_code == 204
        assert rotate_revoked.status_code == 410

    activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.candidate_id == candidate.id
                )
            )
        ).scalars()
    )
    assert "interview_scheduling_link_rotated" in {
        activity.event_type for activity in activities
    }
    serialized = str([activity.payload for activity in activities])
    assert original_token not in serialized
    assert rotated_token not in serialized


async def test_legacy_derived_scheduling_capability_fails_closed_until_rotation(db):
    organization, recruiter, candidate, position = await seed_application(
        db, "legacy-link"
    )
    issue = await create_link(db, organization, recruiter, candidate, position)
    legacy_token = "A" * 64
    issue.link.token_digest = hashlib.sha256(legacy_token.encode()).hexdigest()
    await db.commit()

    with pytest.raises(SchedulingError) as rejected:
        await get_link_by_token(db, legacy_token)
    assert rejected.value.code == "link_not_found"

    replacement = await rotate_scheduling_link(
        db,
        organization_id=organization.id,
        link_id=issue.link.id,
        actor_user_id=recruiter.id,
    )
    assert replacement is not None
    await db.commit()
    assert issued_token(replacement) != legacy_token
    assert (await get_link_by_token(db, issued_token(replacement))).id == issue.link.id


async def test_booking_reschedule_cancel_are_audited_and_reminded(db):
    organization, recruiter, candidate, position = await seed_application(db, "flow")
    issue = await create_link(
        db, organization, recruiter, candidate, position, reminder_offsets=[1440, 60]
    )
    link = issue.link
    token = issued_token(issue)
    now = datetime.now(UTC)
    slots = await available_slots(
        db,
        link=link,
        timezone="Asia/Dushanbe",
        from_date=now.astimezone().date(),
        days=4,
        now=now,
    )
    assert len(slots) >= 2

    appointment, manage_url = await book_appointment(
        db,
        token=token,
        starts_at=slots[0].starts_at,
        timezone="Asia/Dushanbe",
        idempotency_key="candidate-booking-flow",
        now=now,
    )
    await db.commit()
    assert manage_url.endswith(appointment_manage_token(appointment.id))
    assert appointment.status == "scheduled"
    assert appointment.provider == "local"
    assert appointment.location_details is None
    candidate_view = (await public_appointment_out(db, appointment)).model_dump()
    assert "candidate_id" not in candidate_view
    assert "interviewer_user_ids" not in candidate_view
    assert "organizer_user_id" not in candidate_view

    # Replaying a public request is safe and returns the same capability URL.
    repeated, repeated_url = await book_appointment(
        db,
        token=token,
        starts_at=slots[0].starts_at,
        timezone="Asia/Dushanbe",
        idempotency_key="candidate-booking-flow",
        now=now,
    )
    assert repeated.id == appointment.id
    assert repeated_url == manage_url

    reminders = list(
        (
            await db.execute(
                select(InterviewReminder).where(
                    InterviewReminder.appointment_id == appointment.id,
                    InterviewReminder.status == "pending",
                )
            )
        ).scalars()
    )
    assert len(reminders) == 4  # candidate + interviewer, at two offsets

    later_slots = await available_slots(
        db,
        link=link,
        timezone="Asia/Dushanbe",
        from_date=now.astimezone().date(),
        days=4,
        now=now,
        exclude_appointment_id=appointment.id,
    )
    replacement = next(
        item
        for item in later_slots
        if item.starts_at.astimezone(UTC) != appointment.starts_at
    )
    appointment = await reschedule_appointment(
        db,
        manage_token=appointment_manage_token(appointment.id),
        starts_at=replacement.starts_at,
        timezone="Asia/Dushanbe",
        expected_version=1,
        now=now,
    )
    await db.commit()
    assert appointment.version == 2
    pending = list(
        (
            await db.execute(
                select(InterviewReminder).where(
                    InterviewReminder.appointment_id == appointment.id,
                    InterviewReminder.status == "pending",
                )
            )
        ).scalars()
    )
    assert len(pending) == 4

    with pytest.raises(SchedulingError) as stale:
        await reschedule_appointment(
            db,
            manage_token=appointment_manage_token(appointment.id),
            starts_at=slots[0].starts_at,
            timezone="Asia/Dushanbe",
            expected_version=1,
            now=now,
        )
    assert stale.value.code == "version_conflict"

    appointment = await cancel_appointment(
        db,
        manage_token=appointment_manage_token(appointment.id),
        expected_version=2,
        now=now,
    )
    await db.commit()
    assert appointment.status == "cancelled"
    assert appointment.version == 3
    assert (
        await db.execute(
            select(InterviewReminder).where(
                InterviewReminder.appointment_id == appointment.id,
                InterviewReminder.status == "pending",
            )
        )
    ).scalars().all() == []

    events = list(
        (
            await db.execute(
                select(CandidateActivity)
                .where(CandidateActivity.candidate_id == candidate.id)
                .order_by(CandidateActivity.occurred_at, CandidateActivity.id)
            )
        ).scalars()
    )
    assert {
        "interview_scheduling_link_created",
        "interview_scheduled",
        "interview_rescheduled",
        "interview_cancelled",
    }.issubset({event.event_type for event in events})
    serialized = str([event.payload for event in events]).lower()
    assert "location_details" not in serialized
    assert "candidate-booking-flow" not in serialized
    assert "token" not in serialized


async def test_appointment_management_token_rotates_revokes_and_expires(db):
    organization, recruiter, candidate, position = await seed_application(db, "token")
    issue = await create_link(db, organization, recruiter, candidate, position)
    link = issue.link
    current = datetime.now(UTC)
    slots = await available_slots(
        db,
        link=link,
        timezone="UTC",
        from_date=current.date(),
        days=4,
        now=current,
    )
    appointment, original_url = await book_appointment(
        db,
        token=issued_token(issue),
        starts_at=slots[0].starts_at,
        timezone="UTC",
        idempotency_key="management-token-lifecycle",
        now=current,
    )
    await db.commit()
    original_token = original_url.rsplit("/", 1)[-1]

    rotated, rotated_url = await rotate_appointment_manage_token(
        db,
        organization_id=organization.id,
        appointment_id=appointment.id,
        actor_user_id=recruiter.id,
        now=current,
    )
    await db.commit()
    rotated_token = rotated_url.rsplit("/", 1)[-1]
    assert rotated.manage_token_version == 2
    assert rotated_token != original_token
    with pytest.raises(SchedulingError) as replayed:
        await get_appointment_by_manage_token(db, original_token, now=current)
    assert replayed.value.code == "appointment_not_found"
    assert (
        await get_appointment_by_manage_token(db, rotated_token, now=current)
    ).id == appointment.id

    await revoke_appointment_manage_token(
        db,
        organization_id=organization.id,
        appointment_id=appointment.id,
        actor_user_id=recruiter.id,
        now=current,
    )
    await db.commit()
    with pytest.raises(SchedulingError) as revoked:
        await get_appointment_by_manage_token(db, rotated_token, now=current)
    assert revoked.value.code == "link_revoked"

    rotated_again, active_url = await rotate_appointment_manage_token(
        db,
        organization_id=organization.id,
        appointment_id=appointment.id,
        actor_user_id=recruiter.id,
        now=current,
    )
    await db.commit()
    active_token = active_url.rsplit("/", 1)[-1]
    with pytest.raises(SchedulingError) as expired:
        await get_appointment_by_manage_token(
            db,
            active_token,
            now=rotated_again.manage_token_expires_at + timedelta(seconds=1),
        )
    assert expired.value.code == "link_expired"

    event_types = set(
        (
            await db.execute(
                select(CandidateActivity.event_type).where(
                    CandidateActivity.candidate_id == candidate.id
                )
            )
        ).scalars()
    )
    assert "appointment_manage_token_rotated" in event_types
    assert "appointment_manage_token_revoked" in event_types


async def test_concurrent_links_cannot_double_book_one_interviewer(db):
    organization, recruiter, candidate, position = await seed_application(
        db, "concurrent"
    )
    first_issue = await create_link(db, organization, recruiter, candidate, position)
    second_issue = await create_link(db, organization, recruiter, candidate, position)
    first_link = first_issue.link
    now = datetime.now(UTC)
    slots = await available_slots(
        db,
        link=first_link,
        timezone="UTC",
        from_date=now.date(),
        days=3,
        now=now,
    )
    selected = slots[0].starts_at

    async def attempt(token, idempotency_key):
        async with SessionLocal() as session:
            try:
                appointment, _ = await book_appointment(
                    session,
                    token=token,
                    starts_at=selected,
                    timezone="UTC",
                    idempotency_key=idempotency_key,
                    now=now,
                )
                await session.commit()
                return appointment.id
            except SchedulingError as exc:
                await session.rollback()
                return exc.code

    outcomes = await asyncio.gather(
        attempt(issued_token(first_issue), "concurrent-first"),
        attempt(issued_token(second_issue), "concurrent-second"),
    )
    assert sum(outcome == "slot_unavailable" for outcome in outcomes) == 1
    assert (
        len(
            (
                await db.execute(
                    select(InterviewAppointment).where(
                        InterviewAppointment.organization_id == organization.id
                    )
                )
            )
            .scalars()
            .all()
        )
        == 1
    )


async def test_due_reminders_are_durable_tenant_safe_and_merge_rehomes(db):
    organization, recruiter, candidate, position = await seed_application(db, "merge")
    issue = await create_link(
        db,
        organization,
        recruiter,
        candidate,
        position,
        reminder_offsets=[0],
    )
    link = issue.link
    now = datetime.now(UTC)
    slot = (
        await available_slots(
            db,
            link=link,
            timezone="UTC",
            from_date=now.date(),
            days=3,
            now=now,
        )
    )[0]
    appointment, _ = await book_appointment(
        db,
        token=issued_token(issue),
        starts_at=slot.starts_at,
        timezone="UTC",
        idempotency_key="reminder-booking",
        now=now,
    )
    await db.execute(
        update(InterviewReminder)
        .where(InterviewReminder.appointment_id == appointment.id)
        .values(due_at=now - timedelta(seconds=1))
    )
    processed = await process_due_reminders(db, now=now)
    await db.commit()
    assert processed == 2
    assert (
        len(
            (
                await db.execute(
                    select(Notification).where(
                        Notification.organization_id == organization.id,
                        Notification.type == "interview_reminder",
                    )
                )
            )
            .scalars()
            .all()
        )
        == 1
    )
    assert (
        len(
            (
                await db.execute(
                    select(CandidateActivity).where(
                        CandidateActivity.candidate_id == candidate.id,
                        CandidateActivity.event_type == "interview_reminder_sent",
                    )
                )
            )
            .scalars()
            .all()
        )
        == 2
    )

    duplicate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="merge-scheduling-duplicate",
        raw_cv="Another detailed CV record that should become an alias. " * 2,
    )
    duplicate_position = Position(
        organization_id=organization.id,
        title="Second role",
        status="open",
    )
    db.add_all([duplicate, duplicate_position])
    await db.flush()
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=duplicate.id,
            position_id=duplicate_position.id,
            overall_score=70,
            stage="screening",
        )
    )
    await db.flush()
    duplicate_issue = await create_scheduling_link(
        db,
        organization_id=organization.id,
        candidate_id=duplicate.id,
        actor_user_id=recruiter.id,
        body=SchedulingLinkCreate(
            position_id=duplicate_position.id,
            interviewer_user_ids=[recruiter.id],
            min_notice_minutes=0,
        ),
    )
    await db.commit()
    duplicate_link = duplicate_issue.link
    duplicate_link_id = duplicate_link.id
    candidate_id = candidate.id
    await merge_candidates(
        db,
        organization_id=organization.id,
        actor_user_id=recruiter.id,
        primary_candidate_id=candidate.id,
        duplicate_candidate_ids=[duplicate.id],
        score_winners={},
    )
    db.expire_all()
    assert (
        await db.get(SchedulingLink, duplicate_link_id)
    ).candidate_id == candidate_id

    other_org = Organization(name="Other tenant", slug=f"other-{uuid4().hex[:8]}")
    db.add(other_org)
    await db.flush()
    outsider = User(
        organization_id=other_org.id,
        email=f"outsider-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(outsider)
    await db.commit()
    with pytest.raises(HTTPException) as hidden:
        await list_candidate_scheduling_links(
            candidate_id,
            db=db,
            user=outsider,
        )
    assert hidden.value.status_code == 404
