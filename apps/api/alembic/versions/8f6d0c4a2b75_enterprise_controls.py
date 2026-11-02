"""Enterprise audit, recovery, retention, permissions, API and provider policy.

Revision ID: 8f6d0c4a2b75
Revises: 7e5c9b3f1a64
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8f6d0c4a2b75"
down_revision: str | Sequence[str] | None = "7e5c9b3f1a64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidates", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "candidates",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_candidates_deleted_by_user_id_users",
        "candidates",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_candidates_deleted_at", "candidates", ["deleted_at"])

    op.add_column(
        "positions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "positions",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_positions_deleted_by_user_id_users",
        "positions",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_positions_deleted_at", "positions", ["deleted_at"])

    op.create_table(
        "enterprise_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_retention_days", sa.Integer(), server_default="3650", nullable=False),
        sa.Column("deleted_retention_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("audit_retention_days", sa.Integer(), server_default="2555", nullable=False),
        sa.Column("candidate_export_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("backup_export_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "field_visibility",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{\"compensation\":[\"admin\",\"hiring_manager\"]}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("candidate_retention_days BETWEEN 30 AND 36500", name="ck_policy_candidate_retention"),
        sa.CheckConstraint("deleted_retention_days BETWEEN 1 AND 3650", name="ck_policy_deleted_retention"),
        sa.CheckConstraint("audit_retention_days BETWEEN 90 AND 36500", name="ck_policy_audit_retention"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_email_snapshot", sa.String(length=320), nullable=True),
        sa.Column("action", sa.String(length=160), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("path_template", sa.String(length=512), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_org_occurred", "audit_logs", ["organization_id", sa.text("occurred_at DESC"), sa.text("id DESC")])
    op.create_index("ix_audit_logs_org_action_occurred", "audit_logs", ["organization_id", "action", sa.text("occurred_at DESC")])

    op.create_table(
        "permission_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "role", "permission", name="uq_permission_override"),
    )

    op.create_table(
        "organization_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_digest"),
        sa.UniqueConstraint("organization_id", "name", name="uq_api_key_org_name"),
    )
    op.create_index("ix_organization_api_keys_organization_id", "organization_api_keys", ["organization_id"])
    op.create_index("ix_organization_api_keys_key_prefix", "organization_api_keys", ["key_prefix"])

    op.create_table(
        "sso_configurations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=16), server_default="oidc", nullable=False),
        sa.Column("issuer_url", sa.String(length=1024), nullable=False),
        sa.Column("client_id", sa.String(length=512), nullable=False),
        sa.Column("allowed_domains", postgresql.ARRAY(sa.String(length=255)), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("enforce_for_members", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="credentials_not_configured", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("provider IN ('oidc', 'saml')", name="ck_sso_provider"),
        sa.CheckConstraint("status IN ('credentials_not_configured', 'ready', 'error', 'disabled')", name="ck_sso_status"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )

    op.create_table(
        "organization_model_configurations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="inherit", nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="ready", nullable=False),
        sa.Column("data_residency", sa.String(length=32), server_default="provider", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("provider IN ('inherit', 'openrouter', 'openai_compatible', 'vllm', 'ollama')", name="ck_org_model_provider"),
        sa.CheckConstraint("status IN ('ready', 'credentials_not_configured', 'unreachable', 'disabled')", name="ck_org_model_status"),
        sa.CheckConstraint("data_residency IN ('provider', 'private_cloud', 'on_prem')", name="ck_org_model_residency"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )


def downgrade() -> None:
    op.drop_table("organization_model_configurations")
    op.drop_table("sso_configurations")
    op.drop_index("ix_organization_api_keys_key_prefix", table_name="organization_api_keys")
    op.drop_index("ix_organization_api_keys_organization_id", table_name="organization_api_keys")
    op.drop_table("organization_api_keys")
    op.drop_table("permission_overrides")
    op.drop_index("ix_audit_logs_org_action_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_occurred", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_organization_id", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("enterprise_policies")
    op.drop_index("ix_positions_deleted_at", table_name="positions")
    op.drop_constraint("fk_positions_deleted_by_user_id_users", "positions", type_="foreignkey")
    op.drop_column("positions", "deleted_by_user_id")
    op.drop_column("positions", "deleted_at")
    op.drop_index("ix_candidates_deleted_at", table_name="candidates")
    op.drop_constraint("fk_candidates_deleted_by_user_id_users", "candidates", type_="foreignkey")
    op.drop_column("candidates", "deleted_by_user_id")
    op.drop_column("candidates", "deleted_at")
