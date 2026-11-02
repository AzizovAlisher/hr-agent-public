"""Serialize tenant writes with privacy erasure and redact offer history safely.

Revision ID: e18c4f6a0b32
Revises: e07b3d5f9a21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "e18c4f6a0b32"
down_revision: str | Sequence[str] | None = "e07b3d5f9a21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_WRITE_GATE_KEY_FUNCTION = sa.text(
    r"""
    CREATE OR REPLACE FUNCTION public.privacy_erasure_gate_key(tenant_id uuid)
    RETURNS bigint
    LANGUAGE sql
    IMMUTABLE
    STRICT
    PARALLEL SAFE
    SET search_path = pg_catalog
    AS $$
        SELECT pg_catalog.hashtextextended(
            'hr-agent:tenant-privacy-erasure:' || tenant_id::text,
            0
        );
    $$;
    """
)

_WRITE_GATE_TRIGGER_FUNCTION = sa.text(
    r"""
    CREATE OR REPLACE FUNCTION public.enforce_tenant_privacy_write_gate()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $$
    DECLARE
        old_organization_id uuid;
        new_organization_id uuid;
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            old_organization_id := OLD.organization_id;
        END IF;
        IF TG_OP <> 'DELETE' THEN
            new_organization_id := NEW.organization_id;
        END IF;

        -- Existing writers retain this shared transaction lock until commit.
        -- An erasure waits for them before inventorying the tenant, then holds
        -- the exclusive counterpart through its own commit. New writes fail
        -- immediately instead of slipping into an already-scanned PII root.
        IF old_organization_id IS NOT NULL
           AND NOT pg_catalog.pg_try_advisory_xact_lock_shared(
               public.privacy_erasure_gate_key(old_organization_id)
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'Tenant privacy erasure is in progress';
        END IF;

        IF new_organization_id IS NOT NULL
           AND new_organization_id IS DISTINCT FROM old_organization_id
           AND NOT pg_catalog.pg_try_advisory_xact_lock_shared(
               public.privacy_erasure_gate_key(new_organization_id)
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'Tenant privacy erasure is in progress';
        END IF;

        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END;
    $$;
    """
)


_INSTALL_WRITE_GATES = sa.text(
    r"""
    DO $install_tenant_privacy_write_gates$
    DECLARE
        target record;
    BEGIN
        FOR target IN
            SELECT namespace.nspname AS schema_name, relation.relname AS table_name
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p')
              AND NOT relation.relispartition
              AND EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_attribute AS attribute
                  WHERE attribute.attrelid = relation.oid
                    AND attribute.attname = 'organization_id'
                    AND attribute.attnum > 0
                    AND NOT attribute.attisdropped
              )
            ORDER BY relation.relname
        LOOP
            EXECUTE pg_catalog.format(
                'DROP TRIGGER IF EXISTS trg_00_tenant_privacy_write_gate ON %I.%I',
                target.schema_name,
                target.table_name
            );
            EXECUTE pg_catalog.format(
                'CREATE TRIGGER trg_00_tenant_privacy_write_gate '
                'BEFORE INSERT OR UPDATE OR DELETE ON %I.%I '
                'FOR EACH ROW EXECUTE FUNCTION public.enforce_tenant_privacy_write_gate()',
                target.schema_name,
                target.table_name
            );
        END LOOP;
    END;
    $install_tenant_privacy_write_gates$;
    """
)


_PRIVACY_AWARE_OFFER_APPROVAL_FUNCTION = sa.text(
    r"""
    CREATE OR REPLACE FUNCTION public.protect_offer_approval_history()
    RETURNS trigger AS $protect_history$
    BEGIN
        IF TG_OP = 'DELETE'
           AND (
               current_setting('app.allow_erasure', true) = 'on'
               OR pg_trigger_depth() > 1
           ) THEN
            RETURN OLD;
        END IF;

        -- Preserve the existing behavior required by internal FK actions.
        IF TG_OP = 'UPDATE' AND pg_trigger_depth() > 1 THEN
            RETURN NEW;
        END IF;

        -- Approval history remains immutable except for candidate PII copied
        -- into its free-text fields. Erasure can only clear a comment and can
        -- only replace existing subject/body values with the fixed tombstone.
        -- Identity, vote, version/cycle, evidence, and timestamps stay exact.
        IF TG_OP = 'UPDATE'
           AND current_setting('app.allow_erasure', true) = 'on'
           AND NEW.id IS NOT DISTINCT FROM OLD.id
           AND NEW.organization_id IS NOT DISTINCT FROM OLD.organization_id
           AND NEW.offer_id IS NOT DISTINCT FROM OLD.offer_id
           AND NEW.user_id IS NOT DISTINCT FROM OLD.user_id
           AND NEW.user_email_snapshot IS NOT DISTINCT FROM OLD.user_email_snapshot
           AND NEW.offer_version_snapshot IS NOT DISTINCT FROM OLD.offer_version_snapshot
           AND NEW.approval_cycle IS NOT DISTINCT FROM OLD.approval_cycle
           AND NEW.vote IS NOT DISTINCT FROM OLD.vote
           AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at
           AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
           AND (
               NEW.comment IS NOT DISTINCT FROM OLD.comment
               OR NEW.comment IS NULL
           )
           AND (NEW.offer_snapshot - 'subject' - 'body')
               IS NOT DISTINCT FROM (OLD.offer_snapshot - 'subject' - 'body')
           AND (
               NEW.offer_snapshot -> 'subject'
                   IS NOT DISTINCT FROM OLD.offer_snapshot -> 'subject'
               OR (
                   pg_catalog.jsonb_exists(OLD.offer_snapshot, 'subject')
                   AND pg_catalog.jsonb_exists(NEW.offer_snapshot, 'subject')
                   AND NEW.offer_snapshot -> 'subject'
                       = pg_catalog.to_jsonb('[privacy erased]'::text)
               )
           )
           AND (
               NEW.offer_snapshot -> 'body'
                   IS NOT DISTINCT FROM OLD.offer_snapshot -> 'body'
               OR (
                   pg_catalog.jsonb_exists(OLD.offer_snapshot, 'body')
                   AND pg_catalog.jsonb_exists(NEW.offer_snapshot, 'body')
                   AND NEW.offer_snapshot -> 'body'
                       = pg_catalog.to_jsonb('[privacy erased]'::text)
               )
           )
           AND (
               NEW.comment IS DISTINCT FROM OLD.comment
               OR NEW.offer_snapshot IS DISTINCT FROM OLD.offer_snapshot
           ) THEN
            RETURN NEW;
        END IF;

        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
    END;
    $protect_history$ LANGUAGE plpgsql;

    """
)


_INSTALL_OFFER_APPROVAL_TRIGGER = sa.text(
    r"""
    DO $install_offer_approval_privacy_trigger$
    BEGIN
        DROP TRIGGER IF EXISTS trg_offer_approvals_append_only
          ON public.offer_approvals;
        CREATE TRIGGER trg_offer_approvals_append_only
          BEFORE UPDATE OR DELETE ON public.offer_approvals
          FOR EACH ROW EXECUTE FUNCTION public.protect_offer_approval_history();
    END;
    $install_offer_approval_privacy_trigger$;
    """
)


_GRANT_OFFER_APPROVAL_REDACTION = sa.text(
    r"""
    GRANT UPDATE (comment, offer_snapshot)
      ON TABLE public.offer_approvals
      TO hr_agent_runtime, hr_agent_worker;
    """
)


_STRICT_HISTORY_FUNCTION = sa.text(
    r"""
    CREATE OR REPLACE FUNCTION public.protect_structured_hiring_history()
    RETURNS trigger AS $protect_history$
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
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
    END;
    $protect_history$ LANGUAGE plpgsql;
    """
)


_RESTORE_OFFER_APPROVAL_TRIGGER = sa.text(
    r"""
    DO $restore_offer_approval_history_trigger$
    BEGIN
        DROP TRIGGER IF EXISTS trg_offer_approvals_append_only
          ON public.offer_approvals;
        CREATE TRIGGER trg_offer_approvals_append_only
          BEFORE UPDATE OR DELETE ON public.offer_approvals
          FOR EACH ROW EXECUTE FUNCTION public.protect_structured_hiring_history();
    END;
    $restore_offer_approval_history_trigger$;
    """
)


_DROP_OFFER_APPROVAL_FUNCTION = sa.text(
    "DROP FUNCTION IF EXISTS public.protect_offer_approval_history();"
)


_REVOKE_OFFER_APPROVAL_REDACTION = sa.text(
    r"""
    REVOKE UPDATE (comment, offer_snapshot)
      ON TABLE public.offer_approvals
      FROM hr_agent_runtime, hr_agent_worker;
    """
)


_REMOVE_WRITE_GATES = sa.text(
    r"""
    DO $remove_tenant_privacy_write_gates$
    DECLARE
        target record;
    BEGIN
        FOR target IN
            SELECT namespace.nspname AS schema_name, relation.relname AS table_name
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            JOIN pg_catalog.pg_trigger AS trigger
              ON trigger.tgrelid = relation.oid
            WHERE namespace.nspname = 'public'
              AND trigger.tgname = 'trg_00_tenant_privacy_write_gate'
              AND NOT trigger.tgisinternal
        LOOP
            EXECUTE pg_catalog.format(
                'DROP TRIGGER IF EXISTS trg_00_tenant_privacy_write_gate ON %I.%I',
                target.schema_name,
                target.table_name
            );
        END LOOP;
    END;
    $remove_tenant_privacy_write_gates$;

    """
)


_DROP_WRITE_GATE_TRIGGER_FUNCTION = sa.text(
    "DROP FUNCTION IF EXISTS public.enforce_tenant_privacy_write_gate();"
)
_DROP_WRITE_GATE_KEY_FUNCTION = sa.text(
    "DROP FUNCTION IF EXISTS public.privacy_erasure_gate_key(uuid);"
)


def upgrade() -> None:
    op.execute(_WRITE_GATE_KEY_FUNCTION)
    op.execute(_WRITE_GATE_TRIGGER_FUNCTION)
    op.execute(_INSTALL_WRITE_GATES)
    # Keep every other structured-hiring history table on the shared strict
    # trigger. PostgreSQL trigger records are table-shaped, so offer-specific
    # fields belong in a dedicated function rather than a table-name branch.
    op.execute(_STRICT_HISTORY_FUNCTION)
    op.execute(_PRIVACY_AWARE_OFFER_APPROVAL_FUNCTION)
    op.execute(_INSTALL_OFFER_APPROVAL_TRIGGER)
    op.execute(_GRANT_OFFER_APPROVAL_REDACTION)


def downgrade() -> None:
    op.execute(_REVOKE_OFFER_APPROVAL_REDACTION)
    op.execute(_RESTORE_OFFER_APPROVAL_TRIGGER)
    op.execute(_DROP_OFFER_APPROVAL_FUNCTION)
    op.execute(_STRICT_HISTORY_FUNCTION)
    op.execute(_REMOVE_WRITE_GATES)
    op.execute(_DROP_WRITE_GATE_TRIGGER_FUNCTION)
    op.execute(_DROP_WRITE_GATE_KEY_FUNCTION)
