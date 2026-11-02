"""Regional sourcing integrations, imports, publications, and referrals.

Revision ID: 6d4b8a2e0f53
Revises: 5c2e7f9a1d48
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6d4b8a2e0f53"
down_revision: str | Sequence[str] | None = "5c2e7f9a1d48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("webhook_secret_hash", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="ready", nullable=False
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('hh', 'somon', 'telegram', 'csv', 'generic_webhook')",
            name="ck_source_integration_provider",
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'credentials_not_configured', 'error', 'disabled')",
            name="ck_source_integration_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_integrations_organization_id",
        "source_integrations",
        ["organization_id"],
    )
    op.create_index(
        "ix_source_integrations_org_provider_active",
        "source_integrations",
        ["organization_id", "provider", "is_active"],
    )
    op.create_index(
        "uq_source_integration_org_name_ci",
        "source_integrations",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "source_import_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_snapshot", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'partial', 'failed')",
            name="ck_source_import_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["source_integrations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_import_runs_organization_id",
        "source_import_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_source_import_runs_integration_id",
        "source_import_runs",
        ["integration_id"],
    )
    op.create_index(
        "ix_source_import_runs_org_created",
        "source_import_runs",
        ["organization_id", "created_at", "id"],
    )

    op.create_table(
        "referral_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("campaign", sa.String(length=160), nullable=True),
        sa.Column(
            "source", sa.String(length=64), server_default="referral", nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_applications", sa.Integer(), nullable=True),
        sa.Column("click_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "application_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_applications IS NULL OR max_applications > 0",
            name="ck_referral_link_max_applications",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_referral_links_organization_id", "referral_links", ["organization_id"]
    )
    op.create_index("ix_referral_links_position_id", "referral_links", ["position_id"])
    op.create_index(
        "ix_referral_links_org_position_active",
        "referral_links",
        ["organization_id", "position_id", "is_active"],
    )

    op.create_table(
        "source_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider_snapshot", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("tracking_url", sa.String(length=1024), nullable=False),
        sa.Column("post_text", sa.Text(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('preview', 'published_sandbox', 'credentials_not_configured', 'failed')",
            name="ck_source_publication_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["source_integrations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_publications_organization_id",
        "source_publications",
        ["organization_id"],
    )
    op.create_index(
        "ix_source_publications_integration_id",
        "source_publications",
        ["integration_id"],
    )
    op.create_index(
        "ix_source_publications_position_id",
        "source_publications",
        ["position_id"],
    )

    op.create_table(
        "source_candidate_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("integration_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("import_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("provider_snapshot", sa.String(length=32), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["source_integrations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"], ["source_import_runs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "integration_id", "external_id", name="uq_source_receipt_external"
        ),
    )
    op.create_index(
        "ix_source_candidate_receipts_organization_id",
        "source_candidate_receipts",
        ["organization_id"],
    )
    op.create_index(
        "ix_source_candidate_receipts_integration_id",
        "source_candidate_receipts",
        ["integration_id"],
    )
    op.create_index(
        "ix_source_candidate_receipts_candidate_id",
        "source_candidate_receipts",
        ["candidate_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_candidate_receipts_candidate_id",
        table_name="source_candidate_receipts",
    )
    op.drop_index(
        "ix_source_candidate_receipts_integration_id",
        table_name="source_candidate_receipts",
    )
    op.drop_index(
        "ix_source_candidate_receipts_organization_id",
        table_name="source_candidate_receipts",
    )
    op.drop_table("source_candidate_receipts")
    op.drop_index(
        "ix_source_publications_position_id", table_name="source_publications"
    )
    op.drop_index(
        "ix_source_publications_integration_id", table_name="source_publications"
    )
    op.drop_index(
        "ix_source_publications_organization_id", table_name="source_publications"
    )
    op.drop_table("source_publications")
    op.drop_index("ix_referral_links_org_position_active", table_name="referral_links")
    op.drop_index("ix_referral_links_position_id", table_name="referral_links")
    op.drop_index("ix_referral_links_organization_id", table_name="referral_links")
    op.drop_table("referral_links")
    op.drop_index("ix_source_import_runs_org_created", table_name="source_import_runs")
    op.drop_index(
        "ix_source_import_runs_integration_id", table_name="source_import_runs"
    )
    op.drop_index(
        "ix_source_import_runs_organization_id", table_name="source_import_runs"
    )
    op.drop_table("source_import_runs")
    op.drop_index("uq_source_integration_org_name_ci", table_name="source_integrations")
    op.drop_index(
        "ix_source_integrations_org_provider_active",
        table_name="source_integrations",
    )
    op.drop_index(
        "ix_source_integrations_organization_id", table_name="source_integrations"
    )
    op.drop_table("source_integrations")
