"""add shortlist_links table

Revision ID: a7f4e21cb912
Revises: f1c5d8a93b27
Create Date: 2026-05-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "a7f4e21cb912"
down_revision: Union[str, Sequence[str], None] = "f1c5d8a93b27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shortlist_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("top_n", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "include_contact",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_shortlist_links_token",
        "shortlist_links",
        ["token"],
        unique=True,
    )
    op.create_index(
        "ix_shortlist_links_org",
        "shortlist_links",
        ["organization_id"],
    )
    op.create_index(
        "ix_shortlist_links_position",
        "shortlist_links",
        ["position_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_shortlist_links_position", table_name="shortlist_links")
    op.drop_index("ix_shortlist_links_org", table_name="shortlist_links")
    op.drop_index("ix_shortlist_links_token", table_name="shortlist_links")
    op.drop_table("shortlist_links")
