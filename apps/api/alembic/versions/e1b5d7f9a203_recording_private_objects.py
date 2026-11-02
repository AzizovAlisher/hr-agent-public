"""Tenant-bound private objects for owned interview recordings.

Revision ID: e1b5d7f9a203
Revises: e0a4c6d8f102
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1b5d7f9a203"
down_revision: str | Sequence[str] | None = "e0a4c6d8f102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_private_objects_category",
        "private_objects",
        type_="check",
    )
    op.create_check_constraint(
        "ck_private_objects_category",
        "private_objects",
        "category IN ('candidates', 'assessments', 'tasks', 'ai', 'recordings')",
    )

    op.add_column(
        "interview_recordings",
        sa.Column("private_object_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "ix_interview_recordings_private_object_id",
        "interview_recordings",
        ["private_object_id"],
    )
    op.create_foreign_key(
        "fk_interview_recordings_private_object_id",
        "interview_recordings",
        "private_objects",
        ["private_object_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_interview_recordings_tenant_private_object",
        "interview_recordings",
        "private_objects",
        ["organization_id", "private_object_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )

    # Idempotently recover any registry rows prepared before this schema was
    # deployed. A text path alone is never trusted: all four authoritative
    # tenant/resource/category/key fields must agree.
    op.execute(
        """
        UPDATE public.interview_recordings AS recording
           SET private_object_id = object.id
          FROM public.private_objects AS object
         WHERE recording.private_object_id IS NULL
           AND recording.organization_id = object.organization_id
           AND recording.id = object.resource_id
           AND object.resource_type = 'interview_recording'
           AND object.category = 'recordings'
           AND object.state = 'ready'
           AND recording.media_path = object.object_key
        """
    )


def downgrade() -> None:
    # Silently discarding a registry row would strand physical media. Require
    # operators to erase/migrate recording objects before rolling code back.
    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM public.private_objects
             WHERE category = 'recordings'
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade while recording private objects exist';
          END IF;
        END
        $downgrade$;
        """
    )
    op.drop_constraint(
        "fk_interview_recordings_tenant_private_object",
        "interview_recordings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_interview_recordings_private_object_id",
        "interview_recordings",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_interview_recordings_private_object_id",
        table_name="interview_recordings",
    )
    op.drop_column("interview_recordings", "private_object_id")

    op.drop_constraint(
        "ck_private_objects_category",
        "private_objects",
        type_="check",
    )
    op.create_check_constraint(
        "ck_private_objects_category",
        "private_objects",
        "category IN ('candidates', 'assessments', 'tasks', 'ai')",
    )
