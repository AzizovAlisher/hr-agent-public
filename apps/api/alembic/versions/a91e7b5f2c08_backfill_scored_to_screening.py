"""backfill scored rows from 'new' to 'screening'

A score row IS the screening step. Existing data was inserted before the
service started auto-promoting on score, so every row sits at 'new'. Move
everything that has an overall_score into 'screening' so the kanban + the
analytics breakdown match what the recruiter expects to see.

Rows without an overall_score (added but not yet scored) stay at 'new'.
Rows past 'screening' are left alone — the only thing we touch is the
'new' bucket.

Revision ID: a91e7b5f2c08
Revises: c91d4e3a8b67
Create Date: 2026-05-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "a91e7b5f2c08"
down_revision: Union[str, Sequence[str], None] = "c91d4e3a8b67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE scores
        SET stage = 'screening'
        WHERE stage = 'new'
          AND overall_score IS NOT NULL
        """
    )


def downgrade() -> None:
    # Best-effort reverse: if you migrate down you lose the distinction
    # between "originally 'new'" and "promoted from 'new'". We don't try
    # to reconstruct it.
    op.execute(
        """
        UPDATE scores
        SET stage = 'new'
        WHERE stage = 'screening'
        """
    )
