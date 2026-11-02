"""Headcount requisitions, confidential offers, approvals, and handoff."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class Requisition(Base):
    __tablename__ = "requisitions"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    hiring_manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str | None] = mapped_column(String(160), nullable=True)
    business_justification: Mapped[str] = mapped_column(Text, nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="TJS", server_default="TJS"
    )
    salary_confidential: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", server_default="draft"
    )
    required_approvals: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    approval_cycle: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    approvals: Mapped[list["RequisitionApproval"]] = relationship(
        "RequisitionApproval", back_populates="requisition", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("headcount BETWEEN 1 AND 10000", name="ck_requisition_headcount"),
        CheckConstraint("btrim(title) <> ''", name="ck_requisition_title_nonempty"),
        CheckConstraint(
            "btrim(business_justification) <> ''",
            name="ck_requisition_justification_nonempty",
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_min >= 0",
            name="ck_requisition_salary_min_nonnegative",
        ),
        CheckConstraint(
            "salary_max IS NULL OR salary_max >= 0",
            name="ck_requisition_salary_max_nonnegative",
        ),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="ck_requisition_salary_range",
        ),
        CheckConstraint(
            "salary_currency ~ '^[A-Z]{3}$'", name="ck_requisition_salary_currency"
        ),
        CheckConstraint(
            "required_approvals BETWEEN 1 AND 20", name="ck_requisition_approvals"
        ),
        CheckConstraint("version > 0", name="ck_requisition_version_positive"),
        CheckConstraint(
            "approval_cycle >= 0", name="ck_requisition_cycle_nonnegative"
        ),
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'rejected', 'opened', 'cancelled')",
            name="ck_requisition_status",
        ),
        Index("ix_requisitions_org_status_created", "organization_id", "status", "created_at"),
    )


class RequisitionApproval(Base):
    __tablename__ = "requisition_approvals"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    user_email_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    requisition_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    requisition_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    vote: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    requisition: Mapped[Requisition] = relationship("Requisition", back_populates="approvals")

    __table_args__ = (
        UniqueConstraint(
            "requisition_id",
            "approval_cycle",
            "user_id",
            name="uq_requisition_approval_user_cycle",
        ),
        CheckConstraint("vote IN ('approve', 'reject')", name="ck_requisition_approval_vote"),
        CheckConstraint(
            "requisition_version_snapshot > 0",
            name="ck_requisition_approval_version_positive",
        ),
        CheckConstraint(
            "approval_cycle > 0", name="ck_requisition_approval_cycle_positive"
        ),
    )


class OfferTemplate(Base):
    __tablename__ = "offer_templates"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    subject_template: Mapped[str] = mapped_column(String(500), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_offer_template_name_nonempty"),
        CheckConstraint(
            "btrim(subject_template) <> ''", name="ck_offer_template_subject_nonempty"
        ),
        CheckConstraint(
            "btrim(body_template) <> ''", name="ck_offer_template_body_nonempty"
        ),
        Index(
            "uq_offer_template_org_name_ci",
            organization_id,
            func.lower(name),
            unique=True,
        ),
    )


class CandidateOffer(Base):
    __tablename__ = "candidate_offers"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("offer_templates.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    template_name_snapshot: Mapped[str | None] = mapped_column(String(160), nullable=True)
    subject_template_snapshot: Mapped[str] = mapped_column(String(500), nullable=False)
    body_template_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    salary_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="TJS", server_default="TJS")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    approval_cycle: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signature_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signature_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    approvals: Mapped[list["OfferApproval"]] = relationship(
        "OfferApproval", back_populates="offer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_candidate_offer_token_digest"),
        CheckConstraint("btrim(subject) <> ''", name="ck_candidate_offer_subject_nonempty"),
        CheckConstraint("btrim(body) <> ''", name="ck_candidate_offer_body_nonempty"),
        CheckConstraint(
            "btrim(subject_template_snapshot) <> ''",
            name="ck_candidate_offer_subject_template_nonempty",
        ),
        CheckConstraint(
            "btrim(body_template_snapshot) <> ''",
            name="ck_candidate_offer_body_template_nonempty",
        ),
        CheckConstraint(
            "salary_amount IS NULL OR salary_amount >= 0",
            name="ck_candidate_offer_salary_nonnegative",
        ),
        CheckConstraint(
            "salary_currency ~ '^[A-Z]{3}$'", name="ck_candidate_offer_salary_currency"
        ),
        CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'sent', 'accepted', 'declined', 'withdrawn', 'expired')",
            name="ck_candidate_offer_status",
        ),
        CheckConstraint("required_approvals BETWEEN 1 AND 20", name="ck_candidate_offer_approvals"),
        CheckConstraint("version > 0", name="ck_candidate_offer_version_positive"),
        CheckConstraint(
            "approval_cycle >= 0", name="ck_candidate_offer_cycle_nonnegative"
        ),
        Index("ix_candidate_offers_org_status_created", "organization_id", "status", "created_at"),
    )


class OfferApproval(Base):
    __tablename__ = "offer_approvals"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_offers.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    user_email_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    offer_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    approval_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    vote: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    offer: Mapped[CandidateOffer] = relationship("CandidateOffer", back_populates="approvals")

    __table_args__ = (
        UniqueConstraint(
            "offer_id",
            "approval_cycle",
            "user_id",
            name="uq_offer_approval_user_cycle",
        ),
        CheckConstraint("vote IN ('approve', 'reject')", name="ck_offer_approval_vote"),
        CheckConstraint(
            "offer_version_snapshot > 0",
            name="ck_offer_approval_version_positive",
        ),
        CheckConstraint(
            "approval_cycle > 0", name="ck_offer_approval_cycle_positive"
        ),
    )


class CandidateHandoff(Base):
    __tablename__ = "candidate_handoffs"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    offer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidate_offers.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    position_title_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    checklist: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint("offer_id", name="uq_candidate_handoff_offer"),
        CheckConstraint("status IN ('pending', 'in_progress', 'completed')", name="ck_candidate_handoff_status"),
    )


@event.listens_for(Session, "before_flush")
def _protect_approval_history(session: Session, *_args: object) -> None:
    """Recorded requisition and offer votes are append-only."""

    immutable = (RequisitionApproval, OfferApproval)
    if any(isinstance(item, immutable) for item in session.dirty):
        raise ValueError("Approval history is append-only")
    deleted_requisition_ids = {
        item.id for item in session.deleted if isinstance(item, Requisition)
    }
    deleted_offer_ids = {
        item.id for item in session.deleted if isinstance(item, CandidateOffer)
    }
    for item in session.deleted:
        if isinstance(item, RequisitionApproval):
            if item.requisition_id not in deleted_requisition_ids:
                raise ValueError("Requisition approvals are append-only")
        elif isinstance(item, OfferApproval):
            if item.offer_id not in deleted_offer_ids:
                raise ValueError("Offer approvals are append-only")
