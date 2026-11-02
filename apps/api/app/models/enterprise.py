"""Enterprise governance, audit, API access, SSO, and model policy."""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import created_at, updated_at, uuid_pk


class EnterprisePolicy(Base):
    __tablename__ = "enterprise_policies"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    candidate_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default="365"
    )
    deleted_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    audit_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2555, server_default="2555"
    )
    candidate_export_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    backup_export_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    approval_separation_of_duties: Mapped[dict[str, bool]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {
            "requisitions": True,
            "offers": True,
            "hiring_decisions": True,
        },
        server_default=(
            '{"requisitions":true,"offers":true,"hiring_decisions":true}'
        ),
    )
    field_visibility: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {
            "candidate_contacts": ["admin", "recruiter"],
            "candidate_cv": ["admin", "recruiter"],
            "assessment_results": [
                "admin",
                "recruiter",
                "hiring_manager",
                "interviewer",
            ],
            "compensation": ["admin", "hiring_manager"],
            "offers": ["admin", "recruiter", "hiring_manager"],
            "exports": ["admin", "recruiter"],
            "backups": ["admin"],
            "ai_context": ["admin", "recruiter"],
            "secrets": ["admin"],
            "audit": ["admin"],
        },
        server_default=(
            '{"candidate_contacts":["admin","recruiter"],'
            '"candidate_cv":["admin","recruiter"],'
            '"assessment_results":["admin","recruiter","hiring_manager","interviewer"],'
            '"compensation":["admin","hiring_manager"],'
            '"offers":["admin","recruiter","hiring_manager"],'
            '"exports":["admin","recruiter"],'
            '"backups":["admin"],'
            '"ai_context":["admin","recruiter"],'
            '"secrets":["admin"],'
            '"audit":["admin"]}'
        ),
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "candidate_retention_days BETWEEN 30 AND 730",
            name="ck_policy_candidate_retention",
        ),
        CheckConstraint(
            "deleted_retention_days BETWEEN 1 AND 3650",
            name="ck_policy_deleted_retention",
        ),
        CheckConstraint(
            "audit_retention_days BETWEEN 90 AND 36500",
            name="ck_policy_audit_retention",
        ),
        CheckConstraint(
            "jsonb_typeof(approval_separation_of_duties) = 'object' "
            "AND approval_separation_of_duties ? 'requisitions' "
            "AND approval_separation_of_duties ? 'offers' "
            "AND approval_separation_of_duties ? 'hiring_decisions' "
            "AND approval_separation_of_duties "
            "- ARRAY['requisitions', 'offers', 'hiring_decisions']::text[] = '{}'::jsonb "
            "AND jsonb_typeof(approval_separation_of_duties -> 'requisitions') = 'boolean' "
            "AND jsonb_typeof(approval_separation_of_duties -> 'offers') = 'boolean' "
            "AND jsonb_typeof(approval_separation_of_duties -> 'hiring_decisions') = 'boolean'",
            name="ck_policy_approval_separation_exact",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_email_snapshot: Mapped[str | None] = mapped_column(String(320), nullable=True)
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    path_template: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        Index(
            "ix_audit_logs_org_occurred",
            "organization_id",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_audit_logs_org_action_occurred",
            "organization_id",
            "action",
            text("occurred_at DESC"),
        ),
    )


class PermissionOverride(Base):
    __tablename__ = "permission_overrides"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    permission: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'recruiter', 'hiring_manager', 'interviewer', 'restricted')",
            name="ck_permission_override_role",
        ),
        UniqueConstraint(
            "organization_id", "role", "permission", name="uq_permission_override"
        ),
    )


class OrganizationApiKey(Base):
    __tablename__ = "organization_api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_api_key_org_name"),
    )


class SsoConfiguration(Base):
    __tablename__ = "sso_configurations"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="oidc", server_default="oidc")
    issuer_url: Mapped[str] = mapped_column(String(1_024), nullable=False)
    client_id: Mapped[str] = mapped_column(String(512), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    enforce_for_members: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="credentials_not_configured", server_default="credentials_not_configured")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint("provider IN ('oidc', 'saml')", name="ck_sso_provider"),
        CheckConstraint(
            "status IN ('credentials_not_configured', 'ready', 'error', 'disabled')",
            name="ck_sso_status",
        ),
    )


class OrganizationModelConfiguration(Base):
    __tablename__ = "organization_model_configurations"

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="inherit", server_default="inherit")
    base_url: Mapped[str | None] = mapped_column(String(1_024), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", server_default="ready")
    data_residency: Mapped[str] = mapped_column(String(32), nullable=False, default="provider", server_default="provider")
    managed_processing_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    managed_processing_accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    managed_processing_notice_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    managed_processing_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "provider IN ('inherit', 'openrouter', 'openai_compatible', 'vllm', 'ollama')",
            name="ck_org_model_provider",
        ),
        CheckConstraint(
            "status IN ('ready', 'credentials_not_configured', 'unreachable', 'disabled')",
            name="ck_org_model_status",
        ),
        CheckConstraint(
            "data_residency IN ('provider', 'private_cloud', 'on_prem')",
            name="ck_org_model_residency",
        ),
        CheckConstraint(
            "(managed_processing_accepted_at IS NULL AND "
            "managed_processing_notice_version IS NULL) OR "
            "(managed_processing_accepted_at IS NOT NULL AND "
            "managed_processing_notice_version IS NOT NULL)",
            name="ck_org_model_managed_acceptance",
        ),
        CheckConstraint(
            "managed_processing_revoked_at IS NULL OR "
            "managed_processing_accepted_at IS NOT NULL",
            name="ck_org_model_managed_revocation",
        ),
    )
