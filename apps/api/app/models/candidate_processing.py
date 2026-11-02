"""Durable, tenant-scoped CV parsing and candidate scoring work."""

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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk

CANDIDATE_PROCESSING_KINDS = ("parse_cv", "score")
CANDIDATE_PROCESSING_STATES = (
    "queued",
    "running",
    "retrying",
    "succeeded",
    "dead_letter",
    "cancelled",
)


class CandidateProcessingJob(Base):
    """Reference-only work item; CV text and prompts never enter the queue row."""

    __tablename__ = "candidate_processing_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    leased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    leased_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            "kind IN ('parse_cv', 'score')",
            name="ck_candidate_processing_job_kind",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'succeeded', "
            "'dead_letter', 'cancelled')",
            name="ck_candidate_processing_job_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts "
            "AND max_attempts BETWEEN 1 AND 10",
            name="ck_candidate_processing_job_attempts",
        ),
        CheckConstraint(
            "(kind = 'score' AND position_id IS NOT NULL) OR "
            "(kind = 'parse_cv' AND position_id IS NULL)",
            name="ck_candidate_processing_job_target",
        ),
        CheckConstraint(
            "(kind = 'score' AND application_id IS NOT NULL) OR "
            "(kind = 'parse_cv' AND application_id IS NULL)",
            name="ck_candidate_processing_job_application_target",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_candidate_processing_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_candidate_processing_job_cancelled_at",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'dead_letter', 'cancelled') "
            "OR completed_at IS NOT NULL",
            name="ck_candidate_processing_job_terminal_at",
        ),
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_candidate_processing_job_idempotency",
        ),
        Index(
            "ix_candidate_processing_jobs_claimable",
            "status",
            "available_at",
            "lease_expires_at",
            "created_at",
            "id",
            postgresql_where=text("status IN ('queued', 'retrying', 'running')"),
        ),
        Index(
            "ix_candidate_processing_jobs_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "uq_candidate_processing_active_parse",
            "organization_id",
            "candidate_id",
            unique=True,
            postgresql_where=text(
                "kind = 'parse_cv' AND status IN ('queued', 'running', 'retrying')"
            ),
        ),
        Index(
            "uq_candidate_processing_active_score",
            "organization_id",
            "application_id",
            unique=True,
            postgresql_where=text(
                "kind = 'score' AND status IN ('queued', 'running', 'retrying')"
            ),
        ),
        Index(
            "ix_candidate_processing_org_application",
            "organization_id",
            "application_id",
        ),
    )
