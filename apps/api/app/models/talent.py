"""Talent CRM pools and reusable nurture sequences."""

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
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class TalentPool(Base):
    __tablename__ = "talent_pools"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        Index(
            "uq_talent_pool_org_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
    )


class TalentPoolMember(Base):
    __tablename__ = "talent_pool_members"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("talent_pools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("pool_id", "candidate_id", name="uq_talent_pool_member"),
        CheckConstraint(
            "status IN ('active', 'paused', 'converted')",
            name="ck_talent_pool_member_status",
        ),
        Index(
            "ix_talent_pool_members_org_pool_created",
            "organization_id",
            "pool_id",
            "created_at",
            "id",
        ),
    )


class TalentNurtureSequence(Base):
    __tablename__ = "talent_nurture_sequences"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("talent_pools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    steps: Mapped[list["TalentNurtureStep"]] = relationship(
        "TalentNurtureStep",
        back_populates="sequence",
        cascade="all, delete-orphan",
        order_by="TalentNurtureStep.position",
    )

    __table_args__ = (
        Index(
            "uq_talent_nurture_org_name_ci",
            "organization_id",
            text("lower(name)"),
            unique=True,
        ),
    )


class TalentNurtureStep(Base):
    __tablename__ = "talent_nurture_steps"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("talent_nurture_sequences.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("communication_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    template_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = created_at()

    sequence: Mapped[TalentNurtureSequence] = relationship(
        "TalentNurtureSequence", back_populates="steps"
    )

    __table_args__ = (
        UniqueConstraint(
            "sequence_id", "position", name="uq_talent_nurture_step_position"
        ),
        CheckConstraint(
            "delay_days BETWEEN 0 AND 365", name="ck_talent_nurture_delay"
        ),
        CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_talent_nurture_channel",
        ),
    )


class TalentNurtureEnrollment(Base):
    __tablename__ = "talent_nurture_enrollments"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("talent_nurture_sequences.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    sequence_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "sequence_id", "candidate_id", name="uq_talent_nurture_enrollment"
        ),
        CheckConstraint(
            "status IN ('active', 'partial', 'completed', 'cancelled')",
            name="ck_talent_nurture_enrollment_status",
        ),
    )
