import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, uuid_pk


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "id",
            name="uq_tenant_candidates_bef2e239",
        ),
        ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_candidates_tenant_private_object",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "merged_into_candidate_id IS NULL OR merged_into_candidate_id <> id",
            name="ck_candidate_not_merged_into_self",
        ),
        Index(
            "ix_candidates_search_document",
            "search_document",
            postgresql_using="gin",
        ),
        Index(
            "ix_candidates_merged_aliases",
            "organization_id",
            "merged_into_candidate_id",
            postgresql_where=text("merged_at IS NOT NULL"),
        ),
        Index(
            "ix_candidates_org_cv_fingerprint",
            "organization_id",
            "duplicate_cv_fingerprint",
            postgresql_where=text("duplicate_cv_fingerprint IS NOT NULL"),
        ),
        Index(
            "ix_candidates_org_email_identity",
            "organization_id",
            "email_normalized",
            postgresql_where=text("email_normalized IS NOT NULL"),
        ),
        Index(
            "ix_candidates_org_phone_identity",
            "organization_id",
            "phone_normalized",
            postgresql_where=text("phone_normalized IS NOT NULL"),
        ),
        Index(
            "ix_candidates_org_github_identity",
            "organization_id",
            text("lower(github_login)"),
            postgresql_where=text("github_login IS NOT NULL"),
        ),
        Index(
            "ix_candidates_org_source_identity",
            "organization_id",
            text("lower(source)"),
            text("lower(source_ref)"),
            postgresql_where=text("source_ref IS NOT NULL"),
        ),
        Index(
            "ix_candidates_org_source_created",
            "organization_id",
            "source",
            text("created_at DESC"),
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_cv: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_cv: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    private_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("private_objects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # GitHub enrichment. github_login is the attached username (lowercased);
    # github_data is the fetched snapshot {profile, repos, languages,
    # ai_summary}; github_fetched_at marks the last refresh. All null until a
    # recruiter attaches a profile. github_data feeds the scoring / chat /
    # interview prompts as external evidence.
    github_login: Mapped[str | None] = mapped_column(String(120), nullable=True)
    github_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    github_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Normalized phone/email used to detect duplicate applicants within an org.
    # Phone is digits-only with leading '+'; email is lowercased + trimmed.
    # Indexed but NOT unique — different orgs may have the same applicant.
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    email_normalized: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    is_favorite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Search stays inside PostgreSQL.  The generated vector is never returned
    # by the API and the simple dictionary consistently tokenizes RU/EN/Tajik.
    search_document: Mapped[object] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, "
            "coalesce(source_ref, '') || ' ' || "
            "coalesce(raw_cv, '') || ' ' || "
            "coalesce(parsed_cv::text, ''))",
            persisted=True,
        ),
        nullable=True,
    )
    duplicate_cv_fingerprint: Mapped[str | None] = mapped_column(
        Text,
        Computed(
            "CASE WHEN raw_cv IS NULL OR btrim(raw_cv) = '' THEN NULL "
            "ELSE md5(lower(regexp_replace(btrim(raw_cv), "
            "'[[:space:]]+', ' ', 'g'))) END",
            persisted=True,
        ),
        nullable=True,
    )

    # Duplicate merging keeps the source row as a hidden alias so its original
    # CV/source metadata and append-only activity history remain durable.
    merged_into_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=True,
    )
    merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Enterprise soft deletion keeps the full recruiting history recoverable
    # until the organization's retention policy explicitly purges it.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = created_at()
