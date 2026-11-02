"""Complete immutable workflows, provider timing, and legacy history.

Revision ID: fd37e1b2c546
Revises: fc26d0a1b435
Create Date: 2026-08-23

The migration is deliberately data preserving. Existing workflow runs receive
an executable copy of their rule, provider attempts receive conservative
lifecycle timestamps, and the first application event receives the source and
owner snapshot that was already stored on the application aggregate.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "fd37e1b2c546"
down_revision: str | Sequence[str] | None = "fc26d0a1b435"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_workflow_materializer(*, immutable: bool) -> None:
    if immutable:
        columns = """
             source_activity_id, rule_name_snapshot, trigger_snapshot,
             rule_id_snapshot, rule_version_snapshot, conditions_snapshot,
             actions_snapshot, dedupe_key, status, action_results, available_at,
             attempt_count, max_attempts, started_at, created_at"""
        values = """
                 NEW.id, activity_rule.name, activity_rule.trigger,
                 activity_rule.id, activity_rule.version,
                 activity_rule.conditions, activity_rule.actions,
                 'activity:' || NEW.id::text, 'queued', '[]'::jsonb,
                 NEW.occurred_at, 0, 3, NEW.occurred_at, pg_catalog.now()"""
    else:
        columns = """
             source_activity_id, rule_name_snapshot, trigger_snapshot,
             dedupe_key, status, action_results, available_at, attempt_count,
             max_attempts, started_at, created_at"""
        values = """
                 NEW.id, activity_rule.name, activity_rule.trigger,
                 'activity:' || NEW.id::text, 'queued', '[]'::jsonb,
                 NEW.occurred_at, 0, 3, NEW.occurred_at, pg_catalog.now()"""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION security.materialize_workflow_activity()
        RETURNS trigger
        LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog
        AS $function$
        BEGIN
          INSERT INTO public.workflow_runs
            (id, organization_id, rule_id, candidate_id, position_id,
             {columns})
          SELECT pg_catalog.gen_random_uuid(), activity_rule.organization_id,
                 activity_rule.id, NEW.candidate_id, NEW.position_id,
                 {values}
            FROM public.workflow_rules AS activity_rule
           WHERE activity_rule.organization_id = NEW.organization_id
             AND activity_rule.is_active
             AND activity_rule.trigger = NEW.event_type
             AND activity_rule.starts_at <= NEW.occurred_at
             AND activity_rule.trigger NOT IN ('candidate_inactive', 'feedback_overdue')
          ON CONFLICT (rule_id, dedupe_key) DO NOTHING;
          RETURN NEW;
        END
        $function$;
        """
    )


