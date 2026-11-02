"""add scores.stage for kanban pipeline

Revision ID: e92a4f615c8b
Revises: d8b4e2f7c1a3
Create Date: 2026-05-05 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e92a4f615c8b"
down_revision: Union[str, Sequence[str], None] = "d8b4e2f7c1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scores",
        sa.Column(
            "stage",
            sa.String(length=20),
            nullable=False,
            server_default="new",
        ),
    )
    op.create_index(op.f("ix_scores_stage"), "scores", ["stage"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_scores_stage"), table_name="scores")
    op.drop_column("scores", "stage")
