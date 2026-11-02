"""Version structured hiring and persist reversible candidate merges.

Revision ID: f9e3a5c7b182
Revises: e8d2e4f6a970
Create Date: 2026-08-23

This is intentionally the single schema-stewarded migration for the hiring
integrity slices.  It links every legacy hiring artifact to an exact
application cycle, snapshots mutable rubrics and approval inputs, and adds the
encrypted append-only contract used to reverse candidate merges.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "f9e3a5c7b182"
down_revision: str | Sequence[str] | None = "e8d2e4f6a970"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_MIGRATION_MARKER = "f9e3a5c7b182"
_SOD_DEFAULT_JSON = (
    '{"requisitions":true,"offers":true,"hiring_decisions":true}'
)
_SOD_DEFAULT = f"'{_SOD_DEFAULT_JSON}'::jsonb"


def _execute_sql_script(script: str) -> None:
    """Execute a PostgreSQL script one statement at a time for asyncpg."""

    statements: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    block_comment = False
    line_comment = False
    while index < len(script):
        if line_comment:
            if script[index] == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if script.startswith("*/", index):
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is None and script.startswith("--", index):
            line_comment = True
            index += 2
            continue
        if quote is None and script.startswith("/*", index):
            block_comment = True
            index += 2
            continue
        character = script[index]
        if quote in {"'", '"'}:
            if character == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if quote is not None and quote.startswith("$"):
            if script.startswith(quote, index):
                index += len(quote)
                quote = None
            else:
                index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "$":
            end = script.find("$", index + 1)
            if end != -1:
                tag = script[index : end + 1]
                if tag == "$$" or tag[1:-1].replace("_", "a").isalnum():
                    quote = tag
                    index = end + 1
                    continue
        if character == ";":
            statement = script[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    trailing = script[start:].strip()
    if trailing:
        statements.append(trailing)
    if quote is not None or block_comment:
        raise ValueError("Unterminated SQL quote/comment in migration script")
    for statement in statements:
        op.execute(statement)


def _create_version_and_merge_tables() -> None:
    op.create_table(
        "scorecard_template_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("template_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(length=160), nullable=False),
        sa.Column("description_snapshot", sa.Text(), nullable=True),
        sa.Column("position_id_snapshot", sa.UUID(), nullable=True),
        sa.Column(
            "criteria_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_scorecard_version_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(criteria_snapshot) = 'array'",
            name="ck_scorecard_version_criteria_array",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["scorecard_templates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id", "version", name="uq_scorecard_template_version"
        ),
    )
    op.create_index(
        "ix_scorecard_template_versions_organization_id",
        "scorecard_template_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_scorecard_template_versions_template_id",
        "scorecard_template_versions",
        ["template_id"],
    )

    op.create_table(
        "interview_kit_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "questions_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("model_used_snapshot", sa.String(length=128), nullable=True),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_interview_kit_version_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(questions_snapshot) = 'array'",
            name="ck_interview_kit_questions_array",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["interviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "interview_id", "version", name="uq_interview_kit_version"
        ),
    )
    op.create_index(
        "ix_interview_kit_versions_organization_id",
        "interview_kit_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_interview_kit_versions_interview_id",
        "interview_kit_versions",
        ["interview_id"],
    )

    op.create_table(
        "candidate_merge_batches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("primary_candidate_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("reversed_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default="merged", nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "snapshot_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("snapshot_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("snapshot_key_id", sa.String(length=64), nullable=False),
        sa.Column("post_state_sha256", sa.String(length=64), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('merged', 'reversed')",
            name="ck_candidate_merge_batch_status",
        ),
        sa.CheckConstraint(
            "snapshot_version = 1",
            name="ck_candidate_merge_batch_snapshot_version",
        ),
        sa.CheckConstraint(
            "member_count >= 2", name="ck_candidate_merge_batch_member_count"
        ),
        sa.CheckConstraint(
            "octet_length(snapshot_nonce) = 12",
            name="ck_candidate_merge_batch_nonce_size",
        ),
        sa.CheckConstraint(
            "octet_length(snapshot_ciphertext) > 16",
            name="ck_candidate_merge_batch_ciphertext_size",
        ),
        sa.CheckConstraint(
            "post_state_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_merge_batch_digest",
        ),
        sa.CheckConstraint(
            "(status = 'merged' AND version = 1 AND reversed_at IS NULL) OR "
            "(status = 'reversed' AND version = 2 AND reversed_at IS NOT NULL)",
            name="ck_candidate_merge_batch_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "primary_candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_merge_batch_tenant_primary",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_candidate_merge_batches_org_id"
        ),
    )
    op.create_index(
        "ix_candidate_merge_batches_organization_id",
        "candidate_merge_batches",
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_merge_batches_primary_candidate_id",
        "candidate_merge_batches",
        ["primary_candidate_id"],
    )
    op.execute(
        "CREATE INDEX ix_candidate_merge_batches_primary_created "
        "ON candidate_merge_batches "
        "(organization_id, primary_candidate_id, created_at DESC, id)"
    )
    op.execute(
        "CREATE INDEX ix_candidate_merge_batches_org_status_created "
        "ON candidate_merge_batches "
        "(organization_id, status, created_at DESC, id)"
    )

    op.create_table(
        "candidate_merge_batch_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('primary', 'alias')",
            name="ck_candidate_merge_batch_member_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["candidate_merge_batches.organization_id", "candidate_merge_batches.id"],
            name="fk_candidate_merge_member_tenant_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_candidate_merge_member_tenant_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_candidate_merge_batch_members_org_id",
        ),
        sa.UniqueConstraint(
            "batch_id",
            "candidate_id",
            name="uq_candidate_merge_batch_member_candidate",
        ),
    )
    op.create_index(
        "ix_candidate_merge_batch_members_organization_id",
        "candidate_merge_batch_members",
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_merge_batch_members_candidate",
        "candidate_merge_batch_members",
        ["organization_id", "candidate_id", "batch_id"],
    )
    op.create_index(
        "uq_candidate_merge_batch_one_primary",
        "candidate_merge_batch_members",
        ["batch_id"],
        unique=True,
        postgresql_where=sa.text("role = 'primary'"),
    )


def _add_columns() -> None:
    op.add_column(
        "enterprise_policies",
        sa.Column(
            "approval_separation_of_duties",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=_SOD_DEFAULT_JSON,
            nullable=False,
        ),
    )

    op.add_column(
        "interviews", sa.Column("application_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "interviews",
        sa.Column("current_kit_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "interview_questions",
        sa.Column("is_required", sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    op.add_column(
        "scorecard_templates",
        sa.Column("current_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "scorecard_assignments", sa.Column("application_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "scorecard_assignments",
        sa.Column("template_version_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "scorecard_assignments",
        sa.Column(
            "template_version_snapshot",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )
    op.add_column(
        "scorecard_assignments",
        sa.Column(
            "criteria_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "scorecard_assignments",
        sa.Column(
            "blind_until_all_submitted",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )

    op.add_column(
        "requisitions",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "requisitions",
        sa.Column("approval_cycle", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "requisition_approvals",
        sa.Column("requisition_version_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "requisition_approvals",
        sa.Column("approval_cycle", sa.Integer(), nullable=True),
    )
    op.add_column(
        "requisition_approvals",
        sa.Column(
            "requisition_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.add_column(
        "candidate_offers", sa.Column("application_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "candidate_offers",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "candidate_offers",
        sa.Column("approval_cycle", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "offer_approvals",
        sa.Column("offer_version_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "offer_approvals", sa.Column("approval_cycle", sa.Integer(), nullable=True)
    )
    op.add_column(
        "offer_approvals",
        sa.Column(
            "offer_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    op.add_column(
        "hiring_decisions", sa.Column("application_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "hiring_decisions",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "hiring_decisions",
        sa.Column("approval_cycle", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "hiring_decision_approvals",
        sa.Column("decision_version_snapshot", sa.Integer(), nullable=True),
    )
    op.add_column(
        "hiring_decision_approvals",
        sa.Column("approval_cycle", sa.Integer(), nullable=True),
    )
    op.add_column(
        "hiring_decision_approvals",
        sa.Column(
            "decision_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "candidate_handoffs", sa.Column("application_id", sa.UUID(), nullable=True)
    )


def _backfill_structured_hiring() -> None:
    """Snapshot legacy state and resolve exact application-cycle ownership."""

    _execute_sql_script(
        f"""
        UPDATE enterprise_policies
           SET approval_separation_of_duties = jsonb_build_object(
               'requisitions', true,
               'offers', true,
               'hiring_decisions', true
           );

        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM interview_questions
                 GROUP BY interview_id, position
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot version interview kits: duplicate question positions exist';
            END IF;
        END;
        $$;

        CREATE FUNCTION resolve_legacy_hiring_application(
            p_organization_id uuid,
            p_candidate_id uuid,
            p_position_id uuid,
            p_created_at timestamptz,
            p_artifact_type text,
            p_artifact_id uuid,
            p_initial_stage varchar
        ) RETURNS uuid AS $resolver$
        DECLARE
            application_count integer;
            interval_count integer;
            resolved_id uuid;
            resolved_at timestamptz := COALESCE(p_created_at, now());
        BEGIN
            IF p_position_id IS NULL THEN
                RETURN NULL;
            END IF;

            SELECT count(*)
              INTO application_count
              FROM applications application
             WHERE application.organization_id = p_organization_id
               AND application.candidate_id = p_candidate_id
               AND application.position_id = p_position_id;

            IF application_count = 1 THEN
                SELECT application.id
                  INTO resolved_id
                  FROM applications application
                 WHERE application.organization_id = p_organization_id
                   AND application.candidate_id = p_candidate_id
                   AND application.position_id = p_position_id;
                RETURN resolved_id;
            END IF;

            IF application_count > 1 THEN
                SELECT count(*)
                  INTO interval_count
                  FROM applications application
                 WHERE application.organization_id = p_organization_id
                   AND application.candidate_id = p_candidate_id
                   AND application.position_id = p_position_id
                   AND COALESCE(application.applied_at, application.created_at)
                       <= resolved_at
                   AND (
                       application.closed_at IS NULL
                       OR application.closed_at >= resolved_at
                   );
                IF interval_count <> 1 THEN
                    RAISE EXCEPTION
                        'Ambiguous application cycle for % % (% candidate %, position %, at %)',
                        p_artifact_type, p_artifact_id, p_organization_id,
                        p_candidate_id, p_position_id, resolved_at;
                END IF;
                SELECT application.id
                  INTO resolved_id
                  FROM applications application
                 WHERE application.organization_id = p_organization_id
                   AND application.candidate_id = p_candidate_id
                   AND application.position_id = p_position_id
                   AND COALESCE(application.applied_at, application.created_at)
                       <= resolved_at
                   AND (
                       application.closed_at IS NULL
                       OR application.closed_at >= resolved_at
                   );
                RETURN resolved_id;
            END IF;

            resolved_id := gen_random_uuid();
            INSERT INTO applications (
                id, organization_id, candidate_id, position_id, stage, source,
                metadata, applied_at, cycle_number, created_at, updated_at
            ) VALUES (
                resolved_id, p_organization_id, p_candidate_id, p_position_id,
                p_initial_stage, 'legacy_backfill',
                jsonb_build_object(
                    'created_from', 'legacy_hiring_artifact',
                    'artifact_type', p_artifact_type,
                    'artifact_id', p_artifact_id,
                    'migration', '{_MIGRATION_MARKER}',
                    'provenance',
                        'legacy_backfill_current_state_not_historical'
                ),
                resolved_at, 1, resolved_at, resolved_at
            );
            INSERT INTO application_stage_events (
                id, organization_id, application_id, from_stage, to_stage,
                actor_kind, reason, details, occurred_at, created_at
            ) VALUES (
                gen_random_uuid(), p_organization_id, resolved_id, NULL,
                p_initial_stage, 'system', 'legacy_hiring_artifact_backfill',
                jsonb_build_object(
                    'artifact_type', p_artifact_type,
                    'artifact_id', p_artifact_id,
                    'migration', '{_MIGRATION_MARKER}'
                ),
                resolved_at, resolved_at
            );
            RETURN resolved_id;
        END;
        $resolver$ LANGUAGE plpgsql;

        UPDATE interviews artifact
           SET application_id = resolve_legacy_hiring_application(
               artifact.organization_id, artifact.candidate_id,
               artifact.position_id, artifact.created_at,
               'interview', artifact.id, 'interview'
           );

        UPDATE scorecard_assignments artifact
           SET application_id = resolve_legacy_hiring_application(
               artifact.organization_id, artifact.candidate_id,
               artifact.position_id, artifact.created_at,
               'scorecard_assignment', artifact.id, 'interview'
           )
         WHERE artifact.position_id IS NOT NULL;

        UPDATE hiring_decisions artifact
           SET application_id = resolve_legacy_hiring_application(
               artifact.organization_id, artifact.candidate_id,
               artifact.position_id, artifact.created_at,
               'hiring_decision', artifact.id, 'interview'
           )
         WHERE artifact.position_id IS NOT NULL;

        UPDATE candidate_offers artifact
           SET application_id = resolve_legacy_hiring_application(
               artifact.organization_id, artifact.candidate_id,
               artifact.position_id, artifact.created_at,
               'candidate_offer', artifact.id, 'offer'
           )
         WHERE artifact.position_id IS NOT NULL;

        UPDATE candidate_handoffs handoff
           SET application_id = offer.application_id
          FROM candidate_offers offer
         WHERE handoff.offer_id = offer.id
           AND handoff.organization_id = offer.organization_id
           AND offer.application_id IS NOT NULL;

        UPDATE candidate_handoffs artifact
           SET application_id = resolve_legacy_hiring_application(
               artifact.organization_id, artifact.candidate_id,
               artifact.position_id, artifact.created_at,
               'candidate_handoff', artifact.id, 'offer'
           )
         WHERE artifact.application_id IS NULL
           AND artifact.position_id IS NOT NULL;

        DROP FUNCTION resolve_legacy_hiring_application(
            uuid, uuid, uuid, timestamptz, text, uuid, varchar
        );

        INSERT INTO interview_kit_versions (
            id, organization_id, interview_id, version,
            questions_snapshot, model_used_snapshot, created_by_user_id,
            created_at
        )
        SELECT
            gen_random_uuid(), interview.organization_id, interview.id, 1,
            COALESCE(
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'id', question.id,
                            'position', question.position,
                            'category', question.category,
                            'question_text', question.question_text,
                            'rationale', question.rationale,
                            'expected_signals', question.expected_signals,
                            'is_required', question.is_required
                        ) ORDER BY question.position, question.id
                    )
                      FROM interview_questions question
                     WHERE question.interview_id = interview.id
                ),
                '[]'::jsonb
            ),
            interview.model_used, interview.created_by, interview.created_at
          FROM interviews interview;

        INSERT INTO scorecard_template_versions (
            id, organization_id, template_id, version, name_snapshot,
            description_snapshot, position_id_snapshot, criteria_snapshot,
            created_by_user_id, created_at
        )
        SELECT
            gen_random_uuid(), template.organization_id, template.id, 1,
            template.name, template.description, template.position_id,
            COALESCE(
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'id', criterion.id,
                            'key', criterion.key,
                            'label', criterion.label,
                            'description', criterion.description,
                            'weight', criterion.weight,
                            'position', criterion.position,
                            'is_required', criterion.is_required
                        ) ORDER BY criterion.position, criterion.id
                    )
                      FROM scorecard_criteria criterion
                     WHERE criterion.template_id = template.id
                ),
                '[]'::jsonb
            ),
            template.created_by_user_id, template.created_at
          FROM scorecard_templates template;

        UPDATE scorecard_assignments assignment
           SET template_version_id = version.id,
               template_version_snapshot = version.version,
               criteria_snapshot = version.criteria_snapshot
          FROM scorecard_template_versions version
         WHERE assignment.template_id = version.template_id
           AND assignment.organization_id = version.organization_id
           AND version.version = 1;

        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM scorecard_assignments assignment
                 WHERE assignment.template_id IS NULL
                   AND NOT EXISTS (
                       SELECT 1
                         FROM scorecard_ratings rating
                        WHERE rating.assignment_id = assignment.id
                   )
            ) THEN
                RAISE EXCEPTION
                    'Cannot snapshot an orphan scorecard assignment without a rubric';
            END IF;
        END;
        $$;

        WITH ranked_ratings AS (
            SELECT
                rating.*,
                row_number() OVER (
                    PARTITION BY rating.assignment_id
                    ORDER BY rating.criterion_key_snapshot, rating.id
                ) - 1 AS snapshot_position
              FROM scorecard_ratings rating
        ), orphan_snapshots AS (
            SELECT
                rating.assignment_id,
                jsonb_agg(
                    jsonb_build_object(
                        'id', COALESCE(rating.criterion_id, rating.id),
                        'key', rating.criterion_key_snapshot,
                        'label', rating.criterion_label_snapshot,
                        'description', NULL,
                        'weight', rating.criterion_weight_snapshot,
                        'position', rating.snapshot_position,
                        'is_required', true
                    ) ORDER BY rating.snapshot_position, rating.id
                ) AS criteria_snapshot
              FROM ranked_ratings rating
             GROUP BY rating.assignment_id
        )
        UPDATE scorecard_assignments assignment
           SET criteria_snapshot = snapshot.criteria_snapshot,
               template_version_snapshot = 1
          FROM orphan_snapshots snapshot
         WHERE assignment.id = snapshot.assignment_id
           AND assignment.template_id IS NULL;

        UPDATE requisitions requisition
           SET version = 1,
               approval_cycle = CASE
                   WHEN requisition.status <> 'draft'
                        OR EXISTS (
                            SELECT 1 FROM requisition_approvals approval
                             WHERE approval.requisition_id = requisition.id
                        )
                   THEN 1 ELSE 0
               END;

        UPDATE requisition_approvals approval
           SET requisition_version_snapshot = 1,
               approval_cycle = 1,
               requisition_snapshot = jsonb_strip_nulls(jsonb_build_object(
                   'title', requisition.title,
                   'department', requisition.department,
                   'business_justification', requisition.business_justification,
                   'headcount', requisition.headcount,
                   'hiring_manager_user_id', requisition.hiring_manager_user_id,
                   'salary_min', requisition.salary_min::text,
                   'salary_max', requisition.salary_max::text,
                   'salary_currency', requisition.salary_currency,
                   'salary_confidential', requisition.salary_confidential,
                   'required_approvals', requisition.required_approvals,
                   'version', 1,
                   'provenance', jsonb_build_object(
                       'kind',
                           'legacy_backfill_current_state_not_historical',
                       'migration', '{_MIGRATION_MARKER}'
                   )
               ))
          FROM requisitions requisition
         WHERE approval.requisition_id = requisition.id;

        UPDATE candidate_offers offer
           SET version = 1,
               approval_cycle = CASE
                   WHEN offer.status <> 'draft'
                        OR EXISTS (
                            SELECT 1 FROM offer_approvals approval
                             WHERE approval.offer_id = offer.id
                        )
                   THEN 1 ELSE 0
               END;

        UPDATE offer_approvals approval
           SET offer_version_snapshot = 1,
               approval_cycle = 1,
               offer_snapshot = jsonb_strip_nulls(jsonb_build_object(
                   'application_id', offer.application_id,
                   'candidate_id', offer.candidate_id,
                   'position_id', offer.position_id,
                   'subject', offer.subject,
                   'body', offer.body,
                   'salary_amount', offer.salary_amount::text,
                   'salary_currency', offer.salary_currency,
                   'start_date', offer.start_date,
                   'expires_at', offer.expires_at,
                   'required_approvals', offer.required_approvals,
                   'version', 1,
                   'provenance', jsonb_build_object(
                       'kind',
                           'legacy_backfill_current_state_not_historical',
                       'migration', '{_MIGRATION_MARKER}'
                   )
               ))
          FROM candidate_offers offer
         WHERE approval.offer_id = offer.id;

        UPDATE hiring_decisions SET version = 1, approval_cycle = 1;

        UPDATE hiring_decision_approvals approval
           SET decision_version_snapshot = 1,
               approval_cycle = 1,
               decision_snapshot = jsonb_strip_nulls(jsonb_build_object(
                   'application_id', decision.application_id,
                   'candidate_id', decision.candidate_id,
                   'position_id', decision.position_id,
                   'recommendation', decision.recommendation,
                   'summary', decision.summary,
                   'required_approvals', decision.required_approvals,
                   'version', 1,
                   'provenance', jsonb_build_object(
                       'kind',
                           'legacy_backfill_current_state_not_historical',
                       'migration', '{_MIGRATION_MARKER}'
                   )
               ))
          FROM hiring_decisions decision
         WHERE approval.decision_id = decision.id;
        """
    )

    for table, columns in (
        (
            "requisition_approvals",
            ("requisition_version_snapshot", "approval_cycle"),
        ),
        ("offer_approvals", ("offer_version_snapshot", "approval_cycle")),
        (
            "hiring_decision_approvals",
            ("decision_version_snapshot", "approval_cycle"),
        ),
    ):
        for column in columns:
            op.alter_column(table, column, existing_type=sa.Integer(), nullable=False)


def _add_model_constraints_and_indexes() -> None:
    op.create_check_constraint(
        "ck_policy_approval_separation_exact",
        "enterprise_policies",
        "jsonb_typeof(approval_separation_of_duties) = 'object' "
        "AND approval_separation_of_duties ? 'requisitions' "
        "AND approval_separation_of_duties ? 'offers' "
        "AND approval_separation_of_duties ? 'hiring_decisions' "
        "AND approval_separation_of_duties "
        "- ARRAY['requisitions', 'offers', 'hiring_decisions']::text[] = '{}'::jsonb "
        "AND jsonb_typeof(approval_separation_of_duties -> 'requisitions') = 'boolean' "
        "AND jsonb_typeof(approval_separation_of_duties -> 'offers') = 'boolean' "
        "AND jsonb_typeof(approval_separation_of_duties -> 'hiring_decisions') = 'boolean'",
    )
    op.create_check_constraint(
        "ck_positions_status",
        "positions",
        "status IN ('open', 'paused', 'closed')",
    )

    op.create_check_constraint(
        "ck_interviews_status",
        "interviews",
        "status IN ('draft', 'in_progress', 'scored', 'archived')",
    )
    op.create_check_constraint(
        "ck_interviews_kit_version_positive",
        "interviews",
        "current_kit_version > 0",
    )
    op.create_unique_constraint(
        "uq_interview_question_position",
        "interview_questions",
        ["interview_id", "position"],
    )
    op.create_check_constraint(
        "ck_interview_question_score",
        "interview_questions",
        "score IS NULL OR score BETWEEN 1 AND 5",
    )
    op.create_index(
        "ix_interviews_application_id", "interviews", ["application_id"]
    )
    op.create_index(
        "ix_interviews_org_application_created",
        "interviews",
        ["organization_id", "application_id", "created_at"],
    )
    op.create_foreign_key(
        "interviews_application_id_fkey",
        "interviews",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_check_constraint(
        "ck_scorecard_template_version_positive",
        "scorecard_templates",
        "current_version > 0",
    )
    op.create_check_constraint(
        "ck_scorecard_assignment_version_positive",
        "scorecard_assignments",
        "template_version_snapshot > 0",
    )
    op.create_check_constraint(
        "ck_scorecard_assignment_criteria_array",
        "scorecard_assignments",
        "jsonb_typeof(criteria_snapshot) = 'array'",
    )
    op.create_index(
        "ix_scorecard_assignments_application_id",
        "scorecard_assignments",
        ["application_id"],
    )
    op.create_index(
        "ix_scorecard_assignments_template_version_id",
        "scorecard_assignments",
        ["template_version_id"],
    )
    op.create_foreign_key(
        "scorecard_assignments_application_id_fkey",
        "scorecard_assignments",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "scorecard_assignments_template_version_id_fkey",
        "scorecard_assignments",
        "scorecard_template_versions",
        ["template_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_check_constraint(
        "ck_requisition_version_positive", "requisitions", "version > 0"
    )
    op.create_check_constraint(
        "ck_requisition_cycle_nonnegative",
        "requisitions",
        "approval_cycle >= 0",
    )
    op.drop_constraint(
        "uq_requisition_approval_user", "requisition_approvals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_requisition_approval_user_cycle",
        "requisition_approvals",
        ["requisition_id", "approval_cycle", "user_id"],
    )
    op.create_check_constraint(
        "ck_requisition_approval_version_positive",
        "requisition_approvals",
        "requisition_version_snapshot > 0",
    )
    op.create_check_constraint(
        "ck_requisition_approval_cycle_positive",
        "requisition_approvals",
        "approval_cycle > 0",
    )

    op.create_check_constraint(
        "ck_candidate_offer_version_positive", "candidate_offers", "version > 0"
    )
    op.create_check_constraint(
        "ck_candidate_offer_cycle_nonnegative",
        "candidate_offers",
        "approval_cycle >= 0",
    )
    op.create_index(
        "ix_candidate_offers_application_id", "candidate_offers", ["application_id"]
    )
    op.create_foreign_key(
        "candidate_offers_application_id_fkey",
        "candidate_offers",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("uq_offer_approval_user", "offer_approvals", type_="unique")
    op.create_unique_constraint(
        "uq_offer_approval_user_cycle",
        "offer_approvals",
        ["offer_id", "approval_cycle", "user_id"],
    )
    op.create_check_constraint(
        "ck_offer_approval_version_positive",
        "offer_approvals",
        "offer_version_snapshot > 0",
    )
    op.create_check_constraint(
        "ck_offer_approval_cycle_positive",
        "offer_approvals",
        "approval_cycle > 0",
    )

    op.create_check_constraint(
        "ck_hiring_decision_version_positive", "hiring_decisions", "version > 0"
    )
    op.create_check_constraint(
        "ck_hiring_decision_cycle_positive",
        "hiring_decisions",
        "approval_cycle > 0",
    )
    op.create_index(
        "ix_hiring_decisions_application_id",
        "hiring_decisions",
        ["application_id"],
    )
    op.create_foreign_key(
        "hiring_decisions_application_id_fkey",
        "hiring_decisions",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "uq_hiring_decision_vote", "hiring_decision_approvals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_hiring_decision_vote_cycle",
        "hiring_decision_approvals",
        ["decision_id", "approval_cycle", "user_id"],
    )
    op.create_check_constraint(
        "ck_hiring_decision_approval_version_positive",
        "hiring_decision_approvals",
        "decision_version_snapshot > 0",
    )
    op.create_check_constraint(
        "ck_hiring_decision_approval_cycle_positive",
        "hiring_decision_approvals",
        "approval_cycle > 0",
    )

    op.create_index(
        "ix_candidate_handoffs_application_id",
        "candidate_handoffs",
        ["application_id"],
    )
    op.create_foreign_key(
        "candidate_handoffs_application_id_fkey",
        "candidate_handoffs",
        "applications",
        ["application_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _add_tenant_integrity_and_rls() -> None:
    # Database-owned composite edges make a same-tenant parent mandatory even
    # when a globally valid UUID is supplied.  Their prefixes are intentionally
    # excluded from Alembic model drift in env.py.
    op.create_unique_constraint(
        "uq_tenant_scorecard_template_versions_org_id",
        "scorecard_template_versions",
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_scorecard_template_versions_template_id",
        "scorecard_template_versions",
        "scorecard_templates",
        ["organization_id", "template_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_tenant_scorecard_template_versions_created_by_user_id",
        "scorecard_template_versions",
        "users",
        ["organization_id", "created_by_user_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_tenant_scorecard_template_versions_template_id",
        "scorecard_template_versions",
        ["organization_id", "template_id"],
    )
    op.create_index(
        "ix_tenant_scorecard_template_versions_created_by_user_id",
        "scorecard_template_versions",
        ["organization_id", "created_by_user_id"],
    )

    op.create_unique_constraint(
        "uq_tenant_interview_kit_versions_org_id",
        "interview_kit_versions",
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_interview_kit_versions_interview_id",
        "interview_kit_versions",
        "interviews",
        ["organization_id", "interview_id"],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_tenant_interview_kit_versions_created_by_user_id",
        "interview_kit_versions",
        "users",
        ["organization_id", "created_by_user_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_tenant_interview_kit_versions_interview_id",
        "interview_kit_versions",
        ["organization_id", "interview_id"],
    )
    op.create_index(
        "ix_tenant_interview_kit_versions_created_by_user_id",
        "interview_kit_versions",
        ["organization_id", "created_by_user_id"],
    )

    for table, column in (
        ("interviews", "application_id"),
        ("scorecard_assignments", "application_id"),
        ("candidate_offers", "application_id"),
        ("hiring_decisions", "application_id"),
        ("candidate_handoffs", "application_id"),
    ):
        op.create_foreign_key(
            f"fk_tenant_{table}_application_id",
            table,
            "applications",
            ["organization_id", column],
            ["organization_id", "id"],
        )
        op.create_index(
            f"ix_tenant_{table}_application_id",
            table,
            ["organization_id", column],
        )

    op.create_foreign_key(
        "fk_tenant_scorecard_assignments_template_version_id",
        "scorecard_assignments",
        "scorecard_template_versions",
        ["organization_id", "template_version_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_tenant_scorecard_assignments_template_version_id",
        "scorecard_assignments",
        ["organization_id", "template_version_id"],
    )

    op.create_foreign_key(
        "fk_tenant_candidate_merge_batches_created_by_user_id",
        "candidate_merge_batches",
        "users",
        ["organization_id", "created_by_user_id"],
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_tenant_candidate_merge_batches_reversed_by_user_id",
        "candidate_merge_batches",
        "users",
        ["organization_id", "reversed_by_user_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_tenant_candidate_merge_batches_created_by_user_id",
        "candidate_merge_batches",
        ["organization_id", "created_by_user_id"],
    )
    op.create_index(
        "ix_tenant_candidate_merge_batches_reversed_by_user_id",
        "candidate_merge_batches",
        ["organization_id", "reversed_by_user_id"],
    )

    # These provider-assessment edges predate the consolidated RLS migration
    # but were added after it ran.  The global catalogue audit must remain
    # exhaustive, so E9 binds them here instead of grandfathering a hole.
    for table, column, parent in (
        ("provider_assessment_results", "source_job_id", "integration_jobs"),
        ("provider_assessments", "application_id", "applications"),
        ("provider_assessments", "created_by_user_id", "users"),
        ("provider_assessments", "invitation_job_id", "integration_jobs"),
        (
            "provider_assessments",
            "template_version_id",
            "assessment_template_versions",
        ),
    ):
        op.create_foreign_key(
            f"fk_tenant_{table}_{column}",
            table,
            parent,
            ["organization_id", column],
            ["organization_id", "id"],
        )
        op.create_index(
            f"ix_tenant_{table}_{column}",
            table,
            ["organization_id", column],
        )

    _execute_sql_script(
        """
        ALTER TABLE scorecard_template_versions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE scorecard_template_versions FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON scorecard_template_versions
          USING (organization_id = security.current_organization_id())
          WITH CHECK (organization_id = security.current_organization_id());

        ALTER TABLE interview_kit_versions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE interview_kit_versions FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON interview_kit_versions
          USING (organization_id = security.current_organization_id())
          WITH CHECK (organization_id = security.current_organization_id());

        ALTER TABLE candidate_merge_batches ENABLE ROW LEVEL SECURITY;
        ALTER TABLE candidate_merge_batches FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON candidate_merge_batches
          USING (organization_id = security.current_organization_id())
          WITH CHECK (organization_id = security.current_organization_id());

        ALTER TABLE candidate_merge_batch_members ENABLE ROW LEVEL SECURITY;
        ALTER TABLE candidate_merge_batch_members FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON candidate_merge_batch_members
          USING (organization_id = security.current_organization_id())
          WITH CHECK (organization_id = security.current_organization_id());

        REVOKE ALL ON scorecard_template_versions,
          interview_kit_versions, candidate_merge_batches,
          candidate_merge_batch_members FROM PUBLIC;
        GRANT SELECT, INSERT ON scorecard_template_versions,
          interview_kit_versions TO hr_agent_runtime, hr_agent_worker;
        GRANT SELECT, INSERT, UPDATE ON candidate_merge_batches
          TO hr_agent_runtime, hr_agent_worker;
        GRANT SELECT, INSERT ON candidate_merge_batch_members
          TO hr_agent_runtime, hr_agent_worker;
        """
    )


def _add_immutability_and_erasure_guards() -> None:
    _execute_sql_script(
        """
        CREATE FUNCTION require_hiring_artifact_application()
        RETURNS trigger AS $require_application$
        BEGIN
            IF NEW.position_id IS NOT NULL
               AND NEW.application_id IS NULL
               AND pg_trigger_depth() = 1 THEN
                RAISE EXCEPTION
                    'Hiring artifact % requires an application link', NEW.id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $require_application$ LANGUAGE plpgsql;

        CREATE FUNCTION validate_hiring_artifact_application()
        RETURNS trigger AS $validate_application$
        DECLARE
            linked applications%ROWTYPE;
        BEGIN
            IF NEW.application_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT * INTO linked
              FROM applications application
             WHERE application.id = NEW.application_id;
            IF NOT FOUND
               OR linked.organization_id IS DISTINCT FROM NEW.organization_id
               OR linked.candidate_id IS DISTINCT FROM NEW.candidate_id
               OR linked.position_id IS DISTINCT FROM NEW.position_id THEN
                RAISE EXCEPTION
                    'Hiring artifact % application tenant/candidate/position mismatch',
                    NEW.id USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $validate_application$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_interviews_require_application
          BEFORE INSERT OR UPDATE OF organization_id, candidate_id, position_id,
            application_id ON interviews
          FOR EACH ROW EXECUTE FUNCTION require_hiring_artifact_application();
        CREATE CONSTRAINT TRIGGER trg_interviews_validate_application
          AFTER INSERT OR UPDATE ON interviews DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_hiring_artifact_application();

        CREATE TRIGGER trg_scorecard_assignments_require_application
          BEFORE INSERT OR UPDATE OF organization_id, candidate_id, position_id,
            application_id ON scorecard_assignments
          FOR EACH ROW EXECUTE FUNCTION require_hiring_artifact_application();
        CREATE CONSTRAINT TRIGGER trg_scorecard_assignments_validate_application
          AFTER INSERT OR UPDATE ON scorecard_assignments
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_hiring_artifact_application();

        CREATE TRIGGER trg_hiring_decisions_require_application
          BEFORE INSERT OR UPDATE OF organization_id, candidate_id, position_id,
            application_id ON hiring_decisions
          FOR EACH ROW EXECUTE FUNCTION require_hiring_artifact_application();
        CREATE CONSTRAINT TRIGGER trg_hiring_decisions_validate_application
          AFTER INSERT OR UPDATE ON hiring_decisions DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_hiring_artifact_application();

        CREATE TRIGGER trg_candidate_offers_require_application
          BEFORE INSERT OR UPDATE OF organization_id, candidate_id, position_id,
            application_id ON candidate_offers
          FOR EACH ROW EXECUTE FUNCTION require_hiring_artifact_application();
        CREATE CONSTRAINT TRIGGER trg_candidate_offers_validate_application
          AFTER INSERT OR UPDATE ON candidate_offers DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_hiring_artifact_application();

        CREATE TRIGGER trg_candidate_handoffs_require_application
          BEFORE INSERT OR UPDATE OF organization_id, candidate_id, position_id,
            application_id ON candidate_handoffs
          FOR EACH ROW EXECUTE FUNCTION require_hiring_artifact_application();
        CREATE CONSTRAINT TRIGGER trg_candidate_handoffs_validate_application
          AFTER INSERT OR UPDATE ON candidate_handoffs DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_hiring_artifact_application();

        CREATE FUNCTION protect_structured_hiring_history()
        RETURNS trigger AS $protect_history$
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
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END;
        $protect_history$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_interview_kit_versions_append_only
          BEFORE UPDATE OR DELETE ON interview_kit_versions
          FOR EACH ROW EXECUTE FUNCTION protect_structured_hiring_history();
        CREATE TRIGGER trg_scorecard_template_versions_append_only
          BEFORE UPDATE OR DELETE ON scorecard_template_versions
          FOR EACH ROW EXECUTE FUNCTION protect_structured_hiring_history();
        CREATE TRIGGER trg_requisition_approvals_append_only
          BEFORE UPDATE OR DELETE ON requisition_approvals
          FOR EACH ROW EXECUTE FUNCTION protect_structured_hiring_history();
        CREATE TRIGGER trg_offer_approvals_append_only
          BEFORE UPDATE OR DELETE ON offer_approvals
          FOR EACH ROW EXECUTE FUNCTION protect_structured_hiring_history();
        CREATE TRIGGER trg_hiring_decision_approvals_append_only
          BEFORE UPDATE OR DELETE ON hiring_decision_approvals
          FOR EACH ROW EXECUTE FUNCTION protect_structured_hiring_history();

        REVOKE UPDATE, DELETE ON interview_kit_versions,
          scorecard_template_versions, requisition_approvals,
          offer_approvals, hiring_decision_approvals
          FROM hr_agent_runtime, hr_agent_worker;

        CREATE FUNCTION protect_candidate_merge_batch()
        RETURNS trigger AS $protect_merge$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_setting('app.allow_erasure', true) = 'on'
                   OR pg_trigger_depth() > 1 THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'Candidate merge batches are append-only';
            END IF;

            IF pg_trigger_depth() > 1 THEN
                RETURN NEW;
            END IF;

            IF OLD.status = 'merged'
               AND OLD.version = 1
               AND OLD.reversed_at IS NULL
               AND NEW.status = 'reversed'
               AND NEW.version = 2
               AND NEW.reversed_at IS NOT NULL
               AND ROW(
                   NEW.id, NEW.organization_id, NEW.primary_candidate_id,
                   NEW.created_by_user_id, NEW.snapshot_version,
                   NEW.member_count, NEW.snapshot_ciphertext,
                   NEW.snapshot_nonce, NEW.snapshot_key_id,
                   NEW.post_state_sha256, NEW.created_at
               ) IS NOT DISTINCT FROM ROW(
                   OLD.id, OLD.organization_id, OLD.primary_candidate_id,
                   OLD.created_by_user_id, OLD.snapshot_version,
                   OLD.member_count, OLD.snapshot_ciphertext,
                   OLD.snapshot_nonce, OLD.snapshot_key_id,
                   OLD.post_state_sha256, OLD.created_at
               ) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'Candidate merge reversal is a one-time transition';
        END;
        $protect_merge$ LANGUAGE plpgsql;

        CREATE FUNCTION protect_candidate_merge_member()
        RETURNS trigger AS $protect_merge_member$
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
            RAISE EXCEPTION 'Candidate merge members are append-only';
        END;
        $protect_merge_member$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_candidate_merge_batches_append_only
          BEFORE UPDATE OR DELETE ON candidate_merge_batches
          FOR EACH ROW EXECUTE FUNCTION protect_candidate_merge_batch();
        CREATE TRIGGER trg_candidate_merge_members_append_only
          BEFORE UPDATE OR DELETE ON candidate_merge_batch_members
          FOR EACH ROW EXECUTE FUNCTION protect_candidate_merge_member();

        CREATE FUNCTION erase_candidate_merge_snapshots()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $erase_merge$
        BEGIN
            DELETE FROM public.candidate_merge_batches batch
             WHERE batch.organization_id = OLD.organization_id
               AND EXISTS (
                   SELECT 1
                     FROM public.candidate_merge_batch_members member
                    WHERE member.organization_id = OLD.organization_id
                      AND member.batch_id = batch.id
                      AND member.candidate_id = OLD.id
               );
            RETURN OLD;
        END;
        $erase_merge$;
        ALTER FUNCTION erase_candidate_merge_snapshots() OWNER TO hr_agent_broker;
        REVOKE ALL ON FUNCTION erase_candidate_merge_snapshots() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION erase_candidate_merge_snapshots()
          TO hr_agent_runtime, hr_agent_worker;
        GRANT SELECT, DELETE ON candidate_merge_batches,
          candidate_merge_batch_members TO hr_agent_broker;

        CREATE TRIGGER trg_candidates_erase_merge_snapshots
          BEFORE DELETE ON candidates
          FOR EACH ROW EXECUTE FUNCTION erase_candidate_merge_snapshots();
        """
    )


def _guard_downgrade() -> None:
    connection = op.get_bind()
    merge_history_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM candidate_merge_batches)")
    ).scalar_one()
    if merge_history_exists:
        raise RuntimeError(
            "Cannot downgrade while encrypted candidate-merge history exists"
        )

    advanced_history_exists = connection.execute(
        sa.text(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM interview_kit_versions WHERE version <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM interviews WHERE current_kit_version <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM scorecard_template_versions WHERE version <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM scorecard_templates WHERE current_version <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM requisitions
                     WHERE version <> 1 OR approval_cycle > 1
                )
                OR EXISTS (
                    SELECT 1 FROM requisition_approvals
                     WHERE requisition_version_snapshot <> 1
                        OR approval_cycle <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM candidate_offers
                     WHERE version <> 1 OR approval_cycle > 1
                )
                OR EXISTS (
                    SELECT 1 FROM offer_approvals
                     WHERE offer_version_snapshot <> 1 OR approval_cycle <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM hiring_decisions
                     WHERE version <> 1 OR approval_cycle <> 1
                )
                OR EXISTS (
                    SELECT 1 FROM hiring_decision_approvals
                     WHERE decision_version_snapshot <> 1
                        OR approval_cycle <> 1
                )
            """
        )
    ).scalar_one()
    if advanced_history_exists:
        raise RuntimeError(
            "Cannot downgrade after structured-hiring versions or approval "
            "cycles advanced beyond the legacy-compatible first version"
        )


