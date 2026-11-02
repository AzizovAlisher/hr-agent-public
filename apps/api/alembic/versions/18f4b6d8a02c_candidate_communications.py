"""candidate communications

Revision ID: 18f4b6d8a02c
Revises: 07d2a4c6e819
Create Date: 2026-07-28 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "18f4b6d8a02c"
down_revision: Union[str, Sequence[str], None] = "07d2a4c6e819"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "candidate_contact_points",
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
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("value", sa.String(512), nullable=False),
        sa.Column("normalized_value", sa.String(512), nullable=False),
        sa.Column(
            "source", sa.String(32), nullable=False, server_default="manual"
        ),
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "is_opted_out", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('email', 'phone', 'telegram', 'whatsapp')",
            name="ck_contact_point_channel",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "candidate_id",
            "channel",
            "normalized_value",
            name="uq_contact_point_candidate_channel_value",
        ),
    )
    op.create_index(
        "ix_contact_points_candidate_channel",
        "candidate_contact_points",
        ["candidate_id", "channel", "is_primary"],
    )
    op.create_index(
        "ix_contact_points_org_lookup",
        "candidate_contact_points",
        ["organization_id", "channel", "normalized_value"],
    )

    op.create_table(
        "communication_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("sender_label", sa.String(255), nullable=True),
        sa.Column(
            "configuration",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("webhook_secret_hash", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_communication_account_channel",
        ),
        sa.CheckConstraint(
            "provider IN ('sandbox', 'smtp', 'telegram', 'whatsapp')",
            name="ck_communication_account_provider",
        ),
    )
    op.create_index(
        "uq_account_org_channel_name_ci",
        "communication_accounts",
        ["organization_id", "channel", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "ix_communication_accounts_org_channel_active",
        "communication_accounts",
        ["organization_id", "channel", "is_active"],
    )

    op.create_table(
        "communication_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("language", sa.String(8), nullable=False, server_default="ru"),
        sa.Column("subject_template", sa.String(500), nullable=True),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_communication_template_channel",
        ),
    )
    op.create_index(
        "uq_template_org_channel_name_ci",
        "communication_templates",
        ["organization_id", "channel", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "ix_communication_templates_org_active",
        "communication_templates",
        ["organization_id", "channel", "is_active"],
    )

    op.create_table(
        "communication_threads",
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
            "contact_point_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidate_contact_points.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_communication_thread_channel",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name="ck_communication_thread_status"
        ),
    )
    op.create_index(
        "ix_communication_threads_candidate_last",
        "communication_threads",
        ["candidate_id", sa.text("last_message_at DESC NULLS LAST"), "id"],
    )
    op.create_index(
        "ix_communication_threads_org_contact",
        "communication_threads",
        ["organization_id", "candidate_id", "channel", "contact_point_id"],
    )

    op.create_table(
        "communication_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "skip_summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_communication_batch_channel",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'partial', 'failed', 'cancelled')",
            name="ck_communication_batch_status",
        ),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_batch_org_idempotency"
        ),
    )
    op.create_index(
        "ix_communication_batches_org_created",
        "communication_batches",
        ["organization_id", sa.text("created_at DESC"), "id"],
    )

    op.create_table(
        "communication_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_point_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidate_contact_points.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("communication_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_communication_message_channel",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_communication_message_direction",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'sent', 'delivered', 'received', 'failed', 'cancelled')",
            name="ck_communication_message_status",
        ),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_message_org_idempotency"
        ),
    )
    op.create_index(
        "ix_communication_messages_thread_cursor",
        "communication_messages",
        ["thread_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_communication_messages_due",
        "communication_messages",
        ["status", "scheduled_for", "claimed_at"],
        postgresql_where=sa.text(
            "direction = 'outbound' AND status IN ('queued', 'processing')"
        ),
    )
    op.create_index(
        "ix_communication_messages_batch_status",
        "communication_messages",
        ["batch_id", "status"],
    )
    op.create_index(
        "ix_communication_messages_provider",
        "communication_messages",
        ["account_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )

    # Conservative backfill: email remains email, while a phone is retained as
    # a non-sendable phone contact (we do not assume WhatsApp consent).
    op.execute(
        """
        INSERT INTO candidate_contact_points (
            id, organization_id, candidate_id, channel, value,
            normalized_value, source, is_primary, is_opted_out,
            created_at, updated_at
        )
        SELECT gen_random_uuid(), organization_id, id, 'email',
               email_normalized, email_normalized, 'candidate_backfill',
               true, false, created_at, now()
        FROM candidates
        WHERE merged_at IS NULL AND email_normalized IS NOT NULL
          AND btrim(email_normalized) <> ''
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO candidate_contact_points (
            id, organization_id, candidate_id, channel, value,
            normalized_value, source, is_primary, is_opted_out,
            created_at, updated_at
        )
        SELECT gen_random_uuid(), organization_id, id, 'phone',
               phone_normalized, phone_normalized, 'candidate_backfill',
               true, false, created_at, now()
        FROM candidates
        WHERE merged_at IS NULL AND phone_normalized IS NOT NULL
          AND btrim(phone_normalized) <> ''
        ON CONFLICT DO NOTHING
        """
    )
    # Existing tenants can send immediately with zero credentials. New tenants
    # receive the same account lazily through the communication service.
    op.execute(
        """
        INSERT INTO communication_accounts (
            id, organization_id, channel, provider, name, configuration,
            is_active, created_at, updated_at
        )
        SELECT gen_random_uuid(), id, 'email', 'sandbox', 'Local sandbox',
               '{"mode":"local"}'::jsonb, true, now(), now()
        FROM organizations
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_communication_messages_provider", table_name="communication_messages")
    op.drop_index("ix_communication_messages_batch_status", table_name="communication_messages")
    op.drop_index("ix_communication_messages_due", table_name="communication_messages")
    op.drop_index("ix_communication_messages_thread_cursor", table_name="communication_messages")
    op.drop_table("communication_messages")
    op.drop_index("ix_communication_batches_org_created", table_name="communication_batches")
    op.drop_table("communication_batches")
    op.drop_index("ix_communication_threads_org_contact", table_name="communication_threads")
    op.drop_index("ix_communication_threads_candidate_last", table_name="communication_threads")
    op.drop_table("communication_threads")
    op.drop_index("ix_communication_templates_org_active", table_name="communication_templates")
    op.drop_index("uq_template_org_channel_name_ci", table_name="communication_templates")
    op.drop_table("communication_templates")
    op.drop_index("ix_communication_accounts_org_channel_active", table_name="communication_accounts")
    op.drop_index("uq_account_org_channel_name_ci", table_name="communication_accounts")
    op.drop_table("communication_accounts")
    op.drop_index("ix_contact_points_org_lookup", table_name="candidate_contact_points")
    op.drop_index("ix_contact_points_candidate_channel", table_name="candidate_contact_points")
    op.drop_table("candidate_contact_points")
