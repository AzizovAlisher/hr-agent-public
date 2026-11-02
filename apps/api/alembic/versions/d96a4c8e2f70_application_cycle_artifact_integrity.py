"""Bind application-scoped artifacts to one exact application cycle.

Revision ID: d96a4c8e2f70
Revises: c85f2a6d1e43
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "d96a4c8e2f70"
down_revision: str | Sequence[str] | None = "c85f2a6d1e43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_APPLICATION_TABLES = (
    "candidate_activities",
    "candidate_processing_jobs",
    "chat_sessions",
    "communication_threads",
    "interview_recordings",
    "interview_scheduling_links",
    "interview_appointments",
    "source_candidate_receipts",
    "task_submissions",
    "workflow_runs",
)

# Parent erasure must delete artifacts before application-shape checks can see
# a half-null row. The final item records the exact pre-d96 action restored by
# downgrade; ``None`` is PostgreSQL's legacy NO ACTION default.
_ARTIFACT_PARENT_FKS = (
    (
        "candidate_activities",
        "fk_tenant_candidate_activities_candidate_id_52e0a2a1",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        None,
    ),
    (
        "candidate_activities",
        "candidate_activities_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "candidate_activities",
        "fk_tenant_candidate_activities_position_id_24faada0",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "chat_sessions",
        "chat_sessions_candidate_id_fkey",
        ("candidate_id",),
        "candidates",
        ("id",),
        "SET NULL",
    ),
    (
        "chat_sessions",
        "fk_tenant_chat_sessions_candidate_id_9b98ddbf",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        "SET NULL (candidate_id)",
    ),
    (
        "chat_sessions",
        "chat_sessions_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "chat_sessions",
        "fk_tenant_chat_sessions_position_id_5c9e717e",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "communication_threads",
        "fk_tenant_communication_threads_candidate_id_a246e7b2",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        None,
    ),
    (
        "communication_threads",
        "communication_threads_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "communication_threads",
        "fk_tenant_communication_threads_position_id_f9c45fb6",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "interview_recordings",
        "fk_tenant_interview_recordings_candidate_id_fd5d9890",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        None,
    ),
    (
        "interview_recordings",
        "interview_recordings_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "interview_recordings",
        "fk_tenant_interview_recordings_position_id_7fefeb6d",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "interview_scheduling_links",
        "fk_tenant_interview_scheduling_links_candidate_id_de9693e3",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        None,
    ),
    (
        "interview_scheduling_links",
        "fk_tenant_interview_scheduling_links_position_id_d9629534",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        None,
    ),
    (
        "interview_appointments",
        "fk_tenant_interview_appointments_candidate_id_fad37814",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        None,
    ),
    (
        "interview_appointments",
        "interview_appointments_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "interview_appointments",
        "fk_tenant_interview_appointments_position_id_500e3971",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "source_candidate_receipts",
        "source_candidate_receipts_candidate_id_fkey",
        ("candidate_id",),
        "candidates",
        ("id",),
        "SET NULL",
    ),
    (
        "source_candidate_receipts",
        "fk_tenant_source_candidate_receipts_candidate_id_7b2b84a0",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        "SET NULL (candidate_id)",
    ),
    (
        "source_candidate_receipts",
        "source_candidate_receipts_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "source_candidate_receipts",
        "fk_tenant_source_candidate_receipts_position_id_7d40d8ac",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "task_submissions",
        "fk_tenant_task_submissions_candidate_id_6d66e9e2",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        None,
    ),
    (
        "task_submissions",
        "task_submissions_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "task_submissions",
        "fk_tenant_task_submissions_position_id_6479ec46",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
    (
        "workflow_runs",
        "workflow_runs_candidate_id_fkey",
        ("candidate_id",),
        "candidates",
        ("id",),
        "SET NULL",
    ),
    (
        "workflow_runs",
        "fk_tenant_workflow_runs_candidate_id_a062019c",
        ("organization_id", "candidate_id"),
        "candidates",
        ("organization_id", "id"),
        "SET NULL (candidate_id)",
    ),
    (
        "workflow_runs",
        "workflow_runs_position_id_fkey",
        ("position_id",),
        "positions",
        ("id",),
        "SET NULL",
    ),
    (
        "workflow_runs",
        "fk_tenant_workflow_runs_position_id_e1ef894a",
        ("organization_id", "position_id"),
        "positions",
        ("organization_id", "id"),
        "SET NULL (position_id)",
    ),
)

def _add_application_columns() -> None:
    # Every column starts nullable so legacy rows can be classified before any
    # constraint is installed. Scheduling links become NOT NULL after backfill.
    for table in _APPLICATION_TABLES:
        op.add_column(
            table,
            sa.Column(
                "application_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )


def _rewrite_artifact_parent_fks(*, restore_legacy: bool) -> None:
    for (
        table,
        name,
        local_columns,
        parent_table,
        remote_columns,
        legacy_ondelete,
    ) in _ARTIFACT_PARENT_FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name,
            table,
            parent_table,
            list(local_columns),
            list(remote_columns),
            ondelete=legacy_ondelete if restore_legacy else "CASCADE",
        )


def _replace_workflow_materializer(*, application_cycle: bool) -> None:
    """Keep trigger-created runs on the activity's exact application cycle.

    The legacy branch deliberately reproduces the c85 function body byte for
    byte (after PostgreSQL's normal ``pg_get_functiondef`` rendering) so a
    downgrade restores the pre-d96 database contract exactly.
    """

    if application_cycle:
        columns = """
             application_id, source_activity_id, rule_name_snapshot,
             trigger_snapshot, rule_id_snapshot, rule_version_snapshot,
             conditions_snapshot, actions_snapshot, dedupe_key, status,
             action_results, available_at, attempt_count, max_attempts,
             started_at, created_at"""
        values = """
                 NEW.application_id, NEW.id, activity_rule.name,
                 activity_rule.trigger, activity_rule.id, activity_rule.version,
                 activity_rule.conditions, activity_rule.actions,
                 'activity:' || NEW.id::text, 'queued', '[]'::jsonb,
                 NEW.occurred_at, 0, 3, NEW.occurred_at, pg_catalog.now()"""
    else:
        # This is the immutable materializer installed by fd37e1b2c546.
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


def _backfill_pair(
    table: str,
    *,
    required_sql: str,
    timestamp_sql: str,
    authoritative_sql: str = "NULL::uuid",
) -> None:
    """Choose parent evidence, then the latest preceding cycle, then a sole cycle."""

    op.execute(
        sa.text(
            f"""
            UPDATE {table} AS target
               SET application_id = COALESCE(
                   ({authoritative_sql}),
                   (
                       SELECT application.id
                         FROM applications AS application
                        WHERE application.organization_id = target.organization_id
                          AND application.candidate_id = target.candidate_id
                          AND application.position_id = target.position_id
                          AND application.applied_at <= ({timestamp_sql})
                        ORDER BY application.applied_at DESC,
                                 application.cycle_number DESC,
                                 application.id
                        LIMIT 1
                   ),
                   (
                       SELECT sole.id
                         FROM (
                             SELECT application.id,
                                    count(*) OVER () AS pair_count
                               FROM applications AS application
                              WHERE application.organization_id = target.organization_id
                                AND application.candidate_id = target.candidate_id
                                AND application.position_id = target.position_id
                         ) AS sole
                        WHERE sole.pair_count = 1
                        ORDER BY sole.id
                        LIMIT 1
                   )
               )
             WHERE {required_sql}
            """
        )
    )


def _backfill_applications() -> None:
    _backfill_pair(
        "candidate_activities",
        required_sql="target.position_id IS NOT NULL",
        timestamp_sql="target.occurred_at",
        authoritative_sql="""
            COALESCE(
                (
                    SELECT application.id
                      FROM applications AS application
                     WHERE target.subject_type = 'application'
                       AND application.id = target.subject_id
                ),
                (
                    SELECT application.id
                      FROM applications AS application
                     WHERE target.subject_type = 'score'
                       AND application.legacy_score_id = target.subject_id
                ),
                (
                    SELECT interview.application_id
                      FROM interviews AS interview
                     WHERE target.subject_type = 'interview'
                       AND interview.id = target.subject_id
                ),
                (
                    SELECT offer.application_id
                      FROM candidate_offers AS offer
                     WHERE target.subject_type = 'candidate_offer'
                       AND offer.id = target.subject_id
                )
            )
        """,
    )
    _backfill_pair(
        "candidate_processing_jobs",
        required_sql="target.kind = 'score'",
        timestamp_sql="target.created_at",
    )
    _backfill_pair(
        "chat_sessions",
        required_sql=(
            "target.candidate_id IS NOT NULL AND target.position_id IS NOT NULL"
        ),
        timestamp_sql="target.created_at",
    )
    _backfill_pair(
        "communication_threads",
        required_sql="target.position_id IS NOT NULL",
        timestamp_sql="target.created_at",
    )
    _backfill_pair(
        "interview_recordings",
        required_sql="target.position_id IS NOT NULL",
        timestamp_sql="COALESCE(target.recorded_at, target.created_at)",
    )
    _backfill_pair(
        "interview_scheduling_links",
        required_sql="TRUE",
        timestamp_sql="target.created_at",
        authoritative_sql="""
            SELECT interview.application_id
              FROM interviews AS interview
             WHERE interview.id = target.interview_id
        """,
    )
    _backfill_pair(
        "interview_appointments",
        required_sql="target.position_id IS NOT NULL",
        timestamp_sql="target.created_at",
        authoritative_sql="""
            COALESCE(
                (
                    SELECT scheduling_link.application_id
                      FROM interview_scheduling_links AS scheduling_link
                     WHERE scheduling_link.id = target.scheduling_link_id
                ),
                (
                    SELECT interview.application_id
                      FROM interviews AS interview
                     WHERE interview.id = target.interview_id
                )
            )
        """,
    )
    _backfill_pair(
        "source_candidate_receipts",
        required_sql=(
            "target.candidate_id IS NOT NULL AND target.position_id IS NOT NULL"
        ),
        timestamp_sql="target.received_at",
    )
    _backfill_pair(
        "task_submissions",
        required_sql="target.position_id IS NOT NULL",
        timestamp_sql="target.created_at",
    )
    _backfill_pair(
        "workflow_runs",
        required_sql=(
            "target.candidate_id IS NOT NULL AND target.position_id IS NOT NULL"
        ),
        timestamp_sql="target.created_at",
        authoritative_sql="""
            SELECT activity.application_id
              FROM candidate_activities AS activity
             WHERE activity.id = target.source_activity_id
        """,
    )

    # A migration that guesses across repeated cycles is more dangerous than a
    # blocked deployment. The exception is transaction-scoped, so no partial
    # schema or backfill survives an unresolved row.
    op.execute(
        """
        DO $application_backfill$
        DECLARE unresolved_count bigint;
        BEGIN
          SELECT sum(unresolved) INTO unresolved_count
            FROM (
              SELECT count(*) FILTER (WHERE position_id IS NOT NULL AND application_id IS NULL) AS unresolved
                FROM candidate_activities
              UNION ALL
              SELECT count(*) FILTER (WHERE kind = 'score' AND application_id IS NULL)
                FROM candidate_processing_jobs
              UNION ALL
              SELECT count(*) FILTER (
                  WHERE candidate_id IS NOT NULL AND position_id IS NOT NULL
                    AND application_id IS NULL
              ) FROM chat_sessions
              UNION ALL
              SELECT count(*) FILTER (WHERE position_id IS NOT NULL AND application_id IS NULL)
                FROM communication_threads
              UNION ALL
              SELECT count(*) FILTER (WHERE position_id IS NOT NULL AND application_id IS NULL)
                FROM interview_recordings
              UNION ALL
              SELECT count(*) FILTER (WHERE application_id IS NULL)
                FROM interview_scheduling_links
              UNION ALL
              SELECT count(*) FILTER (WHERE position_id IS NOT NULL AND application_id IS NULL)
                FROM interview_appointments
              UNION ALL
              SELECT count(*) FILTER (
                  WHERE candidate_id IS NOT NULL AND position_id IS NOT NULL
                    AND application_id IS NULL
              ) FROM source_candidate_receipts
              UNION ALL
              SELECT count(*) FILTER (WHERE position_id IS NOT NULL AND application_id IS NULL)
                FROM task_submissions
              UNION ALL
              SELECT count(*) FILTER (
                  WHERE candidate_id IS NOT NULL AND position_id IS NOT NULL
                    AND application_id IS NULL
              ) FROM workflow_runs
            ) AS unresolved_rows;
          IF unresolved_count <> 0 THEN
            RAISE EXCEPTION
              'd96a4c8e2f70 cannot resolve % application-scoped artifact rows',
              unresolved_count;
          END IF;
        END
        $application_backfill$;
        """
    )


def _replace_active_score_index() -> None:
    # Legacy uniqueness was candidate-position scoped, so a queued job from a
    # closed cycle could suppress scoring for a later application. Terminalize
    # those stale jobs before changing the index, including any durable lease
    # record that could otherwise let a worker execute an already-cancelled job.
    op.execute(
        """
        WITH cancelled AS (
          UPDATE candidate_processing_jobs AS job
             SET status = 'cancelled',
                 cancelled_at = greatest(
                     job.updated_at,
                     job.created_at,
                     application.closed_at,
                     application.archived_at
                 ),
                 completed_at = greatest(
                     job.updated_at,
                     job.created_at,
                     application.closed_at,
                     application.archived_at
                 ),
                 leased_at = NULL,
                 leased_by = NULL,
                 lease_expires_at = NULL,
                 failure_code = 'application_cycle_inactive',
                 failure_detail =
                     'Cancelled during migration because the application cycle is inactive.',
                 result_summary = jsonb_build_object(
                     'status', 'cancelled',
                     'reason', 'application_cycle_inactive',
                     'application_id', application.id::text
                 ),
                 updated_at = greatest(
                     job.updated_at,
                     job.created_at,
                     application.closed_at,
                     application.archived_at
                 )
            FROM applications AS application
           WHERE job.organization_id = application.organization_id
             AND job.application_id = application.id
             AND job.kind = 'score'
             AND job.status IN ('queued', 'running', 'retrying')
             AND (
                 application.closed_at IS NOT NULL
                 OR application.archived_at IS NOT NULL
             )
          RETURNING job.id
        )
        DELETE FROM worker_queue_leases AS lease
         USING cancelled
         WHERE lease.queue_name = 'candidate_processing_jobs'
           AND lease.work_id = cancelled.id
        """
    )
    op.drop_index(
        "uq_candidate_processing_active_score",
        table_name="candidate_processing_jobs",
    )
    op.create_index(
        "uq_candidate_processing_active_score",
        "candidate_processing_jobs",
        ["organization_id", "application_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'score' AND status IN ('queued', 'running', 'retrying')"
        ),
    )


def _restore_legacy_active_score_index() -> None:
    # The cycle-specific index can legally contain one active job per historic
    # application. Before recreating the old pair-scoped index, retain at most
    # the job for the current active cycle and terminalize every stale/duplicate
    # row while its application identity is still available.
    op.execute(
        """
        WITH ranked AS (
          SELECT job.id,
                 application.id AS application_id,
                 application.closed_at,
                 application.archived_at,
                 row_number() OVER (
                   PARTITION BY job.organization_id,
                                job.candidate_id,
                                job.position_id
                   ORDER BY
                     (application.closed_at IS NULL
                      AND application.archived_at IS NULL) DESC,
                     application.cycle_number DESC,
                     application.applied_at DESC,
                     job.created_at DESC,
                     job.id DESC
                 ) AS legacy_rank
            FROM candidate_processing_jobs AS job
            JOIN applications AS application
              ON application.organization_id = job.organization_id
             AND application.id = job.application_id
           WHERE job.kind = 'score'
             AND job.status IN ('queued', 'running', 'retrying')
        ), cancelled AS (
          UPDATE candidate_processing_jobs AS job
             SET status = 'cancelled',
                 cancelled_at = greatest(
                     job.updated_at,
                     job.created_at,
                     ranked.closed_at,
                     ranked.archived_at
                 ),
                 completed_at = greatest(
                     job.updated_at,
                     job.created_at,
                     ranked.closed_at,
                     ranked.archived_at
                 ),
                 leased_at = NULL,
                 leased_by = NULL,
                 lease_expires_at = NULL,
                 failure_code = 'application_cycle_downgrade',
                 failure_detail =
                     'Cancelled to restore legacy candidate-position score uniqueness.',
                 result_summary = jsonb_build_object(
                     'status', 'cancelled',
                     'reason', 'application_cycle_downgrade',
                     'application_id', ranked.application_id::text
                 ),
                 updated_at = greatest(
                     job.updated_at,
                     job.created_at,
                     ranked.closed_at,
                     ranked.archived_at
                 )
            FROM ranked
           WHERE job.id = ranked.id
             AND (
                 ranked.closed_at IS NOT NULL
                 OR ranked.archived_at IS NOT NULL
                 OR ranked.legacy_rank > 1
             )
          RETURNING job.id
        )
        DELETE FROM worker_queue_leases AS lease
         USING cancelled
         WHERE lease.queue_name = 'candidate_processing_jobs'
           AND lease.work_id = cancelled.id
        """
    )
    op.drop_index(
        "uq_candidate_processing_active_score",
        table_name="candidate_processing_jobs",
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


def _create_parent_identities() -> None:
    op.create_unique_constraint(
        "uq_applications_exact_cycle_identity",
        "applications",
        ["organization_id", "id", "candidate_id", "position_id"],
    )
    op.create_unique_constraint(
        "uq_contact_point_candidate_identity",
        "candidate_contact_points",
        ["organization_id", "candidate_id", "id"],
    )
    op.create_unique_constraint(
        "uq_communication_thread_candidate_identity",
        "communication_threads",
        ["organization_id", "id", "candidate_id"],
    )
    op.create_unique_constraint(
        "uq_interview_application_identity",
        "interviews",
        ["organization_id", "id", "application_id", "candidate_id", "position_id"],
    )
    op.create_unique_constraint(
        "uq_scheduling_link_application_identity",
        "interview_scheduling_links",
        ["organization_id", "id", "application_id", "candidate_id", "position_id"],
    )
    op.create_unique_constraint(
        "uq_ai_agent_run_mission_identity",
        "ai_agent_runs",
        ["organization_id", "id", "mission_id"],
    )
    op.create_unique_constraint(
        "uq_mission_plan_exact_identity",
        "mission_plan_versions",
        ["organization_id", "id", "mission_id", "version"],
    )
    op.create_unique_constraint(
        "uq_mission_assessment_exact_identity",
        "mission_candidate_assessments",
        [
            "organization_id",
            "id",
            "mission_id",
            "plan_version_id",
            "candidate_id",
        ],
    )


def _exact_application_fk(table: str, name: str) -> None:
    op.create_foreign_key(
        name,
        table,
        "applications",
        ["organization_id", "application_id", "candidate_id", "position_id"],
        ["organization_id", "id", "candidate_id", "position_id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )


def _create_application_contract() -> None:
    checks = (
        (
            "candidate_activities",
            "ck_candidate_activity_application_target",
            "(position_id IS NULL) = (application_id IS NULL)",
        ),
        (
            "candidate_processing_jobs",
            "ck_candidate_processing_job_application_target",
            "(kind = 'score' AND application_id IS NOT NULL) OR "
            "(kind = 'parse_cv' AND application_id IS NULL)",
        ),
        (
            "chat_sessions",
            "ck_chat_session_application_target",
            "(application_id IS NOT NULL) = "
            "(candidate_id IS NOT NULL AND position_id IS NOT NULL)",
        ),
        (
            "communication_threads",
            "ck_communication_thread_application_target",
            "(position_id IS NULL) = (application_id IS NULL)",
        ),
        (
            "interview_recordings",
            "ck_interview_recording_application_target",
            "(position_id IS NULL) = (application_id IS NULL)",
        ),
        (
            "interview_appointments",
            "ck_appointment_application_target",
            "(position_id IS NULL) = (application_id IS NULL)",
        ),
        (
            "interview_appointments",
            "ck_appointment_link_requires_application",
            "scheduling_link_id IS NULL OR application_id IS NOT NULL",
        ),
        (
            "source_candidate_receipts",
            "ck_source_receipt_application_target",
            "(application_id IS NOT NULL) = "
            "(candidate_id IS NOT NULL AND position_id IS NOT NULL)",
        ),
        (
            "task_submissions",
            "ck_task_submission_application_target",
            "(position_id IS NULL) = (application_id IS NULL)",
        ),
        (
            "workflow_runs",
            "ck_workflow_run_application_target",
            "(application_id IS NOT NULL) = "
            "(candidate_id IS NOT NULL AND position_id IS NOT NULL)",
        ),
    )
    for table, name, expression in checks:
        op.create_check_constraint(name, table, expression)

    op.alter_column(
        "interview_scheduling_links",
        "application_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    for table, name in (
        ("candidate_activities", "fk_candidate_activity_exact_application"),
        (
            "candidate_processing_jobs",
            "fk_candidate_processing_exact_application",
        ),
        ("chat_sessions", "fk_chat_session_exact_application"),
        (
            "communication_threads",
            "fk_communication_thread_exact_application",
        ),
        (
            "interview_recordings",
            "fk_interview_recording_exact_application",
        ),
        (
            "interview_scheduling_links",
            "fk_scheduling_link_exact_application",
        ),
        ("interview_appointments", "fk_appointment_exact_application"),
        (
            "source_candidate_receipts",
            "fk_source_receipt_exact_application",
        ),
        ("task_submissions", "fk_task_submission_exact_application"),
        ("workflow_runs", "fk_workflow_run_exact_application"),
    ):
        _exact_application_fk(table, name)

    # Interviews already carried application_id, but the old FK proved only
    # tenant membership. This exact edge makes it a trustworthy parent for
    # scheduling records without rewriting any interview IDs or content.
    op.create_foreign_key(
        "fk_interview_exact_application",
        "interviews",
        "applications",
        ["organization_id", "application_id", "candidate_id", "position_id"],
        ["organization_id", "id", "candidate_id", "position_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    indexes = (
        (
            "ix_candidate_activities_org_application_time",
            "candidate_activities",
            ["organization_id", "application_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
        ),
        (
            "ix_candidate_processing_org_application",
            "candidate_processing_jobs",
            ["organization_id", "application_id"],
        ),
        (
            "ix_chat_sessions_org_application",
            "chat_sessions",
            ["organization_id", "application_id"],
        ),
        (
            "ix_communication_threads_org_application",
            "communication_threads",
            ["organization_id", "application_id"],
        ),
        (
            "ix_interview_recordings_org_application",
            "interview_recordings",
            ["organization_id", "application_id"],
        ),
        (
            "ix_scheduling_links_org_application",
            "interview_scheduling_links",
            ["organization_id", "application_id"],
        ),
        (
            "ix_interview_appointments_org_application",
            "interview_appointments",
            ["organization_id", "application_id"],
        ),
        (
            "ix_source_receipts_org_application",
            "source_candidate_receipts",
            ["organization_id", "application_id"],
        ),
        (
            "ix_task_submissions_org_application",
            "task_submissions",
            ["organization_id", "application_id"],
        ),
        (
            "ix_workflow_runs_org_application",
            "workflow_runs",
            ["organization_id", "application_id"],
        ),
    )
    for name, table, columns in indexes:
        op.create_index(name, table, columns)


def _create_communication_contract() -> None:
    op.create_foreign_key(
        "fk_communication_thread_contact_owner",
        "communication_threads",
        "candidate_contact_points",
        ["organization_id", "candidate_id", "contact_point_id"],
        ["organization_id", "candidate_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_communication_message_thread_owner",
        "communication_messages",
        "communication_threads",
        ["organization_id", "thread_id", "candidate_id"],
        ["organization_id", "id", "candidate_id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_communication_message_contact_owner",
        "communication_messages",
        "candidate_contact_points",
        ["organization_id", "candidate_id", "contact_point_id"],
        ["organization_id", "candidate_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        DO $communication_preflight$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM communication_messages AS message
              LEFT JOIN communication_threads AS thread
                ON thread.id = message.thread_id
             WHERE thread.id IS NULL
                OR message.organization_id IS DISTINCT FROM thread.organization_id
                OR message.candidate_id IS DISTINCT FROM thread.candidate_id
                OR message.contact_point_id IS DISTINCT FROM thread.contact_point_id
                OR message.channel IS DISTINCT FROM thread.channel
          ) THEN
            RAISE EXCEPTION
              'd96a4c8e2f70 found inconsistent communication message ownership';
          END IF;
        END
        $communication_preflight$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_communication_message_thread_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM communication_messages WHERE id = NEW.id
          ) AND NOT EXISTS (
            SELECT 1
              FROM communication_messages AS message
              JOIN communication_threads AS thread ON thread.id = message.thread_id
             WHERE message.id = NEW.id
               AND thread.organization_id = message.organization_id
               AND thread.candidate_id = message.candidate_id
               AND thread.contact_point_id IS NOT DISTINCT FROM message.contact_point_id
               AND thread.channel = message.channel
          ) THEN
            RAISE EXCEPTION
              'communication message does not match its thread ownership'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_communication_message_thread_consistency
        AFTER INSERT OR UPDATE OF organization_id, thread_id, candidate_id,
          contact_point_id, channel
        ON communication_messages
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION enforce_communication_message_thread_consistency()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_communication_thread_message_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM communication_messages AS message
              JOIN communication_threads AS thread ON thread.id = message.thread_id
             WHERE thread.id = NEW.id
               AND (
                 message.organization_id IS DISTINCT FROM thread.organization_id
                 OR message.candidate_id IS DISTINCT FROM thread.candidate_id
                 OR message.contact_point_id IS DISTINCT FROM thread.contact_point_id
                 OR message.channel IS DISTINCT FROM thread.channel
               )
          ) THEN
            RAISE EXCEPTION
              'communication thread update would orphan a message'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_communication_thread_message_consistency
        AFTER UPDATE OF organization_id, candidate_id, contact_point_id, channel
        ON communication_threads
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION enforce_communication_thread_message_consistency()
        """
    )


