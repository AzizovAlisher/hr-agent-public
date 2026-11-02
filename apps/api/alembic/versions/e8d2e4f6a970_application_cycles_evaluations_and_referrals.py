"""Persist application cycles, immutable evaluations, and referral expiry.

Revision ID: e8d2e4f6a970
Revises: e7c1d3e5f869
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "e8d2e4f6a970"
down_revision: str | Sequence[str] | None = "e7c1d3e5f869"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MIGRATION_MARKER = "e8d2e4f6a970"


def _enable_tenant_safety() -> None:
    """Apply the database-owned tenant edges used by the forced-RLS baseline."""

    op.create_unique_constraint(
        "uq_tenant_application_evaluations_org_id",
        "application_evaluations",
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_application_evaluations_application_id",
        "application_evaluations",
        "applications",
        ["organization_id", "application_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_tenant_application_evaluations_supersedes_id",
        "application_evaluations",
        "application_evaluations",
        ["organization_id", "supersedes_evaluation_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tenant_application_evaluations_actor_user_id",
        "application_evaluations",
        "users",
        ["organization_id", "actor_user_id"],
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_applications_archived_by_user_id",
        "applications",
        "users",
        ["organization_id", "archived_by_user_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_tenant_application_evaluations_application_id",
        "application_evaluations",
        ["organization_id", "application_id"],
    )
    op.create_index(
        "ix_tenant_application_evaluations_supersedes_id",
        "application_evaluations",
        ["organization_id", "supersedes_evaluation_id"],
    )
    op.create_index(
        "ix_tenant_application_evaluations_actor_user_id",
        "application_evaluations",
        ["organization_id", "actor_user_id"],
    )
    op.create_index(
        "ix_tenant_applications_archived_by_user_id",
        "applications",
        ["organization_id", "archived_by_user_id"],
    )
    op.execute(
        "ALTER TABLE public.application_evaluations ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.application_evaluations FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON public.application_evaluations "
        "USING (organization_id = security.current_organization_id()) "
        "WITH CHECK (organization_id = security.current_organization_id())"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON public.application_evaluations "
        "TO hr_agent_runtime, hr_agent_worker"
    )


def _replace_score_compatibility_trigger() -> None:
    """Keep Score compatibility from silently manufacturing reapplications."""

    op.execute("DROP TRIGGER IF EXISTS trg_scores_sync_application ON scores")
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION sync_application_from_score() RETURNS trigger AS $$
            DECLARE
                application_id uuid;
                application_stage varchar(20);
                history_exists boolean := false;
                skip_sync boolean :=
                    COALESCE(current_setting('hr_agent.skip_application_sync', true), '0') = '1';
                skip_event boolean :=
                    COALESCE(current_setting('hr_agent.skip_application_event', true), '0') = '1';
            BEGIN
                IF skip_sync THEN
                    RETURN NEW;
                END IF;
                SELECT a.id, a.stage
                  INTO application_id, application_stage
                  FROM applications a
                 WHERE a.closed_at IS NULL
                   AND a.archived_at IS NULL
                   AND (
                        a.legacy_score_id = NEW.id
                        OR (
                            a.organization_id = NEW.organization_id
                            AND a.candidate_id = NEW.candidate_id
                            AND a.position_id = NEW.position_id
                        )
                   )
                 ORDER BY (a.legacy_score_id = NEW.id) DESC, a.cycle_number DESC
                 LIMIT 1
                 FOR UPDATE;

                IF application_id IS NULL THEN
                    SELECT EXISTS (
                        SELECT 1
                          FROM applications a
                         WHERE a.organization_id = NEW.organization_id
                           AND a.candidate_id = NEW.candidate_id
                           AND a.position_id = NEW.position_id
                    ) INTO history_exists;

                    -- A new application cycle is a deliberate domain action.
                    -- Replaying a legacy Score write after closure must never
                    -- manufacture another cycle behind the policy boundary.
                    IF history_exists THEN
                        RETURN NEW;
                    END IF;

                    application_id := NEW.id;
                    INSERT INTO applications (
                        id, organization_id, candidate_id, position_id,
                        legacy_score_id, stage, source, owner_user_id,
                        next_action_due_date, cycle_number, metadata, applied_at,
                        closed_at, created_at, updated_at
                    ) VALUES (
                        application_id, NEW.organization_id, NEW.candidate_id,
                        NEW.position_id, NEW.id, NEW.stage,
                        COALESCE(
                            (SELECT NULLIF(c.source, '')
                               FROM candidates c WHERE c.id = NEW.candidate_id),
                            'legacy_score'
                        ),
                        NEW.owner_user_id, NEW.next_action_due_date, 1,
                        jsonb_build_object('created_from', 'score_trigger'),
                        NEW.scored_at,
                        CASE WHEN NEW.stage IN ('hired', 'rejected')
                             THEN COALESCE(NEW.scored_at, now()) ELSE NULL END,
                        NEW.scored_at, now()
                    );
                    INSERT INTO application_stage_events (
                        id, organization_id, application_id, from_stage,
                        to_stage, actor_kind, reason, details
                    ) VALUES (
                        md5(random()::text || clock_timestamp()::text || NEW.id::text)::uuid,
                        NEW.organization_id, application_id, NULL, NEW.stage,
                        'system', 'legacy_score_created',
                        jsonb_build_object('score_id', NEW.id)
                    );
                ELSE
                    UPDATE applications
                       SET organization_id = NEW.organization_id,
                           candidate_id = NEW.candidate_id,
                           position_id = NEW.position_id,
                           legacy_score_id = NEW.id,
                           stage = NEW.stage,
                           owner_user_id = NEW.owner_user_id,
                           next_action_due_date = NEW.next_action_due_date,
                           closed_at = CASE
                               WHEN NEW.stage IN ('hired', 'rejected')
                               THEN COALESCE(closed_at, now())
                               ELSE NULL
                           END,
                           updated_at = now()
                     WHERE id = application_id;
                    IF application_stage IS DISTINCT FROM NEW.stage AND NOT skip_event THEN
                        INSERT INTO application_stage_events (
                            id, organization_id, application_id, from_stage,
                            to_stage, actor_kind, reason, details
                        ) VALUES (
                            md5(random()::text || clock_timestamp()::text || NEW.id::text)::uuid,
                            NEW.organization_id, application_id, application_stage,
                            NEW.stage, 'system', 'legacy_score_sync',
                            jsonb_build_object('score_id', NEW.id)
                        );
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_scores_sync_application "
        "AFTER INSERT OR UPDATE OF organization_id, candidate_id, position_id, "
        "stage, owner_user_id, next_action_due_date ON scores "
        "FOR EACH ROW EXECUTE FUNCTION sync_application_from_score()"
    )


def _create_evaluation_guard() -> None:
    op.execute(
        sa.text(
            """
            CREATE FUNCTION protect_application_evaluation() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE'
                   AND (
                       current_setting('app.allow_erasure', true) = 'on'
                       OR pg_trigger_depth() > 1
                   ) THEN
                    RETURN OLD;
                END IF;
                IF TG_OP = 'UPDATE' AND pg_trigger_depth() > 1 THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'Application evaluations are append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_application_evaluations_append_only "
        "BEFORE UPDATE OR DELETE ON application_evaluations "
        "FOR EACH ROW EXECUTE FUNCTION protect_application_evaluation()"
    )


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("cycle_number", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "applications",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "applications",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "applications",
        sa.Column("archived_by_user_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "applications_archived_by_user_id_fkey",
        "applications",
        "users",
        ["archived_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE applications AS application
           SET closed_at = COALESCE(
               (
                   SELECT MAX(event.occurred_at)
                     FROM application_stage_events AS event
                    WHERE event.application_id = application.id
                      AND event.to_stage IN ('hired', 'rejected')
               ),
               application.updated_at,
               application.applied_at,
               application.created_at,
               now()
           )
         WHERE application.stage IN ('hired', 'rejected')
        """
    )
    op.drop_constraint(
        "uq_applications_org_candidate_position", "applications", type_="unique"
    )
    op.create_unique_constraint(
        "uq_applications_org_candidate_position_cycle",
        "applications",
        ["organization_id", "candidate_id", "position_id", "cycle_number"],
    )
    op.create_index(
        "uq_applications_one_active_cycle",
        "applications",
        ["organization_id", "candidate_id", "position_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL AND archived_at IS NULL"),
    )
    op.create_check_constraint(
        "ck_applications_cycle_positive", "applications", "cycle_number > 0"
    )
    op.create_check_constraint(
        "ck_applications_archive_is_closed",
        "applications",
        "archived_at IS NULL OR closed_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_applications_terminal_is_closed",
        "applications",
        "archived_at IS NOT NULL OR "
        "((stage IN ('hired', 'rejected')) = (closed_at IS NOT NULL))",
    )

    op.create_table(
        "application_evaluations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("application_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_evaluation_id", sa.UUID(), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "scores",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), server_default="system", nullable=False),
        sa.Column(
            "evaluated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('ai_scoring', 'deterministic_scoring', 'manual')",
            name="ck_application_evaluations_kind",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'reviewed', 'failed', 'voided')",
            name="ck_application_evaluations_status",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_application_evaluations_version"
        ),
        sa.CheckConstraint(
            "actor_kind IN ('human', 'ai', 'system', 'automation')",
            name="ck_application_evaluations_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_evaluation_id"],
            ["application_evaluations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id",
            "kind",
            "version",
            name="uq_application_evaluations_kind_version",
        ),
    )
    op.create_index(
        "ix_application_evaluations_organization_id",
        "application_evaluations",
        ["organization_id"],
    )
    op.create_index(
        "ix_application_evaluations_application_id",
        "application_evaluations",
        ["application_id"],
    )
    op.execute(
        "CREATE INDEX ix_application_evaluations_application_kind_version "
        "ON application_evaluations (application_id, kind, version DESC)"
    )
    op.execute(
        "CREATE INDEX ix_application_evaluations_org_created "
        "ON application_evaluations (organization_id, created_at DESC, id)"
    )
    _enable_tenant_safety()

    scored_predicate = """
        s.overall_score IS NOT NULL
        OR s.score_technical_skills IS NOT NULL
        OR s.score_quality_of_projects IS NOT NULL
        OR s.score_previous_experience IS NOT NULL
        OR s.score_impact IS NOT NULL
        OR s.model_used IS NOT NULL
        OR s.overall_recommendation IS NOT NULL
        OR s.comment_technical_skills IS NOT NULL
        OR s.comment_quality_of_projects IS NOT NULL
        OR s.comment_previous_experience IS NOT NULL
        OR s.comment_impact IS NOT NULL
    """
    op.execute(
        sa.text(
            f"""
            INSERT INTO application_evaluations (
                id, organization_id, application_id, kind, status, version,
                evidence, scores, provenance, actor_kind, evaluated_at, created_at
            )
            SELECT
                gen_random_uuid(), s.organization_id, a.id, 'ai_scoring',
                'completed', 1,
                jsonb_strip_nulls(jsonb_build_object(
                    'recommendation', s.overall_recommendation,
                    'comments', jsonb_strip_nulls(jsonb_build_object(
                        'technical_skills', s.comment_technical_skills,
                        'quality_of_projects', s.comment_quality_of_projects,
                        'previous_experience', s.comment_previous_experience,
                        'impact', s.comment_impact
                    ))
                )),
                jsonb_strip_nulls(jsonb_build_object('overall', s.overall_score)),
                jsonb_strip_nulls(jsonb_build_object(
                    'source', 'legacy_score',
                    'legacy_score_id', s.id,
                    'model', s.model_used,
                    'migration', '{_MIGRATION_MARKER}'
                )),
                'ai', s.scored_at, s.scored_at
              FROM scores s
              JOIN applications a ON a.legacy_score_id = s.id
             WHERE ({scored_predicate})
               AND (
                    s.model_used IS NOT NULL
                    OR s.overall_recommendation IS NOT NULL
                    OR s.comment_technical_skills IS NOT NULL
                    OR s.comment_quality_of_projects IS NOT NULL
                    OR s.comment_previous_experience IS NOT NULL
                    OR s.comment_impact IS NOT NULL
               )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO application_evaluations (
                id, organization_id, application_id, kind, status, version,
                evidence, scores, provenance, actor_kind, evaluated_at, created_at
            )
            SELECT
                gen_random_uuid(), s.organization_id, a.id,
                'deterministic_scoring', 'completed', 1,
                jsonb_build_object(
                    'algorithm', 'legacy_weighted_score',
                    'source_fields_present', jsonb_strip_nulls(jsonb_build_object(
                        'technical_skills', s.score_technical_skills,
                        'quality_of_projects', s.score_quality_of_projects,
                        'previous_experience', s.score_previous_experience,
                        'impact', s.score_impact
                    ))
                ),
                jsonb_strip_nulls(jsonb_build_object(
                    'overall', s.overall_score,
                    'technical_skills', s.score_technical_skills,
                    'quality_of_projects', s.score_quality_of_projects,
                    'previous_experience', s.score_previous_experience,
                    'impact', s.score_impact
                )),
                jsonb_build_object(
                    'source', 'legacy_score',
                    'legacy_score_id', s.id,
                    'migration', '{_MIGRATION_MARKER}'
                ),
                'system', s.scored_at, s.scored_at
              FROM scores s
              JOIN applications a ON a.legacy_score_id = s.id
             WHERE ({scored_predicate})
               AND (
                    s.overall_score IS NOT NULL
                    OR s.score_technical_skills IS NOT NULL
                    OR s.score_quality_of_projects IS NOT NULL
                    OR s.score_previous_experience IS NOT NULL
                    OR s.score_impact IS NOT NULL
               )
            """
        )
    )
    _create_evaluation_guard()
    _replace_score_compatibility_trigger()

    op.execute(
        "UPDATE referral_links SET expires_at = "
        "COALESCE(created_at, now()) + interval '30 days' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column(
        "referral_links",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now() + interval '30 days'"),
    )


def _restore_original_score_trigger() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scores_sync_application ON scores")
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION sync_application_from_score() RETURNS trigger AS $$
            DECLARE
                application_id uuid;
                application_stage varchar(20);
                skip_event boolean :=
                    COALESCE(current_setting('hr_agent.skip_application_event', true), '0') = '1';
            BEGIN
                SELECT a.id, a.stage
                  INTO application_id, application_stage
                  FROM applications a
                 WHERE a.legacy_score_id = NEW.id
                    OR (
                        a.organization_id = NEW.organization_id
                        AND a.candidate_id = NEW.candidate_id
                        AND a.position_id = NEW.position_id
                    )
                 ORDER BY (a.legacy_score_id = NEW.id) DESC
                 LIMIT 1
                 FOR UPDATE;

                IF application_id IS NULL THEN
                    application_id := NEW.id;
                    INSERT INTO applications (
                        id, organization_id, candidate_id, position_id,
                        legacy_score_id, stage, source, owner_user_id,
                        next_action_due_date, metadata, applied_at,
                        created_at, updated_at
                    ) VALUES (
                        application_id, NEW.organization_id, NEW.candidate_id,
                        NEW.position_id, NEW.id, NEW.stage,
                        COALESCE(
                            (SELECT NULLIF(c.source, '')
                               FROM candidates c WHERE c.id = NEW.candidate_id),
                            'legacy_score'
                        ),
                        NEW.owner_user_id, NEW.next_action_due_date,
                        jsonb_build_object('created_from', 'score_trigger'),
                        NEW.scored_at, NEW.scored_at, now()
                    );
                    INSERT INTO application_stage_events (
                        id, organization_id, application_id, from_stage,
                        to_stage, actor_kind, reason, details
                    ) VALUES (
                        md5(random()::text || clock_timestamp()::text || NEW.id::text)::uuid,
                        NEW.organization_id, application_id, NULL, NEW.stage,
                        'system', 'legacy_score_created',
                        jsonb_build_object('score_id', NEW.id)
                    );
                ELSE
                    UPDATE applications
                       SET organization_id = NEW.organization_id,
                           candidate_id = NEW.candidate_id,
                           position_id = NEW.position_id,
                           legacy_score_id = NEW.id,
                           stage = NEW.stage,
                           owner_user_id = NEW.owner_user_id,
                           next_action_due_date = NEW.next_action_due_date,
                           updated_at = now()
                     WHERE id = application_id;
                    IF application_stage IS DISTINCT FROM NEW.stage AND NOT skip_event THEN
                        INSERT INTO application_stage_events (
                            id, organization_id, application_id, from_stage,
                            to_stage, actor_kind, reason, details
                        ) VALUES (
                            md5(random()::text || clock_timestamp()::text || NEW.id::text)::uuid,
                            NEW.organization_id, application_id, application_stage,
                            NEW.stage, 'system', 'legacy_score_sync',
                            jsonb_build_object('score_id', NEW.id)
                        );
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_scores_sync_application "
        "AFTER INSERT OR UPDATE OF organization_id, candidate_id, position_id, "
        "stage, owner_user_id, next_action_due_date ON scores "
        "FOR EACH ROW EXECUTE FUNCTION sync_application_from_score()"
    )


def downgrade() -> None:
    connection = op.get_bind()
    unsafe_cycles = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM applications "
            "WHERE cycle_number <> 1 OR archived_at IS NOT NULL)"
        )
    ).scalar_one()
    unsafe_evaluations = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM application_evaluations "
            "WHERE provenance ->> 'migration' IS DISTINCT FROM :marker)"
        ),
        {"marker": _MIGRATION_MARKER},
    ).scalar_one()
    if unsafe_cycles or unsafe_evaluations:
        raise RuntimeError(
            "Cannot downgrade application-cycle storage after reapplications, "
            "archives, or runtime evaluations exist"
        )

    op.alter_column(
        "referral_links",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
    _restore_original_score_trigger()
    op.execute(
        "DROP TRIGGER IF EXISTS trg_application_evaluations_append_only "
        "ON application_evaluations"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_application_evaluation()")
    op.drop_table("application_evaluations")

    op.drop_constraint(
        "ck_applications_terminal_is_closed", "applications", type_="check"
    )
    op.drop_constraint(
        "ck_applications_archive_is_closed", "applications", type_="check"
    )
    op.drop_constraint(
        "ck_applications_cycle_positive", "applications", type_="check"
    )
    op.drop_index("uq_applications_one_active_cycle", table_name="applications")
    op.drop_constraint(
        "uq_applications_org_candidate_position_cycle",
        "applications",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_applications_org_candidate_position",
        "applications",
        ["organization_id", "candidate_id", "position_id"],
    )
    op.drop_constraint(
        "fk_tenant_applications_archived_by_user_id",
        "applications",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_tenant_applications_archived_by_user_id", table_name="applications"
    )
    op.drop_constraint(
        "applications_archived_by_user_id_fkey",
        "applications",
        type_="foreignkey",
    )
    op.drop_column("applications", "archived_by_user_id")
    op.drop_column("applications", "archived_at")
    op.drop_column("applications", "closed_at")
    op.drop_column("applications", "cycle_number")
