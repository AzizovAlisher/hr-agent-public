"""Persist signed communication webhook event identities and fingerprints.

Revision ID: b74e1d9c0a32
Revises: a63d9e4b7c12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b74e1d9c0a32"
down_revision: str | Sequence[str] | None = "a63d9e4b7c12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "communication_messages",
        sa.Column("webhook_event_id", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "communication_messages",
        sa.Column("webhook_payload_sha256", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_communication_message_webhook_fingerprint_pair",
        "communication_messages",
        "(webhook_event_id IS NULL) = (webhook_payload_sha256 IS NULL)",
    )
    op.create_index(
        "uq_communication_messages_webhook_event",
        "communication_messages",
        ["account_id", "webhook_event_id"],
        unique=True,
        postgresql_where=sa.text("webhook_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_communication_messages_webhook_event",
        table_name="communication_messages",
    )
    op.drop_constraint(
        "ck_communication_message_webhook_fingerprint_pair",
        "communication_messages",
        type_="check",
    )
    op.drop_column("communication_messages", "webhook_payload_sha256")
    op.drop_column("communication_messages", "webhook_event_id")
