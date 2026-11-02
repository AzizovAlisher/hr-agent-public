"""github enrichment cols + interview_recordings + task_submissions

Adds:
- candidates.github_login / github_data / github_fetched_at (GitHub profile
  enrichment, feeds AI evidence)
- interview_recordings table (external record→diarize→transcribe→score
  pipeline writes transcript / diarization / interview_score here)
- task_submissions table (technical task we send + AI absorption/evaluation)

Revision ID: f3a1c9d72e80
Revises: a91e7b5f2c08
Create Date: 2026-05-30 19:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f3a1c9d72e80"
down_revision: Union[str, Sequence[str], None] = "a91e7b5f2c08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- candidates: GitHub enrichment columns ----
    op.add_column(
        "candidates",
        sa.Column("github_login", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("github_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("github_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- interview_recordings ----
    op.create_table(
        "interview_recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("media_path", sa.String(length=512), nullable=True),
        sa.Column("media_url", sa.String(length=1024), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("diarization", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("interview_score", sa.Integer(), nullable=True),
        sa.Column("interview_summary", sa.Text(), nullable=True),
        sa.Column("evaluation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interview_recordings_organization_id",
        "interview_recordings",
        ["organization_id"],
    )
    op.create_index(
        "ix_interview_recordings_candidate_id",
        "interview_recordings",
        ["candidate_id"],
    )
    op.create_index(
        "ix_interview_recordings_position_id",
        "interview_recordings",
        ["position_id"],
    )

    # ---- task_submissions ----
    op.create_table(
        "task_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column(
            "submission_type", sa.String(length=16), nullable=False, server_default="text"
        ),
        sa.Column("repo_url", sa.String(length=1024), nullable=True),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="sent"),
        sa.Column("ai_score", sa.Integer(), nullable=True),
        sa.Column("ai_summary", sa.Text(), nullable=True),
        sa.Column("evaluation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_used", sa.String(length=128), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_submissions_organization_id", "task_submissions", ["organization_id"]
    )
    op.create_index(
        "ix_task_submissions_candidate_id", "task_submissions", ["candidate_id"]
    )
    op.create_index(
        "ix_task_submissions_position_id", "task_submissions", ["position_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_submissions_position_id", table_name="task_submissions")
    op.drop_index("ix_task_submissions_candidate_id", table_name="task_submissions")
    op.drop_index("ix_task_submissions_organization_id", table_name="task_submissions")
    op.drop_table("task_submissions")

    op.drop_index(
        "ix_interview_recordings_position_id", table_name="interview_recordings"
    )
    op.drop_index(
        "ix_interview_recordings_candidate_id", table_name="interview_recordings"
    )
    op.drop_index(
        "ix_interview_recordings_organization_id", table_name="interview_recordings"
    )
    op.drop_table("interview_recordings")

    op.drop_column("candidates", "github_fetched_at")
    op.drop_column("candidates", "github_data")
    op.drop_column("candidates", "github_login")
