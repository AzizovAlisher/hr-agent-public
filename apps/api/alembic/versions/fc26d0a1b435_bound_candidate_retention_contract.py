"""Bound candidate retention to the private-pilot contract.

Revision ID: fc26d0a1b435
Revises: fb15c7e9f324
Create Date: 2026-08-23

The pilot retains active candidate data for 365 days by default and permits a
contract range of 30–730 days. Existing policies above the upper bound are
clamped without deleting candidate data, and the previous value is written to
the immutable audit log so an operator can review the policy change. Downgrade
uses that audit record to restore the exact prior value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "fc26d0a1b435"
down_revision: str | Sequence[str] | None = "fb15c7e9f324"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION = "migration.candidate_retention_clamped"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO audit_logs (
                id,
                organization_id,
                actor_user_id,
                actor_email_snapshot,
                action,
                resource_type,
                resource_id,
                path_template,
                method,
                status_code,
                request_id,
                metadata,
                occurred_at,
                created_at
            )
            SELECT gen_random_uuid(),
                   policy.organization_id,
                   NULL,
                   NULL,
                   :action,
                   'enterprise_policy',
                   policy.id,
                   '/migrations/fc26d0a1b435',
                   'MIGRATE',
                   200,
                   gen_random_uuid(),
                   jsonb_build_object(
                       'previous_candidate_retention_days', policy.candidate_retention_days,
                       'candidate_retention_days', 730,
                       'reason', 'private_pilot_contract_bound'
                   ),
                   statement_timestamp(),
                   statement_timestamp()
              FROM enterprise_policies AS policy
             WHERE policy.candidate_retention_days > 730
            """
        ).bindparams(action=_ACTION)
    )
    op.execute(
        "UPDATE enterprise_policies "
        "SET candidate_retention_days = 730, updated_at = statement_timestamp() "
        "WHERE candidate_retention_days > 730"
    )
    op.alter_column(
        "enterprise_policies",
        "candidate_retention_days",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("365"),
    )
    op.drop_constraint(
        "ck_policy_candidate_retention",
        "enterprise_policies",
        type_="check",
    )
    op.create_check_constraint(
        "ck_policy_candidate_retention",
        "enterprise_policies",
        "candidate_retention_days BETWEEN 30 AND 730",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_policy_candidate_retention",
        "enterprise_policies",
        type_="check",
    )
    op.alter_column(
        "enterprise_policies",
        "candidate_retention_days",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("3650"),
    )
    op.execute(
        sa.text(
            """
            UPDATE enterprise_policies AS policy
               SET candidate_retention_days =
                       (audit.metadata ->> 'previous_candidate_retention_days')::integer,
                   updated_at = statement_timestamp()
              FROM audit_logs AS audit
             WHERE audit.organization_id = policy.organization_id
               AND audit.resource_id = policy.id
               AND audit.action = :action
            """
        ).bindparams(action=_ACTION)
    )
    op.create_check_constraint(
        "ck_policy_candidate_retention",
        "enterprise_policies",
        "candidate_retention_days BETWEEN 30 AND 36500",
    )
    op.execute(
        sa.text("DELETE FROM audit_logs WHERE action = :action").bindparams(
            action=_ACTION
        )
    )
