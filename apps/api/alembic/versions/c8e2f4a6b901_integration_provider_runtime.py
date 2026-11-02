"""Encrypted provider connections, durable jobs, and signed webhooks.

Revision ID: c8e2f4a6b901
Revises: b7d3e9f1a205
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c8e2f4a6b901"
down_revision: str | Sequence[str] | None = "b7d3e9f1a205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="disconnected", nullable=False
        ),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("provider_account_id", sa.String(length=255), nullable=True),
        sa.Column(
            "granted_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("credential_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("credential_key_id", sa.String(length=64), nullable=True),
        sa.Column(
            "credential_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("credentials_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
            "provider IN ('google_workspace', 'microsoft_365', 'telegram', 'hh_ru', "
            "'somon', 'csv_xlsx', 'generic_webhook', 'assessment_generic')",
            name="ck_integration_connection_provider",
        ),
        sa.CheckConstraint(
            "status IN ('unavailable', 'disconnected', 'connecting', 'connected', "
            "'degraded', 'expired', 'revoked')",
            name="ck_integration_connection_status",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_integration_connection_failures",
        ),
        sa.CheckConstraint(
            "((credential_ciphertext IS NULL AND credential_nonce IS NULL "
            "AND credential_key_id IS NULL) OR "
            "(credential_ciphertext IS NOT NULL AND credential_nonce IS NOT NULL "
            "AND credential_key_id IS NOT NULL))",
            name="ck_integration_connection_credential_parts",
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
        "ix_integration_connections_organization_id",
        "integration_connections",
        ["organization_id"],
    )
    op.create_index(
        "uq_integration_connection_org_name_ci",
        "integration_connections",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    op.create_index(
        "ix_integration_connections_org_provider_status",
        "integration_connections",
        ["organization_id", "provider", "status"],
    )

    # Bridge the legacy regional-sourcing records into the canonical provider
    # lifecycle while preserving existing rows and compatibility APIs.
    op.add_column(
        "source_integrations",
        sa.Column("connection_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_source_integrations_connection_id_integration_connections",
        "source_integrations",
        "integration_connections",
        ["connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_source_integrations_connection_id",
        "source_integrations",
        ["connection_id"],
    )
    op.add_column(
        "source_import_runs",
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "source_import_runs",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_source_import_run_idempotency",
        "source_import_runs",
        ["organization_id", "integration_id", "idempotency_key"],
    )
    op.add_column(
        "source_publications",
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
    )
    op.create_unique_constraint(
        "uq_source_publication_idempotency",
        "source_publications",
        ["organization_id", "integration_id", "idempotency_key"],
    )
    op.drop_constraint(
        "ck_source_publication_status", "source_publications", type_="check"
    )
    op.create_check_constraint(
        "ck_source_publication_status",
        "source_publications",
        "status IN ('preview', 'tracked', 'queued', 'published', "
        "'published_sandbox', 'credentials_not_configured', 'failed')",
    )

    op.create_table(
        "integration_oauth_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("verifier_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("verifier_key_id", sa.String(length=64), nullable=False),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
            ["connection_id"], ["integration_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_digest"),
    )
    op.create_index(
        "ix_integration_oauth_states_organization_id",
        "integration_oauth_states",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_oauth_states_connection_id",
        "integration_oauth_states",
        ["connection_id"],
    )
    op.create_index(
        "ix_integration_oauth_states_expiry",
        "integration_oauth_states",
        ["expires_at"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

    op.create_table(
        "integration_cursors",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("cursor_name", sa.String(length=96), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("value_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("value_key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["integration_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "cursor_name", name="uq_integration_cursor_name"
        ),
    )
    op.create_index(
        "ix_integration_cursors_organization_id",
        "integration_cursors",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_cursors_connection_id",
        "integration_cursors",
        ["connection_id"],
    )

    op.create_table(
        "integration_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "reference_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("payload_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("payload_key_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_by", sa.String(length=160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "result_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('queued', 'running', 'retrying', 'succeeded', "
            "'dead_letter', 'cancelled')",
            name="ck_integration_job_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND max_attempts <= 10",
            name="ck_integration_job_attempts",
        ),
        sa.CheckConstraint(
            "((payload_ciphertext IS NULL AND payload_nonce IS NULL "
            "AND payload_key_id IS NULL) OR "
            "(payload_ciphertext IS NOT NULL AND payload_nonce IS NOT NULL "
            "AND payload_key_id IS NOT NULL))",
            name="ck_integration_job_payload_parts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["integration_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "connection_id",
            "operation",
            "idempotency_key",
            name="uq_integration_job_idempotency",
        ),
    )
    op.create_index(
        "ix_integration_jobs_organization_id", "integration_jobs", ["organization_id"]
    )
    op.create_index(
        "ix_integration_jobs_connection_id", "integration_jobs", ["connection_id"]
    )
    op.create_index(
        "ix_integration_jobs_claim",
        "integration_jobs",
        ["status", "next_attempt_at", "lease_expires_at"],
        postgresql_where=sa.text("status IN ('queued', 'retrying', 'running')"),
    )
    op.create_index(
        "ix_integration_jobs_org_created",
        "integration_jobs",
        ["organization_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
    )

    op.create_table(
        "integration_webhook_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("envelope_version", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("body_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("body_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("body_key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="accepted", nullable=False
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'processing', 'processed', 'failed', 'ignored')",
            name="ck_integration_webhook_event_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["integration_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "event_id", name="uq_integration_webhook_event"
        ),
    )
    op.create_index(
        "ix_integration_webhook_events_organization_id",
        "integration_webhook_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_webhook_events_connection_id",
        "integration_webhook_events",
        ["connection_id"],
    )
    op.create_index(
        "ix_integration_webhook_events_org_received",
        "integration_webhook_events",
        ["organization_id", sa.literal_column("received_at DESC"), sa.literal_column("id DESC")],
    )

    op.create_table(
        "integration_webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("envelope_version", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("body_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("body_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("body_key_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_response_status", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('queued', 'delivering', 'retrying', 'delivered', "
            "'dead_letter', 'cancelled')",
            name="ck_integration_webhook_delivery_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND max_attempts <= 20",
            name="ck_integration_webhook_delivery_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["integration_connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "event_id", name="uq_integration_webhook_delivery"
        ),
    )
    op.create_index(
        "ix_integration_webhook_deliveries_organization_id",
        "integration_webhook_deliveries",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_webhook_deliveries_connection_id",
        "integration_webhook_deliveries",
        ["connection_id"],
    )
    op.create_index(
        "ix_integration_webhook_deliveries_due",
        "integration_webhook_deliveries",
        ["status", "next_attempt_at"],
        postgresql_where=sa.text("status IN ('queued', 'retrying')"),
    )
    op.create_index(
        "ix_integration_webhook_deliveries_org_created",
        "integration_webhook_deliveries",
        ["organization_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
    )

    op.create_table(
        "integration_webhook_delivery_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_number >= 1", name="ck_integration_webhook_attempt_number"
        ),
        sa.CheckConstraint(
            "status IN ('delivering', 'delivered', 'retrying', 'failed')",
            name="ck_integration_webhook_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["integration_webhook_deliveries.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_integration_webhook_delivery_attempt",
        ),
    )
    op.create_index(
        "ix_integration_webhook_delivery_attempts_organization_id",
        "integration_webhook_delivery_attempts",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_webhook_delivery_attempts_delivery_id",
        "integration_webhook_delivery_attempts",
        ["delivery_id"],
    )
    op.create_index(
        "ix_integration_webhook_attempts_org_created",
        "integration_webhook_delivery_attempts",
        ["organization_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
    )


def downgrade() -> None:
    # Contract safely back to the legacy state vocabulary so rollback remains
    # executable even after the new tracked/queued/published states were used.
    op.execute(
        sa.text(
            "UPDATE source_publications SET status = CASE "
            "WHEN status IN ('tracked', 'queued') THEN 'preview' "
            "WHEN status = 'published' THEN 'published_sandbox' "
            "ELSE status END "
            "WHERE status IN ('tracked', 'queued', 'published')"
        )
    )
    op.drop_constraint(
        "ck_source_publication_status", "source_publications", type_="check"
    )
    op.create_check_constraint(
        "ck_source_publication_status",
        "source_publications",
        "status IN ('preview', 'published_sandbox', "
        "'credentials_not_configured', 'failed')",
    )
    op.drop_constraint(
        "uq_source_publication_idempotency",
        "source_publications",
        type_="unique",
    )
    op.drop_column("source_publications", "idempotency_key")
    op.drop_constraint(
        "uq_source_import_run_idempotency",
        "source_import_runs",
        type_="unique",
    )
    op.drop_column("source_import_runs", "request_fingerprint")
    op.drop_column("source_import_runs", "idempotency_key")
    op.drop_index(
        "ix_source_integrations_connection_id", table_name="source_integrations"
    )
    op.drop_constraint(
        "fk_source_integrations_connection_id_integration_connections",
        "source_integrations",
        type_="foreignkey",
    )
    op.drop_column("source_integrations", "connection_id")
    op.drop_index(
        "ix_integration_webhook_attempts_org_created",
        table_name="integration_webhook_delivery_attempts",
    )
    op.drop_index(
        "ix_integration_webhook_delivery_attempts_delivery_id",
        table_name="integration_webhook_delivery_attempts",
    )
    op.drop_index(
        "ix_integration_webhook_delivery_attempts_organization_id",
        table_name="integration_webhook_delivery_attempts",
    )
    op.drop_table("integration_webhook_delivery_attempts")
    op.drop_index(
        "ix_integration_webhook_deliveries_org_created",
        table_name="integration_webhook_deliveries",
    )
    op.drop_index(
        "ix_integration_webhook_deliveries_due",
        table_name="integration_webhook_deliveries",
    )
    op.drop_index(
        "ix_integration_webhook_deliveries_connection_id",
        table_name="integration_webhook_deliveries",
    )
    op.drop_index(
        "ix_integration_webhook_deliveries_organization_id",
        table_name="integration_webhook_deliveries",
    )
    op.drop_table("integration_webhook_deliveries")
    op.drop_index(
        "ix_integration_webhook_events_org_received",
        table_name="integration_webhook_events",
    )
    op.drop_index(
        "ix_integration_webhook_events_connection_id",
        table_name="integration_webhook_events",
    )
    op.drop_index(
        "ix_integration_webhook_events_organization_id",
        table_name="integration_webhook_events",
    )
    op.drop_table("integration_webhook_events")
    op.drop_index("ix_integration_jobs_org_created", table_name="integration_jobs")
    op.drop_index("ix_integration_jobs_claim", table_name="integration_jobs")
    op.drop_index("ix_integration_jobs_connection_id", table_name="integration_jobs")
    op.drop_index("ix_integration_jobs_organization_id", table_name="integration_jobs")
    op.drop_table("integration_jobs")
    op.drop_index(
        "ix_integration_cursors_connection_id", table_name="integration_cursors"
    )
    op.drop_index(
        "ix_integration_cursors_organization_id", table_name="integration_cursors"
    )
    op.drop_table("integration_cursors")
    op.drop_index(
        "ix_integration_oauth_states_expiry", table_name="integration_oauth_states"
    )
    op.drop_index(
        "ix_integration_oauth_states_connection_id",
        table_name="integration_oauth_states",
    )
    op.drop_index(
        "ix_integration_oauth_states_organization_id",
        table_name="integration_oauth_states",
    )
    op.drop_table("integration_oauth_states")
    op.drop_index(
        "ix_integration_connections_org_provider_status",
        table_name="integration_connections",
    )
    op.drop_index(
        "uq_integration_connection_org_name_ci",
        table_name="integration_connections",
    )
    op.drop_index(
        "ix_integration_connections_organization_id",
        table_name="integration_connections",
    )
    op.drop_table("integration_connections")
