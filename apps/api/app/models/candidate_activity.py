"""Append-only candidate activity events.

The activity stream is deliberately separate from the domain records it
describes. Child records may be deleted or replaced while their safe,
non-sensitive history remains available to the recruiting team.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, uuid_pk


class CandidateActivity(Base):
    __tablename__ = "candidate_activities"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system", server_default="system"
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_agent_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Intentionally not a foreign key: activities survive child-record deletion.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        CheckConstraint(
            "actor_kind IN ('human', 'ai', 'system', 'public')",
            name="ck_candidate_activity_actor_kind",
        ),
        CheckConstraint(
            "(position_id IS NULL) = (application_id IS NULL)",
            name="ck_candidate_activity_application_target",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_candidate_activity_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_candidate_activities_candidate_time",
            candidate_id,
            occurred_at.desc(),
            id.desc(),
        ),
        Index(
            "ix_candidate_activities_org_type_time",
            organization_id,
            event_type,
            occurred_at.desc(),
        ),
        Index("ix_candidate_activities_agent_run_id", agent_run_id),
        Index(
            "ix_candidate_activities_org_application_time",
            organization_id,
            application_id,
            occurred_at.desc(),
            id.desc(),
        ),
    )
