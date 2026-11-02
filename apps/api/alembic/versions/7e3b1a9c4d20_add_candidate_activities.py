"""add append-only candidate activities

Revision ID: 7e3b1a9c4d20
Revises: b1f0c2d34e56
Create Date: 2026-07-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "7e3b1a9c4d20"
down_revision: Union[str, Sequence[str], None] = "b1f0c2d34e56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_activities",
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
            "actor_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "position_id",
            UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=True),
        sa.Column("subject_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_candidate_activities_candidate_time",
        "candidate_activities",
        ["candidate_id", sa.text("occurred_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_candidate_activities_org_type_time",
        "candidate_activities",
        ["organization_id", "event_type", sa.text("occurred_at DESC")],
    )

    # Reconstruct only events whose historical time is known. In particular,
    # current score.stage values are not turned into invented stage changes.
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, event_type, subject_type,
             subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), organization_id, id, 'candidate_added',
               'candidate', id,
               jsonb_build_object('source', source), created_at, now()
        FROM candidates
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, position_id, event_type,
             subject_type, subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), s.organization_id, s.candidate_id, s.position_id,
               'candidate_scored', 'score', s.id,
               jsonb_build_object(
                   'overall_score', s.overall_score,
                   'model_used', s.model_used,
                   'position_title', p.title,
                   'resulting_stage', s.stage
               ),
               s.scored_at, now()
        FROM scores s
        JOIN positions p ON p.id = s.position_id
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, actor_user_id, event_type,
             subject_type, subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), organization_id, candidate_id, author_user_id,
               'note_added', 'note', id, '{}'::jsonb, created_at, now()
        FROM candidate_notes
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, actor_user_id, event_type,
             subject_type, subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), organization_id, candidate_id, created_by,
               'tag_added', 'tag', id,
               jsonb_build_object('label', label), created_at, now()
        FROM candidate_tags
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, actor_user_id, position_id,
             event_type, subject_type, subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), i.organization_id, i.candidate_id, i.created_by,
               i.position_id, 'interview_generated', 'interview', i.id,
               jsonb_build_object(
                   'status', i.status,
                   'overall_score', i.overall_score,
                   'position_title', p.title
               ),
               i.created_at, now()
        FROM interviews i
        JOIN positions p ON p.id = i.position_id
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, event_type, subject_type,
             subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), organization_id, id, 'github_attached',
               'candidate', id,
               jsonb_build_object('github_login', github_login),
               github_fetched_at, now()
        FROM candidates
        WHERE github_login IS NOT NULL AND github_fetched_at IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, actor_user_id, position_id,
             event_type, subject_type, subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), r.organization_id, r.candidate_id, r.created_by,
               r.position_id, 'recording_added', 'recording', r.id,
               jsonb_build_object(
                   'title', r.title,
                   'status', r.status,
                   'position_title', p.title
               ),
               r.created_at, now()
        FROM interview_recordings r
        LEFT JOIN positions p ON p.id = r.position_id
        """
    )
    op.execute(
        """
        INSERT INTO candidate_activities
            (id, organization_id, candidate_id, actor_user_id, position_id,
             event_type, subject_type, subject_id, payload, occurred_at, created_at)
        SELECT gen_random_uuid(), t.organization_id, t.candidate_id, t.created_by,
               t.position_id, 'task_added', 'task', t.id,
               jsonb_build_object(
                   'title', t.title,
                   'status', t.status,
                   'position_title', p.title
               ),
               t.created_at, now()
        FROM task_submissions t
        LEFT JOIN positions p ON p.id = t.position_id
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_activities_org_type_time", table_name="candidate_activities"
    )
    op.drop_index(
        "ix_candidate_activities_candidate_time", table_name="candidate_activities"
    )
    op.drop_table("candidate_activities")
