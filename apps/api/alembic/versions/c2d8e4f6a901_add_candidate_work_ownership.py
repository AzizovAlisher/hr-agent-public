"""add candidate work ownership and due dates

Revision ID: c2d8e4f6a901
Revises: 7e3b1a9c4d20
Create Date: 2026-07-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "c2d8e4f6a901"
down_revision: Union[str, Sequence[str], None] = "7e3b1a9c4d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scores",
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "scores",
        sa.Column("next_action_due_date", sa.Date(), nullable=True),
    )
    op.create_index(
        "ix_scores_org_owner_due",
        "scores",
        ["organization_id", "owner_user_id", "next_action_due_date", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_scores_org_owner_due", table_name="scores")
    op.drop_column("scores", "next_action_due_date")
    op.drop_column("scores", "owner_user_id")
