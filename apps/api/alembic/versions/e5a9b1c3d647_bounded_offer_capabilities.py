"""Bound and require public offer capability expiration.

Revision ID: e5a9b1c3d647
Revises: e4f8a0b2d536
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5a9b1c3d647"
down_revision: str | Sequence[str] | None = "e4f8a0b2d536"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Active legacy links receive a short, bounded grace period. Final links
    # are consumed by the application contract regardless of this timestamp.
    op.execute(
        """
        UPDATE candidate_offers
           SET expires_at = CASE
               WHEN status = 'sent' THEN now() + interval '14 days'
               WHEN status IN ('accepted', 'declined', 'expired', 'withdrawn')
                   THEN COALESCE(sent_at, created_at) + interval '14 days'
               ELSE now() + interval '14 days'
           END
         WHERE expires_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE candidate_offers
           SET expires_at = now() + interval '30 days'
         WHERE status = 'sent'
           AND expires_at > now() + interval '30 days'
        """
    )
    op.alter_column(
        "candidate_offers",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "candidate_offers",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
