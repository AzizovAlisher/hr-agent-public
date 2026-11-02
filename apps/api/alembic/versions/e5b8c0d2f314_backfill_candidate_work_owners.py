"""backfill candidate work owners

Revision ID: e5b8c0d2f314
Revises: d4a7b9c1e203
Create Date: 2026-07-28 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "e5b8c0d2f314"
down_revision: Union[str, Sequence[str], None] = "d4a7b9c1e203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Seed one useful queue item per existing candidate: their strongest,
    # most recently scored application belongs to the teammate who created
    # that vacancy. Other applications stay unassigned instead of flooding
    # My Work after an upgrade.
    op.execute(
        """
        WITH ranked AS (
          SELECT
            score.id,
            position.created_by AS owner_user_id,
            row_number() OVER (
              PARTITION BY score.candidate_id
              ORDER BY
                (score.overall_score IS NOT NULL) DESC,
                score.overall_score DESC NULLS LAST,
                score.scored_at DESC,
                score.id DESC
            ) AS candidate_rank
          FROM scores AS score
          JOIN positions AS position
            ON position.id = score.position_id
           AND position.organization_id = score.organization_id
          LEFT JOIN folders AS folder
            ON folder.id = position.folder_id
          JOIN users AS owner
            ON owner.id = position.created_by
           AND owner.organization_id = position.organization_id
          WHERE score.owner_user_id IS NULL
            AND position.created_by IS NOT NULL
            AND position.status <> 'closed'
            AND (position.folder_id IS NULL OR folder.is_archived IS FALSE)
            AND (position.folder IS NULL OR position.folder NOT LIKE '\\_%' ESCAPE '\\')
        )
        UPDATE scores AS score
        SET owner_user_id = ranked.owner_user_id
        FROM ranked
        WHERE score.id = ranked.id
          AND ranked.candidate_rank = 1
        """
    )


def downgrade() -> None:
    # Ownership may have been edited after the backfill.  It is not safe to
    # guess which values were migration-created, so downgrade preserves them.
    pass
