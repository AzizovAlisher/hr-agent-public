import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
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
from app.models._mixins import created_at, uuid_pk


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("candidate_id", "position_id", name="uq_scores_candidate_position"),
        CheckConstraint(
            "stage IN ('new', 'screening', 'interview', 'offer', 'hired', 'rejected')",
            name="ck_scores_stage",
        ),
        Index(
            "ix_scores_org_owner_due",
            "organization_id",
            "owner_user_id",
            "next_action_due_date",
            "id",
        ),
        Index(
            "ix_scores_org_stage_score_candidate",
            "organization_id",
            "stage",
            text("overall_score DESC"),
            "candidate_id",
        ),
    )

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

    score_technical_skills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_quality_of_projects: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_previous_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_impact: Mapped[int | None] = mapped_column(Integer, nullable=True)

    comment_technical_skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_quality_of_projects: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_previous_experience: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_impact: Mapped[str | None] = mapped_column(Text, nullable=True)

    overall_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scored_at: Mapped[datetime] = created_at()

    # Kanban pipeline stage for this candidate × position pair. Allowed:
    # new | screening | interview | offer | hired | rejected. Defaults to
    # "new" — set automatically when scoring runs.
    stage: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="new", default="new", index=True
    )

    # Ownership is scoped to this application/pipeline relation. A candidate
    # may have a different responsible teammate and due date per vacancy.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    next_action_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
