"""Interview scheduling records for local and connected tenant calendars.

Calendar credentials deliberately do not live in this module. Google and
Microsoft appointments reference encrypted tenant IntegrationConnections;
local availability and busy blocks remain the zero-credential option.
"""

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class AvailabilityRule(Base):
    __tablename__ = "availability_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    end_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "weekday >= 0 AND weekday <= 6", name="ck_availability_rule_weekday"
        ),
        CheckConstraint(
            "start_time < end_time", name="ck_availability_rule_time_order"
        ),
        UniqueConstraint(
            "organization_id",
            "user_id",
            "weekday",
            "start_time",
            "end_time",
            name="uq_availability_rule_interval",
        ),
        Index(
            "ix_availability_rules_user_weekday",
            "organization_id",
            "user_id",
            "weekday",
            "is_active",
        ),
    )


class AvailabilityBlock(Base):
    __tablename__ = "availability_blocks"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual"
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="ck_availability_block_time_order"),
        CheckConstraint(
            "source IN ('manual', 'google', 'outlook')",
            name="ck_availability_block_source",
        ),
        Index(
            "ix_availability_blocks_user_time",
            "organization_id",
            "user_id",
            "starts_at",
            "ends_at",
        ),
    )


class SchedulingLink(Base):
    __tablename__ = "interview_scheduling_links"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    interview_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    organizer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    calendar_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    slot_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    min_notice_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120
    )
    booking_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14
    )
    buffer_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    location_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="video", server_default="video"
    )
    # This can contain a meeting URL or an office/phone instruction and therefore
    # never belongs in activity or notification payloads.
    location_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_offsets: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=lambda: [1440, 60]
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    booked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            "application_id",
            "candidate_id",
            "position_id",
            name="uq_scheduling_link_application_identity",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_scheduling_link_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "interview_id",
                "application_id",
                "candidate_id",
                "position_id",
            ],
            [
                "interviews.organization_id",
                "interviews.id",
                "interviews.application_id",
                "interviews.candidate_id",
                "interviews.position_id",
            ],
            name="fk_scheduling_link_exact_interview",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "calendar_connection_id"],
            [
                "integration_connections.organization_id",
                "integration_connections.id",
            ],
            name="fk_scheduling_link_tenant_calendar_connection",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "duration_minutes >= 15 AND duration_minutes <= 240",
            name="ck_scheduling_link_duration",
        ),
        CheckConstraint(
            "slot_interval_minutes >= 5 AND slot_interval_minutes <= 120",
            name="ck_scheduling_link_interval",
        ),
        CheckConstraint(
            "min_notice_minutes >= 0 AND min_notice_minutes <= 43200",
            name="ck_scheduling_link_notice",
        ),
        CheckConstraint(
            "booking_window_days >= 1 AND booking_window_days <= 90",
            name="ck_scheduling_link_window",
        ),
        CheckConstraint(
            "buffer_minutes >= 0 AND buffer_minutes <= 240",
            name="ck_scheduling_link_buffer",
        ),
        CheckConstraint(
            "location_mode IN ('video', 'phone', 'onsite')",
            name="ck_scheduling_link_location_mode",
        ),
        Index(
            "ix_scheduling_links_candidate_created",
            "candidate_id",
            text("created_at DESC"),
            "id",
        ),
        Index(
            "ix_scheduling_links_org_active",
            "organization_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_scheduling_links_calendar_connection",
            "organization_id",
            "calendar_connection_id",
        ),
        Index(
            "ix_scheduling_links_org_application",
            "organization_id",
            "application_id",
        ),
    )


class SchedulingLinkInterviewer(Base):
    __tablename__ = "interview_scheduling_link_interviewers"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduling_link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_scheduling_links.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "scheduling_link_id",
            "user_id",
            name="uq_scheduling_link_interviewer",
        ),
        Index(
            "ix_scheduling_link_interviewers_user",
            "organization_id",
            "user_id",
            "scheduling_link_id",
        ),
    )


