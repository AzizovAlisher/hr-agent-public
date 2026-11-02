"""add position folder

Revision ID: c5a91f3b0d44
Revises: 9d2b0d1ac872
Create Date: 2026-05-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5a91f3b0d44"
down_revision: Union[str, Sequence[str], None] = "9d2b0d1ac872"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("folder", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_positions_folder"), "positions", ["folder"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_positions_folder"), table_name="positions")
    op.drop_column("positions", "folder")
