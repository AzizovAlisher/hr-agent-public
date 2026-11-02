"""add candidate_notes and candidate_tags

Revision ID: c91d4e3a8b67
Revises: b6e8d9f12a45
Create Date: 2026-05-09 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "c91d4e3a8b67"
down_revision: Union[str, Sequence[str], None] = "b6e8d9f12a45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_candidate_notes_candidate",
        "candidate_notes",
        ["candidate_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_candidate_notes_org",
        "candidate_notes",
        ["organization_id"],
    )

    op.create_table(
        "candidate_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "candidate_id", "label", name="uq_candidate_tag_label"
        ),
    )
    op.create_index(
        "ix_candidate_tags_candidate",
        "candidate_tags",
        ["candidate_id"],
    )
    op.create_index(
        "ix_candidate_tags_org",
        "candidate_tags",
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_tags_label",
        "candidate_tags",
        ["organization_id", "label"],
    )


def downgrade() -> None:
    op.drop_index("ix_candidate_tags_label", table_name="candidate_tags")
    op.drop_index("ix_candidate_tags_org", table_name="candidate_tags")
    op.drop_index("ix_candidate_tags_candidate", table_name="candidate_tags")
    op.drop_table("candidate_tags")
    op.drop_index("ix_candidate_notes_org", table_name="candidate_notes")
    op.drop_index("ix_candidate_notes_candidate", table_name="candidate_notes")
    op.drop_table("candidate_notes")
