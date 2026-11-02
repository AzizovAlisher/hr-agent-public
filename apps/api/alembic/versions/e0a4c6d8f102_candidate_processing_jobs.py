"""Durable candidate parsing and scoring jobs.

Revision ID: e0a4c6d8f102
Revises: d9f1a3c5e7b2
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e0a4c6d8f102"
down_revision: str | Sequence[str] | None = "d9f1a3c5e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_processing_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("position_id", sa.UUID(), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("leased_by", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "result_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('parse_cv', 'score')",
            name="ck_candidate_processing_job_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retrying', 'succeeded', "
            "'dead_letter', 'cancelled')",
            name="ck_candidate_processing_job_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts "
            "AND max_attempts BETWEEN 1 AND 10",
            name="ck_candidate_processing_job_attempts",
        ),
        sa.CheckConstraint(
            "(kind = 'score' AND position_id IS NOT NULL) OR "
            "(kind = 'parse_cv' AND position_id IS NULL)",
            name="ck_candidate_processing_job_target",
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_candidate_processing_job_cancelled_at",
        ),
        sa.CheckConstraint(
            "status NOT IN ('succeeded', 'dead_letter', 'cancelled') "
            "OR completed_at IS NOT NULL",
            name="ck_candidate_processing_job_terminal_at",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_tenant_candidate_processing_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "position_id"],
            ["positions.organization_id", "positions.id"],
            name="fk_tenant_candidate_processing_position",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by_user_id"],
            ["users.organization_id", "users.id"],
            name="fk_tenant_candidate_processing_creator",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_tenant_candidate_processing_jobs_7895bc12",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_candidate_processing_job_idempotency",
        ),
    )
    op.create_index(
        "ix_candidate_processing_jobs_organization_id",
        "candidate_processing_jobs",
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_processing_jobs_candidate_id",
        "candidate_processing_jobs",
        ["candidate_id"],
    )
    op.create_index(
        "ix_candidate_processing_jobs_position_id",
        "candidate_processing_jobs",
        ["position_id"],
    )
    op.create_index(
        "ix_candidate_processing_jobs_claimable",
        "candidate_processing_jobs",
        ["status", "available_at", "lease_expires_at", "created_at", "id"],
        postgresql_where=sa.text("status IN ('queued', 'retrying', 'running')"),
    )
    op.create_index(
        "ix_candidate_processing_jobs_org_created",
        "candidate_processing_jobs",
        ["organization_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_tenant_candidate_processing_candidate",
        "candidate_processing_jobs",
        ["organization_id", "candidate_id"],
    )
    op.create_index(
        "ix_tenant_candidate_processing_position",
        "candidate_processing_jobs",
        ["organization_id", "position_id"],
    )
    op.create_index(
        "ix_tenant_candidate_processing_creator",
        "candidate_processing_jobs",
        ["organization_id", "created_by_user_id"],
    )
    op.create_index(
        "uq_candidate_processing_active_parse",
        "candidate_processing_jobs",
        ["organization_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'parse_cv' AND status IN ('queued', 'running', 'retrying')"
        ),
    )
    op.create_index(
        "uq_candidate_processing_active_score",
        "candidate_processing_jobs",
        ["organization_id", "candidate_id", "position_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'score' AND status IN ('queued', 'running', 'retrying')"
        ),
    )

    op.execute("ALTER TABLE public.candidate_processing_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.candidate_processing_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON public.candidate_processing_jobs "
        "USING (organization_id = security.current_organization_id()) "
        "WITH CHECK (organization_id = security.current_organization_id())"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON public.candidate_processing_jobs "
        "TO hr_agent_runtime, hr_agent_worker"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON public.candidate_processing_jobs TO hr_agent_broker"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION security.claim_candidate_processing_jobs(
          p_worker_id text, p_limit integer, p_now timestamptz
        )
        RETURNS TABLE(
          work_id uuid, organization_id uuid, lease_token uuid,
          lease_expires_at timestamptz
        )
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT job.id, job.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.candidate_processing_jobs AS job
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'candidate_processing_jobs'
               AND existing.work_id = job.id
             WHERE job.cancelled_at IS NULL
               AND job.available_at <= p_now
               AND job.attempt_count < job.max_attempts
               AND (job.status IN ('queued', 'retrying')
                    OR (job.status = 'running'
                        AND job.lease_expires_at IS NOT NULL
                        AND job.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY job.available_at, job.created_at, job.id
             FOR UPDATE OF job SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.candidate_processing_jobs AS job
               SET status = 'running',
                   attempt_count = job.attempt_count + 1,
                   leased_at = p_now,
                   leased_by = p_worker_id,
                   lease_expires_at = p_now + interval '300 seconds',
                   failure_code = NULL,
                   failure_detail = NULL,
                   updated_at = p_now
              FROM candidates
             WHERE job.id = candidates.id
             RETURNING job.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'candidate_processing_jobs', candidates.id,
                   candidates.organization_id, candidates.token, p_worker_id,
                   p_now, p_now + interval '300 seconds'
              FROM candidates JOIN updated ON updated.id = candidates.id
            ON CONFLICT ON CONSTRAINT worker_queue_leases_pkey DO UPDATE
              SET organization_id = EXCLUDED.organization_id,
                  lease_token = EXCLUDED.lease_token,
                  worker_id = EXCLUDED.worker_id,
                  leased_at = EXCLUDED.leased_at,
                  lease_expires_at = EXCLUDED.lease_expires_at
              WHERE public.worker_queue_leases.lease_expires_at <= p_now
            RETURNING worker_queue_leases.work_id,
                      worker_queue_leases.organization_id,
                      worker_queue_leases.lease_token,
                      worker_queue_leases.lease_expires_at
          )
          SELECT leased.work_id, leased.organization_id,
                 leased.lease_token, leased.lease_expires_at FROM leased;
        END
        $function$
        """
    )
    op.execute(
        "ALTER FUNCTION security.claim_candidate_processing_jobs"
        "(text,integer,timestamptz) OWNER TO hr_agent_broker"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION security.claim_candidate_processing_jobs"
        "(text,integer,timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION security.claim_candidate_processing_jobs"
        "(text,integer,timestamptz) TO hr_agent_worker"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION security.candidate_processing_queue_stats(
          p_now timestamptz
        )
        RETURNS TABLE(
          queue_name text, due_count bigint, oldest_due_at timestamptz,
          dlq_count bigint, oldest_dlq_at timestamptz
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT 'candidate_processing_jobs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE job.cancelled_at IS NULL
                     AND job.available_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running'
                              AND job.lease_expires_at IS NOT NULL
                              AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN job.status = 'running' THEN job.lease_expires_at
                        ELSE job.available_at END
                 ) FILTER (
                   WHERE job.cancelled_at IS NULL
                     AND job.available_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running'
                              AND job.lease_expires_at IS NOT NULL
                              AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE job.status = 'dead_letter'),
                 pg_catalog.min(job.completed_at) FILTER (
                   WHERE job.status = 'dead_letter')
            FROM public.candidate_processing_jobs AS job
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'candidate_processing_jobs'
             AND active.work_id = job.id
             AND active.lease_expires_at > p_now
        $function$
        """
    )
    op.execute(
        "ALTER FUNCTION security.candidate_processing_queue_stats(timestamptz) "
        "OWNER TO hr_agent_broker"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION security.candidate_processing_queue_stats"
        "(timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION security.candidate_processing_queue_stats"
        "(timestamptz) TO hr_agent_runtime, hr_agent_worker"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS security.candidate_processing_queue_stats(timestamptz)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS security.claim_candidate_processing_jobs"
        "(text,integer,timestamptz)"
    )
    op.drop_index(
        "uq_candidate_processing_active_score",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "uq_candidate_processing_active_parse",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_tenant_candidate_processing_creator",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_tenant_candidate_processing_position",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_tenant_candidate_processing_candidate",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_candidate_processing_jobs_org_created",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_candidate_processing_jobs_claimable",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_candidate_processing_jobs_position_id",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_candidate_processing_jobs_candidate_id",
        table_name="candidate_processing_jobs",
    )
    op.drop_index(
        "ix_candidate_processing_jobs_organization_id",
        table_name="candidate_processing_jobs",
    )
    op.drop_table("candidate_processing_jobs")
