"""Technical-task submission: a take-home / coding task we send a candidate,
their submission (repo link, uploaded file, or pasted text), and the AI's
absorption + evaluation of it.

Unlike the interview recording (whose scoring the team owns externally), the
task submission IS evaluated here: once a submission lands we run a single LLM
pass that absorbs the work and produces a score + summary, and the evaluation
folds into the candidate's AI evidence for scoring / chat / interview prep.
"""
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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class TaskSubmission(Base):
    __tablename__ = "task_submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_task_submissions_tenant_private_object",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(position_id IS NULL) = (application_id IS NULL)",
            name="ck_task_submission_application_target",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_task_submission_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_task_submissions_org_application",
            "organization_id",
            "application_id",
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
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # The task we sent the candidate (the brief). Optional — a recruiter may
    # only record the submission.
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # repo | file | text — how the candidate handed the work back.
    submission_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text"
    )
    repo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    private_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_objects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # sent -> submitted -> evaluated | failed.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="sent")

    # AI absorption result. Null until evaluated.
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
