"""Durable workflow automation rules and runs.

Revision ID: 4a1d6e8f2c35
Revises: 3d8f0a2c9b71
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "4a1d6e8f2c35"
down_revision: str | Sequence[str] | None = "3d8f0a2c9b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "actions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "starts_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "trigger IN ("
            "'candidate_added', 'candidate_scored', 'candidate_rescored', "
            "'stage_changed', 'scorecard_submitted', 'message_received', "
            "'interview_scheduled', 'candidate_inactive', 'feedback_overdue'"
            ")",
            name="ck_workflow_rule_trigger",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_rules_organization_id",
        "workflow_rules",
        ["organization_id"],
    )
    op.create_index(
        "ix_workflow_rules_org_active_trigger",
        "workflow_rules",
        ["organization_id", "is_active", "trigger"],
    )
    op.create_index(
        "uq_workflow_rule_org_name_ci",
        "workflow_rules",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_activity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rule_name_snapshot", sa.String(length=160), nullable=False),
        sa.Column("trigger_snapshot", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "action_results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'skipped', 'failed')",
            name="ck_workflow_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["workflow_rules.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_activity_id"], ["candidate_activities.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "dedupe_key", name="uq_workflow_run_dedupe"),
    )
    op.create_index(
        "ix_workflow_runs_organization_id",
        "workflow_runs",
        ["organization_id"],
    )
    op.create_index("ix_workflow_runs_rule_id", "workflow_runs", ["rule_id"])
    op.create_index(
        "ix_workflow_runs_candidate_id", "workflow_runs", ["candidate_id"]
    )
    op.create_index(
        "ix_workflow_runs_org_created",
        "workflow_runs",
        ["organization_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_org_created", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_candidate_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_rule_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_organization_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index(
        "ix_workflow_rules_org_active_trigger", table_name="workflow_rules"
    )
    # IF EXISTS keeps downgrade usable for local databases that briefly ran an
    # earlier draft of this not-yet-released revision.
    op.execute("DROP INDEX IF EXISTS uq_workflow_rule_org_name_ci")
    op.drop_index("ix_workflow_rules_organization_id", table_name="workflow_rules")
    op.drop_table("workflow_rules")