def _create_scheduling_contract() -> None:
    op.create_foreign_key(
        "fk_scheduling_link_exact_interview",
        "interview_scheduling_links",
        "interviews",
        [
            "organization_id",
            "interview_id",
            "application_id",
            "candidate_id",
            "position_id",
        ],
        [
            "organization_id",
            "id",
            "application_id",
            "candidate_id",
            "position_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_appointment_exact_interview",
        "interview_appointments",
        "interviews",
        [
            "organization_id",
            "interview_id",
            "application_id",
            "candidate_id",
            "position_id",
        ],
        [
            "organization_id",
            "id",
            "application_id",
            "candidate_id",
            "position_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_appointment_exact_scheduling_link",
        "interview_appointments",
        "interview_scheduling_links",
        [
            "organization_id",
            "scheduling_link_id",
            "application_id",
            "candidate_id",
            "position_id",
        ],
        [
            "organization_id",
            "id",
            "application_id",
            "candidate_id",
            "position_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        DO $scheduling_preflight$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM interview_appointments AS appointment
              JOIN interview_scheduling_links AS scheduling_link
                ON scheduling_link.id = appointment.scheduling_link_id
             WHERE appointment.interview_id
                   IS DISTINCT FROM scheduling_link.interview_id
          ) THEN
            RAISE EXCEPTION
              'd96a4c8e2f70 found inconsistent appointment interview ownership';
          END IF;
        END
        $scheduling_preflight$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_appointment_link_interview_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM interview_appointments
             WHERE id = NEW.id AND scheduling_link_id IS NOT NULL
          ) AND NOT EXISTS (
            SELECT 1
              FROM interview_appointments AS appointment
              JOIN interview_scheduling_links AS scheduling_link
                ON scheduling_link.id = appointment.scheduling_link_id
             WHERE appointment.id = NEW.id
               AND scheduling_link.organization_id = appointment.organization_id
               AND scheduling_link.application_id = appointment.application_id
               AND scheduling_link.candidate_id = appointment.candidate_id
               AND scheduling_link.position_id = appointment.position_id
               AND scheduling_link.interview_id
                   IS NOT DISTINCT FROM appointment.interview_id
          ) THEN
            RAISE EXCEPTION
              'appointment does not match its scheduling link and interview'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_appointment_link_interview_consistency
        AFTER INSERT OR UPDATE OF organization_id, scheduling_link_id,
          application_id, candidate_id, position_id, interview_id
        ON interview_appointments
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION enforce_appointment_link_interview_consistency()
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_scheduling_link_appointment_consistency()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM interview_appointments AS appointment
              JOIN interview_scheduling_links AS scheduling_link
                ON scheduling_link.id = appointment.scheduling_link_id
             WHERE scheduling_link.id = NEW.id
               AND (
                 appointment.organization_id IS DISTINCT FROM scheduling_link.organization_id
                 OR appointment.application_id IS DISTINCT FROM scheduling_link.application_id
                 OR appointment.candidate_id IS DISTINCT FROM scheduling_link.candidate_id
                 OR appointment.position_id IS DISTINCT FROM scheduling_link.position_id
                 OR appointment.interview_id IS DISTINCT FROM scheduling_link.interview_id
               )
          ) THEN
            RAISE EXCEPTION
              'scheduling link update would orphan an appointment'
              USING ERRCODE = '23503';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_scheduling_link_appointment_consistency
        AFTER UPDATE OF organization_id, application_id, candidate_id,
          position_id, interview_id
        ON interview_scheduling_links
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION enforce_scheduling_link_appointment_consistency()
        """
    )


def _create_mission_contract() -> None:
    op.create_foreign_key(
        "fk_mission_assessment_exact_plan",
        "mission_candidate_assessments",
        "mission_plan_versions",
        ["organization_id", "plan_version_id", "mission_id", "plan_version"],
        ["organization_id", "id", "mission_id", "version"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_mission_assessment_exact_agent_run",
        "mission_candidate_assessments",
        "ai_agent_runs",
        ["organization_id", "agent_run_id", "mission_id"],
        ["organization_id", "id", "mission_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_evidence_claim_exact_assessment",
        "evidence_claims",
        "mission_candidate_assessments",
        [
            "organization_id",
            "assessment_id",
            "mission_id",
            "plan_version_id",
            "candidate_id",
        ],
        [
            "organization_id",
            "id",
            "mission_id",
            "plan_version_id",
            "candidate_id",
        ],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_evidence_claim_exact_agent_run",
        "evidence_claims",
        "ai_agent_runs",
        ["organization_id", "agent_run_id", "mission_id"],
        ["organization_id", "id", "mission_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def upgrade() -> None:
    _add_application_columns()
    _backfill_applications()
    _replace_active_score_index()
    _rewrite_artifact_parent_fks(restore_legacy=False)
    _create_parent_identities()
    _create_application_contract()
    _replace_workflow_materializer(application_cycle=True)
    _create_communication_contract()
    _create_scheduling_contract()
    _create_mission_contract()


def downgrade() -> None:
    _restore_legacy_active_score_index()
    for statement in (
        "DROP TRIGGER IF EXISTS trg_scheduling_link_appointment_consistency "
        "ON interview_scheduling_links",
        "DROP TRIGGER IF EXISTS trg_appointment_link_interview_consistency "
        "ON interview_appointments",
        "DROP FUNCTION IF EXISTS enforce_scheduling_link_appointment_consistency()",
        "DROP FUNCTION IF EXISTS enforce_appointment_link_interview_consistency()",
        "DROP TRIGGER IF EXISTS trg_communication_thread_message_consistency "
        "ON communication_threads",
        "DROP TRIGGER IF EXISTS trg_communication_message_thread_consistency "
        "ON communication_messages",
        "DROP FUNCTION IF EXISTS enforce_communication_thread_message_consistency()",
        "DROP FUNCTION IF EXISTS enforce_communication_message_thread_consistency()",
    ):
        op.execute(statement)

    for name, table in (
        ("fk_evidence_claim_exact_agent_run", "evidence_claims"),
        ("fk_evidence_claim_exact_assessment", "evidence_claims"),
        (
            "fk_mission_assessment_exact_agent_run",
            "mission_candidate_assessments",
        ),
        ("fk_mission_assessment_exact_plan", "mission_candidate_assessments"),
        ("fk_appointment_exact_scheduling_link", "interview_appointments"),
        ("fk_appointment_exact_interview", "interview_appointments"),
        ("fk_scheduling_link_exact_interview", "interview_scheduling_links"),
        ("fk_communication_message_contact_owner", "communication_messages"),
        ("fk_communication_message_thread_owner", "communication_messages"),
        ("fk_communication_thread_contact_owner", "communication_threads"),
        ("fk_interview_exact_application", "interviews"),
        ("fk_workflow_run_exact_application", "workflow_runs"),
        ("fk_task_submission_exact_application", "task_submissions"),
        ("fk_source_receipt_exact_application", "source_candidate_receipts"),
        ("fk_appointment_exact_application", "interview_appointments"),
        ("fk_scheduling_link_exact_application", "interview_scheduling_links"),
        ("fk_interview_recording_exact_application", "interview_recordings"),
        ("fk_communication_thread_exact_application", "communication_threads"),
        ("fk_chat_session_exact_application", "chat_sessions"),
        ("fk_candidate_processing_exact_application", "candidate_processing_jobs"),
        ("fk_candidate_activity_exact_application", "candidate_activities"),
    ):
        op.drop_constraint(name, table, type_="foreignkey")

    for name, table in (
        ("ix_workflow_runs_org_application", "workflow_runs"),
        ("ix_task_submissions_org_application", "task_submissions"),
        ("ix_source_receipts_org_application", "source_candidate_receipts"),
        ("ix_interview_appointments_org_application", "interview_appointments"),
        ("ix_scheduling_links_org_application", "interview_scheduling_links"),
        ("ix_interview_recordings_org_application", "interview_recordings"),
        ("ix_communication_threads_org_application", "communication_threads"),
        ("ix_chat_sessions_org_application", "chat_sessions"),
        ("ix_candidate_processing_org_application", "candidate_processing_jobs"),
        ("ix_candidate_activities_org_application_time", "candidate_activities"),
    ):
        op.drop_index(name, table_name=table)

    for table, name in (
        ("workflow_runs", "ck_workflow_run_application_target"),
        ("task_submissions", "ck_task_submission_application_target"),
        ("source_candidate_receipts", "ck_source_receipt_application_target"),
        ("interview_appointments", "ck_appointment_link_requires_application"),
        ("interview_appointments", "ck_appointment_application_target"),
        ("interview_recordings", "ck_interview_recording_application_target"),
        ("communication_threads", "ck_communication_thread_application_target"),
        ("chat_sessions", "ck_chat_session_application_target"),
        (
            "candidate_processing_jobs",
            "ck_candidate_processing_job_application_target",
        ),
        ("candidate_activities", "ck_candidate_activity_application_target"),
    ):
        op.drop_constraint(name, table, type_="check")

    for table, name in (
        ("mission_candidate_assessments", "uq_mission_assessment_exact_identity"),
        ("mission_plan_versions", "uq_mission_plan_exact_identity"),
        ("ai_agent_runs", "uq_ai_agent_run_mission_identity"),
        ("interview_scheduling_links", "uq_scheduling_link_application_identity"),
        ("interviews", "uq_interview_application_identity"),
        ("communication_threads", "uq_communication_thread_candidate_identity"),
        ("candidate_contact_points", "uq_contact_point_candidate_identity"),
        ("applications", "uq_applications_exact_cycle_identity"),
    ):
        op.drop_constraint(name, table, type_="unique")

    # Restore the c85 trigger body while NEW.application_id still exists; the
    # next step removes that column from candidate_activities.
    _replace_workflow_materializer(application_cycle=False)

    for table in reversed(_APPLICATION_TABLES):
        op.drop_column(table, "application_id")

    _rewrite_artifact_parent_fks(restore_legacy=True)
