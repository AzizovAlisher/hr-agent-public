"""add organizations and tenant scoping

Revision ID: e4add12e345f
Revises: 4f47b3e408d2
Create Date: 2026-05-04 19:16:55.322898

Single org per user. New `organizations` table; `organization_id` FK on every
tenant-scoped table (users, positions, candidates, scores, chat_sessions,
messages, interviews, interview_questions). Migration creates a default 'Demo'
org and backfills every existing row to it before enforcing NOT NULL.

Chains after the interview-prep migration so that interview tables get
org-scoped in this single sweep.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4add12e345f'
down_revision: Union[str, Sequence[str], None] = '4f47b3e408d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Stable UUID so the seed script and tests can reference the default org.
DEFAULT_ORG_ID = '00000000-0000-0000-0000-000000000001'

_TENANT_TABLES = (
    'users',
    'positions',
    'candidates',
    'scores',
    'chat_sessions',
    'messages',
    'interviews',
    'interview_questions',
)


def upgrade() -> None:
    # 1) Create organizations table.
    op.create_table(
        'organizations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )

    # 2) Seed the default organization.
    op.execute(
        f"INSERT INTO organizations (id, name, slug, created_at, updated_at) "
        f"VALUES ('{DEFAULT_ORG_ID}', 'Demo', 'demo', NOW(), NOW())"
    )

    # 3) Add organization_id to each tenant table — nullable first, then
    #    backfill, then enforce NOT NULL + FK + index.
    for table in _TENANT_TABLES:
        op.add_column(table, sa.Column('organization_id', sa.UUID(), nullable=True))
        op.execute(
            f"UPDATE {table} SET organization_id = '{DEFAULT_ORG_ID}' "
            f"WHERE organization_id IS NULL"
        )
        op.alter_column(table, 'organization_id', nullable=False)
        op.create_index(
            op.f(f'ix_{table}_organization_id'),
            table,
            ['organization_id'],
            unique=False,
        )
        op.create_foreign_key(
            f'fk_{table}_organization_id',
            table,
            'organizations',
            ['organization_id'],
            ['id'],
            ondelete='CASCADE',
        )


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES):
        op.drop_constraint(f'fk_{table}_organization_id', table, type_='foreignkey')
        op.drop_index(op.f(f'ix_{table}_organization_id'), table_name=table)
        op.drop_column(table, 'organization_id')
    op.drop_table('organizations')
