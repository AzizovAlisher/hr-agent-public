"""Contract entitlements, append-only usage, and worker liveness records."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class OrganizationEntitlement(Base):
    """Operator-managed contract limit for one organization metric.

    ``limit_value`` deliberately keeps three distinct states: NULL is
    unlimited, zero is blocked, and a positive value is a hard cap.
    """

    __tablename__ = "organization_entitlements"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    limit_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    period: Mapped[str] = mapped_column(
        String(16), nullable=False, default="contract", server_default="contract"
    )
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "limit_value IS NULL OR limit_value >= 0",
            name="ck_organization_entitlement_nonnegative",
        ),
        CheckConstraint(
            "period IN ('contract', 'month', 'day')",
            name="ck_organization_entitlement_period",
        ),
        CheckConstraint(
            "effective_until IS NULL OR effective_from IS NULL "
            "OR effective_until > effective_from",
            name="ck_organization_entitlement_window",
        ),
        UniqueConstraint(
            "organization_id", "metric", name="uq_organization_entitlement_metric"
        ),
    )


class UsageLedgerEntry(Base):
    """Immutable, idempotent usage delta in the metric's integer base unit."""

    __tablename__ = "usage_ledger_entries"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('reserve', 'usage', 'settle', 'refund', 'adjustment')",
            name="ck_usage_ledger_kind",
        ),
        UniqueConstraint(
            "organization_id",
            "metric",
            "idempotency_key",
            name="uq_usage_ledger_idempotency",
        ),
        Index(
            "ix_usage_ledger_org_metric_time",
            "organization_id",
            "metric",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
    )


class WorkerHeartbeat(Base):
    """One row per worker replica, refreshed by the dedicated worker process."""

    __tablename__ = "worker_heartbeats"

    id: Mapped[uuid.UUID] = uuid_pk()
    worker_name: Mapped[str] = mapped_column(String(64), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    process_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        Index(
            "ix_worker_heartbeats_name_seen",
            "worker_name",
            text("last_seen_at DESC"),
        ),
    )


class WorkerQueueLease(Base):
    """Cross-replica lease identity; queue payloads remain in tenant tables."""

    __tablename__ = "worker_queue_leases"

    queue_name: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    lease_token: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    leased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "queue_name", "lease_token", name="uq_worker_queue_lease_token"
        ),
        Index(
            "ix_worker_queue_leases_queue_expiry",
            "queue_name",
            "lease_expires_at",
        ),
        Index(
            "ix_worker_queue_leases_org_queue",
            "organization_id",
            "queue_name",
        ),
    )
