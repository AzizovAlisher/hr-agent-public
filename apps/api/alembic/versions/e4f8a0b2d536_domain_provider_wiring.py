"""Wire communication accounts and appointments to tenant provider connections.

Revision ID: e4f8a0b2d536
Revises: e3d7f9a1c425
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f8a0b2d536"
down_revision: str | Sequence[str] | None = "e3d7f9a1c425"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_communication_claim_function(*, retry_safe: bool) -> None:
    attempt_limit = 3 if retry_safe else 10
    stale_attempt_operator = "<=" if retry_safe else "<"
    failure_assignment = (
        "failure_code = CASE "
        "WHEN message.status = 'queued' THEN message.failure_code "
        "ELSE NULL END, "
        if retry_safe
        else ""
    )
    # A provider-declared retry is represented by a queued row with a safe
    # failure code. Preserve that marker when claiming the retry, but clear it
    # when reclaiming an abandoned processing row. The worker can therefore
    # distinguish an authorized retry from an ambiguous post-send crash and
    # refuses to duplicate a delivery whose outcome is unknown.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION security.claim_communication_messages(
          p_worker_id text, p_limit integer, p_now timestamptz
        )
        RETURNS TABLE(
          work_id uuid, organization_id uuid, lease_token uuid,
          lease_expires_at timestamptz
        )
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
          IF p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_limit NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'invalid worker claim arguments' USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT message.id, message.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.communication_messages AS message
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'communication_messages'
               AND existing.work_id = message.id
             WHERE message.direction = 'outbound'
               AND ((message.status = 'queued'
                     AND message.attempt_count < {attempt_limit}
                    AND message.scheduled_for <= p_now)
                    OR (message.status = 'processing'
                        AND message.attempt_count {stale_attempt_operator} {attempt_limit}
                        AND message.claimed_at < p_now - interval '5 minutes'))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY message.scheduled_for, message.created_at, message.id
             FOR UPDATE OF message SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.communication_messages AS message
               SET status = 'processing', claimed_at = p_now,
                   {failure_assignment}
                   attempt_count = message.attempt_count + 1, updated_at = p_now
              FROM candidates
             WHERE message.id = candidates.id
             RETURNING message.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'communication_messages', candidates.id,
                   candidates.organization_id, candidates.token, p_worker_id,
                   p_now, p_now + interval '120 seconds'
              FROM candidates JOIN updated ON updated.id = candidates.id
            ON CONFLICT ON CONSTRAINT worker_queue_leases_pkey DO UPDATE
              SET organization_id = EXCLUDED.organization_id,
                  lease_token = EXCLUDED.lease_token,
                  worker_id = EXCLUDED.worker_id,
                  leased_at = EXCLUDED.leased_at,
                  lease_expires_at = EXCLUDED.lease_expires_at
              WHERE public.worker_queue_leases.lease_expires_at <= p_now
            RETURNING worker_queue_leases.work_id,
                      worker_queue_leases.organization_id,
                      worker_queue_leases.lease_token,
                      worker_queue_leases.lease_expires_at
          )
          SELECT leased.work_id, leased.organization_id,
                 leased.lease_token, leased.lease_expires_at FROM leased;
        END
        $function$;
        """
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_integration_connections_org_id",
        "integration_connections",
        ["organization_id", "id"],
    )

    op.add_column(
        "communication_accounts",
        sa.Column("integration_connection_id", sa.UUID(), nullable=True),
    )
    # Historical direct SMTP/Telegram/WhatsApp account rows had no encrypted
    # unified connection. They remain readable but cannot claim live delivery.
    op.execute(
        """
        UPDATE communication_accounts
           SET is_active = false
         WHERE provider IN ('smtp', 'telegram', 'whatsapp')
           AND integration_connection_id IS NULL
        """
    )
    op.drop_constraint(
        "ck_communication_account_provider",
        "communication_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_communication_account_provider",
        "communication_accounts",
        "provider IN ('sandbox', 'google_workspace', 'microsoft_365', "
        "'telegram', 'smtp', 'whatsapp')",
    )
    op.create_check_constraint(
        "ck_communication_account_connection",
        "communication_accounts",
        "(provider = 'sandbox' AND integration_connection_id IS NULL) OR "
        "(provider IN ('google_workspace', 'microsoft_365') "
        "AND integration_connection_id IS NOT NULL) OR "
        "(provider = 'telegram' AND "
        "(integration_connection_id IS NOT NULL OR is_active IS FALSE)) OR "
        "(provider IN ('smtp', 'whatsapp') AND "
        "integration_connection_id IS NULL AND is_active IS FALSE)",
    )
    op.create_foreign_key(
        "fk_communication_account_tenant_connection",
        "communication_accounts",
        "integration_connections",
        ["organization_id", "integration_connection_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_communication_accounts_connection",
        "communication_accounts",
        ["organization_id", "integration_connection_id"],
    )

    op.add_column(
        "interview_scheduling_links",
        sa.Column("calendar_connection_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scheduling_link_tenant_calendar_connection",
        "interview_scheduling_links",
        "integration_connections",
        ["organization_id", "calendar_connection_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_scheduling_links_calendar_connection",
        "interview_scheduling_links",
        ["organization_id", "calendar_connection_id"],
    )

    op.add_column(
        "interview_appointments",
        sa.Column("calendar_connection_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "interview_appointments",
        sa.Column("external_meeting_url", sa.String(length=2_048), nullable=True),
    )
    op.create_foreign_key(
        "fk_appointment_tenant_calendar_connection",
        "interview_appointments",
        "integration_connections",
        ["organization_id", "calendar_connection_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_interview_appointments_calendar_connection",
        "interview_appointments",
        ["organization_id", "calendar_connection_id", "sync_status"],
    )
    _replace_communication_claim_function(retry_safe=True)


def downgrade() -> None:
    bind = op.get_bind()
    live_accounts = bind.execute(
        sa.text(
            """
            SELECT count(*)
              FROM communication_accounts
             WHERE integration_connection_id IS NOT NULL
                OR provider IN ('google_workspace', 'microsoft_365')
            """
        )
    ).scalar_one()
    live_calendars = bind.execute(
        sa.text(
            """
            SELECT (
                (SELECT count(*) FROM interview_scheduling_links
                  WHERE calendar_connection_id IS NOT NULL) +
                (SELECT count(*) FROM interview_appointments
                  WHERE calendar_connection_id IS NOT NULL
                     OR external_event_id IS NOT NULL
                     OR external_meeting_url IS NOT NULL)
            )
            """
        )
    ).scalar_one()
    if live_accounts or live_calendars:
        raise RuntimeError(
            "Cannot downgrade provider wiring while connected domain records exist"
        )

    _replace_communication_claim_function(retry_safe=False)

    op.drop_index(
        "ix_interview_appointments_calendar_connection",
        table_name="interview_appointments",
    )
    op.drop_constraint(
        "fk_appointment_tenant_calendar_connection",
        "interview_appointments",
        type_="foreignkey",
    )
    op.drop_column("interview_appointments", "external_meeting_url")
    op.drop_column("interview_appointments", "calendar_connection_id")

    op.drop_index(
        "ix_scheduling_links_calendar_connection",
        table_name="interview_scheduling_links",
    )
    op.drop_constraint(
        "fk_scheduling_link_tenant_calendar_connection",
        "interview_scheduling_links",
        type_="foreignkey",
    )
    op.drop_column("interview_scheduling_links", "calendar_connection_id")

    op.drop_index(
        "ix_communication_accounts_connection",
        table_name="communication_accounts",
    )
    op.drop_constraint(
        "fk_communication_account_tenant_connection",
        "communication_accounts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_communication_account_connection",
        "communication_accounts",
        type_="check",
    )
    op.drop_constraint(
        "ck_communication_account_provider",
        "communication_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_communication_account_provider",
        "communication_accounts",
        "provider IN ('sandbox', 'smtp', 'telegram', 'whatsapp')",
    )
    op.drop_column("communication_accounts", "integration_connection_id")
    op.drop_constraint(
        "uq_integration_connections_org_id",
        "integration_connections",
        type_="unique",
    )
