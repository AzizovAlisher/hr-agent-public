"""Transaction-scoped local interview scheduling."""

import base64
import binascii
import hashlib
import hmac
import secrets
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import forced_tenant_rls, resolve_capability, set_tenant_context
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationMessage,
)
from app.models.integration import IntegrationConnection
from app.models.interview import Interview, InterviewAssignment
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.scheduling import (
    AvailabilityBlock,
    AvailabilityRule,
    InterviewAppointment,
    InterviewAppointmentInterviewer,
    InterviewReminder,
    SchedulingLink,
    SchedulingLinkInterviewer,
)
from app.models.user import User
from app.schemas.scheduling import (
    AppointmentOut,
    AvailabilityInterval,
    AvailabilityOut,
    PublicAppointmentDetails,
    PublicAppointmentOut,
    PublicSchedulingDetails,
    ReminderDeliveryOut,
    SchedulingLinkCreate,
    SchedulingLinkOut,
    SchedulingSlotOut,
)
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.communications import (
    PreparedCommunication,
    create_outbound_message,
)
from app.services.integrations.jobs import IntegrationJobError, enqueue_job
from app.services.public_positions import is_confidential
from app.services.structured_hiring import (
    resolve_application_for_write,
    resolve_existing_active_application,
)

settings = get_settings()
_SCHEDULING_TOKEN_BYTES = 32
_SCHEDULING_TOKEN_LENGTH = 43
_URLSAFE_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


class SchedulingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SchedulingLinkIssue:
    """A persisted scheduling link plus its one-time capability URL."""

    link: SchedulingLink
    public_url: str


def calendar_provider_name(connection: IntegrationConnection | None) -> str:
    if connection is None:
        return "local"
    return {
        "google_workspace": "google",
        "microsoft_365": "outlook",
    }[connection.provider]


