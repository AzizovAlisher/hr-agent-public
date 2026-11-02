"""candidate identity fingerprints and cascading merge aliases

Revision ID: 07d2a4c6e819
Revises: f6c9d1e3a425
Create Date: 2026-07-28 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "07d2a4c6e819"
down_revision: Union[str, Sequence[str], None] = "f6c9d1e3a425"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CV_FINGERPRINT_EXPRESSION = (
    "CASE WHEN raw_cv IS NULL OR btrim(raw_cv) = '' THEN NULL "
    "ELSE md5(lower(regexp_replace(btrim(raw_cv), "
    "'[[:space:]]+', ' ', 'g'))) END"
)


def upgrade() -> None:
    op.drop_constraint(
        "fk_candidates_merged_into_candidate", "candidates", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_candidates_merged_into_candidate",
        "candidates",
        "candidates",
        ["merged_into_candidate_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column(
        "candidates",
        sa.Column(
            "duplicate_cv_fingerprint",
            sa.Text(),
            sa.Computed(CV_FINGERPRINT_EXPRESSION, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_candidates_org_cv_fingerprint",
        "candidates",
        ["organization_id", "duplicate_cv_fingerprint"],
        postgresql_where=sa.text("duplicate_cv_fingerprint IS NOT NULL"),
    )
    op.create_index(
        "ix_candidates_org_email_identity",
        "candidates",
        ["organization_id", "email_normalized"],
        postgresql_where=sa.text("email_normalized IS NOT NULL"),
    )
    op.create_index(
        "ix_candidates_org_phone_identity",
        "candidates",
        ["organization_id", "phone_normalized"],
        postgresql_where=sa.text("phone_normalized IS NOT NULL"),
    )
    op.create_index(
        "ix_candidates_org_github_identity",
        "candidates",
        ["organization_id", sa.text("lower(github_login)")],
        postgresql_where=sa.text("github_login IS NOT NULL"),
    )
    op.create_index(
        "ix_candidates_org_source_identity",
        "candidates",
        [
            "organization_id",
            sa.text("lower(source)"),
            sa.text("lower(source_ref)"),
        ],
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_candidates_org_source_identity", table_name="candidates")
    op.drop_index("ix_candidates_org_github_identity", table_name="candidates")
    op.drop_index("ix_candidates_org_phone_identity", table_name="candidates")
    op.drop_index("ix_candidates_org_email_identity", table_name="candidates")
    op.drop_index("ix_candidates_org_cv_fingerprint", table_name="candidates")
    op.drop_column("candidates", "duplicate_cv_fingerprint")
    op.drop_constraint(
        "fk_candidates_merged_into_candidate", "candidates", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_candidates_merged_into_candidate",
        "candidates",
        "candidates",
        ["merged_into_candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )
