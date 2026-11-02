"""Tenant-owned private object registry and durable deletion outbox."""

import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class PrivateObject(Base):
    """Authoritative metadata for one object in a tenant-private bucket prefix."""

    __tablename__ = "private_objects"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="quarantined", server_default="quarantined"
    )
    scanner_engine: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scanner_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scanner_verdict: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    legal_hold_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "category IN ('candidates', 'assessments', 'tasks', 'ai', 'recordings')",
            name="ck_private_objects_category",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 1 AND 15728640",
            name="ck_private_objects_size",
        ),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_private_objects_sha256",
        ),
        CheckConstraint(
            "state IN ('quarantined', 'ready', 'rejected', "
            "'delete_pending', 'deleted')",
            name="ck_private_objects_state",
        ),
        CheckConstraint(
            "object_key ~ ('^private/' || organization_id::text || '/' || "
            "category || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-' || "
            "'[0-9a-f]{4}-[0-9a-f]{12}\\.[a-z0-9]{1,16}$')",
            name="ck_private_objects_key_scope",
        ),
        CheckConstraint(
            "(state <> 'ready' OR ready_at IS NOT NULL) AND "
            "(state <> 'rejected' OR rejected_at IS NOT NULL) AND "
            "(state <> 'deleted' OR deleted_at IS NOT NULL)",
            name="ck_private_objects_lifecycle_timestamps",
        ),
        UniqueConstraint("organization_id", "id", name="uq_private_objects_org_id"),
        UniqueConstraint(
            "organization_id",
            "object_key",
            name="uq_private_objects_org_object_key",
        ),
        Index(
            "ix_private_objects_org_resource",
            "organization_id",
            "resource_type",
            "resource_id",
        ),
        Index(
            "ix_private_objects_retention",
            "state",
            "retention_until",
            postgresql_where=text("state IN ('ready', 'delete_pending')"),
        ),
    )


class PrivateObjectDeletion(Base):
    """Transactional outbox for idempotent deletion from object storage."""

    __tablename__ = "private_object_deletions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    private_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    object_key_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    sha256_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_private_object_deletions_tenant_object",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "sha256_snapshot ~ '^[0-9a-f]{64}$'",
            name="ck_private_object_deletions_sha256",
        ),
        CheckConstraint(
            "reason IN ('candidate_erasure', 'retention_expired', "
            "'resource_deleted', 'upload_rejected', 'replacement', 'admin_request')",
            name="ck_private_object_deletions_reason",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 10",
            name="ck_private_object_deletions_attempts",
        ),
        CheckConstraint(
            "(claimed_at IS NULL) = (claim_token IS NULL)",
            name="ck_private_object_deletions_claim_tuple",
        ),
        Index(
            "uq_private_object_deletions_active",
            "organization_id",
            "private_object_id",
            unique=True,
            postgresql_where=text("completed_at IS NULL"),
        ),
        Index(
            "ix_private_object_deletions_claim",
            "completed_at",
            "next_attempt_at",
            "id",
        ),
    )


class LegacyPrivateObjectDeletion(Base):
    """Durable erasure for verified files created before the object registry."""

    __tablename__ = "legacy_private_object_deletions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_key: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "(category = 'candidates' AND resource_type = 'candidate') OR "
            "(category = 'assessments' AND resource_type = 'assessment_file') OR "
            "(category = 'tasks' AND resource_type = 'task_submission') OR "
            "(category = 'recordings' AND resource_type = 'interview_recording')",
            name="ck_legacy_private_object_deletions_resource_category",
        ),
        CheckConstraint(
            "length(normalized_key) <= 512 AND ("
            "(category = 'candidates' AND normalized_key ~ "
            "'^cvs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{12}\\.(pdf|docx)$') OR "
            "(category = 'assessments' AND normalized_key ~ "
            "('^assessment_files/' || organization_id::text || "
            "'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{12}\\.(pdf|docx|txt|png|jpg|jpeg)$')) OR "
            "(category = 'tasks' AND normalized_key ~ "
            "('^task_files/' || organization_id::text || "
            "'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{12}\\.(pdf|docx|txt|md|csv)$')) OR "
            "(category = 'recordings' AND normalized_key ~ "
            "('^recordings/' || organization_id::text || "
            "'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
            "[0-9a-f]{12}\\.(m4a|mp3|mp4|ogg|wav|webm)$'))) ",
            name="ck_legacy_private_object_deletions_key_scope",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 1 AND 15728640",
            name="ck_legacy_private_object_deletions_size",
        ),
        CheckConstraint(
            "sha256_snapshot ~ '^[0-9a-f]{64}$'",
            name="ck_legacy_private_object_deletions_sha256",
        ),
        CheckConstraint(
            "reason IN ('candidate_erasure', 'retention_expired', "
            "'resource_deleted', 'replacement', 'admin_request')",
            name="ck_legacy_private_object_deletions_reason",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 10",
            name="ck_legacy_private_object_deletions_attempts",
        ),
        CheckConstraint(
            "(claimed_at IS NULL) = (claim_token IS NULL)",
            name="ck_legacy_private_object_deletions_claim_tuple",
        ),
        UniqueConstraint(
            "normalized_key",
            name="uq_legacy_private_object_deletions_key",
        ),
        Index(
            "ix_legacy_private_object_deletions_claim",
            "completed_at",
            "next_attempt_at",
            "id",
        ),
        Index(
            "ix_legacy_private_object_deletions_org_resource",
            "organization_id",
            "resource_type",
            "resource_id",
        ),
    )


class PrivateStorageReconciliationCursor(Base):
    """Tenant-local lexical progress through every bounded reconciliation scan."""

    __tablename__ = "private_storage_reconciliation_cursors"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    namespace: Mapped[str] = mapped_column(String(16), primary_key=True)
    cursor_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    cycle_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "namespace IN ('registry', 'private', 'quarantine')",
            name="ck_private_storage_reconciliation_cursor_namespace",
        ),
        CheckConstraint(
            "cycle_count >= 0",
            name="ck_private_storage_reconciliation_cursor_cycles",
        ),
        CheckConstraint(
            "cursor_key IS NULL OR (length(cursor_key) <= 1024 AND ("
            "(namespace IN ('registry', 'private') AND cursor_key LIKE "
            "('private/' || organization_id::text || '/%')) OR "
            "(namespace = 'quarantine' AND cursor_key LIKE "
            "('quarantine/' || organization_id::text || '/%'))))",
            name="ck_private_storage_reconciliation_cursor_key_scope",
        ),
    )