async def load_calendar_connection(
    db: AsyncSession,
    *,
    organization_id: UUID,
    connection_id: UUID,
    require_connected: bool,
) -> IntegrationConnection:
    connection = (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.id == connection_id,
                IntegrationConnection.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise SchedulingError("calendar_connection_not_found", "Calendar not found")
    if connection.provider not in {
        "google_workspace",
        "microsoft_365",
    } or "calendar.write" not in (connection.capabilities or []):
        raise SchedulingError(
            "calendar_capability_mismatch", "Connection cannot write calendar events"
        )
    if require_connected and connection.status != "connected":
        raise SchedulingError(
            "calendar_connection_unavailable", "Calendar connection is unavailable"
        )
    return connection


async def enqueue_calendar_reconciliation(
    db: AsyncSession,
    *,
    appointment: InterviewAppointment,
    actor_user_id: UUID | None,
) -> None:
    if appointment.calendar_connection_id is None:
        appointment.sync_status = "local"
        return
    connection = await load_calendar_connection(
        db,
        organization_id=appointment.organization_id,
        connection_id=appointment.calendar_connection_id,
        require_connected=False,
    )
    try:
        await enqueue_job(
            db,
            connection=connection,
            actor_user_id=actor_user_id,
            operation="calendar.sync",
            idempotency_key=f"appointment:{appointment.id}:v{appointment.version}",
            references={
                "appointment_id": str(appointment.id),
                "appointment_version": appointment.version,
            },
            max_attempts=5,
            allow_unready=True,
        )
    except IntegrationJobError as exc:  # fixed provider contract; fail closed
        raise SchedulingError("calendar_sync_enqueue_failed", exc.code) from exc
    appointment.sync_status = "pending"


def utc_now() -> datetime:
    return datetime.now(UTC)


def timezone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise SchedulingError("invalid_timezone", "Unknown IANA timezone") from exc


def _token(entity_id: UUID, purpose: str) -> str:
    body = entity_id.bytes
    signature = hmac.new(
        settings.jwt_secret.encode(),
        purpose.encode() + b":" + body,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def new_scheduling_link_token() -> str:
    """Return an opaque capability that cannot be reconstructed from a link ID."""

    return secrets.token_urlsafe(_SCHEDULING_TOKEN_BYTES)


def _valid_scheduling_link_token(token: str) -> bool:
    """Accept only the new 256-bit format, invalidating legacy derived tokens."""

    if len(token) != _SCHEDULING_TOKEN_LENGTH or any(
        character not in _URLSAFE_TOKEN_CHARS for character in token
    ):
        return False
    try:
        decoded = base64.urlsafe_b64decode(token + "=")
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == _SCHEDULING_TOKEN_BYTES


def appointment_manage_token(appointment_id: UUID, version: int = 1) -> str:
    if version < 1:
        raise ValueError("Management token version must be positive")
    purpose = "scheduling-manage" if version == 1 else f"scheduling-manage:{version}"
    return _token(appointment_id, purpose)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def scheduling_public_url(token: str) -> str:
    return f"/public/scheduling/{token}"


def appointment_manage_url(appointment: InterviewAppointment) -> str:
    return (
        "/public/scheduling/manage/"
        f"{appointment_manage_token(appointment.id, appointment.manage_token_version)}"
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchedulingError("invalid_timestamp", "Timestamp must include a timezone")
    return value.astimezone(UTC)


async def acquire_interviewer_locks(
    db: AsyncSession, interviewer_ids: list[UUID]
) -> None:
    """Serialize availability mutations and bookings for each interviewer."""

    for user_id in sorted(set(interviewer_ids), key=str):
        # PostgreSQL advisory locks accept a signed bigint. Keep the high bit
        # clear so UUIDs map deterministically without overflow.
        key = user_id.int & ((1 << 63) - 1)
        await db.execute(select(func.pg_advisory_xact_lock(key)))


async def replace_user_availability(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    timezone: str,
    intervals: list[AvailabilityInterval],
) -> AvailabilityOut:
    timezone_for(timezone)
    await acquire_interviewer_locks(db, [user_id])
    await db.execute(
        delete(AvailabilityRule).where(
            AvailabilityRule.organization_id == organization_id,
            AvailabilityRule.user_id == user_id,
        )
    )
    for interval in intervals:
        db.add(
            AvailabilityRule(
                organization_id=organization_id,
                user_id=user_id,
                weekday=interval.weekday,
                start_time=interval.start_time,
                end_time=interval.end_time,
                timezone=timezone,
            )
        )
    await db.flush()
    return AvailabilityOut(timezone=timezone, intervals=intervals)


async def get_user_availability(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
) -> AvailabilityOut:
    rules = list(
        (
            await db.execute(
                select(AvailabilityRule)
                .where(
                    AvailabilityRule.organization_id == organization_id,
                    AvailabilityRule.user_id == user_id,
                    AvailabilityRule.is_active.is_(True),
                )
                .order_by(
                    AvailabilityRule.weekday,
                    AvailabilityRule.start_time,
                    AvailabilityRule.end_time,
                )
            )
        ).scalars()
    )
    timezone = rules[0].timezone if rules else settings.business_timezone
    return AvailabilityOut(
        timezone=timezone,
        intervals=[
            AvailabilityInterval(
                weekday=rule.weekday,
                start_time=rule.start_time,
                end_time=rule.end_time,
            )
            for rule in rules
        ],
    )


async def create_availability_block(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    actor_user_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
) -> AvailabilityBlock:
    start_utc = _as_utc(starts_at)
    end_utc = _as_utc(ends_at)
    if start_utc >= end_utc:
        raise SchedulingError("invalid_time_range", "starts_at must be before ends_at")
    await acquire_interviewer_locks(db, [user_id])
    block = AvailabilityBlock(
        organization_id=organization_id,
        user_id=user_id,
        created_by_user_id=actor_user_id,
        starts_at=start_utc,
        ends_at=end_utc,
        source="manual",
    )
    db.add(block)
    await db.flush()
    return block


async def delete_availability_block(
    db: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    block_id: UUID,
) -> bool:
    block = (
        await db.execute(
            select(AvailabilityBlock).where(
                AvailabilityBlock.id == block_id,
                AvailabilityBlock.organization_id == organization_id,
                AvailabilityBlock.user_id == user_id,
                AvailabilityBlock.source == "manual",
            )
        )
    ).scalar_one_or_none()
    if block is None:
        return False
    await acquire_interviewer_locks(db, [user_id])
    await db.delete(block)
    await db.flush()
    return True


async def create_scheduling_link(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_id: UUID,
    actor_user_id: UUID,
    body: SchedulingLinkCreate,
    now: datetime | None = None,
) -> SchedulingLinkIssue:
    candidate = await resolve_canonical_candidate(db, candidate_id, organization_id)
    if candidate is None:
        raise SchedulingError("candidate_not_found", "Candidate not found")
    calendar_connection = None
    if body.calendar_connection_id is not None:
        calendar_connection = await load_calendar_connection(
            db,
            organization_id=organization_id,
            connection_id=body.calendar_connection_id,
            require_connected=True,
        )
        email_contact = (
            await db.execute(
                select(CandidateContactPoint.id)
                .where(
                    CandidateContactPoint.organization_id == organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                    CandidateContactPoint.channel == "email",
                    CandidateContactPoint.is_opted_out.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if email_contact is None and candidate.email_normalized is None:
            raise SchedulingError(
                "candidate_email_required",
                "A candidate email is required for a connected calendar",
            )
    position = (
        await db.execute(
            select(Position).where(
                Position.id == body.position_id,
                Position.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise SchedulingError("position_not_found", "Position not found")
    users = list(
        (
            await db.execute(
                select(User).where(
                    User.organization_id == organization_id,
                    User.id.in_(body.interviewer_user_ids),
                )
            )
        ).scalars()
    )
    if len(users) != len(body.interviewer_user_ids):
        raise SchedulingError("interviewer_not_found", "Interviewer not found")
    organizer_id = body.organizer_user_id or actor_user_id
    if organizer_id not in {user.id for user in users}:
        organizer = (
            await db.execute(
                select(User.id).where(
                    User.organization_id == organization_id,
                    User.id == organizer_id,
                )
            )
        ).scalar_one_or_none()
        if organizer is None:
            raise SchedulingError("organizer_not_found", "Organizer not found")
    rule_user_ids = set(
        (
            await db.execute(
                select(AvailabilityRule.user_id)
                .where(
                    AvailabilityRule.organization_id == organization_id,
                    AvailabilityRule.user_id.in_(body.interviewer_user_ids),
                    AvailabilityRule.is_active.is_(True),
                )
                .distinct()
            )
        ).scalars()
    )
    missing_availability = [
        user_id for user_id in body.interviewer_user_ids if user_id not in rule_user_ids
    ]
    if missing_availability:
        raise SchedulingError(
            "availability_missing",
            "Every interviewer must configure availability",
        )
    interview: Interview | None = None
    if body.interview_id is not None:
        interview = (
            await db.execute(
                select(Interview).where(
                    Interview.id == body.interview_id,
                    Interview.organization_id == organization_id,
                    Interview.candidate_id == candidate.id,
                    Interview.position_id == position.id,
                )
            )
        ).scalar_one_or_none()
        if interview is None:
            raise SchedulingError("interview_not_found", "Interview not found")
        if interview.application_id is None:
            raise SchedulingError(
                "application_not_found",
                "Interview is not assigned to an application cycle",
            )
        if (
            body.application_id is not None
            and body.application_id != interview.application_id
        ):
            raise SchedulingError(
                "application_mismatch",
                "Scheduling link must use the interview application cycle",
            )

    try:
        if interview is not None:
            application = await resolve_existing_active_application(
                db,
                organization_id=organization_id,
                candidate_id=candidate.id,
                position_id=position.id,
                application_id=interview.application_id,
            )
        else:
            # Issuing a capability is itself a hiring-record mutation.  The
            # one-release adapter may therefore promote a zero-history legacy
            # Score pair, but never guess across closed/repeated cycles.
            application = await resolve_application_for_write(
                db,
                organization_id=organization_id,
                candidate_id=candidate.id,
                position_id=position.id,
                application_id=body.application_id,
                source=candidate.source or "scheduling",
                actor_user_id=actor_user_id,
                actor_kind="human",
                reason="scheduling_link_created",
            )
    except HTTPException as exc:
        # Keep the scheduling service's stable domain-error contract while the
        # shared resolver remains suitable for HTTP routers as well.
        if exc.status_code in {404, 409}:
            raise SchedulingError(
                "application_not_found",
                "An active application cycle is required",
            ) from exc
        raise

    current = now or utc_now()
    link_id = uuid4()
    token = new_scheduling_link_token()
    link = SchedulingLink(
        id=link_id,
        organization_id=organization_id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=application.id,
        interview_id=body.interview_id,
        created_by_user_id=actor_user_id,
        organizer_user_id=organizer_id,
        calendar_connection_id=(
            calendar_connection.id if calendar_connection is not None else None
        ),
        token_digest=token_digest(token),
        duration_minutes=body.duration_minutes,
        slot_interval_minutes=body.slot_interval_minutes,
        min_notice_minutes=body.min_notice_minutes,
        booking_window_days=body.booking_window_days,
        buffer_minutes=body.buffer_minutes,
        location_mode=body.location_mode,
        location_details=body.location_details,
        reminder_offsets=body.reminder_offsets,
        expires_at=current + timedelta(days=body.expires_in_days),
    )
    db.add(link)
    for user_id in body.interviewer_user_ids:
        db.add(
            SchedulingLinkInterviewer(
                organization_id=organization_id,
                scheduling_link_id=link.id,
                user_id=user_id,
            )
        )
        if body.interview_id is not None:
            already_assigned = (
                await db.execute(
                    select(InterviewAssignment.id).where(
                        InterviewAssignment.organization_id == organization_id,
                        InterviewAssignment.interview_id == body.interview_id,
                        InterviewAssignment.interviewer_user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if already_assigned is None:
                db.add(
                    InterviewAssignment(
                        organization_id=organization_id,
                        interview_id=body.interview_id,
                        interviewer_user_id=user_id,
                        assigned_by_user_id=actor_user_id,
                    )
                )
    append_candidate_activity(
        db,
        organization_id=organization_id,
        candidate_id=candidate.id,
        actor_user_id=actor_user_id,
        position_id=position.id,
        application_id=application.id,
        event_type="interview_scheduling_link_created",
        subject_type="scheduling_link",
        subject_id=link.id,
        payload={
            "position_title": position.title,
            "duration_minutes": link.duration_minutes,
            "location_mode": link.location_mode,
            "interviewer_count": len(body.interviewer_user_ids),
        },
        occurred_at=current,
    )
    await db.flush()
    return SchedulingLinkIssue(link=link, public_url=scheduling_public_url(token))


async def link_interviewer_ids(
    db: AsyncSession, link_id: UUID, organization_id: UUID
) -> list[UUID]:
    return list(
        (
            await db.execute(
                select(SchedulingLinkInterviewer.user_id)
                .where(
                    SchedulingLinkInterviewer.organization_id == organization_id,
                    SchedulingLinkInterviewer.scheduling_link_id == link_id,
                )
                .order_by(SchedulingLinkInterviewer.user_id)
            )
        ).scalars()
    )


async def scheduling_link_out(
    db: AsyncSession,
    link: SchedulingLink,
    *,
    public_url: str | None = None,
) -> SchedulingLinkOut:
    return SchedulingLinkOut(
        id=link.id,
        candidate_id=link.candidate_id,
        position_id=link.position_id,
        application_id=link.application_id,
        interview_id=link.interview_id,
        organizer_user_id=link.organizer_user_id,
        calendar_connection_id=link.calendar_connection_id,
        provider=(
            "local"
            if link.calendar_connection_id is None
            else (
                "google"
                if (
                    await load_calendar_connection(
                        db,
                        organization_id=link.organization_id,
                        connection_id=link.calendar_connection_id,
                        require_connected=False,
                    )
                ).provider
                == "google_workspace"
                else "outlook"
            )
        ),
        interviewer_user_ids=await link_interviewer_ids(
            db, link.id, link.organization_id
        ),
        duration_minutes=link.duration_minutes,
        slot_interval_minutes=link.slot_interval_minutes,
        min_notice_minutes=link.min_notice_minutes,
        booking_window_days=link.booking_window_days,
        buffer_minutes=link.buffer_minutes,
        location_mode=link.location_mode,
        reminder_offsets=list(link.reminder_offsets or []),
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        booked_at=link.booked_at,
        created_at=link.created_at,
        public_url=public_url,
    )


async def rotate_scheduling_link(
    db: AsyncSession,
    *,
    organization_id: UUID,
    link_id: UUID,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> SchedulingLinkIssue | None:
    """Invalidate the current capability and return its replacement exactly once."""

    link = (
        await db.execute(
            select(SchedulingLink)
            .where(
                SchedulingLink.id == link_id,
                SchedulingLink.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        return None
    current = now or utc_now()
    _ensure_link_available(link, now=current, allow_booked=False)
    token = new_scheduling_link_token()
    link.token_digest = token_digest(token)
    position_title = (
        await db.execute(select(Position.title).where(Position.id == link.position_id))
    ).scalar_one_or_none()
    append_candidate_activity(
        db,
        organization_id=organization_id,
        candidate_id=link.candidate_id,
        actor_user_id=actor_user_id,
        position_id=link.position_id,
        application_id=link.application_id,
        event_type="interview_scheduling_link_rotated",
        subject_type="scheduling_link",
        subject_id=link.id,
        payload={"position_title": position_title or "Deleted position"},
        occurred_at=current,
    )
    await db.flush()
    return SchedulingLinkIssue(link=link, public_url=scheduling_public_url(token))


async def revoke_scheduling_link(
    db: AsyncSession,
    *,
    organization_id: UUID,
    link_id: UUID,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> SchedulingLink | None:
    link = (
        await db.execute(
            select(SchedulingLink)
            .where(
                SchedulingLink.id == link_id,
                SchedulingLink.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        return None
    if link.revoked_at is None:
        current = now or utc_now()
        link.revoked_at = current
        position_title = (
            await db.execute(
                select(Position.title).where(Position.id == link.position_id)
            )
        ).scalar_one_or_none()
        append_candidate_activity(
            db,
            organization_id=organization_id,
            candidate_id=link.candidate_id,
            actor_user_id=actor_user_id,
            position_id=link.position_id,
            application_id=link.application_id,
            event_type="interview_scheduling_link_revoked",
            subject_type="scheduling_link",
            subject_id=link.id,
            payload={"position_title": position_title or "Deleted position"},
            occurred_at=current,
        )
        await db.flush()
    return link


def _ensure_link_available(
    link: SchedulingLink,
    *,
    now: datetime,
    allow_booked: bool,
) -> None:
    if link.revoked_at is not None:
        raise SchedulingError("link_revoked", "Scheduling link was revoked")
    if link.expires_at <= now:
        raise SchedulingError("link_expired", "Scheduling link has expired")
    if link.booked_at is not None and not allow_booked:
        raise SchedulingError("link_booked", "Scheduling link was already used")


async def get_link_by_token(
    db: AsyncSession,
    token: str,
    *,
    for_update: bool = False,
    allow_booked: bool = False,
    now: datetime | None = None,
) -> SchedulingLink:
    if not _valid_scheduling_link_token(token):
        raise SchedulingError("link_not_found", "Scheduling link not found")
    digest = token_digest(token)
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_capability(db, kind="scheduling", digest=digest)
        if bootstrap is None:
            raise SchedulingError("link_not_found", "Scheduling link not found")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    stmt = select(SchedulingLink).where(SchedulingLink.token_digest == digest)
    if bootstrap is not None:
        stmt = stmt.where(
            SchedulingLink.id == bootstrap.capability_id,
            SchedulingLink.organization_id == bootstrap.organization_id,
        )
    if for_update:
        stmt = stmt.with_for_update()
    link = (await db.execute(stmt)).scalar_one_or_none()
    if link is None:
        raise SchedulingError("link_not_found", "Scheduling link not found")
    _ensure_link_available(
        link,
        now=now or utc_now(),
        allow_booked=allow_booked,
    )
    return link


async def public_link_details(
    db: AsyncSession, link: SchedulingLink
) -> PublicSchedulingDetails:
    row = (
        await db.execute(
            select(Organization, Position)
            .join(Position, Position.organization_id == Organization.id)
            .where(
                Organization.id == link.organization_id,
                Position.id == link.position_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise SchedulingError("link_not_found", "Scheduling link not found")
    organization, position = row
    # Someone who applied to a confidential role did so without being told who
    # the employer is; the interview link must not be the thing that reveals
    # it. Withhold name, logo and accent together — a brand colour alone
    # identifies a well-known operator.
    show_org = not is_confidential(position)
    return PublicSchedulingDetails(
        organization_name=organization.name if show_org else None,
        organization_logo_url=(
            f"/storage/{organization.logo_path}"
            if show_org and organization.logo_path
            else None
        ),
        accent_color=organization.accent_color if show_org else None,
        position_title=position.title,
        duration_minutes=link.duration_minutes,
        location_mode=link.location_mode,
        expires_at=link.expires_at,
    )


def _valid_local_instants(local_naive: datetime, zone: ZoneInfo) -> list[datetime]:
    """Return real UTC instants for a wall time, including both DST folds."""

    result: list[datetime] = []
    for fold in (0, 1):
        aware = local_naive.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(UTC)
        roundtrip = instant.astimezone(zone)
        if (
            roundtrip.replace(tzinfo=None) == local_naive
            and roundtrip.fold == fold
            and instant not in result
        ):
            result.append(instant)
    return result


def _fits_rules(
    starts_at: datetime,
    ends_at: datetime,
    rules: list[AvailabilityRule],
) -> bool:
    for rule in rules:
        zone = timezone_for(rule.timezone)
        local_start = starts_at.astimezone(zone)
        local_end = ends_at.astimezone(zone)
        if local_start.date() != local_end.date():
            continue
        if local_start.weekday() != rule.weekday:
            continue
        start_wall = local_start.timetz().replace(tzinfo=None)
        end_wall = local_end.timetz().replace(tzinfo=None)
        if start_wall >= rule.start_time and end_wall <= rule.end_time:
            return True
    return False


def _overlaps(
    starts_at: datetime,
    ends_at: datetime,
    busy_start: datetime,
    busy_end: datetime,
) -> bool:
    return starts_at < busy_end and ends_at > busy_start


async def available_slots(
    db: AsyncSession,
    *,
    link: SchedulingLink,
    timezone: str,
    from_date: date,
    days: int,
    now: datetime | None = None,
    exclude_appointment_id: UUID | None = None,
) -> list[SchedulingSlotOut]:
    display_zone = timezone_for(timezone)
    if days < 1 or days > 31:
        raise SchedulingError("invalid_days", "days must be between 1 and 31")
    through_date = from_date + timedelta(days=days - 1)
    current = now or utc_now()
    is_management_request = exclude_appointment_id is not None
    if not is_management_request:
        _ensure_link_available(link, now=current, allow_booked=False)
    interviewer_ids = await link_interviewer_ids(db, link.id, link.organization_id)
    if not interviewer_ids:
        return []

    rules = list(
        (
            await db.execute(
                select(AvailabilityRule).where(
                    AvailabilityRule.organization_id == link.organization_id,
                    AvailabilityRule.user_id.in_(interviewer_ids),
                    AvailabilityRule.is_active.is_(True),
                )
            )
        ).scalars()
    )
    rules_by_user: dict[UUID, list[AvailabilityRule]] = defaultdict(list)
    for rule in rules:
        rules_by_user[rule.user_id].append(rule)
    if any(not rules_by_user[user_id] for user_id in interviewer_ids):
        return []

    range_start = datetime.combine(from_date, time.min, display_zone).astimezone(UTC)
    range_end = datetime.combine(
        through_date + timedelta(days=1), time.min, display_zone
    ).astimezone(UTC)
    blocks = list(
        (
            await db.execute(
                select(AvailabilityBlock).where(
                    AvailabilityBlock.organization_id == link.organization_id,
                    AvailabilityBlock.user_id.in_(interviewer_ids),
                    AvailabilityBlock.starts_at < range_end,
                    AvailabilityBlock.ends_at > range_start,
                )
            )
        ).scalars()
    )
    blocks_by_user: dict[UUID, list[AvailabilityBlock]] = defaultdict(list)
    for block in blocks:
        blocks_by_user[block.user_id].append(block)

    appointment_stmt = (
        select(InterviewAppointment, InterviewAppointmentInterviewer.user_id)
        .join(
            InterviewAppointmentInterviewer,
            InterviewAppointmentInterviewer.appointment_id == InterviewAppointment.id,
        )
        .where(
            InterviewAppointment.organization_id == link.organization_id,
            InterviewAppointment.status == "scheduled",
            InterviewAppointmentInterviewer.user_id.in_(interviewer_ids),
            InterviewAppointment.starts_at
            < range_end + timedelta(minutes=link.buffer_minutes),
            InterviewAppointment.ends_at
            > range_start - timedelta(minutes=link.buffer_minutes),
        )
    )
    if exclude_appointment_id is not None:
        appointment_stmt = appointment_stmt.where(
            InterviewAppointment.id != exclude_appointment_id
        )
    appointment_rows = (await db.execute(appointment_stmt)).all()
    appointments_by_user: dict[UUID, list[InterviewAppointment]] = defaultdict(list)
    for appointment, user_id in appointment_rows:
        if user_id is not None:
            appointments_by_user[user_id].append(appointment)

    duration = timedelta(minutes=link.duration_minutes)
    interval = timedelta(minutes=link.slot_interval_minutes)
    earliest = current + timedelta(minutes=link.min_notice_minutes)
    latest = current + timedelta(days=link.booking_window_days)
    if not is_management_request:
        latest = min(link.expires_at, latest)
    anchor_user = interviewer_ids[0]
    candidates: set[datetime] = set()
    for rule in rules_by_user[anchor_user]:
        zone = timezone_for(rule.timezone)
        # The displayed date window can span one extra rule-local date at each
        # edge after timezone conversion.
        anchor_start = range_start.astimezone(zone).date() - timedelta(days=1)
        anchor_end = range_end.astimezone(zone).date() + timedelta(days=1)
        rule_date = anchor_start
        while rule_date <= anchor_end:
            if rule_date.weekday() == rule.weekday:
                wall = datetime.combine(rule_date, rule.start_time)
                wall_end = datetime.combine(rule_date, rule.end_time)
                while wall + duration <= wall_end:
                    for instant in _valid_local_instants(wall, zone):
                        candidates.add(instant)
                    wall += interval
            rule_date += timedelta(days=1)

    items: list[SchedulingSlotOut] = []
    for starts_at in sorted(candidates):
        ends_at = starts_at + duration
        local_date = starts_at.astimezone(display_zone).date()
        if local_date < from_date or local_date > through_date:
            continue
        if starts_at < earliest or ends_at > latest:
            continue
        available = True
        for user_id in interviewer_ids:
            if not _fits_rules(starts_at, ends_at, rules_by_user[user_id]):
                available = False
                break
            if any(
                _overlaps(starts_at, ends_at, block.starts_at, block.ends_at)
                for block in blocks_by_user[user_id]
            ):
                available = False
                break
            buffer = timedelta(minutes=link.buffer_minutes)
            if any(
                _overlaps(
                    starts_at,
                    ends_at,
                    appointment.starts_at - buffer,
                    appointment.ends_at + buffer,
                )
                for appointment in appointments_by_user[user_id]
            ):
                available = False
                break
        if available:
            items.append(
                SchedulingSlotOut(
                    starts_at=starts_at.astimezone(display_zone),
                    ends_at=ends_at.astimezone(display_zone),
                    timezone=timezone,
                )
            )
    return items


async def _selected_slot_available(
    db: AsyncSession,
    *,
    link: SchedulingLink,
    starts_at: datetime,
    timezone: str,
    now: datetime,
    exclude_appointment_id: UUID | None = None,
) -> bool:
    local_date = starts_at.astimezone(timezone_for(timezone)).date()
    slots = await available_slots(
        db,
        link=link,
        timezone=timezone,
        from_date=local_date,
        days=1,
        now=now,
        exclude_appointment_id=exclude_appointment_id,
    )
    target = starts_at.astimezone(UTC)
    return any(slot.starts_at.astimezone(UTC) == target for slot in slots)


async def _participant_users(db: AsyncSession, link: SchedulingLink) -> list[User]:
    ids = await link_interviewer_ids(db, link.id, link.organization_id)
    users = list(
        (
            await db.execute(
                select(User).where(
                    User.organization_id == link.organization_id,
                    User.id.in_(ids),
                )
            )
        ).scalars()
    )
    by_id = {user.id: user for user in users}
    return [by_id[user_id] for user_id in ids if user_id in by_id]


def _add_notifications(
    db: AsyncSession,
    *,
    appointment: InterviewAppointment,
    interviewer_ids: list[UUID],
    notification_type: str,
) -> None:
    for user_id in interviewer_ids:
        db.add(
            Notification(
                organization_id=appointment.organization_id,
                user_id=user_id,
                type=notification_type,
                payload={
                    "candidate_id": str(appointment.candidate_id),
                    "appointment_id": str(appointment.id),
                    "position_title": appointment.position_title_snapshot,
                    "starts_at": appointment.starts_at.isoformat(),
                },
            )
        )


def _add_reminders(
    db: AsyncSession,
    *,
    appointment: InterviewAppointment,
    interviewer_ids: list[UUID],
    offsets: list[int],
) -> None:
    for offset in offsets:
        due_at = appointment.starts_at - timedelta(minutes=offset)
        db.add(
            InterviewReminder(
                organization_id=appointment.organization_id,
                appointment_id=appointment.id,
                user_id=None,
                recipient_key="candidate",
                audience="candidate",
                channel="sandbox",
                offset_minutes=offset,
                due_at=due_at,
            )
        )
        for user_id in interviewer_ids:
            db.add(
                InterviewReminder(
                    organization_id=appointment.organization_id,
                    appointment_id=appointment.id,
                    user_id=user_id,
                    recipient_key=f"user:{user_id}",
                    audience="interviewer",
                    channel="in_app",
                    offset_minutes=offset,
                    due_at=due_at,
                )
            )


async def book_appointment(
    db: AsyncSession,
    *,
    token: str,
    starts_at: datetime,
    timezone: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> tuple[InterviewAppointment, str]:
    current = now or utc_now()
    timezone_for(timezone)
    requested_start = _as_utc(starts_at)

    link = await get_link_by_token(
        db,
        token,
        for_update=True,
        allow_booked=True,
        now=current,
    )
    existing = (
        await db.execute(
            select(InterviewAppointment).where(
                InterviewAppointment.organization_id == link.organization_id,
                InterviewAppointment.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.scheduling_link_id != link.id:
            raise SchedulingError(
                "idempotency_conflict",
                "Idempotency key was already used for another booking",
            )
        return existing, appointment_manage_url(existing)
    _ensure_link_available(link, now=current, allow_booked=False)

    interviewer_ids = await link_interviewer_ids(db, link.id, link.organization_id)
    await acquire_interviewer_locks(db, interviewer_ids)
    if not await _selected_slot_available(
        db,
        link=link,
        starts_at=requested_start,
        timezone=timezone,
        now=current,
    ):
        raise SchedulingError(
            "slot_unavailable", "Selected slot is no longer available"
        )
    position = (
        await db.execute(
            select(Position).where(
                Position.id == link.position_id,
                Position.organization_id == link.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise SchedulingError("link_not_found", "Scheduling link not found")
    appointment_id = uuid4()
    appointment = InterviewAppointment(
        id=appointment_id,
        organization_id=link.organization_id,
        scheduling_link_id=link.id,
        candidate_id=link.candidate_id,
        position_id=link.position_id,
        application_id=link.application_id,
        interview_id=link.interview_id,
        organizer_user_id=link.organizer_user_id,
        calendar_connection_id=link.calendar_connection_id,
        starts_at=requested_start,
        ends_at=requested_start + timedelta(minutes=link.duration_minutes),
        candidate_timezone=timezone,
        location_mode=link.location_mode,
        location_details=link.location_details,
        position_title_snapshot=position.title,
        provider=(
            "local"
            if link.calendar_connection_id is None
            else (
                "google"
                if (
                    await load_calendar_connection(
                        db,
                        organization_id=link.organization_id,
                        connection_id=link.calendar_connection_id,
                        require_connected=False,
                    )
                ).provider
                == "google_workspace"
                else "outlook"
            )
        ),
        sync_status=("local" if link.calendar_connection_id is None else "pending"),
        manage_token_digest=token_digest(appointment_manage_token(appointment_id, 1)),
        manage_token_version=1,
        manage_token_expires_at=max(
            requested_start + timedelta(days=30), current + timedelta(days=14)
        ),
        idempotency_key=idempotency_key,
    )
    db.add(appointment)
    users = await _participant_users(db, link)
    for user in users:
        db.add(
            InterviewAppointmentInterviewer(
                organization_id=link.organization_id,
                appointment_id=appointment.id,
                user_id=user.id,
                interviewer_label=user.email,
            )
        )
    link.booked_at = current
    _add_reminders(
        db,
        appointment=appointment,
        interviewer_ids=[user.id for user in users],
        offsets=list(link.reminder_offsets or []),
    )
    _add_notifications(
        db,
        appointment=appointment,
        interviewer_ids=[user.id for user in users],
        notification_type="interview_scheduled",
    )
    append_candidate_activity(
        db,
        organization_id=appointment.organization_id,
        candidate_id=appointment.candidate_id,
        position_id=appointment.position_id,
        application_id=appointment.application_id,
        event_type="interview_scheduled",
        subject_type="appointment",
        subject_id=appointment.id,
        payload={
            "position_title": appointment.position_title_snapshot,
            "starts_at": appointment.starts_at.isoformat(),
            "duration_minutes": link.duration_minutes,
            "location_mode": appointment.location_mode,
            "interviewer_count": len(users),
        },
        occurred_at=current,
    )
    await enqueue_calendar_reconciliation(
        db,
        appointment=appointment,
        actor_user_id=link.created_by_user_id,
    )
    await db.flush()
    return appointment, appointment_manage_url(appointment)


async def get_appointment_by_manage_token(
    db: AsyncSession,
    token: str,
    *,
    for_update: bool = False,
    now: datetime | None = None,
) -> InterviewAppointment:
    digest = token_digest(token)
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_capability(
            db, kind="scheduling_manage", digest=digest
        )
        if bootstrap is None:
            raise SchedulingError("appointment_not_found", "Appointment not found")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    stmt = select(InterviewAppointment).where(
        InterviewAppointment.manage_token_digest == digest
    )
    if bootstrap is not None:
        stmt = stmt.where(
            InterviewAppointment.id == bootstrap.capability_id,
            InterviewAppointment.organization_id == bootstrap.organization_id,
        )
    if for_update:
        stmt = stmt.with_for_update()
    appointment = (await db.execute(stmt)).scalar_one_or_none()
    if appointment is None:
        raise SchedulingError("appointment_not_found", "Appointment not found")
    current = now or utc_now()
    if appointment.manage_token_revoked_at is not None:
        raise SchedulingError("link_revoked", "Appointment management link was revoked")
    if appointment.manage_token_expires_at <= current:
        raise SchedulingError("link_expired", "Appointment management link has expired")
    return appointment


async def rotate_appointment_manage_token(
    db: AsyncSession,
    *,
    organization_id: UUID,
    appointment_id: UUID,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> tuple[InterviewAppointment, str]:
    current = now or utc_now()
    appointment = (
        await db.execute(
            select(InterviewAppointment)
            .where(
                InterviewAppointment.id == appointment_id,
                InterviewAppointment.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise SchedulingError("appointment_not_found", "Appointment not found")
    appointment.manage_token_version += 1
    raw_token = appointment_manage_token(
        appointment.id, appointment.manage_token_version
    )
    appointment.manage_token_digest = token_digest(raw_token)
    appointment.manage_token_expires_at = max(
        appointment.ends_at + timedelta(days=30), current + timedelta(days=14)
    )
    appointment.manage_token_revoked_at = None
    append_candidate_activity(
        db,
        organization_id=appointment.organization_id,
        candidate_id=appointment.candidate_id,
        actor_user_id=actor_user_id,
        position_id=appointment.position_id,
        application_id=appointment.application_id,
        event_type="appointment_manage_token_rotated",
        subject_type="interview_appointment",
        subject_id=appointment.id,
        payload={"version": appointment.manage_token_version},
        occurred_at=current,
    )
    await db.flush()
    return appointment, f"/public/scheduling/manage/{raw_token}"


async def revoke_appointment_manage_token(
    db: AsyncSession,
    *,
    organization_id: UUID,
    appointment_id: UUID,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> InterviewAppointment:
    current = now or utc_now()
    appointment = (
        await db.execute(
            select(InterviewAppointment)
            .where(
                InterviewAppointment.id == appointment_id,
                InterviewAppointment.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if appointment is None:
        raise SchedulingError("appointment_not_found", "Appointment not found")
    if appointment.manage_token_revoked_at is None:
        appointment.manage_token_revoked_at = current
        append_candidate_activity(
            db,
            organization_id=appointment.organization_id,
            candidate_id=appointment.candidate_id,
            actor_user_id=actor_user_id,
            position_id=appointment.position_id,
            application_id=appointment.application_id,
            event_type="appointment_manage_token_revoked",
            subject_type="interview_appointment",
            subject_id=appointment.id,
            payload={"version": appointment.manage_token_version},
            occurred_at=current,
        )
        await db.flush()
    return appointment


async def appointment_participants(
    db: AsyncSession, appointment: InterviewAppointment
) -> list[InterviewAppointmentInterviewer]:
    return list(
        (
            await db.execute(
                select(InterviewAppointmentInterviewer)
                .where(
                    InterviewAppointmentInterviewer.organization_id
                    == appointment.organization_id,
                    InterviewAppointmentInterviewer.appointment_id == appointment.id,
                )
                .order_by(InterviewAppointmentInterviewer.interviewer_label)
            )
        ).scalars()
    )


async def appointment_reminders(
    db: AsyncSession, appointment: InterviewAppointment
) -> list[ReminderDeliveryOut]:
    reminders = list(
        (
            await db.execute(
                select(InterviewReminder)
                .where(
                    InterviewReminder.organization_id == appointment.organization_id,
                    InterviewReminder.appointment_id == appointment.id,
                )
                .order_by(
                    InterviewReminder.due_at,
                    InterviewReminder.audience,
                    InterviewReminder.id,
                )
            )
        ).scalars()
    )
    message_ids = {
        reminder.communication_message_id
        for reminder in reminders
        if reminder.communication_message_id is not None
    }
    messages = (
        {
            message.id: message
            for message in (
                await db.execute(
                    select(CommunicationMessage).where(
                        CommunicationMessage.organization_id
                        == appointment.organization_id,
                        CommunicationMessage.id.in_(message_ids),
                    )
                )
            ).scalars()
        }
        if message_ids
        else {}
    )
    return [
        ReminderDeliveryOut(
            id=reminder.id,
            audience=reminder.audience,
            channel=reminder.channel,
            offset_minutes=reminder.offset_minutes,
            due_at=reminder.due_at,
            status=reminder.status,
            sent_at=reminder.sent_at,
            communication_message_id=reminder.communication_message_id,
            delivery_status=(
                messages[reminder.communication_message_id].status
                if reminder.communication_message_id in messages
                else None
            ),
            delivery_attempts=(
                messages[reminder.communication_message_id].attempt_count
                if reminder.communication_message_id in messages
                else 0
            ),
            failure_code=(
                messages[reminder.communication_message_id].failure_code
                if reminder.communication_message_id in messages
                else None
            ),
        )
        for reminder in reminders
    ]


async def appointment_out(
    db: AsyncSession, appointment: InterviewAppointment
) -> AppointmentOut:
    participants = await appointment_participants(db, appointment)
    return AppointmentOut(
        id=appointment.id,
        candidate_id=appointment.candidate_id,
        position_id=appointment.position_id,
        application_id=appointment.application_id,
        interview_id=appointment.interview_id,
        organizer_user_id=appointment.organizer_user_id,
        calendar_connection_id=appointment.calendar_connection_id,
        interviewer_user_ids=[
            participant.user_id
            for participant in participants
            if participant.user_id is not None
        ],
        interviewer_labels=[
            participant.interviewer_label for participant in participants
        ],
        status=appointment.status,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        candidate_timezone=appointment.candidate_timezone,
        location_mode=appointment.location_mode,
        location_details=appointment.location_details,
        meeting_url=appointment.external_meeting_url,
        position_title=appointment.position_title_snapshot,
        provider=appointment.provider,
        sync_status=appointment.sync_status,
        reminders=await appointment_reminders(db, appointment),
        version=appointment.version,
        cancelled_at=appointment.cancelled_at,
        created_at=appointment.created_at,
        updated_at=appointment.updated_at,
    )


async def public_appointment_details(
    db: AsyncSession, appointment: InterviewAppointment
) -> PublicAppointmentDetails:
    organization = (
        await db.execute(
            select(Organization).where(Organization.id == appointment.organization_id)
        )
    ).scalar_one()
    # Same rule as public_link_details: the manage link is held by a candidate
    # who was never told who the employer is.
    position = (
        await db.execute(select(Position).where(Position.id == appointment.position_id))
    ).scalar_one_or_none()
    show_org = position is None or not is_confidential(position)
    return PublicAppointmentDetails(
        appointment=await public_appointment_out(db, appointment),
        organization_name=organization.name if show_org else None,
        organization_logo_url=(
            f"/storage/{organization.logo_path}"
            if show_org and organization.logo_path
            else None
        ),
        accent_color=organization.accent_color if show_org else None,
    )


async def public_appointment_out(
    db: AsyncSession, appointment: InterviewAppointment
) -> PublicAppointmentOut:
    participants = await appointment_participants(db, appointment)
    return PublicAppointmentOut(
        id=appointment.id,
        interviewer_labels=[
            participant.interviewer_label for participant in participants
        ],
        status=appointment.status,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        candidate_timezone=appointment.candidate_timezone,
        location_mode=appointment.location_mode,
        location_details=appointment.location_details,
        meeting_url=appointment.external_meeting_url,
        position_title=appointment.position_title_snapshot,
        provider=appointment.provider,
        sync_status=appointment.sync_status,
        version=appointment.version,
        cancelled_at=appointment.cancelled_at,
    )


async def reschedule_appointment(
    db: AsyncSession,
    *,
    manage_token: str,
    starts_at: datetime,
    timezone: str,
    expected_version: int,
    now: datetime | None = None,
) -> InterviewAppointment:
    current = now or utc_now()
    requested_start = _as_utc(starts_at)
    timezone_for(timezone)
    appointment = await get_appointment_by_manage_token(
        db, manage_token, for_update=True
    )
    if appointment.status != "scheduled":
        raise SchedulingError(
            "appointment_not_scheduled",
            "Only a scheduled appointment can be rescheduled",
        )
    if appointment.version != expected_version:
        raise SchedulingError(
            "version_conflict",
            "Appointment changed; refresh before trying again",
        )
    if appointment.scheduling_link_id is None:
        raise SchedulingError(
            "reschedule_unavailable",
            "This appointment cannot be self-rescheduled",
        )
    link = (
        await db.execute(
            select(SchedulingLink)
            .where(
                SchedulingLink.id == appointment.scheduling_link_id,
                SchedulingLink.organization_id == appointment.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        raise SchedulingError(
            "reschedule_unavailable",
            "This appointment cannot be self-rescheduled",
        )
    participants = await appointment_participants(db, appointment)
    interviewer_ids = [
        participant.user_id
        for participant in participants
        if participant.user_id is not None
    ]
    await acquire_interviewer_locks(db, interviewer_ids)
    if not await _selected_slot_available(
        db,
        link=link,
        starts_at=requested_start,
        timezone=timezone,
        now=current,
        exclude_appointment_id=appointment.id,
    ):
        raise SchedulingError(
            "slot_unavailable", "Selected slot is no longer available"
        )
    previous_start = appointment.starts_at
    appointment.starts_at = requested_start
    appointment.ends_at = requested_start + timedelta(minutes=link.duration_minutes)
    appointment.candidate_timezone = timezone
    appointment.version += 1
    if appointment.calendar_connection_id is not None:
        appointment.sync_status = "pending"
    await db.execute(
        update(InterviewReminder)
        .where(
            InterviewReminder.appointment_id == appointment.id,
            InterviewReminder.status == "pending",
        )
        .values(status="cancelled")
    )
    _add_reminders(
        db,
        appointment=appointment,
        interviewer_ids=interviewer_ids,
        offsets=list(link.reminder_offsets or []),
    )
    _add_notifications(
        db,
        appointment=appointment,
        interviewer_ids=interviewer_ids,
        notification_type="interview_rescheduled",
    )
    append_candidate_activity(
        db,
        organization_id=appointment.organization_id,
        candidate_id=appointment.candidate_id,
        position_id=appointment.position_id,
        application_id=appointment.application_id,
        event_type="interview_rescheduled",
        subject_type="appointment",
        subject_id=appointment.id,
        payload={
            "position_title": appointment.position_title_snapshot,
            "previous_starts_at": previous_start.isoformat(),
            "starts_at": appointment.starts_at.isoformat(),
            "duration_minutes": link.duration_minutes,
        },
        occurred_at=current,
    )
    await enqueue_calendar_reconciliation(
        db,
        appointment=appointment,
        actor_user_id=None,
    )
    await db.flush()
    return appointment


async def cancel_appointment(
    db: AsyncSession,
    *,
    manage_token: str,
    expected_version: int,
    now: datetime | None = None,
) -> InterviewAppointment:
    current = now or utc_now()
    appointment = await get_appointment_by_manage_token(
        db, manage_token, for_update=True
    )
    if appointment.status == "cancelled":
        return appointment
    if appointment.status != "scheduled":
        raise SchedulingError(
            "appointment_not_scheduled",
            "Only a scheduled appointment can be cancelled",
        )
    if appointment.version != expected_version:
        raise SchedulingError(
            "version_conflict",
            "Appointment changed; refresh before trying again",
        )
    participants = await appointment_participants(db, appointment)
    interviewer_ids = [
        participant.user_id
        for participant in participants
        if participant.user_id is not None
    ]
    await acquire_interviewer_locks(db, interviewer_ids)
    appointment.status = "cancelled"
    appointment.cancelled_at = current
    appointment.version += 1
    if appointment.calendar_connection_id is not None:
        appointment.sync_status = "pending"
    await db.execute(
        update(InterviewReminder)
        .where(
            InterviewReminder.appointment_id == appointment.id,
            InterviewReminder.status == "pending",
        )
        .values(status="cancelled")
    )
    _add_notifications(
        db,
        appointment=appointment,
        interviewer_ids=interviewer_ids,
        notification_type="interview_cancelled",
    )
    append_candidate_activity(
        db,
        organization_id=appointment.organization_id,
        candidate_id=appointment.candidate_id,
        position_id=appointment.position_id,
        application_id=appointment.application_id,
        event_type="interview_cancelled",
        subject_type="appointment",
        subject_id=appointment.id,
        payload={
            "position_title": appointment.position_title_snapshot,
            "starts_at": appointment.starts_at.isoformat(),
        },
        occurred_at=current,
    )
    await enqueue_calendar_reconciliation(
        db,
        appointment=appointment,
        actor_user_id=None,
    )
    await db.flush()
    return appointment


async def process_due_reminders(
    db: AsyncSession,
    *,
    limit: int = 50,
    now: datetime | None = None,
    organization_id: UUID | None = None,
    reminder_ids: Iterable[UUID] | None = None,
) -> int:
    current = now or utc_now()
    statement = select(InterviewReminder).where(
        InterviewReminder.status == "pending",
        InterviewReminder.due_at <= current,
    )
    if organization_id is not None:
        statement = statement.where(
            InterviewReminder.organization_id == organization_id
        )
    requested_ids = set(reminder_ids or [])
    if requested_ids:
        statement = statement.where(InterviewReminder.id.in_(requested_ids))
    reminders = list(
        (
            await db.execute(
                statement.order_by(InterviewReminder.due_at, InterviewReminder.id)
                .limit(max(1, min(limit, 100)))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    processed = 0
    for reminder in reminders:
        appointment = (
            await db.execute(
                select(InterviewAppointment).where(
                    InterviewAppointment.id == reminder.appointment_id,
                    InterviewAppointment.organization_id == reminder.organization_id,
                )
            )
        ).scalar_one_or_none()
        if appointment is None or appointment.status != "scheduled":
            reminder.status = "skipped"
            reminder.claimed_at = current
            processed += 1
            continue
        reminder.claimed_at = current
        if reminder.audience == "interviewer" and reminder.user_id is not None:
            db.add(
                Notification(
                    organization_id=reminder.organization_id,
                    user_id=reminder.user_id,
                    type="interview_reminder",
                    payload={
                        "candidate_id": str(appointment.candidate_id),
                        "appointment_id": str(appointment.id),
                        "position_title": appointment.position_title_snapshot,
                        "starts_at": appointment.starts_at.isoformat(),
                        "offset_minutes": reminder.offset_minutes,
                    },
                )
            )
        if reminder.audience == "candidate" and appointment.calendar_connection_id:
            linked_message = (
                (
                    await db.execute(
                        select(CommunicationMessage).where(
                            CommunicationMessage.id
                            == reminder.communication_message_id,
                            CommunicationMessage.organization_id
                            == reminder.organization_id,
                        )
                    )
                ).scalar_one_or_none()
                if reminder.communication_message_id is not None
                else None
            )
            if linked_message is not None:
                if linked_message.status in {"sent", "delivered"}:
                    reminder.status = "sent"
                    reminder.sent_at = (
                        linked_message.sent_at
                        or linked_message.delivered_at
                        or current
                    )
                elif linked_message.status in {"failed", "cancelled"}:
                    reminder.status = "failed"
                else:
                    processed += 1
                    continue
            account = (
                await db.execute(
                    select(CommunicationAccount)
                    .where(
                        CommunicationAccount.organization_id
                        == appointment.organization_id,
                        CommunicationAccount.integration_connection_id
                        == appointment.calendar_connection_id,
                        CommunicationAccount.channel == "email",
                        CommunicationAccount.provider.in_(
                            {"google_workspace", "microsoft_365"}
                        ),
                        CommunicationAccount.is_active.is_(True),
                    )
                    .order_by(CommunicationAccount.created_at, CommunicationAccount.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if account is not None:
                message = (
                    (
                        await db.execute(
                            select(CommunicationMessage).where(
                                CommunicationMessage.id
                                == reminder.communication_message_id,
                                CommunicationMessage.organization_id
                                == reminder.organization_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if reminder.communication_message_id is not None
                    else None
                )
                if message is None:
                    candidate = (
                        await db.execute(
                            select(Candidate).where(
                                Candidate.id == appointment.candidate_id,
                                Candidate.organization_id
                                == appointment.organization_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if candidate is None:
                        reminder.status = "failed"
                        processed += 1
                        continue
                    contact = (
                        await db.execute(
                            select(CandidateContactPoint)
                            .where(
                                CandidateContactPoint.organization_id
                                == appointment.organization_id,
                                CandidateContactPoint.candidate_id
                                == appointment.candidate_id,
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
                    position = (
                        (
                            await db.execute(
                                select(Position).where(
                                    Position.id == appointment.position_id,
                                    Position.organization_id
                                    == appointment.organization_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if appointment.position_id is not None
                        else None
                    )
                    if contact is None:
                        reminder.channel = "email"
                        reminder.status = "failed"
                        processed += 1
                        continue
                    local_start = appointment.starts_at.astimezone(
                        timezone_for(appointment.candidate_timezone)
                    )
                    prepared = PreparedCommunication(
                        candidate=candidate,
                        contact=contact,
                        account=account,
                        template=None,
                        position=position,
                        application=(
                            (
                                await db.execute(
                                    select(Application).where(
                                        Application.id == appointment.application_id,
                                        Application.organization_id
                                        == appointment.organization_id,
                                    )
                                )
                            ).scalar_one_or_none()
                            if appointment.application_id is not None
                            else None
                        ),
                        subject=(
                            "Interview reminder — "
                            f"{appointment.position_title_snapshot}"
                        ),
                        body=(
                            "Reminder: your interview for "
                            f"{appointment.position_title_snapshot} starts at "
                            f"{local_start.isoformat()}."
                        ),
                    )
                    message, _created = await create_outbound_message(
                        db,
                        prepared=prepared,
                        actor_user_id=None,
                        idempotency_key=f"interview-reminder:{reminder.id}",
                        scheduled_for=current,
                    )
                    reminder.communication_message_id = message.id
                    reminder.channel = "email"
                if message.status in {"sent", "delivered"}:
                    reminder.status = "sent"
                    reminder.sent_at = message.sent_at or message.delivered_at or current
                elif message.status in {"failed", "cancelled"}:
                    reminder.status = "failed"
                else:
                    # The canonical communications worker owns bounded retry
                    # and terminal failure.  This reminder remains pending so
                    # the scheduling worker can project that terminal state.
                    processed += 1
                    continue
        if reminder.status == "pending":
            reminder.status = "sent"
            reminder.sent_at = current
        event_type = (
            "interview_reminder_sent"
            if reminder.status == "sent"
            else "interview_reminder_failed"
        )
        append_candidate_activity(
            db,
            organization_id=appointment.organization_id,
            candidate_id=appointment.candidate_id,
            position_id=appointment.position_id,
            application_id=appointment.application_id,
            event_type=event_type,
            subject_type="appointment",
            subject_id=appointment.id,
            payload={
                "position_title": appointment.position_title_snapshot,
                "audience": reminder.audience,
                "channel": reminder.channel,
                "offset_minutes": reminder.offset_minutes,
            },
            occurred_at=current,
        )
        processed += 1
    await db.flush()
    return processed
