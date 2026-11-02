"""Interview recording: the meeting capture + its transcript/diarization and,
eventually, an AI score of how the candidate answered.

This is the write-target for the external recording pipeline (record →
diarize → transcribe → score) that the team owns. The API only creates the
row and exposes a PATCH hook the pipeline calls to fill in transcript,
diarization, and the eventual interview score. We do NOT transcribe or score
audio here — we store and surface whatever the pipeline writes, and feed any
transcript/score into the candidate's AI evidence.
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


class InterviewRecording(Base):
    __tablename__ = "interview_recordings"

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_interview_recordings_tenant_private_object",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(position_id IS NULL) = (application_id IS NULL)",
            name="ck_interview_recording_application_target",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_interview_recording_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_interview_recordings_org_application",
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
    # A recording may not be tied to a specific position (general screen).
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

    # pending -> uploaded -> transcribing -> transcribed -> scored | failed.
    # The recording pipeline advances this as it works.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")

    # Where the media lives. media_path = on our storage; media_url = hosted
    # elsewhere (Zoom/Drive/etc.). Either, both, or neither may be set.
    media_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    private_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_objects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    media_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Filled by the pipeline. transcript = flat text; diarization = list of
    # speaker-tagged turns [{speaker, start, end, text}, ...].
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    diarization: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # The eventual interview score (0–100) + its breakdown. Null until the
    # pipeline scores the transcript. Folded into AI evidence once present.
    interview_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interview_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
