"""Durable legacy-file erasure and private-storage scan cursors.

Revision ID: e2c6f8a0b314
Revises: e1b5d7f9a203
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2c6f8a0b314"
down_revision: str | Sequence[str] | None = "e1b5d7f9a203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def upgrade() -> None:
    op.create_table(
        "legacy_private_object_deletions",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("normalized_key", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256_snapshot", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
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
            "(category = 'candidates' AND resource_type = 'candidate') OR "
            "(category = 'assessments' AND resource_type = 'assessment_file') OR "
            "(category = 'tasks' AND resource_type = 'task_submission') OR "
            "(category = 'recordings' AND resource_type = 'interview_recording')",
            name="ck_legacy_private_object_deletions_resource_category",
        ),
        sa.CheckConstraint(
            "length(normalized_key) <= 512 AND ("
            "(category = 'candidates' AND normalized_key ~ "
            f"'^cvs/{_UUID_PATTERN}\\.(pdf|docx)$') OR "
            "(category = 'assessments' AND normalized_key ~ "
            f"('^assessment_files/' || organization_id::text || '/{_UUID_PATTERN}"
            "\\.(pdf|docx|txt|png|jpg|jpeg)$')) OR "
            "(category = 'tasks' AND normalized_key ~ "
            f"('^task_files/' || organization_id::text || '/{_UUID_PATTERN}"
            "\\.(pdf|docx|txt|md|csv)$')) OR "
            "(category = 'recordings' AND normalized_key ~ "
            f"('^recordings/' || organization_id::text || '/{_UUID_PATTERN}"
            "\\.(m4a|mp3|mp4|ogg|wav|webm)$')))",
            name="ck_legacy_private_object_deletions_key_scope",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 15728640",
            name="ck_legacy_private_object_deletions_size",
        ),
        sa.CheckConstraint(
            "sha256_snapshot ~ '^[0-9a-f]{64}$'",
            name="ck_legacy_private_object_deletions_sha256",
        ),
        sa.CheckConstraint(
            "reason IN ('candidate_erasure', 'retention_expired', "
            "'resource_deleted', 'replacement', 'admin_request')",
            name="ck_legacy_private_object_deletions_reason",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 10",
            name="ck_legacy_private_object_deletions_attempts",
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claim_token IS NULL)",
            name="ck_legacy_private_object_deletions_claim_tuple",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_key", name="uq_legacy_private_object_deletions_key"
        ),
    )
    op.create_index(
        "ix_legacy_private_object_deletions_claim",
        "legacy_private_object_deletions",
        ["completed_at", "next_attempt_at", "id"],
    )
    op.create_index(
        "ix_legacy_private_object_deletions_org_resource",
        "legacy_private_object_deletions",
        ["organization_id", "resource_type", "resource_id"],
    )

    op.create_table(
        "private_storage_reconciliation_cursors",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("namespace", sa.String(length=16), nullable=False),
        sa.Column("cursor_key", sa.Text(), nullable=True),
        sa.Column("cycle_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "namespace IN ('registry', 'private', 'quarantine')",
            name="ck_private_storage_reconciliation_cursor_namespace",
        ),
        sa.CheckConstraint(
            "cycle_count >= 0",
            name="ck_private_storage_reconciliation_cursor_cycles",
        ),
        sa.CheckConstraint(
            "cursor_key IS NULL OR (length(cursor_key) <= 1024 AND ("
            "(namespace IN ('registry', 'private') AND cursor_key LIKE "
            "('private/' || organization_id::text || '/%')) OR "
            "(namespace = 'quarantine' AND cursor_key LIKE "
            "('quarantine/' || organization_id::text || '/%'))))",
            name="ck_private_storage_reconciliation_cursor_key_scope",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", "namespace"),
    )

    op.execute(
        "ALTER TABLE public.legacy_private_object_deletions ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.legacy_private_object_deletions FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON public.legacy_private_object_deletions "
        "USING (organization_id = security.current_organization_id()) "
        "WITH CHECK (organization_id = security.current_organization_id())"
    )
    op.execute(
        "ALTER TABLE public.private_storage_reconciliation_cursors "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.private_storage_reconciliation_cursors "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "CREATE POLICY tenant_isolation "
        "ON public.private_storage_reconciliation_cursors "
        "USING (organization_id = security.current_organization_id()) "
        "WITH CHECK (organization_id = security.current_organization_id())"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON public.legacy_private_object_deletions, "
        "public.private_storage_reconciliation_cursors "
        "TO hr_agent_runtime, hr_agent_worker"
    )
    op.execute(
        "GRANT SELECT, UPDATE ON public.legacy_private_object_deletions "
        "TO hr_agent_broker"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION security.claim_legacy_private_object_deletions(
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
            SELECT deletion.id, deletion.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.legacy_private_object_deletions AS deletion
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'legacy_private_object_deletions'
               AND existing.work_id = deletion.id
             WHERE deletion.completed_at IS NULL
               AND deletion.not_before <= p_now
               AND deletion.next_attempt_at <= p_now
               AND deletion.attempt_count < 10
               AND (deletion.claimed_at IS NULL
                    OR deletion.claimed_at < p_now - interval '120 seconds')
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY deletion.next_attempt_at, deletion.requested_at, deletion.id
             FOR UPDATE OF deletion SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.legacy_private_object_deletions AS deletion
               SET attempt_count = deletion.attempt_count + 1,
                   claimed_at = p_now,
                   claim_token = candidates.token,
                   updated_at = p_now
              FROM candidates
             WHERE deletion.id = candidates.id
             RETURNING deletion.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'legacy_private_object_deletions', candidates.id,
                   candidates.organization_id, candidates.token, p_worker_id,
                   p_now, p_now + interval '120 seconds'
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
        """
        CREATE OR REPLACE FUNCTION
          security.legacy_private_object_deletion_queue_stats(p_now timestamptz)
        RETURNS TABLE(
          queue_name text, due_count bigint, oldest_due_at timestamptz,
          dlq_count bigint, oldest_dlq_at timestamptz
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT 'legacy_private_object_deletions'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.not_before <= p_now
                     AND deletion.next_attempt_at <= p_now
                     AND deletion.attempt_count < 10
                     AND (deletion.claimed_at IS NULL
                          OR deletion.claimed_at < p_now - interval '120 seconds')
                     AND active.work_id IS NULL),
                 pg_catalog.min(deletion.next_attempt_at) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.not_before <= p_now
                     AND deletion.next_attempt_at <= p_now
                     AND deletion.attempt_count < 10
                     AND (deletion.claimed_at IS NULL
                          OR deletion.claimed_at < p_now - interval '120 seconds')
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.attempt_count >= 10),
                 pg_catalog.min(deletion.updated_at) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.attempt_count >= 10)
            FROM public.legacy_private_object_deletions AS deletion
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'legacy_private_object_deletions'
             AND active.work_id = deletion.id
             AND active.lease_expires_at > p_now
        $function$
        """
    )
    for signature in (
        "claim_legacy_private_object_deletions(text,integer,timestamptz)",
        "legacy_private_object_deletion_queue_stats(timestamptz)",
    ):
        op.execute(f"ALTER FUNCTION security.{signature} OWNER TO hr_agent_broker")
        op.execute(f"REVOKE ALL ON FUNCTION security.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION security.{signature} TO hr_agent_worker")
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "security.legacy_private_object_deletion_queue_stats(timestamptz) "
        "TO hr_agent_runtime"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM public.legacy_private_object_deletions
             WHERE completed_at IS NULL
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade while legacy private-object deletions are incomplete';
          END IF;
        END
        $downgrade$;
        """
    )
    op.execute(
        "DELETE FROM public.worker_queue_leases "
        "WHERE queue_name = 'legacy_private_object_deletions'"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "security.legacy_private_object_deletion_queue_stats(timestamptz)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "security.claim_legacy_private_object_deletions(text,integer,timestamptz)"
    )
    op.drop_table("private_storage_reconciliation_cursors")
    op.drop_index(
        "ix_legacy_private_object_deletions_org_resource",
        table_name="legacy_private_object_deletions",
    )
    op.drop_index(
        "ix_legacy_private_object_deletions_claim",
        table_name="legacy_private_object_deletions",
    )
    op.drop_table("legacy_private_object_deletions")
