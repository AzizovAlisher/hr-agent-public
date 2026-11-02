"""API contracts for local interview scheduling."""

from datetime import date, datetime, time
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LocationMode = Literal["video", "phone", "onsite"]
AppointmentStatus = Literal["scheduled", "cancelled", "completed", "no_show"]


def _valid_timezone(value: str) -> str:
    cleaned = value.strip()
    try:
        ZoneInfo(cleaned)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Unknown IANA timezone") from exc
    return cleaned


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return value


class AvailabilityInterval(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def validate_order(self) -> "AvailabilityInterval":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class AvailabilityUpdate(BaseModel):
    timezone: str
    intervals: list[AvailabilityInterval] = Field(max_length=50)

    _timezone = field_validator("timezone")(_valid_timezone)

    @field_validator("intervals")
    @classmethod
    def reject_duplicates(
        cls, value: list[AvailabilityInterval]
    ) -> list[AvailabilityInterval]:
        keys = {(item.weekday, item.start_time, item.end_time) for item in value}
        if len(keys) != len(value):
            raise ValueError("Duplicate availability interval")
        return value


class AvailabilityOut(BaseModel):
    timezone: str
    intervals: list[AvailabilityInterval]


class AvailabilityBlockCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime

    _starts = field_validator("starts_at")(_aware)
    _ends = field_validator("ends_at")(_aware)

    @model_validator(mode="after")
    def validate_order(self) -> "AvailabilityBlockCreate":
        if self.starts_at >= self.ends_at:
            raise ValueError("starts_at must be before ends_at")
        return self


class AvailabilityBlockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    starts_at: datetime
    ends_at: datetime
    source: Literal["manual", "google", "outlook"]
    created_at: datetime


class SchedulingLinkCreate(BaseModel):
    position_id: UUID
    application_id: UUID | None = None
    interviewer_user_ids: list[UUID] = Field(min_length=1, max_length=20)
    organizer_user_id: UUID | None = None
    interview_id: UUID | None = None
    calendar_connection_id: UUID | None = None
    duration_minutes: int = Field(default=60, ge=15, le=240)
    slot_interval_minutes: int = Field(default=30, ge=5, le=120)
    min_notice_minutes: int = Field(default=120, ge=0, le=43_200)
    booking_window_days: int = Field(default=14, ge=1, le=90)
    buffer_minutes: int = Field(default=0, ge=0, le=240)
    location_mode: LocationMode = "video"
    location_details: str | None = Field(default=None, max_length=2000)
    expires_in_days: int = Field(default=14, ge=1, le=90)
    reminder_offsets: list[int] = Field(
        default_factory=lambda: [1440, 60], max_length=10
    )

    @field_validator("interviewer_user_ids")
    @classmethod
    def unique_interviewers(cls, value: list[UUID]) -> list[UUID]:
        unique = list(dict.fromkeys(value))
        if len(unique) != len(value):
            raise ValueError("Duplicate interviewer_user_ids")
        return unique

    @field_validator("reminder_offsets")
    @classmethod
    def valid_offsets(cls, value: list[int]) -> list[int]:
        if any(offset < 0 or offset > 10_080 for offset in value):
            raise ValueError("Reminder offsets must be between 0 and 10080 minutes")
        if len(set(value)) != len(value):
            raise ValueError("Duplicate reminder offset")
        return sorted(value, reverse=True)

    @field_validator("location_details")
    @classmethod
    def clean_location(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class SchedulingLinkOut(BaseModel):
    id: UUID
    candidate_id: UUID
    position_id: UUID
    application_id: UUID
    interview_id: UUID | None
    organizer_user_id: UUID | None
    calendar_connection_id: UUID | None
    provider: Literal["local", "google", "outlook"]
    interviewer_user_ids: list[UUID]
    duration_minutes: int
    slot_interval_minutes: int
    min_notice_minutes: int
    booking_window_days: int
    buffer_minutes: int
    location_mode: LocationMode
    reminder_offsets: list[int]
    expires_at: datetime
    revoked_at: datetime | None
    booked_at: datetime | None
    created_at: datetime
    # Capability material is returned only by create/rotate responses. It is
    # deliberately redacted from list/read responses and cannot be recovered.
    public_url: str | None = None


class SchedulingLinkIssueOut(SchedulingLinkOut):
    public_url: str


class CalendarProviderOut(BaseModel):
    provider: Literal["local", "google", "outlook"]
    connection_id: UUID | None = None
    name: str
    status: (
        Literal[
            "unavailable",
            "disconnected",
            "connecting",
            "connected",
            "degraded",
            "expired",
            "revoked",
        ]
        | None
    ) = None
    capability: Literal["local", "calendar.write"]
    available: bool
    reason: str | None = None


class PublicSchedulingDetails(BaseModel):
    # None when the role is confidential — the employer is withheld from the
    # candidate at this stage just as it is on the apply page.
    organization_name: str | None = None
    organization_logo_url: str | None = None
    accent_color: str | None = None
    position_title: str
    duration_minutes: int
    location_mode: LocationMode
    expires_at: datetime


class SchedulingSlotOut(BaseModel):
    starts_at: datetime
    ends_at: datetime
    timezone: str


class SchedulingSlotPage(BaseModel):
    timezone: str
    from_date: date
    through_date: date
    items: list[SchedulingSlotOut]
    next_cursor: str | None = None


class PublicBookingCreate(BaseModel):
    starts_at: datetime
    timezone: str
    idempotency_key: str = Field(min_length=1, max_length=128)

    _starts = field_validator("starts_at")(_aware)
    _timezone = field_validator("timezone")(_valid_timezone)

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("idempotency_key cannot be blank")
        return cleaned


class PublicRescheduleRequest(BaseModel):
    starts_at: datetime
    timezone: str
    expected_version: int = Field(ge=1)

    _starts = field_validator("starts_at")(_aware)
    _timezone = field_validator("timezone")(_valid_timezone)


class PublicCancelRequest(BaseModel):
    expected_version: int = Field(ge=1)


class ReminderDeliveryOut(BaseModel):
    id: UUID
    audience: Literal["candidate", "interviewer"]
    channel: Literal["in_app", "sandbox", "email"]
    offset_minutes: int
    due_at: datetime
    status: Literal["pending", "sent", "failed", "cancelled", "skipped"]
    sent_at: datetime | None
    communication_message_id: UUID | None
    delivery_status: Literal[
        "queued", "processing", "sent", "delivered", "failed", "cancelled"
    ] | None
    delivery_attempts: int
    failure_code: str | None


class AppointmentOut(BaseModel):
    id: UUID
    candidate_id: UUID
    position_id: UUID | None
    application_id: UUID | None = None
    interview_id: UUID | None
    organizer_user_id: UUID | None
    calendar_connection_id: UUID | None
    interviewer_user_ids: list[UUID]
    interviewer_labels: list[str]
    status: AppointmentStatus
    starts_at: datetime
    ends_at: datetime
    candidate_timezone: str
    location_mode: LocationMode
    location_details: str | None
    meeting_url: str | None
    position_title: str
    provider: Literal["local", "google", "outlook"]
    sync_status: Literal["local", "pending", "synced", "failed"]
    reminders: list[ReminderDeliveryOut]
    version: int
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PublicAppointmentOut(BaseModel):
    """Candidate-facing appointment data without internal tenant identifiers."""

    id: UUID
    interviewer_labels: list[str]
    status: AppointmentStatus
    starts_at: datetime
    ends_at: datetime
    candidate_timezone: str
    location_mode: LocationMode
    location_details: str | None
    meeting_url: str | None
    position_title: str
    provider: Literal["local", "google", "outlook"]
    sync_status: Literal["local", "pending", "synced", "failed"]
    version: int
    cancelled_at: datetime | None


class PublicBookingOut(BaseModel):
    appointment: PublicAppointmentOut
    manage_url: str


class AppointmentManageTokenOut(BaseModel):
    manage_url: str
    expires_at: datetime


class PublicAppointmentDetails(BaseModel):
    appointment: PublicAppointmentOut
    # None when the role is confidential — see PublicSchedulingDetails.
    organization_name: str | None = None
    organization_logo_url: str | None = None
    accent_color: str | None = None


class ReminderProcessOut(BaseModel):
    processed: int
