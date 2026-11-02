"""Headcount requisitions, candidate offers, approvals, and hired handoff.

Revision ID: 7e5c9b3f1a64
Revises: 6d4b8a2e0f53
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "7e5c9b3f1a64"
down_revision: str | Sequence[str] | None = "6d4b8a2e0f53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "requisitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hiring_manager_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("department", sa.String(length=160), nullable=True),
        sa.Column("business_justification", sa.Text(), nullable=False),
        sa.Column("headcount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("salary_min", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("salary_max", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=False, server_default="TJS"),
        sa.Column("salary_confidential", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("required_approvals", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("headcount BETWEEN 1 AND 10000", name="ck_requisition_headcount"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_requisition_title_nonempty"),
        sa.CheckConstraint(
            "btrim(business_justification) <> ''",
            name="ck_requisition_justification_nonempty",
        ),
        sa.CheckConstraint(
            "salary_min IS NULL OR salary_min >= 0",
            name="ck_requisition_salary_min_nonnegative",
        ),
        sa.CheckConstraint(
            "salary_max IS NULL OR salary_max >= 0",
            name="ck_requisition_salary_max_nonnegative",
        ),
        sa.CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="ck_requisition_salary_range",
        ),
        sa.CheckConstraint(
            "salary_currency ~ '^[A-Z]{3}$'",
            name="ck_requisition_salary_currency",
        ),
        sa.CheckConstraint(
            "required_approvals BETWEEN 1 AND 20",
            name="ck_requisition_approvals",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'rejected', 'opened', 'cancelled')",
            name="ck_requisition_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["hiring_manager_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requisitions_organization_id", "requisitions", ["organization_id"])
    op.create_index(
        "ix_requisitions_org_status_created",
        "requisitions",
        ["organization_id", "status", "created_at"],
    )

    op.create_table(
        "requisition_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requisition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_email_snapshot", sa.String(length=320), nullable=False),
        sa.Column("vote", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "vote IN ('approve', 'reject')", name="ck_requisition_approval_vote"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requisition_id"], ["requisitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "requisition_id", "user_id", name="uq_requisition_approval_user"
        ),
    )

    op.create_table(
        "offer_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("subject_template", sa.String(length=500), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        *_timestamps(),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_offer_template_name_nonempty"),
        sa.CheckConstraint(
            "btrim(subject_template) <> ''", name="ck_offer_template_subject_nonempty"
        ),
        sa.CheckConstraint(
            "btrim(body_template) <> ''", name="ck_offer_template_body_nonempty"
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
        "ix_offer_templates_organization_id", "offer_templates", ["organization_id"]
    )
    op.create_index(
        "uq_offer_template_org_name_ci",
        "offer_templates",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "candidate_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_digest", sa.String(length=64), nullable=True),
        sa.Column("position_title_snapshot", sa.String(length=255), nullable=False),
        sa.Column("template_name_snapshot", sa.String(length=160), nullable=True),
        sa.Column("subject_template_snapshot", sa.String(length=500), nullable=False),
        sa.Column("body_template_snapshot", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("salary_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=False, server_default="TJS"),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("required_approvals", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signature_name", sa.String(length=255), nullable=True),
        sa.Column("signature_ip_hash", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "btrim(subject) <> ''", name="ck_candidate_offer_subject_nonempty"
        ),
        sa.CheckConstraint("btrim(body) <> ''", name="ck_candidate_offer_body_nonempty"),
        sa.CheckConstraint(
            "btrim(subject_template_snapshot) <> ''",
            name="ck_candidate_offer_subject_template_nonempty",
        ),
        sa.CheckConstraint(
            "btrim(body_template_snapshot) <> ''",
            name="ck_candidate_offer_body_template_nonempty",
        ),
        sa.CheckConstraint(
            "salary_amount IS NULL OR salary_amount >= 0",
            name="ck_candidate_offer_salary_nonnegative",
        ),
        sa.CheckConstraint(
            "salary_currency ~ '^[A-Z]{3}$'",
            name="ck_candidate_offer_salary_currency",
        ),
        sa.CheckConstraint(
            "required_approvals BETWEEN 1 AND 20",
            name="ck_candidate_offer_approvals",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending', 'approved', 'sent', 'accepted', "
            "'declined', 'withdrawn', 'expired')",
            name="ck_candidate_offer_status",
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
            ["template_id"], ["offer_templates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_candidate_offer_token_digest"),
    )
    op.create_index(
        "ix_candidate_offers_organization_id", "candidate_offers", ["organization_id"]
    )
    op.create_index(
        "ix_candidate_offers_candidate_id", "candidate_offers", ["candidate_id"]
    )
    op.create_index(
        "ix_candidate_offers_position_id", "candidate_offers", ["position_id"]
    )
    op.create_index(
        "ix_candidate_offers_org_status_created",
        "candidate_offers",
        ["organization_id", "status", "created_at"],
    )

    op.create_table(
        "offer_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_email_snapshot", sa.String(length=320), nullable=False),
        sa.Column("vote", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("vote IN ('approve', 'reject')", name="ck_offer_approval_vote"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"], ["candidate_offers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("offer_id", "user_id", name="uq_offer_approval_user"),
    )

    op.create_table(
        "candidate_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_title_snapshot", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column(
            "checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed')",
            name="ck_candidate_handoff_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"], ["candidate_offers.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["position_id"], ["positions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("offer_id", name="uq_candidate_handoff_offer"),
    )
    op.create_index(
        "ix_candidate_handoffs_organization_id", "candidate_handoffs", ["organization_id"]
    )
    op.create_index(
        "ix_candidate_handoffs_candidate_id", "candidate_handoffs", ["candidate_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_handoffs_candidate_id", table_name="candidate_handoffs"
    )
    op.drop_index(
        "ix_candidate_handoffs_organization_id", table_name="candidate_handoffs"
    )
    op.drop_table("candidate_handoffs")
    op.drop_table("offer_approvals")
    op.drop_index(
        "ix_candidate_offers_org_status_created", table_name="candidate_offers"
    )
    op.drop_index("ix_candidate_offers_position_id", table_name="candidate_offers")
    op.drop_index("ix_candidate_offers_candidate_id", table_name="candidate_offers")
    op.drop_index(
        "ix_candidate_offers_organization_id", table_name="candidate_offers"
    )
    op.drop_table("candidate_offers")
    op.drop_index("uq_offer_template_org_name_ci", table_name="offer_templates")
    op.drop_index(
        "ix_offer_templates_organization_id", table_name="offer_templates"
    )
    op.drop_table("offer_templates")
    op.drop_table("requisition_approvals")
    op.drop_index(
        "ix_requisitions_org_status_created", table_name="requisitions"
    )
    op.drop_index("ix_requisitions_organization_id", table_name="requisitions")
    op.drop_table("requisitions")
