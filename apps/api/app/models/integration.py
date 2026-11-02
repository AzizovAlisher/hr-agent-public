"""Tenant-scoped provider connections and durable integration work.

Secrets and provider payloads are encrypted by the service layer before they
reach these tables.  JSON columns are intentionally limited to non-secret
configuration, references, and response summaries safe for operational views.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk

INTEGRATION_PROVIDERS = (
    "'google_workspace', 'microsoft_365', 'telegram', 'hh_ru', "
    "'somon', 'csv_xlsx', 'generic_webhook', 'assessment_generic'"
)
INTEGRATION_STATES = (
    "'unavailable', 'disconnected', 'connecting', 'connected', "
    "'degraded', 'expired', 'revoked'"
)
INTEGRATION_JOB_STATES = (
    "'queued', 'running', 'retrying', 'succeeded', 'dead_letter', 'cancelled'"
)


class IntegrationConnection(Base):
    __tablename__ = "integration_connections"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="disconnected",
        server_default="disconnected",
    )
    capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    configuration: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    granted_scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    # AES-GCM ciphertext and nonce.  credential_key_id lets operators rotate
    # wrapping keys without ever placing plaintext provider secrets in the DB.
    credential_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    credential_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    credential_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    credentials_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credential_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_health_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_integration_connections_org_id",
        ),
        CheckConstraint(
            f"provider IN ({INTEGRATION_PROVIDERS})",
            name="ck_integration_connection_provider",
        ),
        CheckConstraint(
            f"status IN ({INTEGRATION_STATES})",
            name="ck_integration_connection_status",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_integration_connection_failures",
        ),
        CheckConstraint(
            "((credential_ciphertext IS NULL AND credential_nonce IS NULL "
            "AND credential_key_id IS NULL) OR "
            "(credential_ciphertext IS NOT NULL AND credential_nonce IS NOT NULL "
            "AND credential_key_id IS NOT NULL))",
            name="ck_integration_connection_credential_parts",
        ),
        Index(
            "uq_integration_connection_org_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
        Index(
            "ix_integration_connections_org_provider_status",
            "organization_id",
            "provider",
            "status",
        ),
    )


class IntegrationOAuthState(Base):
    __tablename__ = "integration_oauth_states"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verifier_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    verifier_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(1_024), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        Index(
            "ix_integration_oauth_states_expiry",
            "expires_at",
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )


class IntegrationCursor(Base):
    __tablename__ = "integration_cursors"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cursor_name: Mapped[str] = mapped_column(String(96), nullable=False)
    value_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    value_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    value_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "cursor_name", name="uq_integration_cursor_name"
        ),
    )


class IntegrationJob(Base):
    __tablename__ = "integration_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    operation: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # References only: entity IDs and non-sensitive options, never credentials,
    # candidate contact values, CV text, or message bodies.
    reference_payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    payload_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    leased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    leased_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            f"status IN ({INTEGRATION_JOB_STATES})",
            name="ck_integration_job_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND max_attempts <= 10",
            name="ck_integration_job_attempts",
        ),
        CheckConstraint(
            "((payload_ciphertext IS NULL AND payload_nonce IS NULL AND payload_key_id IS NULL) "
            "OR (payload_ciphertext IS NOT NULL AND payload_nonce IS NOT NULL "
            "AND payload_key_id IS NOT NULL))",
            name="ck_integration_job_payload_parts",
        ),
        UniqueConstraint(
            "organization_id",
            "connection_id",
            "operation",
            "idempotency_key",
            name="uq_integration_job_idempotency",
        ),
        Index(
            "ix_integration_jobs_claim",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            postgresql_where=text("status IN ('queued', 'retrying', 'running')"),
        ),
        Index(
            "ix_integration_jobs_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class IntegrationWebhookEvent(Base):
    __tablename__ = "integration_webhook_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    envelope_version: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="accepted", server_default="accepted"
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "provider_assessment_id"],
            ["provider_assessments.organization_id", "provider_assessments.id"],
            name="fk_webhook_event_tenant_provider_assessment",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('accepted', 'processing', 'processed', 'failed', 'ignored')",
            name="ck_integration_webhook_event_status",
        ),
        UniqueConstraint(
            "connection_id", "event_id", name="uq_integration_webhook_event"
        ),
        Index(
            "ix_integration_webhook_events_org_received",
            "organization_id",
            text("received_at DESC"),
            text("id DESC"),
        ),
    )


class IntegrationWebhookDelivery(Base):
    __tablename__ = "integration_webhook_deliveries"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    envelope_version: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    target_url: Mapped[str] = mapped_column(String(2_048), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'delivering', 'retrying', 'delivered', 'dead_letter', 'cancelled')",
            name="ck_integration_webhook_delivery_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND max_attempts <= 20",
            name="ck_integration_webhook_delivery_attempts",
        ),
        UniqueConstraint(
            "connection_id", "event_id", name="uq_integration_webhook_delivery"
        ),
        Index(
            "ix_integration_webhook_deliveries_due",
            "status",
            "next_attempt_at",
            postgresql_where=text("status IN ('queued', 'retrying')"),
        ),
        Index(
            "ix_integration_webhook_deliveries_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class IntegrationWebhookDeliveryAttempt(Base):
    __tablename__ = "integration_webhook_delivery_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_webhook_deliveries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "attempt_number >= 1", name="ck_integration_webhook_attempt_number"
        ),
        CheckConstraint(
            "status IN ('delivering', 'delivered', 'retrying', 'failed')",
            name="ck_integration_webhook_attempt_status",
        ),
        UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_integration_webhook_delivery_attempt",
        ),
        Index(
            "ix_integration_webhook_attempts_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )
