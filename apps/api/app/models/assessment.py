"""Native, versioned candidate assessments and deterministic results."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk

QUESTION_TYPES = (
    "boolean",
    "single",
    "multi",
    "number",
    "short",
    "long",
    "file",
    "url",
)


class AssessmentTemplate(Base):
    __tablename__ = "assessment_templates"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_assessment_templates_status"),
        Index(
            "uq_assessment_templates_org_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
    )


class AssessmentTemplateVersion(Base):
    __tablename__ = "assessment_template_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    passing_percentage: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_assessment_template_version"),
        CheckConstraint("version >= 1", name="ck_assessment_template_version_number"),
        CheckConstraint(
            "passing_percentage BETWEEN 0 AND 100",
            name="ck_assessment_version_passing_percentage",
        ),
        CheckConstraint(
            "time_limit_minutes IS NULL OR time_limit_minutes BETWEEN 1 AND 10080",
            name="ck_assessment_version_time_limit",
        ),
    )


class AssessmentSection(Base):
    __tablename__ = "assessment_sections"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_template_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("version_id", "position", name="uq_assessment_section_position"),
        CheckConstraint("position >= 0", name="ck_assessment_section_position"),
    )


class AssessmentQuestion(Base):
    __tablename__ = "assessment_questions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_template_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_sections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    question_type: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    options: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    validation: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    condition: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    scoring: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    knockout: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    max_score: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("version_id", "key", name="uq_assessment_question_key"),
        UniqueConstraint(
            "section_id", "position", name="uq_assessment_question_section_position"
        ),
        CheckConstraint(
            "question_type IN ('boolean', 'single', 'multi', 'number', 'short', "
            "'long', 'file', 'url')",
            name="ck_assessment_question_type",
        ),
        CheckConstraint("position >= 0", name="ck_assessment_question_position"),
        CheckConstraint("max_score >= 0", name="ck_assessment_question_max_score"),
    )


class AssessmentInvitation(Base):
    __tablename__ = "assessment_invitations"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_template_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    authorized_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    accommodations: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    first_opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'revoked', 'completed', 'expired')",
            name="ck_assessment_invitation_status",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 20", name="ck_assessment_invitation_max_attempts"
        ),
        CheckConstraint(
            "authorized_attempts BETWEEN 1 AND max_attempts",
            name="ck_assessment_invitation_authorized_attempts",
        ),
        Index(
            "ix_assessment_invitations_org_application_created",
            "organization_id",
            "application_id",
            "created_at",
        ),
    )


class AssessmentAttempt(Base):
    __tablename__ = "assessment_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invitation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_invitations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_template_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress", server_default="in_progress"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_saved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint("invitation_id", "sequence", name="uq_assessment_attempt_sequence"),
        CheckConstraint("sequence >= 1", name="ck_assessment_attempt_sequence"),
        CheckConstraint(
            "status IN ('in_progress', 'submitted', 'expired')",
            name="ck_assessment_attempt_status",
        ),
        Index(
            "uq_assessment_attempt_invitation_active",
            "invitation_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
    )


class AssessmentAnswer(Base):
    __tablename__ = "assessment_answers"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_assessment_answer_question"),
    )


class AssessmentFile(Base):
    """Opaque private upload bound to one invitation attempt and question."""

    __tablename__ = "assessment_files"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invitation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_invitations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_questions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    private_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_objects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="quarantined", server_default="quarantined"
    )
    scan_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_assessment_files_tenant_private_object",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('quarantined', 'ready', 'rejected', 'deleted')",
            name="ck_assessment_files_status",
        ),
        CheckConstraint(
            "size_bytes BETWEEN 1 AND 15728640",
            name="ck_assessment_files_size",
        ),
        Index(
            "ix_assessment_files_attempt_question",
            "attempt_id",
            "question_id",
            "created_at",
        ),
    )


class AssessmentResult(Base):
    __tablename__ = "assessment_results"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    raw_score: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    max_score: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    percentage: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    knocked_out: Mapped[bool] = mapped_column(Boolean, nullable=False)
    knockout_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_questions.id", ondelete="SET NULL"),
        nullable=True,
    )
    knockout_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The rule match is immutable evidence.  Human adjudication is recorded
    # separately so clearing a false positive never erases what matched.
    knockout_review: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    breakdown: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    graded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint("raw_score >= 0", name="ck_assessment_result_raw_score"),
        CheckConstraint("max_score >= 0", name="ck_assessment_result_max_score"),
        CheckConstraint(
            "percentage BETWEEN 0 AND 100", name="ck_assessment_result_percentage"
        ),
        CheckConstraint(
            "knockout_review IS NULL OR ("
            "knocked_out AND "
            "jsonb_typeof(knockout_review) = 'object' AND "
            "knockout_review ?& ARRAY['outcome', 'reason', "
            "'reviewed_by_user_id', 'reviewed_at'] AND "
            "knockout_review->>'outcome' IN ('confirmed', 'cleared') AND "
            "jsonb_typeof(knockout_review->'reason') = 'string' AND "
            "jsonb_typeof(knockout_review->'reviewed_by_user_id') = 'string' AND "
            "jsonb_typeof(knockout_review->'reviewed_at') = 'string'"
            ")",
            name="ck_assessment_result_knockout_review_valid",
        ),
    )


@event.listens_for(Session, "before_flush")
def _protect_published_assessment_versions(session: Session, *_args: object) -> None:
    """Mirror the migration's immutability guard for metadata-created test DBs."""

    immutable_types = (AssessmentTemplateVersion, AssessmentSection, AssessmentQuestion)
    changed = set(session.dirty).union(session.deleted)
    if any(isinstance(item, immutable_types) for item in changed):
        raise ValueError("Published assessment versions are immutable")
