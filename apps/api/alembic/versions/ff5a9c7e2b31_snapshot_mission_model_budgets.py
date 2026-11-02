"""Snapshot and enforce per-mission model budgets.

Revision ID: ff5a9c7e2b31
Revises: fe48b7c1d902
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ff5a9c7e2b31"
down_revision: str | Sequence[str] | None = "fe48b7c1d902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hiring_missions",
        sa.Column("model_budget_cents", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE hiring_missions AS mission
        SET model_budget_cents = settings.default_model_budget_cents
        FROM ai_organization_settings AS settings
        WHERE settings.organization_id = mission.organization_id
        """
    )
    op.create_check_constraint(
        "ck_hiring_mission_model_budget",
        "hiring_missions",
        "model_budget_cents IS NULL OR model_budget_cents >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_hiring_mission_model_budget",
        "hiring_missions",
        type_="check",
    )
    op.drop_column("hiring_missions", "model_budget_cents")
