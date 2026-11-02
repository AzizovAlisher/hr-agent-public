"""add position sensitivity, candidate_profile and public_description

Revision ID: a3f7c1d20e94
Revises: 9b2d4f6a8c10
Create Date: 2026-08-11 11:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f7c1d20e94"
down_revision: Union[str, Sequence[str], None] = "9b2d4f6a8c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows are all publicly visible today, so 'public' is both the
    # server_default and the backfill value — no separate UPDATE needed.
    op.add_column(
        "positions",
        sa.Column(
            "sensitivity",
            sa.String(length=16),
            nullable=False,
            server_default="public",
        ),
    )
    op.add_column(
        "positions",
        sa.Column("candidate_profile", sa.Text(), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("public_description", sa.Text(), nullable=True),
    )
    # Guard the enum at the DB level. The legacy `status` column predates this
    # convention and is unconstrained, but a column that gates what leaves the
    # building over unauthenticated endpoints must fail closed on a typo: an
    # unrecognised value would otherwise silently compare != 'public' in some
    # places and be treated as unknown-but-permitted in others.
    op.create_check_constraint(
        "ck_positions_sensitivity",
        "positions",
        "sensitivity IN ('public', 'nda', 'restricted')",
    )
    # Partial index: the public feed filters on sensitivity = 'public', which is
    # the overwhelming majority of rows, so index the rare non-public ones for
    # the recruiter-side "show me everything confidential" reads instead.
    op.create_index(
        "ix_positions_sensitivity_confidential",
        "positions",
        ["sensitivity"],
        postgresql_where=sa.text("sensitivity <> 'public'"),
    )


def downgrade() -> None:
    op.drop_index("ix_positions_sensitivity_confidential", table_name="positions")
    op.drop_constraint("ck_positions_sensitivity", "positions", type_="check")
    op.drop_column("positions", "public_description")
    op.drop_column("positions", "candidate_profile")
    op.drop_column("positions", "sensitivity")
