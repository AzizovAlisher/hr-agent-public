"""Persist application-bound specialist assessment evidence.

Revision ID: e6b0c2d4e758
Revises: e5a9b1c3d647
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e6b0c2d4e758"
down_revision: str | Sequence[str] | None = "e5a9b1c3d647"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON public.{table} "
        "USING (organization_id = security.current_organization_id()) "
        "WITH CHECK (organization_id = security.current_organization_id())"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} "
        "TO hr_agent_runtime, hr_agent_worker"
    )


def upgrade() -> None:
    op.create_table(
        "provider_assessments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("connection_id", sa.UUID(), nullable=False),
        sa.Column("application_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("template_version_id", sa.UUID(), nullable=True),
        sa.Column("invitation_job_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("external_template_ref", sa.String(length=255), nullable=False),
        sa.Column("provider_invitation_id", sa.String(length=255), nullable=True),
        sa.Column("provider_attempt_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status", sa.String(length=24), server_default="queued", nullable=False
        ),
        sa.Column("launch_url_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("launch_url_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("launch_url_key_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_status", sa.String(length=24), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
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
            "status IN ('queued', 'invited', 'started', 'submitted', 'graded', "
            "'expired', 'cancelled', 'failed')",
            name="ck_provider_assessment_status",
        ),
        sa.CheckConstraint(
            "((launch_url_ciphertext IS NULL AND launch_url_nonce IS NULL "
            "AND launch_url_key_id IS NULL) OR "
            "(launch_url_ciphertext IS NOT NULL AND launch_url_nonce IS NOT NULL "
            "AND launch_url_key_id IS NOT NULL))",
            name="ck_provider_assessment_launch_parts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["integration_connections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_version_id"],
            ["assessment_template_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invitation_job_id"], ["integration_jobs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "connection_id"],
            ["integration_connections.organization_id", "integration_connections.id"],
            name="fk_provider_assessment_tenant_connection",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_provider_assessment_tenant_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_provider_assessments_org_id"
        ),
        sa.UniqueConstraint("invitation_job_id"),
    )
    op.create_index(
        "ix_provider_assessments_organization_id",
        "provider_assessments",
        ["organization_id"],
    )
    op.create_index(
        "ix_provider_assessments_connection_id",
        "provider_assessments",
        ["connection_id"],
    )
    op.create_index(
        "ix_provider_assessments_application_id",
        "provider_assessments",
        ["application_id"],
    )
    op.create_index(
        "ix_provider_assessments_candidate_id",
        "provider_assessments",
        ["candidate_id"],
    )
    op.create_index(
        "ix_provider_assessments_template_version_id",
        "provider_assessments",
        ["template_version_id"],
    )
    op.create_index(
        "uq_provider_assessment_external_invitation",
        "provider_assessments",
        ["connection_id", "provider_invitation_id"],
        unique=True,
        postgresql_where=sa.text("provider_invitation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_provider_assessment_external_attempt",
        "provider_assessments",
        ["connection_id", "provider_attempt_id"],
        unique=True,
        postgresql_where=sa.text("provider_attempt_id IS NOT NULL"),
    )
    op.create_index(
        "ix_provider_assessments_org_application_created",
        "provider_assessments",
        [
            "organization_id",
            "application_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    )

    op.create_table(
        "provider_assessment_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("provider_assessment_id", sa.UUID(), nullable=False),
        sa.Column("source_event_id", sa.UUID(), nullable=True),
        sa.Column("source_job_id", sa.UUID(), nullable=True),
        sa.Column("provider_attempt_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("recommendation", sa.String(length=24), nullable=True),
        sa.Column("evidence_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("evidence_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("evidence_key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
            "status IN ('submitted', 'graded')",
            name="ck_provider_assessment_result_status",
        ),
        sa.CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 100",
            name="ck_provider_assessment_result_score",
        ),
        sa.CheckConstraint(
            "recommendation IS NULL OR recommendation IN "
            "('advance', 'review', 'do_not_advance')",
            name="ck_provider_assessment_result_recommendation",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_provider_assessment_result_version"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["provider_assessment_id"],
            ["provider_assessments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_job_id"], ["integration_jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "provider_assessment_id"],
            ["provider_assessments.organization_id", "provider_assessments.id"],
            name="fk_provider_assessment_result_tenant_assessment",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_provider_assessment_results_org_id"
        ),
        sa.UniqueConstraint("provider_assessment_id"),
    )
    op.create_index(
        "ix_provider_assessment_results_organization_id",
        "provider_assessment_results",
        ["organization_id"],
    )
    op.create_index(
        "ix_provider_assessment_results_org_completed",
        "provider_assessment_results",
        ["organization_id", sa.text("completed_at DESC"), sa.text("id DESC")],
    )

    op.add_column(
        "integration_webhook_events",
        sa.Column("provider_assessment_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_webhook_event_tenant_provider_assessment",
        "integration_webhook_events",
        "provider_assessments",
        ["organization_id", "provider_assessment_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_integration_webhook_events_provider_assessment_id",
        "integration_webhook_events",
        ["provider_assessment_id"],
    )

    # Several referenced domain tables pre-date SQLAlchemy declarations of
    # their generated (organization_id, id) tenant keys. Fixed, non-dynamic
    # constraint triggers preserve the same database-level invariant without
    # weakening metadata-created local/test schemas.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION security.enforce_provider_assessment_edges()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM public.applications AS application
             WHERE application.id = NEW.application_id
               AND application.organization_id = NEW.organization_id
               AND application.candidate_id = NEW.candidate_id
          ) THEN
            RAISE EXCEPTION 'provider assessment application tenant mismatch'
              USING ERRCODE = '23503';
          END IF;
          IF NEW.template_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.assessment_template_versions AS version
             WHERE version.id = NEW.template_version_id
               AND version.organization_id = NEW.organization_id
          ) THEN
            RAISE EXCEPTION 'provider assessment template tenant mismatch'
              USING ERRCODE = '23503';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.integration_jobs AS job
             WHERE job.id = NEW.invitation_job_id
               AND job.organization_id = NEW.organization_id
               AND job.connection_id = NEW.connection_id
               AND job.operation = 'assessment.invite'
          ) THEN
            RAISE EXCEPTION 'provider assessment job tenant mismatch'
              USING ERRCODE = '23503';
          END IF;
          IF NEW.created_by_user_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.users AS actor
             WHERE actor.id = NEW.created_by_user_id
               AND actor.organization_id = NEW.organization_id
          ) THEN
            RAISE EXCEPTION 'provider assessment actor tenant mismatch'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $function$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_provider_assessment_edges
          BEFORE INSERT OR UPDATE OF organization_id, connection_id,
            application_id, candidate_id, template_version_id,
            invitation_job_id, created_by_user_id
          ON public.provider_assessments
          FOR EACH ROW EXECUTE FUNCTION security.enforce_provider_assessment_edges();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION security.enforce_provider_assessment_result_edges()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
          IF NEW.source_job_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.integration_jobs AS job
             WHERE job.id = NEW.source_job_id
               AND job.organization_id = NEW.organization_id
               AND job.operation = 'assessment.result'
          ) THEN
            RAISE EXCEPTION 'provider assessment result job tenant mismatch'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $function$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_provider_assessment_result_edges
          BEFORE INSERT OR UPDATE OF organization_id, source_job_id
          ON public.provider_assessment_results
          FOR EACH ROW EXECUTE FUNCTION security.enforce_provider_assessment_result_edges();
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION security.enforce_provider_assessment_edges() "
        "FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION security.enforce_provider_assessment_result_edges() "
        "FROM PUBLIC"
    )

    _tenant_rls("provider_assessments")
    _tenant_rls("provider_assessment_results")


def downgrade() -> None:
    bind = op.get_bind()
    material = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM provider_assessments) + "
            "(SELECT count(*) FROM provider_assessment_results)"
        )
    ).scalar_one()
    if material:
        raise RuntimeError(
            "Cannot downgrade while specialist assessment evidence exists"
        )
    op.execute(
        "DROP TRIGGER IF EXISTS enforce_provider_assessment_result_edges "
        "ON public.provider_assessment_results"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS security.enforce_provider_assessment_result_edges()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS enforce_provider_assessment_edges "
        "ON public.provider_assessments"
    )
    op.execute("DROP FUNCTION IF EXISTS security.enforce_provider_assessment_edges()")
    op.drop_index(
        "ix_integration_webhook_events_provider_assessment_id",
        table_name="integration_webhook_events",
    )
    op.drop_constraint(
        "fk_webhook_event_tenant_provider_assessment",
        "integration_webhook_events",
        type_="foreignkey",
    )
    op.drop_column("integration_webhook_events", "provider_assessment_id")
    op.drop_table("provider_assessment_results")
    op.drop_table("provider_assessments")
