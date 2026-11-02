"""Add explicit assessment accommodations and authorized retakes.

Revision ID: e7c1d3e5f869
Revises: e6b0c2d4e758
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7c1d3e5f869"
down_revision: str | Sequence[str] | None = "e6b0c2d4e758"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assessment_invitations",
        sa.Column(
            "authorized_attempts",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )
    op.add_column(
        "assessment_invitations",
        sa.Column(
            "accommodations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_assessment_invitation_authorized_attempts",
        "assessment_invitations",
        "authorized_attempts BETWEEN 1 AND max_attempts",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_assessment_invitation_authorized_attempts",
        "assessment_invitations",
        type_="check",
    )
    op.drop_column("assessment_invitations", "accommodations")
    op.drop_column("assessment_invitations", "authorized_attempts")
