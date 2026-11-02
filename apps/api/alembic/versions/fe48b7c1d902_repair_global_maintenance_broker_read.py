"""Repair the broker read required by global maintenance.

Revision ID: fe48b7c1d902
Revises: fd37e1b2c546
Create Date: 2026-08-23

``security.run_global_maintenance`` is owned by ``hr_agent_broker`` and filters
login throttles by ``updated_at`` before deleting them.  PostgreSQL therefore
requires the function owner to hold both ``SELECT`` and ``DELETE`` on the
table.  The original forced-RLS migration granted only ``DELETE``.  Keep that
historical migration immutable and repair deployed databases forward here.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "fe48b7c1d902"
down_revision: str | Sequence[str] | None = "fd37e1b2c546"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT ON public.auth_login_throttles TO hr_agent_broker"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT ON public.auth_login_throttles FROM hr_agent_broker"
    )
