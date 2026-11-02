"""Tenant-bound provenance for candidates imported from AI attachments.

Revision ID: e3d7f9a1c425
Revises: e2c6f8a0b314
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3d7f9a1c425"
down_revision: str | Sequence[str] | None = "e2c6f8a0b314"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_candidate_source_objects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("candidate_id", sa.UUID(), nullable=False),
        sa.Column("private_object_id", sa.UUID(), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["private_object_id"],
            ["private_objects.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "candidate_id"],
            ["candidates.organization_id", "candidates.id"],
            name="fk_ai_candidate_sources_tenant_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_ai_candidate_sources_tenant_object",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_ai_candidate_source_objects_org_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "candidate_id",
            "private_object_id",
            name="uq_ai_candidate_source_object_edge",
        ),
    )
    op.create_index(
        "ix_ai_candidate_sources_org_candidate",
        "ai_candidate_source_objects",
        ["organization_id", "candidate_id"],
    )
    op.create_index(
        "ix_ai_candidate_sources_org_object",
        "ai_candidate_source_objects",
        ["organization_id", "private_object_id"],
    )
    op.execute(
        "ALTER TABLE public.ai_candidate_source_objects ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE public.ai_candidate_source_objects FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON public.ai_candidate_source_objects "
        "USING (organization_id = security.current_organization_id()) "
        "WITH CHECK (organization_id = security.current_organization_id())"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON public.ai_candidate_source_objects TO hr_agent_runtime"
    )


def downgrade() -> None:
    # Losing these edges while candidates or source objects remain would make
    # a rolled-back deployment unable to prove complete candidate erasure.
    op.execute(
        """
        DO $downgrade$
        BEGIN
          IF EXISTS (SELECT 1 FROM public.ai_candidate_source_objects) THEN
            RAISE EXCEPTION
              'cannot downgrade while AI candidate source provenance exists';
          END IF;
        END
        $downgrade$;
        """
    )
    op.drop_index(
        "ix_ai_candidate_sources_org_object",
        table_name="ai_candidate_source_objects",
    )
    op.drop_index(
        "ix_ai_candidate_sources_org_candidate",
        table_name="ai_candidate_source_objects",
    )
    op.drop_table("ai_candidate_source_objects")