def _drop_immutability_and_erasure_guards() -> None:
    _execute_sql_script(
        """
        DROP TRIGGER IF EXISTS trg_candidates_erase_merge_snapshots ON candidates;
        DROP FUNCTION IF EXISTS erase_candidate_merge_snapshots();

        DROP TRIGGER IF EXISTS trg_candidate_merge_members_append_only
          ON candidate_merge_batch_members;
        DROP TRIGGER IF EXISTS trg_candidate_merge_batches_append_only
          ON candidate_merge_batches;
        DROP FUNCTION IF EXISTS protect_candidate_merge_member();
        DROP FUNCTION IF EXISTS protect_candidate_merge_batch();

        DROP TRIGGER IF EXISTS trg_hiring_decision_approvals_append_only
          ON hiring_decision_approvals;
        DROP TRIGGER IF EXISTS trg_offer_approvals_append_only
          ON offer_approvals;
        DROP TRIGGER IF EXISTS trg_requisition_approvals_append_only
          ON requisition_approvals;
        DROP TRIGGER IF EXISTS trg_scorecard_template_versions_append_only
          ON scorecard_template_versions;
        DROP TRIGGER IF EXISTS trg_interview_kit_versions_append_only
          ON interview_kit_versions;
        DROP FUNCTION IF EXISTS protect_structured_hiring_history();

        GRANT UPDATE, DELETE ON requisition_approvals, offer_approvals,
          hiring_decision_approvals TO hr_agent_runtime, hr_agent_worker;

        DROP TRIGGER IF EXISTS trg_candidate_handoffs_validate_application
          ON candidate_handoffs;
        DROP TRIGGER IF EXISTS trg_candidate_handoffs_require_application
          ON candidate_handoffs;
        DROP TRIGGER IF EXISTS trg_candidate_offers_validate_application
          ON candidate_offers;
        DROP TRIGGER IF EXISTS trg_candidate_offers_require_application
          ON candidate_offers;
        DROP TRIGGER IF EXISTS trg_hiring_decisions_validate_application
          ON hiring_decisions;
        DROP TRIGGER IF EXISTS trg_hiring_decisions_require_application
          ON hiring_decisions;
        DROP TRIGGER IF EXISTS trg_scorecard_assignments_validate_application
          ON scorecard_assignments;
        DROP TRIGGER IF EXISTS trg_scorecard_assignments_require_application
          ON scorecard_assignments;
        DROP TRIGGER IF EXISTS trg_interviews_validate_application ON interviews;
        DROP TRIGGER IF EXISTS trg_interviews_require_application ON interviews;
        DROP FUNCTION IF EXISTS validate_hiring_artifact_application();
        DROP FUNCTION IF EXISTS require_hiring_artifact_application();
        """
    )


