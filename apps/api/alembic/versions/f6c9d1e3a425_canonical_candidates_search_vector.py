"""canonical candidate aliases and generated search vector

Revision ID: f6c9d1e3a425
Revises: e5b8c0d2f314
Create Date: 2026-07-28 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID


revision: str = "f6c9d1e3a425"
down_revision: Union[str, Sequence[str], None] = "e5b8c0d2f314"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEARCH_EXPRESSION = (
    "to_tsvector('simple'::regconfig, "
    "coalesce(source_ref, '') || ' ' || "
    "coalesce(raw_cv, '') || ' ' || "
    "coalesce(parsed_cv::text, ''))"
)


def upgrade() -> None:
    op.drop_index("ix_candidates_search_document", table_name="candidates")
    op.add_column(
        "candidates",
        sa.Column(
            "search_document",
            TSVECTOR(),
            sa.Computed(SEARCH_EXPRESSION, persisted=True),
            nullable=True,
        ),
    )
    op.add_column(
        "candidates",
        sa.Column("merged_into_candidate_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("merged_by_user_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_candidates_merged_into_candidate",
        "candidates",
        "candidates",
        ["merged_into_candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_candidates_merged_by_user",
        "candidates",
        "users",
        ["merged_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_candidate_not_merged_into_self",
        "candidates",
        "merged_into_candidate_id IS NULL OR merged_into_candidate_id <> id",
    )
    op.create_index(
        "ix_candidates_search_document",
        "candidates",
        ["search_document"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_candidates_merged_aliases",
        "candidates",
        ["organization_id", "merged_into_candidate_id"],
        postgresql_where=sa.text("merged_at IS NOT NULL"),
    )

    op.drop_constraint(
        "uq_candidate_saved_view_user_name",
        "candidate_saved_views",
        type_="unique",
    )
    op.create_index(
        "uq_candidate_saved_view_user_name_ci",
        "candidate_saved_views",
        ["user_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_candidate_saved_view_user_name_ci", table_name="candidate_saved_views"
    )
    op.create_unique_constraint(
        "uq_candidate_saved_view_user_name",
        "candidate_saved_views",
        ["user_id", "name"],
    )

    op.drop_index("ix_candidates_merged_aliases", table_name="candidates")
    op.drop_index("ix_candidates_search_document", table_name="candidates")
    op.drop_constraint(
        "ck_candidate_not_merged_into_self", "candidates", type_="check"
    )
    op.drop_constraint(
        "fk_candidates_merged_by_user", "candidates", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_candidates_merged_into_candidate", "candidates", type_="foreignkey"
    )
    op.drop_column("candidates", "merged_by_user_id")
    op.drop_column("candidates", "merged_at")
    op.drop_column("candidates", "merged_into_candidate_id")
    op.drop_column("candidates", "search_document")
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
