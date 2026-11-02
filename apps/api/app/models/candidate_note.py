import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class CandidateNote(Base):
    """Free-form recruiter note on a candidate, separate from interview
    notes (which live on InterviewQuestion). Use cases: "called Tuesday,
    didn't pick up", "asked for higher salary", "manager liked CV but
    needs in-person".

    Notes are append-mostly: we render them as a chronological feed on
    the candidate detail page. Authors can edit/delete their own; admins
    can delete anyone's.
    """

    __tablename__ = "candidate_notes"
    __table_args__ = (
        Index("ix_candidate_notes_candidate", "candidate_id", text("created_at DESC")),
        Index("ix_candidate_notes_org", "organization_id"),
    )

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
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class CandidateTag(Base):
    """Free-text label on a candidate ("ghosted", "second-round",
    "знает таджикский"). Recruiter-defined, no central taxonomy — the team
    self-organizes. Label is unique per candidate so accidentally adding
    "ghosted" twice isn't possible; same label across candidates is fine.
    """

    __tablename__ = "candidate_tags"
    __table_args__ = (
        UniqueConstraint("candidate_id", "label", name="uq_candidate_tag_label"),
        Index("ix_candidate_tags_candidate", "candidate_id"),
        Index("ix_candidate_tags_org", "organization_id"),
        Index("ix_candidate_tags_label", "organization_id", "label"),
    )

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
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at()
