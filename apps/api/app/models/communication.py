"""Durable candidate communication records.

Message bodies and contact values intentionally live in these access-controlled
domain tables, never in the append-only candidate activity payloads.
"""

import uuid
from datetime import datetime

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk

CONTACT_CHANNELS = "'email', 'phone', 'telegram', 'whatsapp'"
MESSAGE_CHANNELS = "'email', 'telegram', 'whatsapp'"


class CandidateContactPoint(Base):
    __tablename__ = "candidate_contact_points"

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
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual"
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_opted_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    opted_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            f"channel IN ({CONTACT_CHANNELS})", name="ck_contact_point_channel"
        ),
        UniqueConstraint(
            "organization_id",
            "candidate_id",
            "channel",
            "normalized_value",
            name="uq_contact_point_candidate_channel_value",
        ),
        UniqueConstraint(
            "organization_id",
            "candidate_id",
            "id",
            name="uq_contact_point_candidate_identity",
        ),
        Index(
            "ix_contact_points_candidate_channel",
            "candidate_id",
            "channel",
            "is_primary",
        ),
        Index(
            "ix_contact_points_org_lookup",
            "organization_id",
            "channel",
            "normalized_value",
        ),
    )


class CommunicationAccount(Base):
    __tablename__ = "communication_accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    integration_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sender_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Only non-secret sender settings belong here. Provider credentials live
    # on the encrypted tenant IntegrationConnection referenced above.
    configuration: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    webhook_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            f"channel IN ({MESSAGE_CHANNELS})", name="ck_communication_account_channel"
        ),
        CheckConstraint(
            "provider IN ('sandbox', 'google_workspace', 'microsoft_365', "
            "'telegram', 'smtp', 'whatsapp')",
            name="ck_communication_account_provider",
        ),
        CheckConstraint(
            "(provider = 'sandbox' AND integration_connection_id IS NULL) OR "
            "(provider IN ('google_workspace', 'microsoft_365') "
            "AND integration_connection_id IS NOT NULL) OR "
            "(provider = 'telegram' AND "
            "(integration_connection_id IS NOT NULL OR is_active IS FALSE)) OR "
            "(provider IN ('smtp', 'whatsapp') AND "
            "integration_connection_id IS NULL AND is_active IS FALSE)",
            name="ck_communication_account_connection",
        ),
        ForeignKeyConstraint(
            ["organization_id", "integration_connection_id"],
            [
                "integration_connections.organization_id",
                "integration_connections.id",
            ],
            name="fk_communication_account_tenant_connection",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_account_org_channel_name_ci",
            "organization_id",
            "channel",
            text("lower(name)"),
            unique=True,
        ),
        Index(
            "ix_communication_accounts_org_channel_active",
            "organization_id",
            "channel",
            "is_active",
        ),
        Index(
            "ix_communication_accounts_connection",
            "organization_id",
            "integration_connection_id",
        ),
    )


class CommunicationTemplate(Base):
    __tablename__ = "communication_templates"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str] = mapped_column(
        String(8), nullable=False, default="ru", server_default="ru"
    )
    subject_template: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            f"channel IN ({MESSAGE_CHANNELS})", name="ck_communication_template_channel"
        ),
        Index(
            "uq_template_org_channel_name_ci",
            "organization_id",
            "channel",
            text("lower(name)"),
            unique=True,
        ),
        Index(
            "ix_communication_templates_org_active",
            "organization_id",
            "channel",
            "is_active",
        ),
    )


class CommunicationThread(Base):
    __tablename__ = "communication_threads"

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
    contact_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_contact_points.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    unread_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            f"channel IN ({MESSAGE_CHANNELS})", name="ck_communication_thread_channel"
        ),
        CheckConstraint(
            "status IN ('open', 'closed')", name="ck_communication_thread_status"
        ),
        CheckConstraint(
            "(position_id IS NULL) = (application_id IS NULL)",
            name="ck_communication_thread_application_target",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            "candidate_id",
            name="uq_communication_thread_candidate_identity",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_communication_thread_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id", "contact_point_id"],
            [
                "candidate_contact_points.organization_id",
                "candidate_contact_points.candidate_id",
                "candidate_contact_points.id",
            ],
            name="fk_communication_thread_contact_owner",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_communication_threads_candidate_last",
            "candidate_id",
            text("last_message_at DESC NULLS LAST"),
            "id",
        ),
        Index(
            "ix_communication_threads_org_contact",
            "organization_id",
            "candidate_id",
            "channel",
            "contact_point_id",
        ),
        Index(
            "ix_communication_threads_org_application",
            "organization_id",
            "application_id",
        ),
    )


class CommunicationBatch(Base):
    __tablename__ = "communication_batches"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skip_summary: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            f"channel IN ({MESSAGE_CHANNELS})", name="ck_communication_batch_channel"
        ),
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'partial', 'failed', 'cancelled')",
            name="ck_communication_batch_status",
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_batch_org_idempotency"
        ),
        Index(
            "ix_communication_batches_org_created",
            "organization_id",
            text("created_at DESC"),
            "id",
        ),
    )


class CommunicationMessage(Base):
    __tablename__ = "communication_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    contact_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_contact_points.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Public sandbox callbacks use the same signed, versioned envelope as the
    # generic integration contract. Keeping the event identifier and exact raw
    # payload fingerprint makes retries inspectable and lets us reject an event
    # ID that is replayed with different content.
    webhook_event_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    webhook_payload_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_tenant_communication_messages_717c3bac",
        ),
        ForeignKeyConstraint(
            ["organization_id", "thread_id", "candidate_id"],
            [
                "communication_threads.organization_id",
                "communication_threads.id",
                "communication_threads.candidate_id",
            ],
            name="fk_communication_message_thread_owner",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id", "contact_point_id"],
            [
                "candidate_contact_points.organization_id",
                "candidate_contact_points.candidate_id",
                "candidate_contact_points.id",
            ],
            name="fk_communication_message_contact_owner",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            f"channel IN ({MESSAGE_CHANNELS})", name="ck_communication_message_channel"
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_communication_message_direction",
        ),
        CheckConstraint(
            "status IN ('queued', 'processing', 'sent', 'delivered', 'received', 'failed', 'cancelled')",
            name="ck_communication_message_status",
        ),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_message_org_idempotency"
        ),
        Index(
            "ix_communication_messages_thread_cursor",
            "thread_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_communication_messages_due",
            "status",
            "scheduled_for",
            "claimed_at",
            postgresql_where=text(
                "direction = 'outbound' AND status IN ('queued', 'processing')"
            ),
        ),
        Index("ix_communication_messages_batch_status", "batch_id", "status"),
        Index(
            "ix_communication_messages_provider",
            "account_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index(
            "uq_communication_messages_webhook_event",
            "account_id",
            "webhook_event_id",
            unique=True,
            postgresql_where=text("webhook_event_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(webhook_event_id IS NULL) = (webhook_payload_sha256 IS NULL)",
            name="ck_communication_message_webhook_fingerprint_pair",
        ),
    )
