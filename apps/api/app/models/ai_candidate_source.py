"""Provenance linking AI-imported candidates to their private source objects."""

import uuid
from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, uuid_pk


class AiCandidateSourceObject(Base):
    """One candidate/source-object edge retained for privacy-safe erasure.

    A single CSV/XLSX attachment may produce multiple candidates and a
    duplicate candidate may be observed through multiple attachments. Keeping
    the edge explicit lets erasure find every raw object that contains a
    candidate's personal data without exposing object keys to application
    payloads.
    """

    __tablename__ = "ai_candidate_source_objects"

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
    private_object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_objects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_ai_candidate_sources_tenant_candidate",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_ai_candidate_sources_tenant_object",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_ai_candidate_source_objects_org_id",
        ),
        UniqueConstraint(
            "organization_id",
            "candidate_id",
            "private_object_id",
            name="uq_ai_candidate_source_object_edge",
        ),
        Index(
            "ix_ai_candidate_sources_org_candidate",
            "organization_id",
            "candidate_id",
        ),
        Index(
            "ix_ai_candidate_sources_org_object",
            "organization_id",
            "private_object_id",
        ),
    )


__all__ = ["AiCandidateSourceObject"]