def _drop_tenant_integrity_and_rls() -> None:
    for table, column in (
        ("provider_assessments", "template_version_id"),
        ("provider_assessments", "invitation_job_id"),
        ("provider_assessments", "created_by_user_id"),
        ("provider_assessments", "application_id"),
        ("provider_assessment_results", "source_job_id"),
    ):
        op.drop_constraint(
            f"fk_tenant_{table}_{column}", table, type_="foreignkey"
        )
        op.drop_index(f"ix_tenant_{table}_{column}", table_name=table)

    op.drop_constraint(
        "fk_tenant_scorecard_assignments_template_version_id",
        "scorecard_assignments",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_tenant_scorecard_assignments_template_version_id",
        table_name="scorecard_assignments",
    )
    for table in (
        "candidate_handoffs",
        "hiring_decisions",
        "candidate_offers",
        "scorecard_assignments",
        "interviews",
    ):
        op.drop_constraint(
            f"fk_tenant_{table}_application_id", table, type_="foreignkey"
        )
        op.drop_index(
            f"ix_tenant_{table}_application_id", table_name=table
        )


def _drop_model_constraints_and_indexes() -> None:
    op.drop_constraint(
        "candidate_handoffs_application_id_fkey",
        "candidate_handoffs",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_candidate_handoffs_application_id", table_name="candidate_handoffs"
    )

    op.drop_constraint(
        "hiring_decisions_application_id_fkey",
        "hiring_decisions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_hiring_decisions_application_id", table_name="hiring_decisions"
    )
    op.drop_constraint(
        "ck_hiring_decision_approval_cycle_positive",
        "hiring_decision_approvals",
        type_="check",
    )
    op.drop_constraint(
        "ck_hiring_decision_approval_version_positive",
        "hiring_decision_approvals",
        type_="check",
    )
    op.drop_constraint(
        "uq_hiring_decision_vote_cycle",
        "hiring_decision_approvals",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_hiring_decision_vote",
        "hiring_decision_approvals",
        ["decision_id", "user_id"],
    )
    op.drop_constraint(
        "ck_hiring_decision_cycle_positive", "hiring_decisions", type_="check"
    )
    op.drop_constraint(
        "ck_hiring_decision_version_positive", "hiring_decisions", type_="check"
    )

    op.drop_constraint(
        "candidate_offers_application_id_fkey",
        "candidate_offers",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_candidate_offers_application_id", table_name="candidate_offers"
    )
    op.drop_constraint(
        "ck_offer_approval_cycle_positive", "offer_approvals", type_="check"
    )
    op.drop_constraint(
        "ck_offer_approval_version_positive", "offer_approvals", type_="check"
    )
    op.drop_constraint(
        "uq_offer_approval_user_cycle", "offer_approvals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_offer_approval_user", "offer_approvals", ["offer_id", "user_id"]
    )
    op.drop_constraint(
        "ck_candidate_offer_cycle_nonnegative", "candidate_offers", type_="check"
    )
    op.drop_constraint(
        "ck_candidate_offer_version_positive", "candidate_offers", type_="check"
    )

    op.drop_constraint(
        "ck_requisition_approval_cycle_positive",
        "requisition_approvals",
        type_="check",
    )
    op.drop_constraint(
        "ck_requisition_approval_version_positive",
        "requisition_approvals",
        type_="check",
    )
    op.drop_constraint(
        "uq_requisition_approval_user_cycle",
        "requisition_approvals",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_requisition_approval_user",
        "requisition_approvals",
        ["requisition_id", "user_id"],
    )
    op.drop_constraint(
        "ck_requisition_cycle_nonnegative", "requisitions", type_="check"
    )
    op.drop_constraint(
        "ck_requisition_version_positive", "requisitions", type_="check"
    )

    op.drop_constraint(
        "scorecard_assignments_template_version_id_fkey",
        "scorecard_assignments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "scorecard_assignments_application_id_fkey",
        "scorecard_assignments",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_scorecard_assignments_template_version_id",
        table_name="scorecard_assignments",
    )
    op.drop_index(
        "ix_scorecard_assignments_application_id",
        table_name="scorecard_assignments",
    )
    op.drop_constraint(
        "ck_scorecard_assignment_criteria_array",
        "scorecard_assignments",
        type_="check",
    )
    op.drop_constraint(
        "ck_scorecard_assignment_version_positive",
        "scorecard_assignments",
        type_="check",
    )
    op.drop_constraint(
        "ck_scorecard_template_version_positive",
        "scorecard_templates",
        type_="check",
    )

    op.drop_constraint(
        "interviews_application_id_fkey", "interviews", type_="foreignkey"
    )
    op.drop_index("ix_interviews_org_application_created", table_name="interviews")
    op.drop_index("ix_interviews_application_id", table_name="interviews")
    op.drop_constraint(
        "ck_interview_question_score", "interview_questions", type_="check"
    )
    op.drop_constraint(
        "uq_interview_question_position", "interview_questions", type_="unique"
    )
    op.drop_constraint(
        "ck_interviews_kit_version_positive", "interviews", type_="check"
    )
    op.drop_constraint("ck_interviews_status", "interviews", type_="check")

    op.drop_constraint("ck_positions_status", "positions", type_="check")
    op.drop_constraint(
        "ck_policy_approval_separation_exact",
        "enterprise_policies",
        type_="check",
    )


