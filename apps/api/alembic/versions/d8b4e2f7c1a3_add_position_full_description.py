"""add position full_description

Revision ID: d8b4e2f7c1a3
Revises: c5a91f3b0d44
Create Date: 2026-05-05 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8b4e2f7c1a3"
down_revision: Union[str, Sequence[str], None] = "c5a91f3b0d44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("full_description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "full_description")
