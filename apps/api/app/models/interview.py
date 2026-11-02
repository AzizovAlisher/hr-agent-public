"""Interview-prep entity: AI-generated questions + recruiter rubric scoring.

A single Interview is scoped to one (candidate, position) pair. It owns an
ordered list of InterviewQuestion rows; the recruiter scores each on a 1–5
rubric and the overall score is the average.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class Interview(Base):
    __tablename__ = "interviews"

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
    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # draft -> in_progress -> scored -> archived
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Computed average of question scores (1-5). Null until any question scored.
    overall_score: Mapped[float | None] = mapped_column(nullable=True)

    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_kit_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    questions: Mapped[list["InterviewQuestion"]] = relationship(
        "InterviewQuestion",
        back_populates="interview",
        cascade="all, delete-orphan",
        order_by="InterviewQuestion.position",
    )
    assignments: Mapped[list["InterviewAssignment"]] = relationship(
        "InterviewAssignment",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    kit_versions: Mapped[list["InterviewKitVersion"]] = relationship(
        "InterviewKitVersion",
        # Version rows are immutable.  Parent deletion must flow through the
        # database FK so the append-only trigger can distinguish a nested
        # cascade from a direct application DELETE.
        cascade="save-update, merge",
        passive_deletes="all",
        order_by="InterviewKitVersion.version",
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            "application_id",
            "candidate_id",
            "position_id",
            name="uq_interview_application_identity",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_interview_exact_application",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "status IN ('draft', 'in_progress', 'scored', 'archived')",
            name="ck_interviews_status",
        ),
        CheckConstraint(
            "current_kit_version > 0", name="ck_interviews_kit_version_positive"
        ),
        Index(
            "ix_interviews_org_application_created",
            "organization_id",
            "application_id",
            "created_at",
        ),
    )


class InterviewAssignment(Base):
    """Explicit interviewer access to one interview, not merely its candidate."""

    __tablename__ = "interview_assignments"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    interview_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    interviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "interview_id",
            "interviewer_user_id",
            name="uq_interview_assignment_org_interview_user",
        ),
        Index(
            "ix_interview_assignments_org_user_interview",
            "organization_id",
            "interviewer_user_id",
            "interview_id",
        ),
    )


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    interview_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Stable display order
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # technical | behavioral | situational | culture
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_signals: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Recruiter rubric: 1–5, null until they grade. Notes are recruiter-only.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    interview: Mapped[Interview] = relationship("Interview", back_populates="questions")

    __table_args__ = (
        UniqueConstraint(
            "interview_id", "position", name="uq_interview_question_position"
        ),
        CheckConstraint(
            "score IS NULL OR score BETWEEN 1 AND 5",
            name="ck_interview_question_score",
        ),
    )


class InterviewKitVersion(Base):
    """Immutable snapshot of the exact question kit presented to reviewers."""

    __tablename__ = "interview_kit_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    interview_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    questions_snapshot: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    model_used_snapshot: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "interview_id", "version", name="uq_interview_kit_version"
        ),
        CheckConstraint("version > 0", name="ck_interview_kit_version_positive"),
        CheckConstraint(
            "jsonb_typeof(questions_snapshot) = 'array'",
            name="ck_interview_kit_questions_array",
        ),
    )


@event.listens_for(Session, "before_flush")
def _protect_interview_kit_versions(session: Session, *_args: object) -> None:
    """Kit definitions are append-only; feedback remains tied to reviewed text."""

    if any(isinstance(item, InterviewKitVersion) for item in session.dirty):
        raise ValueError("Interview kit versions are append-only")
    deleted_interview_ids = {
        item.id for item in session.deleted if isinstance(item, Interview)
    }
    if any(
        isinstance(item, InterviewKitVersion)
        and item.interview_id not in deleted_interview_ids
        for item in session.deleted
    ):
        raise ValueError("Interview kit versions are append-only")
