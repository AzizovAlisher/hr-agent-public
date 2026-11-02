"""Make nullable tenant foreign-key actions internally consistent.

Revision ID: fb15c7e9f324
Revises: fa04b6d8e213
Create Date: 2026-08-23

The database has both ordinary single-column foreign keys and database-only
tenant composite foreign keys.  For nullable references the ordinary edge
uses ``ON DELETE SET NULL``, while the tenant edge historically used the
default ``NO ACTION``.  The latter could therefore reject the exact parent
deletion that the former was meant to preserve.  PostgreSQL 15+ supports a
column list for composite ``SET NULL`` actions, so only the nullable reference
is cleared and the non-null tenant identity remains intact.

Deferred structured-hiring validation is also changed to validate the final
row at transaction end.  A parent deletion may produce more than one internal
foreign-key update; validating the captured intermediate ``NEW`` record made a
valid final state fail depending on trigger order.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "fb15c7e9f324"
down_revision: str | Sequence[str] | None = "fa04b6d8e213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EXPECTED_EDGE_COUNT = 138


def _repair_nullable_tenant_edges(*, set_null: bool) -> None:
    source_delete_action = "n" if not set_null else "a"
    expected_set_columns = (
        "AND composite.confdelsetcols = "
        "ARRAY[composite.conkey[2]]::smallint[]"
        if not set_null
        else "AND composite.confdelsetcols IS NULL"
    )
    delete_clause = (
        "ON DELETE SET NULL (%I)"
        if set_null
        else ""
    )

    op.execute(
        f"""
        DO $repair_nullable_tenant_edges$
        DECLARE
            edge record;
            repaired_count integer := 0;
        BEGIN
            FOR edge IN
                SELECT DISTINCT
                       child_namespace.nspname AS child_schema,
                       child_table.relname AS child_table,
                       composite.conname AS constraint_name,
                       child_org.attname AS child_org_column,
                       child_reference.attname AS child_reference_column,
                       parent_namespace.nspname AS parent_schema,
                       parent_table.relname AS parent_table,
                       parent_org.attname AS parent_org_column,
                       parent_reference.attname AS parent_reference_column
                  FROM pg_catalog.pg_constraint AS composite
                  JOIN pg_catalog.pg_constraint AS simple
                    ON simple.contype = 'f'
                   AND simple.confdeltype = 'n'
                   AND pg_catalog.cardinality(simple.conkey) = 1
                   AND simple.conrelid = composite.conrelid
                   AND simple.confrelid = composite.confrelid
                   AND simple.conkey[1] = composite.conkey[2]
                   AND simple.confkey[1] = composite.confkey[2]
                  JOIN pg_catalog.pg_class AS child_table
                    ON child_table.oid = composite.conrelid
                  JOIN pg_catalog.pg_namespace AS child_namespace
                    ON child_namespace.oid = child_table.relnamespace
                  JOIN pg_catalog.pg_class AS parent_table
                    ON parent_table.oid = composite.confrelid
                  JOIN pg_catalog.pg_namespace AS parent_namespace
                    ON parent_namespace.oid = parent_table.relnamespace
                  JOIN pg_catalog.pg_attribute AS child_org
                    ON child_org.attrelid = composite.conrelid
                   AND child_org.attnum = composite.conkey[1]
                  JOIN pg_catalog.pg_attribute AS child_reference
                    ON child_reference.attrelid = composite.conrelid
                   AND child_reference.attnum = composite.conkey[2]
                  JOIN pg_catalog.pg_attribute AS parent_org
                    ON parent_org.attrelid = composite.confrelid
                   AND parent_org.attnum = composite.confkey[1]
                  JOIN pg_catalog.pg_attribute AS parent_reference
                    ON parent_reference.attrelid = composite.confrelid
                   AND parent_reference.attnum = composite.confkey[2]
                 WHERE composite.contype = 'f'
                   AND composite.confdeltype = '{source_delete_action}'
                   {expected_set_columns}
                   AND composite.confupdtype = 'a'
                   AND composite.confmatchtype = 's'
                   AND NOT composite.condeferrable
                   AND NOT composite.condeferred
                   AND composite.convalidated
                   AND pg_catalog.cardinality(composite.conkey) = 2
                   AND pg_catalog.cardinality(composite.confkey) = 2
                   AND composite.conname LIKE 'fk_tenant_%'
                   AND child_namespace.nspname = 'public'
                   AND parent_namespace.nspname = 'public'
                   AND child_org.attname = 'organization_id'
                   AND parent_org.attname = 'organization_id'
                   AND parent_reference.attname = 'id'
                   AND NOT child_reference.attnotnull
                 ORDER BY child_namespace.nspname, child_table.relname,
                          composite.conname
            LOOP
                EXECUTE pg_catalog.format(
                    'ALTER TABLE %I.%I DROP CONSTRAINT %I',
                    edge.child_schema,
                    edge.child_table,
                    edge.constraint_name
                );
                EXECUTE pg_catalog.format(
                    'ALTER TABLE %I.%I ADD CONSTRAINT %I '
                    'FOREIGN KEY (%I, %I) REFERENCES %I.%I (%I, %I) '
                    '{delete_clause}',
                    edge.child_schema,
                    edge.child_table,
                    edge.constraint_name,
                    edge.child_org_column,
                    edge.child_reference_column,
                    edge.parent_schema,
                    edge.parent_table,
                    edge.parent_org_column,
                    edge.parent_reference_column,
                    edge.child_reference_column
                );
                repaired_count := repaired_count + 1;
            END LOOP;

            IF repaired_count <> {_EXPECTED_EDGE_COUNT} THEN
                RAISE EXCEPTION
                    'Expected to repair {_EXPECTED_EDGE_COUNT} nullable tenant '
                    'foreign keys, repaired %', repaired_count
                    USING ERRCODE = '55000';
            END IF;
        END;
        $repair_nullable_tenant_edges$;
        """
    )


def _install_final_state_artifact_validator() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.validate_hiring_artifact_application()
        RETURNS trigger AS $validate_application$
        DECLARE
            current_id uuid;
            current_application_id uuid;
            current_organization_id uuid;
            current_candidate_id uuid;
            current_position_id uuid;
            linked public.applications%ROWTYPE;
        BEGIN
            IF TG_TABLE_SCHEMA <> 'public'
               OR TG_TABLE_NAME NOT IN (
                    'interviews',
                    'scorecard_assignments',
                    'hiring_decisions',
                    'candidate_offers',
                    'candidate_handoffs'
               ) THEN
                RAISE EXCEPTION
                    'Unexpected hiring artifact validation target %.%',
                    TG_TABLE_SCHEMA, TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;

            -- Constraint triggers are deferred until transaction end.  Read
            -- the row again so a cascade comprising several FK updates is
            -- judged by its final state instead of an intermediate NEW image.
            EXECUTE pg_catalog.format(
                'SELECT artifact.id, artifact.application_id, '
                'artifact.organization_id, artifact.candidate_id, '
                'artifact.position_id FROM %I.%I AS artifact '
                'WHERE artifact.id = $1',
                TG_TABLE_SCHEMA,
                TG_TABLE_NAME
            )
            INTO current_id, current_application_id,
                 current_organization_id, current_candidate_id,
                 current_position_id
            USING NEW.id;

            -- A later cascade may have removed the artifact completely.
            IF current_id IS NULL OR current_application_id IS NULL THEN
                RETURN NEW;
            END IF;

            SELECT * INTO linked
              FROM public.applications AS application
             WHERE application.id = current_application_id;
            IF NOT FOUND
               OR linked.organization_id IS DISTINCT FROM current_organization_id
               OR linked.candidate_id IS DISTINCT FROM current_candidate_id
               OR linked.position_id IS DISTINCT FROM current_position_id THEN
                RAISE EXCEPTION
                    'Hiring artifact % application tenant/candidate/position mismatch',
                    current_id USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $validate_application$ LANGUAGE plpgsql;
        """
    )


