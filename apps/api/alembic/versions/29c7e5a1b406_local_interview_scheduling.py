"""local interview scheduling

Revision ID: 29c7e5a1b406
Revises: 18f4b6d8a02c
Create Date: 2026-07-28 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "29c7e5a1b406"
down_revision: Union[str, Sequence[str], None] = "18f4b6d8a02c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "availability_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(timezone=False), nullable=False),
        sa.Column("end_time", sa.Time(timezone=False), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default="true"
        ),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "weekday >= 0 AND weekday <= 6",
            name="ck_availability_rule_weekday",
        ),
        sa.CheckConstraint(
            "start_time < end_time",
            name="ck_availability_rule_time_order",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "weekday",
            "start_time",
            "end_time",
            name="uq_availability_rule_interval",
        ),
    )
    op.create_index(
        "ix_availability_rules_user_weekday",
        "availability_rules",
        ["organization_id", "user_id", "weekday", "is_active"],
    )

    op.create_table(
        "availability_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="manual",
        ),
        _created_at(),
        sa.CheckConstraint(
            "starts_at < ends_at", name="ck_availability_block_time_order"
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'google', 'outlook')",
            name="ck_availability_block_source",
        ),
    )
    op.create_index(
        "ix_availability_blocks_user_time",
        "availability_blocks",
        ["organization_id", "user_id", "starts_at", "ends_at"],
    )

    op.create_table(
        "interview_scheduling_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interview_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interviews.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "organizer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("token_digest", sa.String(length=64), nullable=False, unique=True),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("slot_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("min_notice_minutes", sa.Integer(), nullable=False),
        sa.Column("booking_window_days", sa.Integer(), nullable=False),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "location_mode",
            sa.String(length=16),
            nullable=False,
            server_default="video",
        ),
        sa.Column("location_details", sa.Text(), nullable=True),
        sa.Column(
            "reminder_offsets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[1440, 60]'::jsonb"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "duration_minutes >= 15 AND duration_minutes <= 240",
            name="ck_scheduling_link_duration",
        ),
        sa.CheckConstraint(
            "slot_interval_minutes >= 5 AND slot_interval_minutes <= 120",
            name="ck_scheduling_link_interval",
        ),
        sa.CheckConstraint(
            "min_notice_minutes >= 0 AND min_notice_minutes <= 43200",
            name="ck_scheduling_link_notice",
        ),
        sa.CheckConstraint(
            "booking_window_days >= 1 AND booking_window_days <= 90",
            name="ck_scheduling_link_window",
        ),
        sa.CheckConstraint(
            "buffer_minutes >= 0 AND buffer_minutes <= 240",
            name="ck_scheduling_link_buffer",
        ),
        sa.CheckConstraint(
            "location_mode IN ('video', 'phone', 'onsite')",
            name="ck_scheduling_link_location_mode",
        ),
    )
    op.create_index(
        "ix_scheduling_links_candidate_created",
        "interview_scheduling_links",
        ["candidate_id", sa.text("created_at DESC"), "id"],
    )
    op.create_index(
        "ix_scheduling_links_org_active",
        "interview_scheduling_links",
        ["organization_id", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "interview_scheduling_link_interviewers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scheduling_link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_scheduling_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _created_at(),
        sa.UniqueConstraint(
            "scheduling_link_id",
            "user_id",
            name="uq_scheduling_link_interviewer",
        ),
    )
    op.create_index(
        "ix_scheduling_link_interviewers_user",
        "interview_scheduling_link_interviewers",
        ["organization_id", "user_id", "scheduling_link_id"],
    )

    op.create_table(
        "interview_appointments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scheduling_link_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_scheduling_links.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "interview_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interviews.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "organizer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candidate_timezone", sa.String(length=64), nullable=False),
        sa.Column("location_mode", sa.String(length=16), nullable=False),
        sa.Column("location_details", sa.Text(), nullable=True),
        sa.Column(
            "position_title_snapshot", sa.String(length=255), nullable=False
        ),
        sa.Column(
            "provider",
            sa.String(length=16),
            nullable=False,
            server_default="local",
        ),
        sa.Column("external_event_id", sa.String(length=255), nullable=True),
        sa.Column(
            "sync_status",
            sa.String(length=16),
            nullable=False,
            server_default="local",
        ),
        sa.Column(
            "manage_token_digest", sa.String(length=64), nullable=False, unique=True
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "starts_at < ends_at", name="ck_appointment_time_order"
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'cancelled', 'completed', 'no_show')",
            name="ck_appointment_status",
        ),
        sa.CheckConstraint(
            "location_mode IN ('video', 'phone', 'onsite')",
            name="ck_appointment_location_mode",
        ),
        sa.CheckConstraint(
            "provider IN ('local', 'google', 'outlook')",
            name="ck_appointment_provider",
        ),
        sa.CheckConstraint(
            "sync_status IN ('local', 'pending', 'synced', 'failed')",
            name="ck_appointment_sync_status",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_appointment_org_idempotency",
        ),
    )
    op.create_index(
        "ix_interview_appointments_candidate_time",
        "interview_appointments",
        ["candidate_id", sa.text("starts_at DESC"), "id"],
    )
    op.create_index(
        "ix_interview_appointments_org_time",
        "interview_appointments",
        ["organization_id", "status", "starts_at", "ends_at"],
    )

    op.create_table(
        "interview_appointment_interviewers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("interviewer_label", sa.String(length=320), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "appointment_id", "user_id", name="uq_appointment_interviewer"
        ),
    )
    op.create_index(
        "ix_appointment_interviewers_user",
        "interview_appointment_interviewers",
        ["organization_id", "user_id", "appointment_id"],
    )

    op.create_table(
        "interview_reminders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("interview_appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient_key", sa.String(length=80), nullable=False),
        sa.Column("audience", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "audience IN ('candidate', 'interviewer')",
            name="ck_interview_reminder_audience",
        ),
        sa.CheckConstraint(
            "channel IN ('in_app', 'sandbox')",
            name="ck_interview_reminder_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'cancelled', 'skipped')",
            name="ck_interview_reminder_status",
        ),
        sa.CheckConstraint(
            "offset_minutes >= 0 AND offset_minutes <= 10080",
            name="ck_interview_reminder_offset",
        ),
    )
    op.create_index(
        "uq_interview_reminder_pending_recipient_offset",
        "interview_reminders",
        ["appointment_id", "recipient_key", "offset_minutes"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_interview_reminders_due",
        "interview_reminders",
        ["status", "due_at", "claimed_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_interview_reminders_due", table_name="interview_reminders")
    op.drop_index(
        "uq_interview_reminder_pending_recipient_offset",
        table_name="interview_reminders",
    )
    op.drop_table("interview_reminders")
    op.drop_index(
        "ix_appointment_interviewers_user",
        table_name="interview_appointment_interviewers",
    )
    op.drop_table("interview_appointment_interviewers")
    op.drop_index(
        "ix_interview_appointments_org_time", table_name="interview_appointments"
    )
    op.drop_index(
        "ix_interview_appointments_candidate_time",
        table_name="interview_appointments",
    )
    op.drop_table("interview_appointments")
    op.drop_index(
        "ix_scheduling_link_interviewers_user",
        table_name="interview_scheduling_link_interviewers",
    )
    op.drop_table("interview_scheduling_link_interviewers")
    op.drop_index(
        "ix_scheduling_links_org_active", table_name="interview_scheduling_links"
    )
    op.drop_index(
        "ix_scheduling_links_candidate_created",
        table_name="interview_scheduling_links",
    )
    op.drop_table("interview_scheduling_links")
    op.drop_index(
        "ix_availability_blocks_user_time", table_name="availability_blocks"
    )
    op.drop_table("availability_blocks")
    op.drop_index(
        "ix_availability_rules_user_weekday", table_name="availability_rules"
    )
    op.drop_table("availability_rules")
