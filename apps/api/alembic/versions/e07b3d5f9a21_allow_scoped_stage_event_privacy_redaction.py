"""Allow narrowly scoped privacy redaction of stage-event free text.

Revision ID: e07b3d5f9a21
Revises: d96a4c8e2f70
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "e07b3d5f9a21"
down_revision: str | Sequence[str] | None = "d96a4c8e2f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PRIVACY_AWARE_TRIGGER_FUNCTION = sa.text(
    r"""
    CREATE OR REPLACE FUNCTION protect_application_stage_event()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE'
           AND (
               current_setting('app.allow_erasure', true) = 'on'
               OR pg_trigger_depth() > 1
           ) THEN
            RETURN OLD;
        END IF;

        -- Internal FK actions may preserve history while a parent identity is
        -- erased. Direct lifecycle mutations remain forbidden.
        IF TG_OP = 'UPDATE' AND pg_trigger_depth() > 1 THEN
            RETURN NEW;
        END IF;

        -- Candidate erasure may redact only the two free-text payload fields,
        -- and only to fixed tombstones inside the audited transaction-local
        -- erasure scope. Every lifecycle, tenant, actor, and timestamp field
        -- must remain byte-for-byte unchanged. This cannot forge a stage event.
        IF TG_OP = 'UPDATE'
           AND current_setting('app.allow_erasure', true) = 'on'
           AND NEW.id IS NOT DISTINCT FROM OLD.id
           AND NEW.organization_id IS NOT DISTINCT FROM OLD.organization_id
           AND NEW.application_id IS NOT DISTINCT FROM OLD.application_id
           AND NEW.from_stage IS NOT DISTINCT FROM OLD.from_stage
           AND NEW.to_stage IS NOT DISTINCT FROM OLD.to_stage
           AND NEW.actor_user_id IS NOT DISTINCT FROM OLD.actor_user_id
           AND NEW.actor_kind IS NOT DISTINCT FROM OLD.actor_kind
           AND NEW.occurred_at IS NOT DISTINCT FROM OLD.occurred_at
           AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
           AND (
               NEW.reason IS NOT DISTINCT FROM OLD.reason
               OR NEW.reason IS NULL
           )
           AND (
               NEW.details IS NOT DISTINCT FROM OLD.details
               OR NEW.details = jsonb_build_object('privacy_erased', true)
           )
           AND (
               NEW.reason IS DISTINCT FROM OLD.reason
               OR NEW.details IS DISTINCT FROM OLD.details
           ) THEN
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'Application stage events are append-only';
    END;
    $$ LANGUAGE plpgsql;
    """
)


_STRICT_TRIGGER_FUNCTION = sa.text(
    r"""
    CREATE OR REPLACE FUNCTION protect_application_stage_event()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE'
           AND (
               current_setting('app.allow_erasure', true) = 'on'
               OR pg_trigger_depth() > 1
           ) THEN
            RETURN OLD;
        END IF;
        IF TG_OP = 'UPDATE' AND pg_trigger_depth() > 1 THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'Application stage events are append-only';
    END;
    $$ LANGUAGE plpgsql;
    """
)


def upgrade() -> None:
    op.execute(_PRIVACY_AWARE_TRIGGER_FUNCTION)


def downgrade() -> None:
    op.execute(_STRICT_TRIGGER_FUNCTION)
