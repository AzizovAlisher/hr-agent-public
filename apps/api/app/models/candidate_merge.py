"""Encrypted, tenant-bound audit records for reversible candidate merges."""

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
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.models._mixins import created_at, uuid_pk


class CandidateMergeBatch(Base):
    """One merge and its authenticated, encrypted reversal contract.

    The encrypted document contains both the pre-merge patch and the exact
    post-merge integrity guard.  Keeping those values out of ordinary JSON
    columns prevents contact/CV material from entering operational queries or
    logs.  A batch permits exactly one ``merged`` -> ``reversed`` transition.
    """

    __tablename__ = "candidate_merge_batches"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    primary_candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reversed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="merged", server_default="merged"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    snapshot_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    snapshot_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    snapshot_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    post_state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reversed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_candidate_merge_batches_org_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "primary_candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_merge_batch_tenant_primary",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('merged', 'reversed')",
            name="ck_candidate_merge_batch_status",
        ),
        CheckConstraint(
            "snapshot_version = 1",
            name="ck_candidate_merge_batch_snapshot_version",
        ),
        CheckConstraint(
            "member_count >= 2",
            name="ck_candidate_merge_batch_member_count",
        ),
        CheckConstraint(
            "octet_length(snapshot_nonce) = 12",
            name="ck_candidate_merge_batch_nonce_size",
        ),
        CheckConstraint(
            "octet_length(snapshot_ciphertext) > 16",
            name="ck_candidate_merge_batch_ciphertext_size",
        ),
        CheckConstraint(
            "post_state_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_merge_batch_digest",
        ),
        CheckConstraint(
            "(status = 'merged' AND version = 1 AND reversed_at IS NULL) OR "
            "(status = 'reversed' AND version = 2 AND reversed_at IS NOT NULL)",
            name="ck_candidate_merge_batch_lifecycle",
        ),
        Index(
            "ix_candidate_merge_batches_primary_created",
            "organization_id",
            "primary_candidate_id",
            text("created_at DESC"),
            "id",
        ),
        Index(
            "ix_candidate_merge_batches_org_status_created",
            "organization_id",
            "status",
            text("created_at DESC"),
            "id",
        ),
    )


class CandidateMergeBatchMember(Base):
    """Candidate membership used for tenant integrity and erasure fan-out."""

    __tablename__ = "candidate_merge_batch_members"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_candidate_merge_batch_members_org_id",
        ),
        UniqueConstraint(
            "batch_id",
            "candidate_id",
            name="uq_candidate_merge_batch_member_candidate",
        ),
        ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["candidate_merge_batches.organization_id", "candidate_merge_batches.id"],
            name="fk_candidate_merge_member_tenant_batch",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_merge_member_tenant_candidate",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "role IN ('primary', 'alias')",
            name="ck_candidate_merge_batch_member_role",
        ),
        Index(
            "uq_candidate_merge_batch_one_primary",
            "batch_id",
            unique=True,
            postgresql_where=text("role = 'primary'"),
        ),
        Index(
            "ix_candidate_merge_batch_members_candidate",
            "organization_id",
            "candidate_id",
            "batch_id",
        ),
    )


@event.listens_for(Session, "before_flush")
def _protect_candidate_merge_audit(session: Session, *_args: object) -> None:
    """Mirror the database append-only contract in metadata-created tests."""

    if any(
        isinstance(item, CandidateMergeBatchMember)
        for item in set(session.dirty).union(session.deleted)
    ):
        raise ValueError("Candidate merge members are append-only")
    if any(isinstance(item, CandidateMergeBatch) for item in session.deleted):
        raise ValueError("Candidate merge batches are append-only")
    for item in session.dirty:
        if not isinstance(item, CandidateMergeBatch):
            continue
        state = inspect(item)
        changed = {
            attribute.key
            for attribute in state.attrs
            if attribute.history.has_changes()
        }
        if not changed.issubset(
            {"status", "version", "reversed_at", "reversed_by_user_id"}
        ):
            raise ValueError("Candidate merge batches are append-only")
        status_history = state.attrs.status.history
        version_history = state.attrs.version.history
        old_status = status_history.deleted[0] if status_history.deleted else "merged"
        old_version = version_history.deleted[0] if version_history.deleted else 1
        if (
            old_status != "merged"
            or item.status != "reversed"
            or old_version != 1
            or item.version != 2
            or item.reversed_at is None
        ):
            raise ValueError("Candidate merge reversal is a one-time transition")