def _drop_columns_and_tables() -> None:
    op.drop_column("candidate_handoffs", "application_id")

    op.drop_column("hiring_decision_approvals", "decision_snapshot")
    op.drop_column("hiring_decision_approvals", "approval_cycle")
    op.drop_column("hiring_decision_approvals", "decision_version_snapshot")
    op.drop_column("hiring_decisions", "approval_cycle")
    op.drop_column("hiring_decisions", "version")
    op.drop_column("hiring_decisions", "application_id")

    op.drop_column("offer_approvals", "offer_snapshot")
    op.drop_column("offer_approvals", "approval_cycle")
    op.drop_column("offer_approvals", "offer_version_snapshot")
    op.drop_column("candidate_offers", "approval_cycle")
    op.drop_column("candidate_offers", "version")
    op.drop_column("candidate_offers", "application_id")

    op.drop_column("requisition_approvals", "requisition_snapshot")
    op.drop_column("requisition_approvals", "approval_cycle")
    op.drop_column("requisition_approvals", "requisition_version_snapshot")
    op.drop_column("requisitions", "approval_cycle")
    op.drop_column("requisitions", "version")

    op.drop_column("scorecard_assignments", "blind_until_all_submitted")
    op.drop_column("scorecard_assignments", "criteria_snapshot")
    op.drop_column("scorecard_assignments", "template_version_snapshot")
    op.drop_column("scorecard_assignments", "template_version_id")
    op.drop_column("scorecard_assignments", "application_id")
    op.drop_column("scorecard_templates", "current_version")

    op.drop_column("interview_questions", "is_required")
    op.drop_column("interviews", "current_kit_version")
    op.drop_column("interviews", "application_id")

    op.drop_column("enterprise_policies", "approval_separation_of_duties")

    op.drop_table("candidate_merge_batch_members")
    op.drop_table("candidate_merge_batches")
    op.drop_table("interview_kit_versions")
    op.drop_table("scorecard_template_versions")


def upgrade() -> None:
    _create_version_and_merge_tables()
    _add_columns()
    _backfill_structured_hiring()
    _add_model_constraints_and_indexes()
    _add_tenant_integrity_and_rls()
    _add_immutability_and_erasure_guards()


def downgrade() -> None:
    _guard_downgrade()
    _drop_immutability_and_erasure_guards()
    _drop_tenant_integrity_and_rls()
    _drop_model_constraints_and_indexes()
    _drop_columns_and_tables()
