"""Tenant-bound specialist assessment invitations and reviewable evidence."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class ProviderAssessment(Base):
    """One application-bound invitation issued by a specialist provider.

    Candidate launch URLs are capability-bearing values, so they use the same
    purpose-bound AES-GCM storage as integration credentials and job payloads.
    Public operational summaries expose only whether a URL is available.
    """

    __tablename__ = "provider_assessments"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_template_versions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    invitation_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_jobs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_template_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_invitation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_attempt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="queued", server_default="queued"
    )
    launch_url_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    launch_url_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    launch_url_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_job_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_provider_assessments_org_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "connection_id"],
            ["integration_connections.organization_id", "integration_connections.id"],
            name="fk_provider_assessment_tenant_connection",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_provider_assessment_tenant_candidate",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('queued', 'invited', 'started', 'submitted', 'graded', "
            "'expired', 'cancelled', 'failed')",
            name="ck_provider_assessment_status",
        ),
        CheckConstraint(
            "((launch_url_ciphertext IS NULL AND launch_url_nonce IS NULL "
            "AND launch_url_key_id IS NULL) OR "
            "(launch_url_ciphertext IS NOT NULL AND launch_url_nonce IS NOT NULL "
            "AND launch_url_key_id IS NOT NULL))",
            name="ck_provider_assessment_launch_parts",
        ),
        Index(
            "uq_provider_assessment_external_invitation",
            "connection_id",
            "provider_invitation_id",
            unique=True,
            postgresql_where=text("provider_invitation_id IS NOT NULL"),
        ),
        Index(
            "uq_provider_assessment_external_attempt",
            "connection_id",
            "provider_attempt_id",
            unique=True,
            postgresql_where=text("provider_attempt_id IS NOT NULL"),
        ),
        Index(
            "ix_provider_assessments_org_application_created",
            "organization_id",
            "application_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )


class ProviderAssessmentResult(Base):
    """Latest normalized vendor result, retained as human-review evidence."""

    __tablename__ = "provider_assessment_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("provider_assessments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider_attempt_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(24), nullable=True)
    evidence_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    evidence_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    evidence_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provider_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "id", name="uq_provider_assessment_results_org_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "provider_assessment_id"],
            ["provider_assessments.organization_id", "provider_assessments.id"],
            name="fk_provider_assessment_result_tenant_assessment",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('submitted', 'graded')",
            name="ck_provider_assessment_result_status",
        ),
        CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100",
            name="ck_provider_assessment_result_score",
        ),
        CheckConstraint(
            "recommendation IS NULL OR recommendation IN "
            "('advance', 'review', 'do_not_advance')",
            name="ck_provider_assessment_result_recommendation",
        ),
        CheckConstraint("version >= 1", name="ck_provider_assessment_result_version"),
        Index(
            "ix_provider_assessment_results_org_completed",
            "organization_id",
            text("completed_at DESC"),
            text("id DESC"),
        ),
    )