def _configure_scorecard_version_fk_deferral(*, deferred: bool) -> None:
    """Allow the template/version cascade to settle before FK validation.

    Deleting a scorecard template both nulls the assignment's template link
    and cascades through the immutable version table.  Those referential
    actions can update the same assignment in either order.  Deferring the two
    equivalent version checks preserves immediate referential actions while
    validating the final transaction state.
    """

    mode = "DEFERRABLE INITIALLY DEFERRED" if deferred else "NOT DEFERRABLE"
    for constraint_name in (
        "scorecard_assignments_template_version_id_fkey",
        "fk_tenant_scorecard_assignments_template_version_id",
    ):
        op.execute(
            "ALTER TABLE public.scorecard_assignments "
            f"ALTER CONSTRAINT {constraint_name} {mode}"
        )


def _restore_intermediate_state_artifact_validator() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.validate_hiring_artifact_application()
        RETURNS trigger AS $validate_application$
        DECLARE
            linked public.applications%ROWTYPE;
        BEGIN
            IF NEW.application_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT * INTO linked
              FROM public.applications AS application
             WHERE application.id = NEW.application_id;
            IF NOT FOUND
               OR linked.organization_id IS DISTINCT FROM NEW.organization_id
               OR linked.candidate_id IS DISTINCT FROM NEW.candidate_id
               OR linked.position_id IS DISTINCT FROM NEW.position_id THEN
                RAISE EXCEPTION
                    'Hiring artifact % application tenant/candidate/position mismatch',
                    NEW.id USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $validate_application$ LANGUAGE plpgsql;
        """
    )


def upgrade() -> None:
    _repair_nullable_tenant_edges(set_null=True)
    _configure_scorecard_version_fk_deferral(deferred=True)
    _install_final_state_artifact_validator()


def downgrade() -> None:
    _restore_intermediate_state_artifact_validator()
    _configure_scorecard_version_fk_deferral(deferred=False)
    _repair_nullable_tenant_edges(set_null=False)
