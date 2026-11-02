"""candidate search indexes and saved views

Revision ID: d4a7b9c1e203
Revises: c2d8e4f6a901
Create Date: 2026-07-28 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "d4a7b9c1e203"
down_revision: Union[str, Sequence[str], None] = "c2d8e4f6a901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_saved_views",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "filters",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "name", name="uq_candidate_saved_view_user_name"
        ),
    )
    op.create_index(
        "ix_candidate_saved_views_organization_id",
        "candidate_saved_views",
        ["organization_id"],
    )
    op.create_index(
        "ix_candidate_saved_views_user_id",
        "candidate_saved_views",
        ["user_id"],
    )
    op.create_index(
        "ix_candidates_org_source_created",
        "candidates",
        ["organization_id", "source", sa.text("created_at DESC"), "id"],
    )
    op.create_index(
        "ix_scores_org_stage_score_candidate",
        "scores",
        [
            "organization_id",
            "stage",
            sa.text("overall_score DESC"),
            "candidate_id",
        ],
    )
    # PostgreSQL full-text search over the existing CV fields keeps the API
    # backward-compatible and avoids duplicating resume/contact data in a new
    # column.  The "simple" dictionary works consistently for RU and EN tokens.
    op.execute(
        """
        CREATE INDEX ix_candidates_search_document
        ON candidates USING gin (
          to_tsvector(
            'simple'::regconfig,
            coalesce(source_ref, '') || ' ' ||
            coalesce(raw_cv, '') || ' ' ||
            coalesce(parsed_cv::text, '')
          )
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_candidates_search_document", table_name="candidates")
    op.drop_index("ix_scores_org_stage_score_candidate", table_name="scores")
    op.drop_index("ix_candidates_org_source_created", table_name="candidates")
    op.drop_index(
        "ix_candidate_saved_views_user_id", table_name="candidate_saved_views"
    )
    op.drop_index(
        "ix_candidate_saved_views_organization_id", table_name="candidate_saved_views"
    )
    op.drop_table("candidate_saved_views")
