"""Talent CRM pools and nurture sequences.

Revision ID: 5c2e7f9a1d48
Revises: 4a1d6e8f2c35
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "5c2e7f9a1d48"
down_revision: str | Sequence[str] | None = "4a1d6e8f2c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "talent_pools",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_talent_pools_organization_id", "talent_pools", ["organization_id"])
    op.create_index(
        "uq_talent_pool_org_name_ci",
        "talent_pools",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "talent_pool_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pool_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("added_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'converted')",
            name="ck_talent_pool_member_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pool_id"], ["talent_pools.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pool_id", "candidate_id", name="uq_talent_pool_member"),
    )
    op.create_index(
        "ix_talent_pool_members_organization_id", "talent_pool_members", ["organization_id"]
    )
    op.create_index("ix_talent_pool_members_pool_id", "talent_pool_members", ["pool_id"])
    op.create_index(
        "ix_talent_pool_members_candidate_id", "talent_pool_members", ["candidate_id"]
    )
    op.create_index(
        "ix_talent_pool_members_org_pool_created",
        "talent_pool_members",
        ["organization_id", "pool_id", "created_at", "id"],
    )

    op.create_table(
        "talent_nurture_sequences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pool_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pool_id"], ["talent_pools.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_talent_nurture_sequences_organization_id",
        "talent_nurture_sequences",
        ["organization_id"],
    )
    op.create_index(
        "ix_talent_nurture_sequences_pool_id", "talent_nurture_sequences", ["pool_id"]
    )
    op.create_index(
        "uq_talent_nurture_org_name_ci",
        "talent_nurture_sequences",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "talent_nurture_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("delay_days", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("template_name_snapshot", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "delay_days BETWEEN 0 AND 365", name="ck_talent_nurture_delay"
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'telegram', 'whatsapp')",
            name="ck_talent_nurture_channel",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sequence_id"], ["talent_nurture_sequences.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"], ["communication_templates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sequence_id", "position", name="uq_talent_nurture_step_position"
        ),
    )
    op.create_index(
        "ix_talent_nurture_steps_organization_id", "talent_nurture_steps", ["organization_id"]
    )
    op.create_index(
        "ix_talent_nurture_steps_sequence_id", "talent_nurture_steps", ["sequence_id"]
    )

    op.create_table(
        "talent_nurture_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence_name_snapshot", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scheduled_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'partial', 'completed', 'cancelled')",
            name="ck_talent_nurture_enrollment_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sequence_id"], ["talent_nurture_sequences.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sequence_id", "candidate_id", name="uq_talent_nurture_enrollment"
        ),
    )
    op.create_index(
        "ix_talent_nurture_enrollments_organization_id",
        "talent_nurture_enrollments",
        ["organization_id"],
    )
    op.create_index(
        "ix_talent_nurture_enrollments_sequence_id",
        "talent_nurture_enrollments",
        ["sequence_id"],
    )
    op.create_index(
        "ix_talent_nurture_enrollments_candidate_id",
        "talent_nurture_enrollments",
        ["candidate_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_talent_nurture_enrollments_candidate_id",
        table_name="talent_nurture_enrollments",
    )
    op.drop_index(
        "ix_talent_nurture_enrollments_sequence_id",
        table_name="talent_nurture_enrollments",
    )
    op.drop_index(
        "ix_talent_nurture_enrollments_organization_id",
        table_name="talent_nurture_enrollments",
    )
    op.drop_table("talent_nurture_enrollments")
    op.drop_index("ix_talent_nurture_steps_sequence_id", table_name="talent_nurture_steps")
    op.drop_index(
        "ix_talent_nurture_steps_organization_id", table_name="talent_nurture_steps"
    )
    op.drop_table("talent_nurture_steps")
    op.drop_index(
        "uq_talent_nurture_org_name_ci", table_name="talent_nurture_sequences"
    )
    op.drop_index(
        "ix_talent_nurture_sequences_pool_id", table_name="talent_nurture_sequences"
    )
    op.drop_index(
        "ix_talent_nurture_sequences_organization_id",
        table_name="talent_nurture_sequences",
    )
    op.drop_table("talent_nurture_sequences")
    op.drop_index(
        "ix_talent_pool_members_org_pool_created", table_name="talent_pool_members"
    )
    op.drop_index("ix_talent_pool_members_candidate_id", table_name="talent_pool_members")
    op.drop_index("ix_talent_pool_members_pool_id", table_name="talent_pool_members")
    op.drop_index(
        "ix_talent_pool_members_organization_id", table_name="talent_pool_members"
    )
    op.drop_table("talent_pool_members")
    op.drop_index("uq_talent_pool_org_name_ci", table_name="talent_pools")
    op.drop_index("ix_talent_pools_organization_id", table_name="talent_pools")
    op.drop_table("talent_pools")