def _replace_workflow_claim(*, immutable: bool) -> None:
    cancellation = ""
    rule_join = ""
    if not immutable:
        cancellation = """
          UPDATE public.workflow_runs AS run
             SET status = 'cancelled', cancelled_at = p_now, completed_at = p_now,
                 leased_at = NULL, leased_by = NULL, lease_token = NULL,
                 lease_expires_at = NULL,
                 error_code = COALESCE(run.error_code, 'rule_disabled')
            FROM public.workflow_rules AS rule
           WHERE run.rule_id = rule.id AND run.organization_id = rule.organization_id
             AND NOT rule.is_active
             AND run.status IN ('queued', 'retrying', 'running');
        """
        rule_join = """
              JOIN public.workflow_rules AS rule
                ON rule.id = run.rule_id
               AND rule.organization_id = run.organization_id
               AND rule.is_active"""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION security.claim_workflow_runs(
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

          UPDATE public.workflow_runs AS run
             SET status = 'dead_letter', error_code = 'attempts_exhausted',
                 completed_at = p_now, leased_at = NULL, leased_by = NULL,
                 lease_token = NULL, lease_expires_at = NULL
           WHERE run.status = 'running' AND run.lease_expires_at <= p_now
             AND run.attempt_count >= run.max_attempts;
          {cancellation}

          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT run.id, run.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.workflow_runs AS run
              {rule_join}
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
        $function$;
        """
    )


def _replace_worker_queue_stats(*, immutable: bool) -> None:
    workflow_where = "" if immutable else "rule.is_active AND "
    workflow_join = "" if immutable else """
            JOIN public.workflow_rules AS rule
              ON rule.id = run.rule_id
             AND rule.organization_id = run.organization_id"""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION security.worker_queue_stats(p_now timestamptz)
        RETURNS TABLE(
          queue_name text, due_count bigint, oldest_due_at timestamptz,
          dlq_count bigint, oldest_dlq_at timestamptz
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT 'integration_jobs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE job.next_attempt_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND job.cancelled_at IS NULL
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running' AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(CASE WHEN job.status = 'running'
                   THEN job.lease_expires_at ELSE job.next_attempt_at END) FILTER (
                   WHERE job.next_attempt_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND job.cancelled_at IS NULL
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running' AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE job.status = 'dead_letter'),
                 pg_catalog.min(job.completed_at) FILTER (WHERE job.status = 'dead_letter')
            FROM public.integration_jobs AS job
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'integration_jobs'
             AND active.work_id = job.id AND active.lease_expires_at > p_now
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
                 pg_catalog.count(*) FILTER (WHERE delivery.status = 'dead_letter'),
                 pg_catalog.min(delivery.updated_at) FILTER (
                   WHERE delivery.status = 'dead_letter')
            FROM public.integration_webhook_deliveries AS delivery
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'integration_webhook_deliveries'
             AND active.work_id = delivery.id AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'communication_messages'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.attempt_count < 10
                     AND ((message.status = 'queued' AND message.scheduled_for <= p_now)
                          OR (message.status = 'processing'
                              AND message.claimed_at < p_now - interval '5 minutes'))
                     AND active.work_id IS NULL),
                 pg_catalog.min(CASE WHEN message.status = 'processing'
                   THEN message.claimed_at + interval '5 minutes'
                   ELSE message.scheduled_for END) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.attempt_count < 10
                     AND ((message.status = 'queued' AND message.scheduled_for <= p_now)
                          OR (message.status = 'processing'
                              AND message.claimed_at < p_now - interval '5 minutes'))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE message.direction = 'outbound' AND message.status = 'failed'),
                 pg_catalog.min(message.failed_at) FILTER (
                   WHERE message.direction = 'outbound' AND message.status = 'failed')
            FROM public.communication_messages AS message
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'communication_messages'
             AND active.work_id = message.id AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'interview_reminders'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE reminder.status = 'pending' AND reminder.due_at <= p_now
                     AND active.work_id IS NULL),
                 pg_catalog.min(reminder.due_at) FILTER (
                   WHERE reminder.status = 'pending' AND reminder.due_at <= p_now
                     AND active.work_id IS NULL),
                 0::bigint, NULL::timestamptz
            FROM public.interview_reminders AS reminder
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'interview_reminders'
             AND active.work_id = reminder.id AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'workflow_runs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE {workflow_where}run.available_at <= p_now
                     AND run.attempt_count < run.max_attempts
                     AND (run.status IN ('queued', 'retrying')
                          OR (run.status = 'running' AND run.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(CASE WHEN run.status = 'running'
                   THEN run.lease_expires_at ELSE run.available_at END) FILTER (
                   WHERE {workflow_where}run.available_at <= p_now
                     AND run.attempt_count < run.max_attempts
                     AND (run.status IN ('queued', 'retrying')
                          OR (run.status = 'running' AND run.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE run.status = 'dead_letter'),
                 pg_catalog.min(run.completed_at) FILTER (WHERE run.status = 'dead_letter')
            FROM public.workflow_runs AS run
            {workflow_join}
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'workflow_runs'
             AND active.work_id = run.id AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'mission_steps'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE settings.feature_enabled
                     AND mission.status IN ('planning', 'running')
                     AND attempts.attempt_count < step.max_attempts
                     AND ((step.status = 'ready' AND step.available_at <= p_now)
                          OR (step.status = 'running' AND step.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(CASE WHEN step.status = 'running'
                   THEN step.lease_expires_at ELSE step.available_at END) FILTER (
                   WHERE settings.feature_enabled
                     AND mission.status IN ('planning', 'running')
                     AND attempts.attempt_count < step.max_attempts
                     AND ((step.status = 'ready' AND step.available_at <= p_now)
                          OR (step.status = 'running' AND step.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE step.status = 'failed'),
                 pg_catalog.min(step.completed_at) FILTER (WHERE step.status = 'failed')
            FROM public.mission_steps AS step
            JOIN public.hiring_missions AS mission
              ON mission.id = step.mission_id AND mission.organization_id = step.organization_id
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
             AND active.work_id = step.id AND active.lease_expires_at > p_now
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
                   WHERE deletion.completed_at IS NULL AND deletion.attempt_count >= 10),
                 pg_catalog.min(deletion.updated_at) FILTER (
                   WHERE deletion.completed_at IS NULL AND deletion.attempt_count >= 10)
            FROM public.private_object_deletions AS deletion
            JOIN public.private_objects AS object
              ON object.id = deletion.private_object_id
             AND object.organization_id = deletion.organization_id
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'private_object_deletions'
             AND active.work_id = deletion.id AND active.lease_expires_at > p_now
        $function$;
        """
    )


