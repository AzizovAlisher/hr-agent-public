import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, uuid_pk


class ShortlistLink(Base):
    """A revokable, read-only public link to the top-N candidates of a position.

    Hiring managers and external stakeholders are the audience: they don't
    have org accounts but need to see who the recruiter shortlisted and why.
    Tokens are random URL-safe strings. Only a SHA-256 digest is persisted and
    the bearer value is returned once when the link is created or rotated.

    `include_contact = False` redacts phone/email on the public page so a
    forwarded link doesn't leak applicant PII to whomever happens to read
    the email it was sent in. Recruiters can flip it on per-link when they
    explicitly want the manager to be able to call/text.
    """

    __tablename__ = "shortlist_links"
    __table_args__ = (
        Index("ix_shortlist_links_org", "organization_id"),
        Index("ix_shortlist_links_position", "position_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    token_digest: Mapped[str] = mapped_column(
        "token", String(64), nullable=False, unique=True, index=True
    )
    top_n: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    include_contact: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
