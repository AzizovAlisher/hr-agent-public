import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="free")
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=True,
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
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "(application_id IS NOT NULL) = "
            "(candidate_id IS NOT NULL AND position_id IS NOT NULL)",
            name="ck_chat_session_application_target",
        ),
        ForeignKeyConstraint(
            ["organization_id", "application_id", "candidate_id", "position_id"],
            [
                "applications.organization_id",
                "applications.id",
                "applications.candidate_id",
                "applications.position_id",
            ],
            name="fk_chat_session_exact_application",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_chat_sessions_org_application",
            "organization_id",
            "application_id",
        ),
    )