def _recreate_application_history_trigger() -> None:
    op.execute(
        "CREATE TRIGGER trg_application_stage_events_append_only "
        "BEFORE UPDATE OR DELETE ON application_stage_events "
        "FOR EACH ROW EXECUTE FUNCTION protect_application_stage_event()"
    )


def upgrade() -> None:
    op.add_column(
        "workflow_rules",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_workflow_rule_version", "workflow_rules", "version >= 1"
    )

    op.add_column(
        "workflow_runs",
        sa.Column("rule_id_snapshot", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("rule_version_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("conditions_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("actions_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("replay_of_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE workflow_runs AS run
           SET rule_id_snapshot = COALESCE(
                   run.rule_id,
                   (SELECT rule.id FROM workflow_rules AS rule
                     WHERE rule.id = run.rule_id),
                   run.id
               ),
               rule_version_snapshot = COALESCE(
                   (SELECT rule.version FROM workflow_rules AS rule
                     WHERE rule.id = run.rule_id), 1
               ),
               conditions_snapshot = COALESCE(
                   (SELECT rule.conditions FROM workflow_rules AS rule
                     WHERE rule.id = run.rule_id), '{}'::jsonb
               ),
               actions_snapshot = COALESCE(
                   (SELECT rule.actions FROM workflow_rules AS rule
                     WHERE rule.id = run.rule_id), '[]'::jsonb
               )
        """
    )
    for column, existing_type, default in (
        ("rule_id_snapshot", postgresql.UUID(as_uuid=True), None),
        ("rule_version_snapshot", sa.Integer(), sa.text("1")),
        ("conditions_snapshot", postgresql.JSONB(), sa.text("'{}'::jsonb")),
        ("actions_snapshot", postgresql.JSONB(), sa.text("'[]'::jsonb")),
    ):
        op.alter_column(
            "workflow_runs",
            column,
            existing_type=existing_type,
            nullable=False,
            server_default=default,
        )
    op.create_unique_constraint(
        "uq_workflow_runs_org_id", "workflow_runs", ["organization_id", "id"]
    )
    op.create_foreign_key(
        "fk_workflow_run_tenant_replay_source",
        "workflow_runs",
        "workflow_runs",
        ["organization_id", "replay_of_run_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_workflow_run_rule_version_snapshot",
        "workflow_runs",
        "rule_version_snapshot >= 1",
    )
    op.create_check_constraint(
        "ck_workflow_run_conditions_snapshot_object",
        "workflow_runs",
        "jsonb_typeof(conditions_snapshot) = 'object'",
    )
    op.create_check_constraint(
        "ck_workflow_run_actions_snapshot_array",
        "workflow_runs",
        "jsonb_typeof(actions_snapshot) = 'array'",
    )
    op.create_index(
        "uq_workflow_run_one_replay",
        "workflow_runs",
        ["replay_of_run_id"],
        unique=True,
        postgresql_where=sa.text("replay_of_run_id IS NOT NULL"),
    )

    op.add_column(
        "provider_assessments",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_assessments",
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE provider_assessments
           SET started_at = provider_updated_at
         WHERE status IN ('started', 'submitted', 'graded')
           AND provider_updated_at IS NOT NULL
           AND started_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE provider_assessments AS assessment
           SET submitted_at = COALESCE(
                 (SELECT result.completed_at
                    FROM provider_assessment_results AS result
                   WHERE result.provider_assessment_id = assessment.id),
                 assessment.provider_updated_at
               )
         WHERE assessment.status IN ('submitted', 'graded')
           AND assessment.submitted_at IS NULL
        """
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_application_stage_events_append_only "
        "ON application_stage_events"
    )
    op.execute(
        """
        WITH earliest AS (
          SELECT DISTINCT ON (event.application_id)
                 event.id, event.application_id
            FROM application_stage_events AS event
           ORDER BY event.application_id, event.occurred_at, event.id
        )
        UPDATE application_stage_events AS event
           SET details = COALESCE(event.details, '{}'::jsonb)
                       || jsonb_build_object(
                            'source', application.source,
                            'owner_user_id', CASE
                              WHEN application.owner_user_id IS NULL THEN NULL
                              ELSE application.owner_user_id::text
                            END,
                            'legacy_snapshot_backfill', true
                          )
          FROM earliest
          JOIN applications AS application
            ON application.id = earliest.application_id
         WHERE event.id = earliest.id
           AND (
             NOT COALESCE(event.details, '{}'::jsonb) ? 'source'
             OR NOT COALESCE(event.details, '{}'::jsonb) ? 'owner_user_id'
           )
        """
    )
    _recreate_application_history_trigger()

    _replace_workflow_materializer(immutable=True)
    _replace_workflow_claim(immutable=True)
    _replace_worker_queue_stats(immutable=True)


def downgrade() -> None:
    _replace_worker_queue_stats(immutable=False)
    _replace_workflow_claim(immutable=False)
    _replace_workflow_materializer(immutable=False)

    op.execute(
        "DROP TRIGGER IF EXISTS trg_application_stage_events_append_only "
        "ON application_stage_events"
    )
    op.execute(
        """
        UPDATE application_stage_events
           SET details = details
                         - 'source'
                         - 'owner_user_id'
                         - 'legacy_snapshot_backfill'
         WHERE details @> '{"legacy_snapshot_backfill": true}'::jsonb
        """
    )
    _recreate_application_history_trigger()

    op.drop_column("provider_assessments", "submitted_at")
    op.drop_column("provider_assessments", "started_at")

    op.drop_index("uq_workflow_run_one_replay", table_name="workflow_runs")
    op.drop_constraint(
        "ck_workflow_run_actions_snapshot_array", "workflow_runs", type_="check"
    )
    op.drop_constraint(
        "ck_workflow_run_conditions_snapshot_object",
        "workflow_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_workflow_run_rule_version_snapshot", "workflow_runs", type_="check"
    )
    op.drop_constraint(
        "fk_workflow_run_tenant_replay_source", "workflow_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_workflow_runs_org_id", "workflow_runs", type_="unique"
    )
    for column in (
        "replay_of_run_id",
        "actions_snapshot",
        "conditions_snapshot",
        "rule_version_snapshot",
        "rule_id_snapshot",
    ):
        op.drop_column("workflow_runs", column)
    op.drop_constraint("ck_workflow_rule_version", "workflow_rules", type_="check")
    op.drop_column("workflow_rules", "version")
