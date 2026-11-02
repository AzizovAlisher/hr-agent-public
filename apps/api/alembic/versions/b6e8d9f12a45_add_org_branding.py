"""add organization branding columns

Revision ID: b6e8d9f12a45
Revises: a7f4e21cb912
Create Date: 2026-05-09 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6e8d9f12a45"
down_revision: Union[str, Sequence[str], None] = "a7f4e21cb912"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("logo_path", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("accent_color", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "accent_color")
    op.drop_column("organizations", "logo_path")
