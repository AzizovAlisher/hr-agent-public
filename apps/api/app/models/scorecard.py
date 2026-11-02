"""Structured, independent hiring-team scorecards and decisions."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class ScorecardTemplate(Base):
    __tablename__ = "scorecard_templates"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    criteria: Mapped[list["ScorecardCriterion"]] = relationship(
        "ScorecardCriterion",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="ScorecardCriterion.position",
    )
    versions: Mapped[list["ScorecardTemplateVersion"]] = relationship(
        "ScorecardTemplateVersion",
        # Immutable snapshots are removed only by the parent's database-level
        # ON DELETE CASCADE.  Direct ORM deletes remain blocked by both the
        # session and PostgreSQL append-only guards.
        cascade="save-update, merge",
        passive_deletes="all",
        order_by="ScorecardTemplateVersion.version",
    )

    __table_args__ = (
        Index(
            "uq_scorecard_template_org_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
        CheckConstraint(
            "current_version > 0", name="ck_scorecard_template_version_positive"
        ),
    )


class ScorecardCriterion(Base):
    __tablename__ = "scorecard_criteria"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = created_at()

    template: Mapped[ScorecardTemplate] = relationship(
        "ScorecardTemplate", back_populates="criteria"
    )

    __table_args__ = (
        UniqueConstraint("template_id", "key", name="uq_scorecard_criterion_key"),
        UniqueConstraint(
            "template_id", "position", name="uq_scorecard_criterion_position"
        ),
        CheckConstraint("weight BETWEEN 1 AND 100", name="ck_scorecard_weight"),
    )


class ScorecardTemplateVersion(Base):
    """Append-only definition of a scorecard template revision."""

    __tablename__ = "scorecard_template_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    description_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_id_snapshot: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    criteria_snapshot: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "template_id", "version", name="uq_scorecard_template_version"
        ),
        CheckConstraint("version > 0", name="ck_scorecard_version_positive"),
        CheckConstraint(
            "jsonb_typeof(criteria_snapshot) = 'array'",
            name="ck_scorecard_version_criteria_array",
        ),
    )


class ScorecardAssignment(Base):
    __tablename__ = "scorecard_assignments"

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
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "scorecard_template_versions.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
        index=True,
    )
    assigned_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    template_version_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    criteria_snapshot: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    blind_until_all_submitted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    position_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    assignee_email_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    ratings: Mapped[list["ScorecardRating"]] = relationship(
        "ScorecardRating",
        back_populates="assignment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'submitted')", name="ck_scorecard_assignment_status"
        ),
        CheckConstraint(
            "template_version_snapshot > 0",
            name="ck_scorecard_assignment_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(criteria_snapshot) = 'array'",
            name="ck_scorecard_assignment_criteria_array",
        ),
        Index(
            "ix_scorecard_assignments_org_assignee_due",
            "organization_id",
            "assigned_to_user_id",
            "status",
            "due_at",
        ),
    )


class ScorecardRating(Base):
    __tablename__ = "scorecard_ratings"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_assignments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    criterion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scorecard_criteria.id", ondelete="SET NULL"),
        nullable=True,
    )
    criterion_key_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    criterion_label_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    criterion_weight_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    assignment: Mapped[ScorecardAssignment] = relationship(
        "ScorecardAssignment", back_populates="ratings"
    )

    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "criterion_key_snapshot", name="uq_scorecard_rating_key"
        ),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_scorecard_rating_value"),
        CheckConstraint(
            "criterion_weight_snapshot BETWEEN 1 AND 100",
            name="ck_scorecard_rating_weight",
        ),
    )


class HiringDecision(Base):
    __tablename__ = "hiring_decisions"

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
        ForeignKey("positions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    position_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_email_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    approval_cycle: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    required_approvals: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    approvals: Mapped[list["HiringDecisionApproval"]] = relationship(
        "HiringDecisionApproval",
        back_populates="decision",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "recommendation IN ('advance', 'hold', 'reject', 'hire')",
            name="ck_hiring_decision_recommendation",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_hiring_decision_status",
        ),
        CheckConstraint(
            "required_approvals BETWEEN 1 AND 20",
            name="ck_hiring_decision_required_approvals",
        ),
        CheckConstraint("version > 0", name="ck_hiring_decision_version_positive"),
        CheckConstraint(
            "approval_cycle > 0", name="ck_hiring_decision_cycle_positive"
        ),
    )


class HiringDecisionApproval(Base):
    __tablename__ = "hiring_decision_approvals"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hiring_decisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_email_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    decision_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    vote: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    decision: Mapped[HiringDecision] = relationship(
        "HiringDecision", back_populates="approvals"
    )

    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            "approval_cycle",
            "user_id",
            name="uq_hiring_decision_vote_cycle",
        ),
        CheckConstraint(
            "vote IN ('approve', 'reject')", name="ck_hiring_decision_vote"
        ),
        CheckConstraint(
            "decision_version_snapshot > 0",
            name="ck_hiring_decision_approval_version_positive",
        ),
        CheckConstraint(
            "approval_cycle > 0",
            name="ck_hiring_decision_approval_cycle_positive",
        ),
    )


@event.listens_for(Session, "before_flush")
def _protect_scorecard_history(session: Session, *_args: object) -> None:
    """Template versions and recorded approval votes are append-only."""

    immutable = (ScorecardTemplateVersion, HiringDecisionApproval)
    if any(isinstance(item, immutable) for item in session.dirty):
        raise ValueError("Scorecard versions and decision approvals are append-only")
    deleted_template_ids = {
        item.id for item in session.deleted if isinstance(item, ScorecardTemplate)
    }
    deleted_decision_ids = {
        item.id for item in session.deleted if isinstance(item, HiringDecision)
    }
    for item in session.deleted:
        if isinstance(item, ScorecardTemplateVersion):
            if item.template_id not in deleted_template_ids:
                raise ValueError("Scorecard template versions are append-only")
        elif isinstance(item, HiringDecisionApproval):
            if item.decision_id not in deleted_decision_ids:
                raise ValueError("Decision approvals are append-only")
