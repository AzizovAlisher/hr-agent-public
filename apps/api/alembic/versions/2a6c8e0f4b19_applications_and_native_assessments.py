"""first-class applications and native assessments

Revision ID: 2a6c8e0f4b19
Revises: a3f7c1d20e94
Create Date: 2026-08-22 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "2a6c8e0f4b19"
down_revision: str | Sequence[str] | None = "a3f7c1d20e94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _id() -> sa.Column:
    return sa.Column("id", UUID, nullable=False)


def _org() -> sa.Column:
    return sa.Column("organization_id", UUID, nullable=False)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _json_object(name: str) -> sa.Column:
    return sa.Column(
        name,
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )


def _json_array(name: str) -> sa.Column:
    return sa.Column(
        name,
        JSONB,
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )


STAGES = "'new', 'screening', 'interview', 'offer', 'hired', 'rejected'"


def upgrade() -> None:
    # Normalize legacy values before adding the database invariant shared by
    # Score and Application. Application code already emitted only this set.
    op.execute(
        "UPDATE scores SET stage = 'new' "
        f"WHERE stage IS NULL OR stage NOT IN ({STAGES})"
    )
    op.create_check_constraint(
        "ck_scores_stage", "scores", f"stage IN ({STAGES})"
    )

    op.create_table(
        "applications",
        _id(),
        _org(),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("position_id", UUID, nullable=False),
        sa.Column("legacy_score_id", UUID, nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("owner_user_id", UUID, nullable=True),
        sa.Column("next_action_due_date", sa.Date(), nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=True),
        _json_object("metadata"),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(f"stage IN ({STAGES})", name="ck_applications_stage"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["legacy_score_id"], ["scores.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legacy_score_id"),
        sa.UniqueConstraint(
            "organization_id",
            "candidate_id",
            "position_id",
            name="uq_applications_org_candidate_position",
        ),
    )
    op.create_index("ix_applications_organization_id", "applications", ["organization_id"])
    op.create_index("ix_applications_candidate_id", "applications", ["candidate_id"])
    op.create_index("ix_applications_position_id", "applications", ["position_id"])
    op.create_index("ix_applications_stage", "applications", ["stage"])
    op.create_index(
        "ix_applications_org_stage_created",
        "applications",
        ["organization_id", "stage", "created_at", "id"],
    )
    op.create_index(
        "ix_applications_org_owner_due",
        "applications",
        ["organization_id", "owner_user_id", "next_action_due_date", "id"],
    )

    op.create_table(
        "application_stage_events",
        _id(),
        _org(),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("from_stage", sa.String(length=20), nullable=True),
        sa.Column("to_stage", sa.String(length=20), nullable=False),
        sa.Column("actor_user_id", UUID, nullable=True),
        sa.Column("actor_kind", sa.String(length=16), nullable=False, server_default="system"),
        sa.Column("reason", sa.Text(), nullable=True),
        _json_object("details"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        _created_at(),
        sa.CheckConstraint(
            f"from_stage IS NULL OR from_stage IN ({STAGES})",
            name="ck_application_stage_events_from_stage",
        ),
        sa.CheckConstraint(
            f"to_stage IN ({STAGES})", name="ck_application_stage_events_to_stage"
        ),
        sa.CheckConstraint(
            "actor_kind IN ('human', 'system', 'public', 'automation')",
            name="ck_application_stage_events_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_stage_events_organization_id",
        "application_stage_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_application_stage_events_application_id",
        "application_stage_events",
        ["application_id"],
    )
    op.create_index(
        "ix_application_stage_events_application_time",
        "application_stage_events",
        ["application_id", "occurred_at", "id"],
    )

    # The Score id is a safe deterministic backfill id in a separate table and
    # makes reconciliation easy during rollout.
    op.execute(
        sa.text(
            """
            INSERT INTO applications (
                id, organization_id, candidate_id, position_id, legacy_score_id,
                stage, source, owner_user_id, next_action_due_date,
                created_by_user_id, metadata, applied_at, created_at, updated_at
            )
            SELECT
                s.id, s.organization_id, s.candidate_id, s.position_id, s.id,
                s.stage, COALESCE(NULLIF(c.source, ''), 'legacy'),
                s.owner_user_id, s.next_action_due_date, s.owner_user_id,
                jsonb_build_object('backfilled_from', 'scores'),
                s.scored_at, s.scored_at, s.scored_at
            FROM scores s
            JOIN candidates c ON c.id = s.candidate_id
            ON CONFLICT (organization_id, candidate_id, position_id) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO application_stage_events (
                id, organization_id, application_id, from_stage, to_stage,
                actor_kind, reason, details, occurred_at, created_at
            )
            SELECT
                a.id, a.organization_id, a.id, NULL, a.stage,
                'system', 'legacy_score_backfill',
                jsonb_build_object('score_id', a.legacy_score_id),
                a.applied_at, a.created_at
            FROM applications a
            WHERE a.legacy_score_id IS NOT NULL
            """
        )
    )

    op.create_table(
        "assessment_templates",
        _id(),
        _org(),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_assessment_templates_status"
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
        "ix_assessment_templates_organization_id",
        "assessment_templates",
        ["organization_id"],
    )
    op.create_index(
        "uq_assessment_templates_org_name_ci",
        "assessment_templates",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "assessment_template_versions",
        _id(),
        _org(),
        sa.Column("template_id", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("passing_percentage", sa.Numeric(7, 4), nullable=False, server_default="0"),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        _created_at(),
        sa.CheckConstraint("version >= 1", name="ck_assessment_template_version_number"),
        sa.CheckConstraint(
            "passing_percentage BETWEEN 0 AND 100",
            name="ck_assessment_version_passing_percentage",
        ),
        sa.CheckConstraint(
            "time_limit_minutes IS NULL OR time_limit_minutes BETWEEN 1 AND 10080",
            name="ck_assessment_version_time_limit",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["assessment_templates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id", "version", name="uq_assessment_template_version"
        ),
    )
    op.create_index(
        "ix_assessment_template_versions_organization_id",
        "assessment_template_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_assessment_template_versions_template_id",
        "assessment_template_versions",
        ["template_id"],
    )

    op.create_table(
        "assessment_sections",
        _id(),
        _org(),
        sa.Column("version_id", UUID, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint("position >= 0", name="ck_assessment_section_position"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["assessment_template_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id", "position", name="uq_assessment_section_position"
        ),
    )
    op.create_index(
        "ix_assessment_sections_organization_id",
        "assessment_sections",
        ["organization_id"],
    )
    op.create_index(
        "ix_assessment_sections_version_id", "assessment_sections", ["version_id"]
    )

    op.create_table(
        "assessment_questions",
        _id(),
        _org(),
        sa.Column("version_id", UUID, nullable=False),
        sa.Column("section_id", UUID, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("question_type", sa.String(length=16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("position", sa.Integer(), nullable=False),
        _json_array("options"),
        _json_object("validation"),
        _json_object("condition"),
        _json_object("scoring"),
        _json_object("knockout"),
        sa.Column("max_score", sa.Numeric(12, 4), nullable=False, server_default="0"),
        _created_at(),
        sa.CheckConstraint(
            "question_type IN ('boolean', 'single', 'multi', 'number', "
            "'short', 'long', 'file', 'url')",
            name="ck_assessment_question_type",
        ),
        sa.CheckConstraint("position >= 0", name="ck_assessment_question_position"),
        sa.CheckConstraint("max_score >= 0", name="ck_assessment_question_max_score"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["assessment_template_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["section_id"], ["assessment_sections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "key", name="uq_assessment_question_key"),
        sa.UniqueConstraint(
            "section_id", "position", name="uq_assessment_question_section_position"
        ),
    )
    op.create_index(
        "ix_assessment_questions_organization_id",
        "assessment_questions",
        ["organization_id"],
    )
    op.create_index(
        "ix_assessment_questions_version_id", "assessment_questions", ["version_id"]
    )
    op.create_index(
        "ix_assessment_questions_section_id", "assessment_questions", ["section_id"]
    )

    op.create_table(
        "assessment_invitations",
        _id(),
        _org(),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("template_version_id", UUID, nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("invited_by_user_id", UUID, nullable=True),
        sa.Column("first_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'completed', 'expired')",
            name="ck_assessment_invitation_status",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20",
            name="ck_assessment_invitation_max_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_version_id"],
            ["assessment_template_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_assessment_invitations_organization_id",
        "assessment_invitations",
        ["organization_id"],
    )
    op.create_index(
        "ix_assessment_invitations_application_id",
        "assessment_invitations",
        ["application_id"],
    )
    op.create_index(
        "ix_assessment_invitations_template_version_id",
        "assessment_invitations",
        ["template_version_id"],
    )
    op.create_index(
        "ix_assessment_invitations_org_application_created",
        "assessment_invitations",
        ["organization_id", "application_id", "created_at"],
    )

    op.create_table(
        "assessment_attempts",
        _id(),
        _org(),
        sa.Column("invitation_id", UUID, nullable=False),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("template_version_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="in_progress"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "last_saved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("sequence >= 1", name="ck_assessment_attempt_sequence"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'submitted', 'expired')",
            name="ck_assessment_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["invitation_id"], ["assessment_invitations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_version_id"],
            ["assessment_template_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invitation_id", "sequence", name="uq_assessment_attempt_sequence"
        ),
    )
    for column in (
        "organization_id",
        "invitation_id",
        "application_id",
        "template_version_id",
    ):
        op.create_index(f"ix_assessment_attempts_{column}", "assessment_attempts", [column])
    op.create_index(
        "uq_assessment_attempt_invitation_active",
        "assessment_attempts",
        ["invitation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )

    op.create_table(
        "assessment_answers",
        _id(),
        _org(),
        sa.Column("attempt_id", UUID, nullable=False),
        sa.Column("question_id", UUID, nullable=False),
        sa.Column("value", JSONB, nullable=False),
        sa.Column(
            "answered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        _created_at(),
        _updated_at(),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["assessment_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["question_id"], ["assessment_questions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id", "question_id", name="uq_assessment_answer_question"
        ),
    )
    for column in ("organization_id", "attempt_id", "question_id"):
        op.create_index(f"ix_assessment_answers_{column}", "assessment_answers", [column])

    op.create_table(
        "assessment_results",
        _id(),
        _org(),
        sa.Column("attempt_id", UUID, nullable=False),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column("raw_score", sa.Numeric(12, 4), nullable=False),
        sa.Column("max_score", sa.Numeric(12, 4), nullable=False),
        sa.Column("percentage", sa.Numeric(7, 4), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("knocked_out", sa.Boolean(), nullable=False),
        sa.Column("knockout_question_id", UUID, nullable=True),
        sa.Column("knockout_reason", sa.Text(), nullable=True),
        sa.Column("needs_manual_review", sa.Boolean(), nullable=False),
        _json_array("breakdown"),
        sa.Column(
            "graded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        _created_at(),
        sa.CheckConstraint("raw_score >= 0", name="ck_assessment_result_raw_score"),
        sa.CheckConstraint("max_score >= 0", name="ck_assessment_result_max_score"),
        sa.CheckConstraint(
            "percentage BETWEEN 0 AND 100", name="ck_assessment_result_percentage"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["assessment_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knockout_question_id"], ["assessment_questions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
    )
    op.create_index(
        "ix_assessment_results_organization_id",
        "assessment_results",
        ["organization_id"],
    )
    op.create_index(
        "ix_assessment_results_application_id",
        "assessment_results",
        ["application_id"],
    )

    # Every historical and future Score write remains compatible. The trigger
    # creates/synchronizes the new aggregate and captures legacy stage changes.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION sync_application_from_score() RETURNS trigger AS $$
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
                            (SELECT NULLIF(c.source, '') FROM candidates c WHERE c.id = NEW.candidate_id),
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
        sa.text(
            """
            CREATE TRIGGER trg_scores_sync_application
            AFTER INSERT OR UPDATE OF organization_id, candidate_id, position_id,
                stage, owner_user_id, next_action_due_date
            ON scores
            FOR EACH ROW EXECUTE FUNCTION sync_application_from_score()
            """
        )
    )

    # Published content is append-only. Child inserts are allowed only in the
    # transaction that created the version, enabling atomic publication.
    op.execute(
        sa.text(
            """
            CREATE FUNCTION protect_assessment_version() RETURNS trigger AS $$
            DECLARE
                parent_published_at timestamptz;
                parent_exists boolean;
            BEGIN
                IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'Published assessment versions are immutable';
                END IF;
                IF TG_TABLE_NAME = 'assessment_template_versions' THEN
                    SELECT EXISTS(
                        SELECT 1 FROM assessment_templates WHERE id = OLD.template_id
                    ) INTO parent_exists;
                    IF TG_OP = 'DELETE' AND parent_exists THEN
                        RAISE EXCEPTION 'Published assessment versions are immutable';
                    END IF;
                    RETURN OLD;
                END IF;
                IF TG_OP = 'INSERT' THEN
                    SELECT published_at INTO parent_published_at
                      FROM assessment_template_versions
                     WHERE id = NEW.version_id;
                    IF parent_published_at IS DISTINCT FROM transaction_timestamp() THEN
                        RAISE EXCEPTION 'Published assessment versions are immutable';
                    END IF;
                    RETURN NEW;
                END IF;
                SELECT EXISTS(
                    SELECT 1 FROM assessment_template_versions WHERE id = OLD.version_id
                ) INTO parent_exists;
                IF parent_exists THEN
                    RAISE EXCEPTION 'Published assessment versions are immutable';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        "CREATE TRIGGER trg_assessment_versions_immutable "
        "BEFORE UPDATE OR DELETE ON assessment_template_versions "
        "FOR EACH ROW EXECUTE FUNCTION protect_assessment_version()"
    )
    op.execute(
        "CREATE TRIGGER trg_assessment_sections_immutable "
        "BEFORE INSERT OR UPDATE OR DELETE ON assessment_sections "
        "FOR EACH ROW EXECUTE FUNCTION protect_assessment_version()"
    )
    op.execute(
        "CREATE TRIGGER trg_assessment_questions_immutable "
        "BEFORE INSERT OR UPDATE OR DELETE ON assessment_questions "
        "FOR EACH ROW EXECUTE FUNCTION protect_assessment_version()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_assessment_questions_immutable ON assessment_questions")
    op.execute("DROP TRIGGER IF EXISTS trg_assessment_sections_immutable ON assessment_sections")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_assessment_versions_immutable "
        "ON assessment_template_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_assessment_version()")
    op.execute("DROP TRIGGER IF EXISTS trg_scores_sync_application ON scores")
    op.execute("DROP FUNCTION IF EXISTS sync_application_from_score()")

    op.drop_table("assessment_results")
    op.drop_table("assessment_answers")
    op.drop_table("assessment_attempts")
    op.drop_table("assessment_invitations")
    op.drop_table("assessment_questions")
    op.drop_table("assessment_sections")
    op.drop_table("assessment_template_versions")
    op.drop_index(
        "uq_assessment_templates_org_name_ci", table_name="assessment_templates"
    )
    op.drop_table("assessment_templates")
    op.drop_table("application_stage_events")
    op.drop_table("applications")
    op.drop_constraint("ck_scores_stage", "scores", type_="check")
