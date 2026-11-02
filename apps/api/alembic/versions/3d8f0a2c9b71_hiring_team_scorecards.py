"""Hiring-team scorecards and approval decisions.

Revision ID: 3d8f0a2c9b71
Revises: 29c7e5a1b406
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "3d8f0a2c9b71"
down_revision: str | Sequence[str] | None = "29c7e5a1b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scorecard_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
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
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scorecard_templates_organization_id",
        "scorecard_templates",
        ["organization_id"],
    )
    op.create_index(
        "ix_scorecard_templates_position_id",
        "scorecard_templates",
        ["position_id"],
    )
    op.create_index(
        "uq_scorecard_template_org_name_ci",
        "scorecard_templates",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "scorecard_criteria",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("weight", sa.Integer(), server_default="1", nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_required", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("weight BETWEEN 1 AND 100", name="ck_scorecard_weight"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["scorecard_templates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "key", name="uq_scorecard_criterion_key"),
        sa.UniqueConstraint(
            "template_id", "position", name="uq_scorecard_criterion_position"
        ),
    )
    op.create_index(
        "ix_scorecard_criteria_organization_id",
        "scorecard_criteria",
        ["organization_id"],
    )
    op.create_index(
        "ix_scorecard_criteria_template_id",
        "scorecard_criteria",
        ["template_id"],
    )

    op.create_table(
        "scorecard_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_to_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_name_snapshot", sa.String(length=160), nullable=False),
        sa.Column("position_title_snapshot", sa.String(length=255), nullable=False),
        sa.Column("assignee_email_snapshot", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('draft', 'submitted')", name="ck_scorecard_assignment_status"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["scorecard_templates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scorecard_assignments_organization_id",
        "scorecard_assignments",
        ["organization_id"],
    )
    op.create_index(
        "ix_scorecard_assignments_candidate_id",
        "scorecard_assignments",
        ["candidate_id"],
    )
    op.create_index(
        "ix_scorecard_assignments_position_id",
        "scorecard_assignments",
        ["position_id"],
    )
    op.create_index(
        "ix_scorecard_assignments_template_id",
        "scorecard_assignments",
        ["template_id"],
    )
    op.create_index(
        "ix_scorecard_assignments_assigned_to_user_id",
        "scorecard_assignments",
        ["assigned_to_user_id"],
    )
    op.create_index(
        "ix_scorecard_assignments_org_assignee_due",
        "scorecard_assignments",
        ["organization_id", "assigned_to_user_id", "status", "due_at"],
    )

    op.create_table(
        "scorecard_ratings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("criterion_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("criterion_key_snapshot", sa.String(length=64), nullable=False),
        sa.Column("criterion_label_snapshot", sa.String(length=160), nullable=False),
        sa.Column("criterion_weight_snapshot", sa.Integer(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_scorecard_rating_value"),
        sa.CheckConstraint(
            "criterion_weight_snapshot BETWEEN 1 AND 100",
            name="ck_scorecard_rating_weight",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["scorecard_assignments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["criterion_id"], ["scorecard_criteria.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id",
            "criterion_key_snapshot",
            name="uq_scorecard_rating_key",
        ),
    )
    op.create_index(
        "ix_scorecard_ratings_organization_id",
        "scorecard_ratings",
        ["organization_id"],
    )
    op.create_index(
        "ix_scorecard_ratings_assignment_id",
        "scorecard_ratings",
        ["assignment_id"],
    )

    op.create_table(
        "hiring_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_title_snapshot", sa.String(length=255), nullable=False),
        sa.Column("created_by_email_snapshot", sa.String(length=320), nullable=False),
        sa.Column("recommendation", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("required_approvals", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
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
            "recommendation IN ('advance', 'hold', 'reject', 'hire')",
            name="ck_hiring_decision_recommendation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_hiring_decision_status",
        ),
        sa.CheckConstraint(
            "required_approvals BETWEEN 1 AND 20",
            name="ck_hiring_decision_required_approvals",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_hiring_decisions_organization_id",
        "hiring_decisions",
        ["organization_id"],
    )
    op.create_index(
        "ix_hiring_decisions_candidate_id",
        "hiring_decisions",
        ["candidate_id"],
    )
    op.create_index(
        "ix_hiring_decisions_position_id",
        "hiring_decisions",
        ["position_id"],
    )

    op.create_table(
        "hiring_decision_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_email_snapshot", sa.String(length=320), nullable=False),
        sa.Column("vote", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
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
            "vote IN ('approve', 'reject')", name="ck_hiring_decision_vote"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["hiring_decisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_id", "user_id", name="uq_hiring_decision_vote"),
    )
    op.create_index(
        "ix_hiring_decision_approvals_organization_id",
        "hiring_decision_approvals",
        ["organization_id"],
    )
    op.create_index(
        "ix_hiring_decision_approvals_decision_id",
        "hiring_decision_approvals",
        ["decision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hiring_decision_approvals_decision_id",
        table_name="hiring_decision_approvals",
    )
    op.drop_index(
        "ix_hiring_decision_approvals_organization_id",
        table_name="hiring_decision_approvals",
    )
    op.drop_table("hiring_decision_approvals")
    op.drop_index("ix_hiring_decisions_position_id", table_name="hiring_decisions")
    op.drop_index("ix_hiring_decisions_candidate_id", table_name="hiring_decisions")
    op.drop_index("ix_hiring_decisions_organization_id", table_name="hiring_decisions")
    op.drop_table("hiring_decisions")
    op.drop_index("ix_scorecard_ratings_assignment_id", table_name="scorecard_ratings")
    op.drop_index("ix_scorecard_ratings_organization_id", table_name="scorecard_ratings")
    op.drop_table("scorecard_ratings")
    op.drop_index(
        "ix_scorecard_assignments_org_assignee_due",
        table_name="scorecard_assignments",
    )
    op.drop_index(
        "ix_scorecard_assignments_assigned_to_user_id",
        table_name="scorecard_assignments",
    )
    op.drop_index("ix_scorecard_assignments_template_id", table_name="scorecard_assignments")
    op.drop_index("ix_scorecard_assignments_position_id", table_name="scorecard_assignments")
    op.drop_index("ix_scorecard_assignments_candidate_id", table_name="scorecard_assignments")
    op.drop_index(
        "ix_scorecard_assignments_organization_id", table_name="scorecard_assignments"
    )
    op.drop_table("scorecard_assignments")
    op.drop_index("ix_scorecard_criteria_template_id", table_name="scorecard_criteria")
    op.drop_index("ix_scorecard_criteria_organization_id", table_name="scorecard_criteria")
    op.drop_table("scorecard_criteria")
    op.drop_index(
        "uq_scorecard_template_org_name_ci", table_name="scorecard_templates"
    )
    op.drop_index("ix_scorecard_templates_position_id", table_name="scorecard_templates")
    op.drop_index(
        "ix_scorecard_templates_organization_id", table_name="scorecard_templates"
    )
    op.drop_table("scorecard_templates")
