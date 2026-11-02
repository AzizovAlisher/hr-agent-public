"""Reconcile security contracts added to already-applied migrations.

Revision ID: fa04b6d8e213
Revises: f9e3a5c7b182
Create Date: 2026-08-23

An early deployed form of D9 did not include the one-time user capability
table and several later-hardened worker function bodies.  Editing D9 repaired
fresh databases but could not repair databases that had already recorded that
revision.  This migration makes both histories converge without rebuilding
shared tables or touching hiring data.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "fa04b6d8e213"
down_revision: str | Sequence[str] | None = "f9e3a5c7b182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EXPECTED_COLUMNS = {
    ("id", "uuid", True, ""),
    ("organization_id", "uuid", True, ""),
    ("user_id", "uuid", True, ""),
    ("created_by_user_id", "uuid", False, ""),
    ("purpose", "character varying(24)", True, ""),
    ("token_digest", "character varying(64)", True, ""),
    ("token_prefix", "character varying(16)", True, ""),
    ("expires_at", "timestamp with time zone", True, ""),
    ("consumed_at", "timestamp with time zone", False, ""),
    ("revoked_at", "timestamp with time zone", False, ""),
    ("created_at", "timestamp with time zone", True, "now()"),
}

_EXPECTED_CONSTRAINTS = {
    "ck_user_access_token_expiry",
    "ck_user_access_token_purpose",
    "ck_user_access_token_terminal_state",
    "fk_tenant_user_access_tokens_created_by_user_id_0e6a47eb",
    "fk_tenant_user_access_tokens_user_id_3a72702e",
    "uq_tenant_user_access_tokens_a8d38991",
    "uq_user_access_tokens_token_digest",
    "user_access_tokens_created_by_user_id_fkey",
    "user_access_tokens_organization_id_fkey",
    "user_access_tokens_pkey",
    "user_access_tokens_user_id_fkey",
}

_EXPECTED_INDEXES = {
    "ix_tenant_user_access_tokens_created_by_user_id_c78f2ba5",
    "ix_tenant_user_access_tokens_user_id_768ead00",
    "ix_user_access_tokens_expires_at",
    "ix_user_access_tokens_org_purpose_created",
    "ix_user_access_tokens_organization_id",
    "ix_user_access_tokens_user_id",
    "uq_tenant_user_access_tokens_a8d38991",
    "uq_user_access_token_active",
    "uq_user_access_tokens_token_digest",
    "user_access_tokens_pkey",
}

_CANONICAL_FUNCTIONS: tuple[str, ...] = (
    r"""CREATE OR REPLACE FUNCTION public.protect_application_stage_event()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
            BEGIN
                IF TG_OP = 'DELETE'
                   AND (
                       current_setting('app.allow_erasure', true) = 'on'
                       OR pg_trigger_depth() > 1
                   ) THEN
                    RETURN OLD;
                END IF;
                -- FK actions such as ON DELETE SET NULL preserve readable
                -- history while a parent identity is erased. PostgreSQL runs
                -- those internal constraint-trigger writes at nested depth;
                -- direct caller UPDATEs remain forbidden.
                IF TG_OP = 'UPDATE' AND pg_trigger_depth() > 1 THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Application stage events are append-only';
            END;
            $function$""",
    r"""CREATE OR REPLACE FUNCTION security.resolve_user_access_token(p_digest text)
 RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
          SELECT token.id, token.organization_id, token.user_id
            FROM public.user_access_tokens AS token
           WHERE token.token_digest = p_digest
           LIMIT 1
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_integration_jobs(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT job.id, job.organization_id, pg_catalog.gen_random_uuid() AS token
              FROM public.integration_jobs AS job
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'integration_jobs'
               AND existing.work_id = job.id
             WHERE job.next_attempt_at <= p_now
               AND job.attempt_count < job.max_attempts
               AND (job.status IN ('queued', 'retrying')
                    OR (job.status = 'running' AND job.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY job.next_attempt_at, job.created_at, job.id
             FOR UPDATE OF job SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.integration_jobs AS job
               SET status = 'running', attempt_count = job.attempt_count + 1,
                   leased_at = p_now, leased_by = p_worker_id,
                   lease_expires_at = p_now + interval '120 seconds',
                   updated_at = p_now
              FROM candidates
             WHERE job.id = candidates.id
             RETURNING job.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'integration_jobs', candidates.id, candidates.organization_id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
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
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_integration_webhook_deliveries(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT delivery.id, delivery.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.integration_webhook_deliveries AS delivery
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'integration_webhook_deliveries'
               AND existing.work_id = delivery.id
             WHERE delivery.status IN ('queued', 'retrying')
               AND delivery.next_attempt_at <= p_now
               AND delivery.attempt_count < delivery.max_attempts
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY delivery.next_attempt_at, delivery.created_at, delivery.id
             FOR UPDATE OF delivery SKIP LOCKED
             LIMIT p_limit
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'integration_webhook_deliveries', candidates.id,
                   candidates.organization_id, candidates.token, p_worker_id,
                   p_now, p_now + interval '120 seconds'
              FROM candidates
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
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_interview_reminders(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT reminder.id, reminder.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.interview_reminders AS reminder
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'interview_reminders'
               AND existing.work_id = reminder.id
             WHERE reminder.status = 'pending' AND reminder.due_at <= p_now
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY reminder.due_at, reminder.created_at, reminder.id
             FOR UPDATE OF reminder SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.interview_reminders AS reminder
               SET claimed_at = p_now
              FROM candidates
             WHERE reminder.id = candidates.id
             RETURNING reminder.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'interview_reminders', candidates.id,
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
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_workflow_runs(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;

          UPDATE public.workflow_runs AS run
             SET status = 'dead_letter', error_code = 'attempts_exhausted',
                 completed_at = p_now, leased_at = NULL, leased_by = NULL,
                 lease_token = NULL, lease_expires_at = NULL
           WHERE run.status = 'running' AND run.lease_expires_at <= p_now
             AND run.attempt_count >= run.max_attempts;
          UPDATE public.workflow_runs AS run
             SET status = 'cancelled', cancelled_at = p_now, completed_at = p_now,
                 leased_at = NULL, leased_by = NULL, lease_token = NULL,
                 lease_expires_at = NULL,
                 error_code = COALESCE(run.error_code, 'rule_disabled')
            FROM public.workflow_rules AS rule
           WHERE run.rule_id = rule.id AND run.organization_id = rule.organization_id
             AND NOT rule.is_active
             AND run.status IN ('queued', 'retrying', 'running');

          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT run.id, run.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.workflow_runs AS run
              JOIN public.workflow_rules AS rule
                ON rule.id = run.rule_id
               AND rule.organization_id = run.organization_id
               AND rule.is_active
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'workflow_runs'
               AND existing.work_id = run.id
             WHERE run.available_at <= p_now
               AND run.attempt_count < run.max_attempts
               AND (run.status IN ('queued', 'retrying')
                    OR (run.status = 'running' AND run.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY run.available_at, run.created_at, run.id
             FOR UPDATE OF run SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.workflow_runs AS run
               SET status = 'running', attempt_count = run.attempt_count + 1,
                   leased_at = p_now, leased_by = p_worker_id,
                   lease_token = candidates.token,
                   lease_expires_at = p_now + interval '120 seconds',
                   started_at = COALESCE(run.started_at, p_now),
                   completed_at = NULL, cancelled_at = NULL, error_code = NULL
              FROM candidates
             WHERE run.id = candidates.id
             RETURNING run.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'workflow_runs', candidates.id, candidates.organization_id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
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
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_mission_steps(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT step.id, step.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.mission_steps AS step
              JOIN public.hiring_missions AS mission
                ON mission.id = step.mission_id
               AND mission.organization_id = step.organization_id
               AND mission.status IN ('planning', 'running')
              JOIN public.ai_organization_settings AS settings
                ON settings.organization_id = step.organization_id
               AND settings.feature_enabled
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'mission_steps'
               AND existing.work_id = step.id
             WHERE ((step.status = 'ready' AND step.available_at <= p_now)
                    OR (step.status = 'running'
                        AND step.lease_expires_at IS NOT NULL
                        AND step.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
               AND (SELECT pg_catalog.count(*)
                      FROM public.mission_step_attempts AS attempt
                     WHERE attempt.step_id = step.id
                       AND attempt.organization_id = step.organization_id)
                   < step.max_attempts
             ORDER BY step.available_at, step.created_at, step.id
             FOR UPDATE OF step SKIP LOCKED
             LIMIT p_limit
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'mission_steps', candidates.id, candidates.organization_id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
              FROM candidates
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
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_private_object_deletions(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
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
              FROM public.private_object_deletions AS deletion
              JOIN public.private_objects AS object
                ON object.id = deletion.private_object_id
               AND object.organization_id = deletion.organization_id
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'private_object_deletions'
               AND existing.work_id = deletion.id
             WHERE deletion.completed_at IS NULL
               AND deletion.not_before <= p_now
               AND deletion.next_attempt_at <= p_now
               AND deletion.attempt_count < 10
               AND object.state = 'delete_pending'
               AND object.legal_hold_at IS NULL
               AND (deletion.claimed_at IS NULL
                    OR deletion.claimed_at < p_now - interval '120 seconds')
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY deletion.next_attempt_at, deletion.requested_at, deletion.id
             FOR UPDATE OF deletion SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.private_object_deletions AS deletion
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
            SELECT 'private_object_deletions', candidates.id,
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
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.worker_queue_stats(p_now timestamp with time zone)
 RETURNS TABLE(queue_name text, due_count bigint, oldest_due_at timestamp with time zone, dlq_count bigint, oldest_dlq_at timestamp with time zone)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
          SELECT 'integration_jobs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE job.next_attempt_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND job.cancelled_at IS NULL
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running'
                              AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN job.status = 'running' THEN job.lease_expires_at
                        ELSE job.next_attempt_at END
                 ) FILTER (
                   WHERE job.next_attempt_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND job.cancelled_at IS NULL
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running'
                              AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE job.status = 'dead_letter'),
                 pg_catalog.min(job.completed_at) FILTER (
                   WHERE job.status = 'dead_letter')
            FROM public.integration_jobs AS job
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'integration_jobs'
             AND active.work_id = job.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'integration_webhook_deliveries'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE delivery.next_attempt_at <= p_now
                     AND delivery.attempt_count < delivery.max_attempts
                     AND delivery.status IN ('queued', 'retrying')
                     AND active.work_id IS NULL),
                 pg_catalog.min(delivery.next_attempt_at) FILTER (
                   WHERE delivery.next_attempt_at <= p_now
                     AND delivery.attempt_count < delivery.max_attempts
                     AND delivery.status IN ('queued', 'retrying')
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE delivery.status = 'dead_letter'),
                 pg_catalog.min(delivery.updated_at) FILTER (
                   WHERE delivery.status = 'dead_letter')
            FROM public.integration_webhook_deliveries AS delivery
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'integration_webhook_deliveries'
             AND active.work_id = delivery.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'communication_messages'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.attempt_count < 10
                     AND ((message.status = 'queued'
                           AND message.scheduled_for <= p_now)
                          OR (message.status = 'processing'
                              AND message.claimed_at < p_now - interval '5 minutes'))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN message.status = 'processing'
                        THEN message.claimed_at + interval '5 minutes'
                        ELSE message.scheduled_for END
                 ) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.attempt_count < 10
                     AND ((message.status = 'queued'
                           AND message.scheduled_for <= p_now)
                          OR (message.status = 'processing'
                              AND message.claimed_at < p_now - interval '5 minutes'))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.status = 'failed'),
                 pg_catalog.min(message.failed_at) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.status = 'failed')
            FROM public.communication_messages AS message
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'communication_messages'
             AND active.work_id = message.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'interview_reminders'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE reminder.status = 'pending'
                     AND reminder.due_at <= p_now
                     AND active.work_id IS NULL),
                 pg_catalog.min(reminder.due_at) FILTER (
                   WHERE reminder.status = 'pending'
                     AND reminder.due_at <= p_now
                     AND active.work_id IS NULL),
                 0::bigint, NULL::timestamptz
            FROM public.interview_reminders AS reminder
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'interview_reminders'
             AND active.work_id = reminder.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'workflow_runs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE rule.is_active
                     AND run.available_at <= p_now
                     AND run.attempt_count < run.max_attempts
                     AND (run.status IN ('queued', 'retrying')
                          OR (run.status = 'running'
                              AND run.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN run.status = 'running' THEN run.lease_expires_at
                        ELSE run.available_at END
                 ) FILTER (
                   WHERE rule.is_active
                     AND run.available_at <= p_now
                     AND run.attempt_count < run.max_attempts
                     AND (run.status IN ('queued', 'retrying')
                          OR (run.status = 'running'
                              AND run.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE run.status = 'dead_letter'),
                 pg_catalog.min(run.completed_at) FILTER (
                   WHERE run.status = 'dead_letter')
            FROM public.workflow_runs AS run
            JOIN public.workflow_rules AS rule
              ON rule.id = run.rule_id
             AND rule.organization_id = run.organization_id
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'workflow_runs'
             AND active.work_id = run.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'mission_steps'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE settings.feature_enabled
                     AND mission.status IN ('planning', 'running')
                     AND attempts.attempt_count < step.max_attempts
                     AND ((step.status = 'ready' AND step.available_at <= p_now)
                          OR (step.status = 'running'
                              AND step.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN step.status = 'running' THEN step.lease_expires_at
                        ELSE step.available_at END
                 ) FILTER (
                   WHERE settings.feature_enabled
                     AND mission.status IN ('planning', 'running')
                     AND attempts.attempt_count < step.max_attempts
                     AND ((step.status = 'ready' AND step.available_at <= p_now)
                          OR (step.status = 'running'
                              AND step.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE step.status = 'failed'),
                 pg_catalog.min(step.completed_at) FILTER (
                   WHERE step.status = 'failed')
            FROM public.mission_steps AS step
            JOIN public.hiring_missions AS mission
              ON mission.id = step.mission_id
             AND mission.organization_id = step.organization_id
            LEFT JOIN public.ai_organization_settings AS settings
              ON settings.organization_id = step.organization_id
            CROSS JOIN LATERAL (
              SELECT pg_catalog.count(*) AS attempt_count
                FROM public.mission_step_attempts AS attempt
               WHERE attempt.step_id = step.id
                 AND attempt.organization_id = step.organization_id
            ) AS attempts
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'mission_steps'
             AND active.work_id = step.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'private_object_deletions'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.not_before <= p_now
                     AND deletion.next_attempt_at <= p_now
                     AND deletion.attempt_count < 10
                     AND object.state = 'delete_pending'
                     AND object.legal_hold_at IS NULL
                     AND (deletion.claimed_at IS NULL
                          OR deletion.claimed_at < p_now - interval '120 seconds')
                     AND active.work_id IS NULL),
                 pg_catalog.min(deletion.next_attempt_at) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.not_before <= p_now
                     AND deletion.next_attempt_at <= p_now
                     AND deletion.attempt_count < 10
                     AND object.state = 'delete_pending'
                     AND object.legal_hold_at IS NULL
                     AND (deletion.claimed_at IS NULL
                          OR deletion.claimed_at < p_now - interval '120 seconds')
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.attempt_count >= 10),
                 pg_catalog.min(deletion.updated_at) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.attempt_count >= 10)
            FROM public.private_object_deletions AS deletion
            JOIN public.private_objects AS object
              ON object.id = deletion.private_object_id
             AND object.organization_id = deletion.organization_id
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'private_object_deletions'
             AND active.work_id = deletion.id
             AND active.lease_expires_at > p_now
        $function$""",
    r"""CREATE OR REPLACE FUNCTION security.claim_maintenance_organizations(p_worker_id text, p_limit integer, p_now timestamp with time zone)
 RETURNS TABLE(work_id uuid, organization_id uuid, lease_token uuid, lease_expires_at timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog'
AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid maintenance claim arguments'
              USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT organization.id, pg_catalog.gen_random_uuid() AS token
              FROM public.organizations AS organization
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'maintenance'
               AND existing.work_id = organization.id
             WHERE existing.work_id IS NULL OR existing.lease_expires_at <= p_now
             ORDER BY organization.id
             FOR UPDATE OF organization SKIP LOCKED
             LIMIT p_limit
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'maintenance', candidates.id, candidates.id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
              FROM candidates
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
        $function$""",
)


def _table_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT pg_catalog.to_regclass("
                "'public.user_access_tokens') IS NOT NULL"
            )
        )
        .scalar_one()
    )


def _create_user_access_tokens() -> None:
    op.create_table(
        "user_access_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose IN ('invitation', 'password_reset')",
            name="ck_user_access_token_purpose",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_user_access_token_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR revoked_at IS NULL",
            name="ck_user_access_token_terminal_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_digest",
            name="uq_user_access_tokens_token_digest",
        ),
    )
    op.create_unique_constraint(
        "uq_tenant_user_access_tokens_a8d38991",
        "user_access_tokens",
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_user_access_tokens_user_id_3a72702e",
        "user_access_tokens",
        "users",
        ["organization_id", "user_id"],
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_user_access_tokens_created_by_user_id_0e6a47eb",
        "user_access_tokens",
        "users",
        ["organization_id", "created_by_user_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_user_access_tokens_organization_id",
        "user_access_tokens",
        ["organization_id"],
    )
    op.create_index(
        "ix_user_access_tokens_user_id",
        "user_access_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_user_access_tokens_expires_at",
        "user_access_tokens",
        ["expires_at"],
    )
    op.create_index(
        "uq_user_access_token_active",
        "user_access_tokens",
        ["organization_id", "user_id", "purpose"],
        unique=True,
        postgresql_where=sa.text(
            "consumed_at IS NULL AND revoked_at IS NULL"
        ),
    )
    op.create_index(
        "ix_user_access_tokens_org_purpose_created",
        "user_access_tokens",
        ["organization_id", "purpose", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_tenant_user_access_tokens_user_id_768ead00",
        "user_access_tokens",
        ["organization_id", "user_id"],
    )
    op.create_index(
        "ix_tenant_user_access_tokens_created_by_user_id_c78f2ba5",
        "user_access_tokens",
        ["organization_id", "created_by_user_id"],
    )
    op.execute(
        "ALTER TABLE public.user_access_tokens ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.user_access_tokens FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation
          ON public.user_access_tokens
          USING (
            organization_id = security.current_organization_id()
          )
          WITH CHECK (
            organization_id = security.current_organization_id()
          )
        """
    )
def _assert_user_access_token_contract() -> None:
    bind = op.get_bind()
    columns = {
        tuple(row)
        for row in bind.execute(
            sa.text(
                """
                SELECT attribute.attname,
                       pg_catalog.format_type(
                         attribute.atttypid,
                         attribute.atttypmod
                       ),
                       attribute.attnotnull,
                       COALESCE(
                         pg_catalog.pg_get_expr(
                           default_value.adbin,
                           default_value.adrelid
                         ),
                         ''
                       )
                  FROM pg_catalog.pg_attribute AS attribute
                  JOIN pg_catalog.pg_class AS relation
                    ON relation.oid = attribute.attrelid
                  JOIN pg_catalog.pg_namespace AS namespace
                    ON namespace.oid = relation.relnamespace
                  LEFT JOIN pg_catalog.pg_attrdef AS default_value
                    ON default_value.adrelid = attribute.attrelid
                   AND default_value.adnum = attribute.attnum
                 WHERE namespace.nspname = 'public'
                   AND relation.relname = 'user_access_tokens'
                   AND attribute.attnum > 0
                   AND NOT attribute.attisdropped
                """
            )
        )
    }
    if columns != _EXPECTED_COLUMNS:
        raise RuntimeError(
            "Unknown partial public.user_access_tokens column contract; "
            "refusing automatic reconciliation"
        )

    constraints = {
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT constraint_record.conname
                  FROM pg_catalog.pg_constraint AS constraint_record
                 WHERE constraint_record.conrelid =
                       'public.user_access_tokens'::regclass
                """
            )
        )
    }
    if constraints != _EXPECTED_CONSTRAINTS:
        raise RuntimeError(
            "Unknown partial public.user_access_tokens constraints; "
            "refusing automatic reconciliation"
        )

    indexes = {
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT index_record.indexname
                  FROM pg_catalog.pg_indexes AS index_record
                 WHERE index_record.schemaname = 'public'
                   AND index_record.tablename = 'user_access_tokens'
                """
            )
        )
    }
    if indexes != _EXPECTED_INDEXES:
        raise RuntimeError(
            "Unknown partial public.user_access_tokens indexes; "
            "refusing automatic reconciliation"
        )

    rls = tuple(
        bind.execute(
            sa.text(
                """
                SELECT relation.relrowsecurity,
                       relation.relforcerowsecurity
                  FROM pg_catalog.pg_class AS relation
                 WHERE relation.oid =
                       'public.user_access_tokens'::regclass
                """
            )
        ).one()
    )
    if rls != (True, True):
        raise RuntimeError(
            "public.user_access_tokens must enable and force tenant RLS"
        )

    policy = bind.execute(
        sa.text(
            """
            SELECT policy.cmd, policy.qual, policy.with_check
              FROM pg_catalog.pg_policies AS policy
             WHERE policy.schemaname = 'public'
               AND policy.tablename = 'user_access_tokens'
               AND policy.policyname = 'tenant_isolation'
            """
        )
    ).one_or_none()
    if (
        policy is None
        or policy[0] != "ALL"
        or "security.current_organization_id()" not in (policy[1] or "")
        or "security.current_organization_id()" not in (policy[2] or "")
    ):
        raise RuntimeError(
            "public.user_access_tokens tenant policy is missing or unsafe"
        )


def _replace_security_functions_and_acl() -> None:
    for definition in _CANONICAL_FUNCTIONS:
        op.execute(definition)

    op.execute(
        "ALTER FUNCTION security.resolve_user_access_token(text) "
        "OWNER TO hr_agent_broker"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "security.resolve_user_access_token(text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "security.resolve_user_access_token(text) TO hr_agent_runtime"
    )

    worker_signatures = (
        "claim_integration_jobs(text,integer,timestamptz)",
        "claim_integration_webhook_deliveries(text,integer,timestamptz)",
        "claim_interview_reminders(text,integer,timestamptz)",
        "claim_workflow_runs(text,integer,timestamptz)",
        "claim_mission_steps(text,integer,timestamptz)",
        "claim_private_object_deletions(text,integer,timestamptz)",
        "claim_maintenance_organizations(text,integer,timestamptz)",
        "worker_queue_stats(timestamptz)",
    )
    for signature in worker_signatures:
        op.execute(
            f"ALTER FUNCTION security.{signature} OWNER TO hr_agent_broker"
        )
        op.execute(
            f"REVOKE ALL ON FUNCTION security.{signature} FROM PUBLIC"
        )
        op.execute(
            f"GRANT EXECUTE ON FUNCTION security.{signature} "
            "TO hr_agent_worker"
        )
    op.execute(
        "GRANT EXECUTE ON FUNCTION security.worker_queue_stats(timestamptz) "
        "TO hr_agent_runtime"
    )


def _grant_canonical_relation_access() -> None:
    op.execute(
        "REVOKE ALL ON TABLE public.user_access_tokens FROM PUBLIC"
    )
    op.execute(
        "GRANT SELECT ON TABLE public.user_access_tokens "
        "TO hr_agent_broker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLE public.user_access_tokens "
        "TO hr_agent_runtime, hr_agent_worker"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE "
        "ON TABLE public.ai_organization_settings, "
        "public.hiring_missions TO hr_agent_broker"
    )


def upgrade() -> None:
    if not _table_exists():
        _create_user_access_tokens()
    _assert_user_access_token_contract()
    _replace_security_functions_and_acl()
    _grant_canonical_relation_access()


def downgrade() -> None:
    # This revision repairs the schema contract already declared by D9 and
    # used by the E9 ORM.  Reverting the repair would make an E9 deployment
    # internally inconsistent and could destroy live invitation/reset tokens.
    # The table is removed only when downgrading across D9 itself.
    pass
