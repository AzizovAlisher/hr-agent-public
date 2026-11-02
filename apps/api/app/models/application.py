"""First-class candidate applications and append-only stage history."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy import event

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


APPLICATION_STAGES = ("new", "screening", "interview", "offer", "hired", "rejected")


class Application(Base):
    """The durable candidate x position aggregate.

    ``legacy_score_id`` keeps the historical Score contract usable while new
    code stops treating an optional AI score as the application itself.
    """

    __tablename__ = "applications"

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
    legacy_score_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scores.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", server_default="new", index=True
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual", server_default="manual"
    )
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    next_action_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cycle_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    application_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "candidate_id",
            "position_id",
            "cycle_number",
            name="uq_applications_org_candidate_position_cycle",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            "candidate_id",
            "position_id",
            name="uq_applications_exact_cycle_identity",
        ),
        Index(
            "uq_applications_one_active_cycle",
            "organization_id",
            "candidate_id",
            "position_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL AND archived_at IS NULL"),
        ),
        CheckConstraint(
            "stage IN ('new', 'screening', 'interview', 'offer', 'hired', 'rejected')",
            name="ck_applications_stage",
        ),
        CheckConstraint("cycle_number > 0", name="ck_applications_cycle_positive"),
        CheckConstraint(
            "archived_at IS NULL OR closed_at IS NOT NULL",
            name="ck_applications_archive_is_closed",
        ),
        CheckConstraint(
            "archived_at IS NOT NULL OR "
            "((stage IN ('hired', 'rejected')) = (closed_at IS NOT NULL))",
            name="ck_applications_terminal_is_closed",
        ),
        Index(
            "ix_applications_org_stage_created",
            "organization_id",
            "stage",
            "created_at",
            "id",
        ),
        Index(
            "ix_applications_org_owner_due",
            "organization_id",
            "owner_user_id",
            "next_action_due_date",
            "id",
        ),
    )


class ApplicationEvaluation(Base):
    """Immutable, versioned evidence and scores for one application cycle."""

    __tablename__ = "application_evaluations"

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
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_evaluations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    scores: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    provenance: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system", server_default="system"
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "kind",
            "version",
            name="uq_application_evaluations_kind_version",
        ),
        CheckConstraint(
            "kind IN ('ai_scoring', 'deterministic_scoring', 'manual')",
            name="ck_application_evaluations_kind",
        ),
        CheckConstraint(
            "status IN ('completed', 'reviewed', 'failed', 'voided')",
            name="ck_application_evaluations_status",
        ),
        CheckConstraint("version > 0", name="ck_application_evaluations_version"),
        CheckConstraint(
            "actor_kind IN ('human', 'ai', 'system', 'automation')",
            name="ck_application_evaluations_actor_kind",
        ),
        Index(
            "ix_application_evaluations_application_kind_version",
            "application_id",
            "kind",
            version.desc(),
        ),
        Index(
            "ix_application_evaluations_org_created",
            "organization_id",
            created_at.desc(),
            "id",
        ),
    )


class ApplicationStageEvent(Base):
    """Append-only audit record for every application stage transition."""

    __tablename__ = "application_stage_events"

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
    from_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system", server_default="system"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "from_stage IS NULL OR from_stage IN "
            "('new', 'screening', 'interview', 'offer', 'hired', 'rejected')",
            name="ck_application_stage_events_from_stage",
        ),
        CheckConstraint(
            "to_stage IN ('new', 'screening', 'interview', 'offer', 'hired', 'rejected')",
            name="ck_application_stage_events_to_stage",
        ),
        CheckConstraint(
            "actor_kind IN ('human', 'system', 'public', 'automation')",
            name="ck_application_stage_events_actor_kind",
        ),
        Index(
            "ix_application_stage_events_application_time",
            "application_id",
            "occurred_at",
            "id",
        ),
    )


@event.listens_for(Session, "before_flush")
def _protect_application_stage_events(session: Session, *_args: object) -> None:
    """Keep lifecycle history and evaluations append-only in ORM sessions."""

    changed = set(session.dirty).union(session.deleted)
    if any(isinstance(item, ApplicationStageEvent) for item in changed):
        raise ValueError("Application stage events are append-only")
    if any(isinstance(item, ApplicationEvaluation) for item in changed):
        raise ValueError("Application evaluations are append-only")
