"""Add assessment adjudication evidence and durable reminder delivery links.

Revision ID: a63d9e4b7c12
Revises: ff5a9c7e2b31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a63d9e4b7c12"
down_revision: str | Sequence[str] | None = "ff5a9c7e2b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "interview_reminders",
        sa.Column(
            "communication_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.execute(
        "ALTER TABLE interview_reminders "
        "ADD CONSTRAINT fk_interview_reminders_communication_message "
        "FOREIGN KEY (organization_id, communication_message_id) "
        "REFERENCES communication_messages (organization_id, id) "
        "ON DELETE SET NULL (communication_message_id)"
    )
    op.create_index(
        "uq_interview_reminders_communication_message",
        "interview_reminders",
        ["communication_message_id"],
        unique=True,
        postgresql_where=sa.text("communication_message_id IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_interview_reminder_channel",
        "interview_reminders",
        type_="check",
    )
    op.create_check_constraint(
        "ck_interview_reminder_channel",
        "interview_reminders",
        "channel IN ('in_app', 'sandbox', 'email')",
    )
    op.drop_constraint(
        "ck_interview_reminder_status",
        "interview_reminders",
        type_="check",
    )
    op.create_check_constraint(
        "ck_interview_reminder_status",
        "interview_reminders",
        "status IN ('pending', 'sent', 'failed', 'cancelled', 'skipped')",
    )

    op.add_column(
        "assessment_results",
        sa.Column(
            "knockout_review",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_assessment_result_knockout_review_valid",
        "assessment_results",
        "knockout_review IS NULL OR ("
        "knocked_out AND jsonb_typeof(knockout_review) = 'object' AND "
        "knockout_review ?& ARRAY['outcome', 'reason', 'reviewed_by_user_id', "
        "'reviewed_at'] AND "
        "knockout_review->>'outcome' IN ('confirmed', 'cleared') AND "
        "jsonb_typeof(knockout_review->'reason') = 'string' AND "
        "jsonb_typeof(knockout_review->'reviewed_by_user_id') = 'string' AND "
        "jsonb_typeof(knockout_review->'reviewed_at') = 'string')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_assessment_result_knockout_review_valid",
        "assessment_results",
        type_="check",
    )
    op.drop_column("assessment_results", "knockout_review")

    op.execute(
        "UPDATE interview_reminders SET channel = 'sandbox' "
        "WHERE channel = 'email'"
    )
    op.execute(
        "UPDATE interview_reminders SET status = 'cancelled' "
        "WHERE status = 'failed'"
    )
    op.drop_constraint(
        "ck_interview_reminder_status",
        "interview_reminders",
        type_="check",
    )
    op.create_check_constraint(
        "ck_interview_reminder_status",
        "interview_reminders",
        "status IN ('pending', 'sent', 'cancelled', 'skipped')",
    )
    op.drop_constraint(
        "ck_interview_reminder_channel",
        "interview_reminders",
        type_="check",
    )
    op.create_check_constraint(
        "ck_interview_reminder_channel",
        "interview_reminders",
        "channel IN ('in_app', 'sandbox')",
    )
    op.drop_index(
        "uq_interview_reminders_communication_message",
        table_name="interview_reminders",
    )
    op.drop_constraint(
        "fk_interview_reminders_communication_message",
        "interview_reminders",
        type_="foreignkey",
    )
    op.drop_column("interview_reminders", "communication_message_id")