class InterviewAppointment(Base):
    __tablename__ = "interview_appointments"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduling_link_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_scheduling_links.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    interview_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="SET NULL"),
        nullable=True,
    )
    organizer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    calendar_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="scheduled", server_default="scheduled"
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    candidate_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    location_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    location_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local", server_default="local"
    )
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_meeting_url: Mapped[str | None] = mapped_column(
        String(2_048), nullable=True
    )
    sync_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local", server_default="local"
    )
    manage_token_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    manage_token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    manage_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC) + timedelta(days=30),
    )
    manage_token_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "(position_id IS NULL) = (application_id IS NULL)",
            name="ck_appointment_application_target",
        ),
        CheckConstraint(
            "scheduling_link_id IS NULL OR application_id IS NOT NULL",
            name="ck_appointment_link_requires_application",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_appointment_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "interview_id",
                "application_id",
                "candidate_id",
                "position_id",
            ],
            [
                "interviews.organization_id",
                "interviews.id",
                "interviews.application_id",
                "interviews.candidate_id",
                "interviews.position_id",
            ],
            name="fk_appointment_exact_interview",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            [
                "organization_id",
                "scheduling_link_id",
                "application_id",
                "candidate_id",
                "position_id",
            ],
            [
                "interview_scheduling_links.organization_id",
                "interview_scheduling_links.id",
                "interview_scheduling_links.application_id",
                "interview_scheduling_links.candidate_id",
                "interview_scheduling_links.position_id",
            ],
            name="fk_appointment_exact_scheduling_link",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "calendar_connection_id"],
            [
                "integration_connections.organization_id",
                "integration_connections.id",
            ],
            name="fk_appointment_tenant_calendar_connection",
            ondelete="RESTRICT",
        ),
        CheckConstraint("starts_at < ends_at", name="ck_appointment_time_order"),
        CheckConstraint(
            "status IN ('scheduled', 'cancelled', 'completed', 'no_show')",
            name="ck_appointment_status",
        ),
        CheckConstraint(
            "location_mode IN ('video', 'phone', 'onsite')",
            name="ck_appointment_location_mode",
        ),
        CheckConstraint(
            "provider IN ('local', 'google', 'outlook')",
            name="ck_appointment_provider",
        ),
        CheckConstraint(
            "sync_status IN ('local', 'pending', 'synced', 'failed')",
            name="ck_appointment_sync_status",
        ),
        CheckConstraint(
            "manage_token_version >= 1",
            name="ck_appointment_manage_token_version",
        ),
        CheckConstraint(
            "manage_token_expires_at > created_at",
            name="ck_appointment_manage_token_expiry",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_appointment_org_idempotency",
        ),
        Index(
            "ix_interview_appointments_candidate_time",
            "candidate_id",
            text("starts_at DESC"),
            "id",
        ),
        Index(
            "ix_interview_appointments_org_time",
            "organization_id",
            "status",
            "starts_at",
            "ends_at",
        ),
        Index(
            "ix_interview_appointments_manage_expiry",
            "organization_id",
            "manage_token_expires_at",
            postgresql_where=text("manage_token_revoked_at IS NULL"),
        ),
        Index(
            "ix_interview_appointments_calendar_connection",
            "organization_id",
            "calendar_connection_id",
            "sync_status",
        ),
        Index(
            "ix_interview_appointments_org_application",
            "organization_id",
            "application_id",
        ),
    )


class InterviewAppointmentInterviewer(Base):
    __tablename__ = "interview_appointment_interviewers"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_appointments.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    interviewer_label: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "appointment_id",
            "user_id",
            name="uq_appointment_interviewer",
        ),
        Index(
            "ix_appointment_interviewers_user",
            "organization_id",
            "user_id",
            "appointment_id",
        ),
    )


class InterviewReminder(Base):
    __tablename__ = "interview_reminders"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_appointments.id", ondelete="CASCADE"),
        nullable=False,
    )
    communication_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    recipient_key: Mapped[str] = mapped_column(String(80), nullable=False)
    audience: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "communication_message_id"],
            [
                "communication_messages.organization_id",
                "communication_messages.id",
            ],
            name="fk_interview_reminders_communication_message",
            ondelete="SET NULL (communication_message_id)",
        ),
        CheckConstraint(
            "audience IN ('candidate', 'interviewer')",
            name="ck_interview_reminder_audience",
        ),
        CheckConstraint(
            "channel IN ('in_app', 'sandbox', 'email')",
            name="ck_interview_reminder_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'cancelled', 'skipped')",
            name="ck_interview_reminder_status",
        ),
        CheckConstraint(
            "offset_minutes >= 0 AND offset_minutes <= 10080",
            name="ck_interview_reminder_offset",
        ),
        Index(
            "uq_interview_reminder_pending_recipient_offset",
            "appointment_id",
            "recipient_key",
            "offset_minutes",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_interview_reminders_due",
            "status",
            "due_at",
            "claimed_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_interview_reminders_communication_message",
            "communication_message_id",
            unique=True,
            postgresql_where=text("communication_message_id IS NOT NULL"),
        ),
    )
