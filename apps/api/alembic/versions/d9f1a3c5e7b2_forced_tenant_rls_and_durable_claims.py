"""Forced tenant RLS, fixed bootstrap claims, and durable worker leases.

Revision ID: d9f1a3c5e7b2
Revises: c8e2f4a6b901
Create Date: 2026-08-22

The tenant constraints and indexes deliberately use deterministic prefixes.
They are catalogue-derived so an ID-only tenant edge cannot be missed when the
schema grows. ``alembic.env.include_object`` ignores only those database-owned
objects while continuing to report ordinary model drift.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d9f1a3c5e7b2"
down_revision: str | Sequence[str] | None = "c8e2f4a6b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CANONICAL_FIELD_VISIBILITY = (
    "'{\"candidate_contacts\":[\"admin\",\"recruiter\"],"
    "\"candidate_cv\":[\"admin\",\"recruiter\"],"
    "\"assessment_results\":[\"admin\",\"recruiter\","
    "\"hiring_manager\",\"interviewer\"],"
    "\"compensation\":[\"admin\",\"hiring_manager\"],"
    "\"offers\":[\"admin\",\"recruiter\",\"hiring_manager\"],"
    "\"exports\":[\"admin\",\"recruiter\"],"
    "\"backups\":[\"admin\"],"
    "\"ai_context\":[\"admin\",\"recruiter\"],"
    "\"secrets\":[\"admin\"],\"audit\":[\"admin\"]}'::jsonb"
)


def _execute_sql_script(script: str) -> None:
    """Execute a PostgreSQL script one statement at a time for asyncpg.

    asyncpg intentionally rejects multiple top-level commands in a prepared
    statement. This small splitter understands SQL strings, identifiers,
    comments, and dollar-quoted function/DO bodies so migration scripts remain
    readable without relying on a driver-specific raw connection escape hatch.
    """

    statements: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    block_comment = False
    line_comment = False
    while index < len(script):
        if line_comment:
            if script[index] == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if script.startswith("*/", index):
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is None and script.startswith("--", index):
            line_comment = True
            index += 2
            continue
        if quote is None and script.startswith("/*", index):
            block_comment = True
            index += 2
            continue
        character = script[index]
        if quote in {"'", '"'}:
            if character == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if quote is not None and quote.startswith("$"):
            if script.startswith(quote, index):
                index += len(quote)
                quote = None
            else:
                index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "$":
            end = script.find("$", index + 1)
            if end != -1:
                tag = script[index : end + 1]
                if tag == "$$" or tag[1:-1].replace("_", "a").isalnum():
                    quote = tag
                    index = end + 1
                    continue
        if character == ";":
            statement = script[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    trailing = script[start:].strip()
    if trailing:
        statements.append(trailing)
    if quote is not None or block_comment:
        raise ValueError("Unterminated SQL quote/comment in migration script")
    for statement in statements:
        op.execute(statement)


def _create_roles_and_security_schema() -> None:
    _execute_sql_script(
        """
        DO $roles$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_runtime') THEN
            CREATE ROLE hr_agent_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION NOBYPASSRLS;
          ELSE
            ALTER ROLE hr_agent_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION NOBYPASSRLS;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_worker') THEN
            CREATE ROLE hr_agent_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION NOBYPASSRLS;
          ELSE
            ALTER ROLE hr_agent_worker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION NOBYPASSRLS;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_broker') THEN
            CREATE ROLE hr_agent_broker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION BYPASSRLS;
          ELSE
            ALTER ROLE hr_agent_broker NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION BYPASSRLS;
          END IF;
        END
        $roles$;
        CREATE SCHEMA IF NOT EXISTS security AUTHORIZATION hr_agent_broker;
        ALTER SCHEMA security OWNER TO hr_agent_broker;
        REVOKE ALL ON SCHEMA security FROM PUBLIC;
        GRANT USAGE ON SCHEMA security TO hr_agent_runtime, hr_agent_worker;
        GRANT USAGE ON SCHEMA public TO hr_agent_runtime, hr_agent_worker, hr_agent_broker;
        """
    )


def _add_auth_tenant_columns() -> None:
    for table in (
        "revoked_access_tokens",
        "auth_refresh_sessions",
        "auth_refresh_tokens",
        "user_mfa_recovery_codes",
    ):
        op.add_column(table, sa.Column("organization_id", sa.UUID(), nullable=True))

    _execute_sql_script(
        """
        UPDATE public.revoked_access_tokens AS child
           SET organization_id = parent.organization_id
          FROM public.users AS parent
         WHERE parent.id = child.user_id;
        UPDATE public.auth_refresh_sessions AS child
           SET organization_id = parent.organization_id
          FROM public.users AS parent
         WHERE parent.id = child.user_id;
        UPDATE public.user_mfa_recovery_codes AS child
           SET organization_id = parent.organization_id
          FROM public.users AS parent
         WHERE parent.id = child.user_id;
        UPDATE public.auth_refresh_tokens AS child
           SET organization_id = parent.organization_id
          FROM public.auth_refresh_sessions AS parent
         WHERE parent.id = child.session_id;
        """
    )
    for table in (
        "revoked_access_tokens",
        "auth_refresh_sessions",
        "auth_refresh_tokens",
        "user_mfa_recovery_codes",
    ):
        op.alter_column(table, "organization_id", existing_type=sa.UUID(), nullable=False)
        op.create_index(
            f"ix_{table}_organization_id",
            table,
            ["organization_id"],
        )
        op.create_foreign_key(
            f"fk_{table}_organization_id_organizations",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )


def _create_user_access_tokens() -> None:
    op.create_table(
        "user_access_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose IN ('invitation', 'password_reset')",
            name="ck_user_access_token_purpose",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_user_access_token_expiry"
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR revoked_at IS NULL",
            name="ck_user_access_token_terminal_state",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_digest", name="uq_user_access_tokens_token_digest"
        ),
    )
    op.create_index(
        "ix_user_access_tokens_organization_id",
        "user_access_tokens",
        ["organization_id"],
    )
    op.create_index(
        "ix_user_access_tokens_user_id",
        "user_access_tokens",
        ["user_id"],
    )
    op.create_index(
        "ix_user_access_tokens_expires_at",
        "user_access_tokens",
        ["expires_at"],
    )
    op.create_index(
        "uq_user_access_token_active",
        "user_access_tokens",
        ["organization_id", "user_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL AND revoked_at IS NULL"),
    )
    op.create_index(
        "ix_user_access_tokens_org_purpose_created",
        "user_access_tokens",
        ["organization_id", "purpose", sa.text("created_at DESC")],
    )


def _add_model_governance_columns() -> None:
    op.add_column(
        "organization_model_configurations",
        sa.Column("managed_processing_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organization_model_configurations",
        sa.Column("managed_processing_accepted_by_user_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "organization_model_configurations",
        sa.Column("managed_processing_notice_version", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "organization_model_configurations",
        sa.Column("managed_processing_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_org_model_managed_acceptance_user",
        "organization_model_configurations",
        "users",
        ["managed_processing_accepted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_org_model_managed_acceptance",
        "organization_model_configurations",
        "(managed_processing_accepted_at IS NULL AND "
        "managed_processing_notice_version IS NULL) OR "
        "(managed_processing_accepted_at IS NOT NULL AND "
        "managed_processing_notice_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_org_model_managed_revocation",
        "organization_model_configurations",
        "managed_processing_revoked_at IS NULL OR "
        "managed_processing_accepted_at IS NOT NULL",
    )


def _create_interview_assignments() -> None:
    op.create_table(
        "interview_assignments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("interviewer_user_id", sa.UUID(), nullable=False),
        sa.Column("assigned_by_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["interview_id"], ["interviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["interviewer_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "interview_id",
            "interviewer_user_id",
            name="uq_interview_assignment_org_interview_user",
        ),
    )
    op.create_index(
        "ix_interview_assignments_organization_id",
        "interview_assignments",
        ["organization_id"],
    )
    op.create_index(
        "ix_interview_assignments_interview_id",
        "interview_assignments",
        ["interview_id"],
    )
    op.create_index(
        "ix_interview_assignments_interviewer_user_id",
        "interview_assignments",
        ["interviewer_user_id"],
    )
    op.create_index(
        "ix_interview_assignments_org_user_interview",
        "interview_assignments",
        ["organization_id", "interviewer_user_id", "interview_id"],
    )
    _execute_sql_script(
        """
        INSERT INTO public.interview_assignments
          (id, organization_id, interview_id, interviewer_user_id,
           assigned_by_user_id, created_at)
        SELECT gen_random_uuid(), link.organization_id, link.interview_id,
               member.user_id,
               COALESCE(link.created_by_user_id, link.organizer_user_id, member.user_id),
               member.created_at
          FROM public.interview_scheduling_link_interviewers AS member
          JOIN public.interview_scheduling_links AS link
            ON link.id = member.scheduling_link_id
           AND link.organization_id = member.organization_id
         WHERE link.interview_id IS NOT NULL
        ON CONFLICT (organization_id, interview_id, interviewer_user_id) DO NOTHING;

        INSERT INTO public.interview_assignments
          (id, organization_id, interview_id, interviewer_user_id,
           assigned_by_user_id, created_at)
        SELECT gen_random_uuid(), appointment.organization_id,
               appointment.interview_id, member.user_id,
               COALESCE(appointment.organizer_user_id, member.user_id),
               member.created_at
          FROM public.interview_appointment_interviewers AS member
          JOIN public.interview_appointments AS appointment
            ON appointment.id = member.appointment_id
           AND appointment.organization_id = member.organization_id
         WHERE appointment.interview_id IS NOT NULL
           AND member.user_id IS NOT NULL
        ON CONFLICT (organization_id, interview_id, interviewer_user_id) DO NOTHING;
        """
    )


def _create_worker_queue_leases() -> None:
    op.create_table(
        "worker_queue_leases",
        sa.Column("queue_name", sa.String(length=64), nullable=False),
        sa.Column("work_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("queue_name", "work_id"),
        sa.UniqueConstraint(
            "queue_name", "lease_token", name="uq_worker_queue_lease_token"
        ),
    )
    op.create_index(
        "ix_worker_queue_leases_queue_expiry",
        "worker_queue_leases",
        ["queue_name", "lease_expires_at"],
    )
    op.create_index(
        "ix_worker_queue_leases_org_queue",
        "worker_queue_leases",
        ["organization_id", "queue_name"],
    )


def _create_private_object_registry() -> None:
    op.create_table(
        "private_objects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            nullable=False,
            server_default="quarantined",
        ),
        sa.Column("scanner_engine", sa.String(length=64), nullable=True),
        sa.Column("scanner_version", sa.String(length=64), nullable=True),
        sa.Column("scanner_verdict", sa.String(length=64), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.UUID(), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legal_hold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "category IN ('candidates', 'assessments', 'tasks', 'ai')",
            name="ck_private_objects_category",
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 15728640",
            name="ck_private_objects_size",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_private_objects_sha256"
        ),
        sa.CheckConstraint(
            "state IN ('quarantined', 'ready', 'rejected', "
            "'delete_pending', 'deleted')",
            name="ck_private_objects_state",
        ),
        sa.CheckConstraint(
            "object_key ~ ('^private/' || organization_id::text || '/' || "
            "category || '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-' || "
            "'[0-9a-f]{4}-[0-9a-f]{12}\\.[a-z0-9]{1,16}$')",
            name="ck_private_objects_key_scope",
        ),
        sa.CheckConstraint(
            "(state <> 'ready' OR ready_at IS NOT NULL) AND "
            "(state <> 'rejected' OR rejected_at IS NOT NULL) AND "
            "(state <> 'deleted' OR deleted_at IS NOT NULL)",
            name="ck_private_objects_lifecycle_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_private_objects_object_key"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_private_objects_org_id"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "object_key",
            name="uq_private_objects_org_object_key",
        ),
    )
    op.create_index(
        "ix_private_objects_org_resource",
        "private_objects",
        ["organization_id", "resource_type", "resource_id"],
    )
    op.create_index(
        "ix_private_objects_retention",
        "private_objects",
        ["state", "retention_until"],
        postgresql_where=sa.text("state IN ('ready', 'delete_pending')"),
    )

    op.create_table(
        "private_object_deletions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("private_object_id", sa.UUID(), nullable=False),
        sa.Column("object_key_snapshot", sa.Text(), nullable=False),
        sa.Column("sha256_snapshot", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "not_before",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "sha256_snapshot ~ '^[0-9a-f]{64}$'",
            name="ck_private_object_deletions_sha256",
        ),
        sa.CheckConstraint(
            "reason IN ('candidate_erasure', 'retention_expired', "
            "'resource_deleted', 'upload_rejected', 'replacement', 'admin_request')",
            name="ck_private_object_deletions_reason",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 10",
            name="ck_private_object_deletions_attempts",
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claim_token IS NULL)",
            name="ck_private_object_deletions_claim_tuple",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["private_object_id"],
            ["private_objects.id"],
            name="fk_private_object_deletions_object",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "private_object_id"],
            ["private_objects.organization_id", "private_objects.id"],
            name="fk_private_object_deletions_tenant_object",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_private_object_deletions_active",
        "private_object_deletions",
        ["organization_id", "private_object_id"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NULL"),
    )
    op.create_index(
        "ix_private_object_deletions_claim",
        "private_object_deletions",
        ["completed_at", "next_attempt_at", "id"],
    )

    for table in ("candidates", "assessment_files", "task_submissions"):
        op.add_column(table, sa.Column("private_object_id", sa.UUID(), nullable=True))
        op.create_index(
            f"ix_{table}_private_object_id", table, ["private_object_id"]
        )
        op.create_foreign_key(
            f"fk_{table}_private_object_id",
            table,
            "private_objects",
            ["private_object_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            f"fk_{table}_tenant_private_object",
            table,
            "private_objects",
            ["organization_id", "private_object_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )


def _secure_appointment_management_tokens() -> None:
    op.add_column(
        "interview_appointments",
        sa.Column(
            "manage_token_version", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "interview_appointments",
        sa.Column("manage_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "interview_appointments",
        sa.Column("manage_token_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE public.interview_appointments "
        "SET manage_token_expires_at = GREATEST("
        "ends_at + interval '30 days', created_at + interval '14 days')"
    )
    op.alter_column(
        "interview_appointments",
        "manage_token_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_appointment_manage_token_version",
        "interview_appointments",
        "manage_token_version >= 1",
    )
    op.create_check_constraint(
        "ck_appointment_manage_token_expiry",
        "interview_appointments",
        "manage_token_expires_at > created_at",
    )
    op.create_index(
        "ix_interview_appointments_manage_expiry",
        "interview_appointments",
        ["organization_id", "manage_token_expires_at"],
        postgresql_where=sa.text("manage_token_revoked_at IS NULL"),
    )


def _ensure_webhook_attempt_table() -> None:
    """Repair deployments that applied the evolving c8 revision too early."""

    if sa.inspect(op.get_bind()).has_table(
        "integration_webhook_delivery_attempts", schema="public"
    ):
        return
    op.create_table(
        "integration_webhook_delivery_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_number >= 1", name="ck_integration_webhook_attempt_number"
        ),
        sa.CheckConstraint(
            "status IN ('delivering', 'delivered', 'retrying', 'failed')",
            name="ck_integration_webhook_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["integration_webhook_deliveries.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_integration_webhook_delivery_attempt",
        ),
    )
    op.create_index(
        "ix_integration_webhook_delivery_attempts_organization_id",
        "integration_webhook_delivery_attempts",
        ["organization_id"],
    )
    op.create_index(
        "ix_integration_webhook_delivery_attempts_delivery_id",
        "integration_webhook_delivery_attempts",
        ["delivery_id"],
    )
    op.create_index(
        "ix_integration_webhook_attempts_org_created",
        "integration_webhook_delivery_attempts",
        [
            "organization_id",
            sa.literal_column("created_at DESC"),
            sa.literal_column("id DESC"),
        ],
    )


def _make_workflow_runs_durable() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "workflow_runs", sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workflow_runs", sa.Column("leased_by", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "workflow_runs", sa.Column("lease_token", sa.UUID(), nullable=True)
    )
    op.add_column(
        "workflow_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    _execute_sql_script(
        """
        UPDATE public.workflow_runs
           SET available_at = COALESCE(started_at, created_at, now()),
               attempt_count = CASE WHEN status = 'processing' THEN 1 ELSE 0 END,
               status = CASE WHEN status = 'processing' THEN 'failed' ELSE status END,
               error_code = CASE WHEN status = 'processing'
                                 THEN COALESCE(error_code, 'migration_interrupted')
                                 ELSE error_code END,
               completed_at = CASE
                 WHEN status IN ('processing', 'completed', 'skipped', 'failed')
                 THEN COALESCE(completed_at, started_at, created_at, now())
                 ELSE completed_at END;
        """
    )
    op.alter_column(
        "workflow_runs",
        "available_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.drop_constraint("ck_workflow_run_status", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_run_status",
        "workflow_runs",
        "status IN ('queued', 'running', 'retrying', 'completed', 'skipped', "
        "'failed', 'dead_letter', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_workflow_run_attempts",
        "workflow_runs",
        "attempt_count >= 0 AND attempt_count <= max_attempts "
        "AND max_attempts BETWEEN 1 AND 10",
    )
    op.create_check_constraint(
        "ck_workflow_run_lease_tuple",
        "workflow_runs",
        "((leased_at IS NULL AND leased_by IS NULL AND lease_token IS NULL "
        "AND lease_expires_at IS NULL) OR "
        "(leased_at IS NOT NULL AND leased_by IS NOT NULL "
        "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL))",
    )
    op.create_check_constraint(
        "ck_workflow_run_running_lease",
        "workflow_runs",
        "(status = 'running') = (lease_token IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_workflow_run_terminal_completed_at",
        "workflow_runs",
        "status NOT IN ('completed', 'skipped', 'failed', 'dead_letter', "
        "'cancelled') OR completed_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_workflow_run_cancelled_at",
        "workflow_runs",
        "status <> 'cancelled' OR cancelled_at IS NOT NULL",
    )
    op.create_index(
        "ix_workflow_runs_claimable",
        "workflow_runs",
        ["status", "available_at", "lease_expires_at", "created_at", "id"],
        postgresql_where=sa.text("status IN ('queued', 'retrying', 'running')"),
    )


def _add_static_constraints_and_defaults() -> None:
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('admin', 'recruiter', 'hiring_manager', 'interviewer', 'restricted')",
    )
    op.create_check_constraint(
        "ck_permission_override_role",
        "permission_overrides",
        "role IN ('admin', 'recruiter', 'hiring_manager', 'interviewer', 'restricted')",
    )
    op.alter_column(
        "enterprise_policies",
        "field_visibility",
        existing_type=postgresql.JSONB(),
        server_default=sa.text(CANONICAL_FIELD_VISIBILITY),
        existing_nullable=False,
    )
    _execute_sql_script(
        "CREATE UNIQUE INDEX uq_tenant_users_email_ci "
        "ON public.users (lower(email))"
    )


def _create_tenant_constraints() -> None:
    _execute_sql_script(
        """
        DO $tenant_uniques$
        DECLARE
          relation record;
          constraint_name text;
        BEGIN
          FOR relation IN
            SELECT table_name
              FROM information_schema.columns
             WHERE table_schema = 'public' AND column_name = 'organization_id'
               AND EXISTS (
                 SELECT 1 FROM information_schema.columns AS id_col
                  WHERE id_col.table_schema = 'public'
                    AND id_col.table_name = columns.table_name
                    AND id_col.column_name = 'id'
               )
             ORDER BY table_name
          LOOP
            constraint_name := left('uq_tenant_' || relation.table_name, 52)
                               || '_' || substr(md5(relation.table_name), 1, 8);
            IF NOT EXISTS (
              SELECT 1
                FROM pg_catalog.pg_constraint c
                JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
                JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
               WHERE n.nspname = 'public' AND t.relname = relation.table_name
                 AND c.contype IN ('u', 'p')
                 AND (SELECT array_agg(a.attname ORDER BY key.ord)
                        FROM unnest(c.conkey) WITH ORDINALITY AS key(attnum, ord)
                        JOIN pg_catalog.pg_attribute a
                          ON a.attrelid = c.conrelid AND a.attnum = key.attnum)
                     = ARRAY['organization_id', 'id']::name[]
            ) THEN
              EXECUTE format(
                'ALTER TABLE public.%I ADD CONSTRAINT %I UNIQUE (organization_id, id)',
                relation.table_name, constraint_name
              );
            END IF;
          END LOOP;
        END
        $tenant_uniques$;

        DO $tenant_fks$
        DECLARE
          edge record;
          constraint_name text;
          index_name text;
        BEGIN
          FOR edge IN
            SELECT child.relname AS child_table,
                   child_column.attname AS child_column,
                   parent.relname AS parent_table
              FROM pg_catalog.pg_constraint original
              JOIN pg_catalog.pg_class child ON child.oid = original.conrelid
              JOIN pg_catalog.pg_namespace child_ns ON child_ns.oid = child.relnamespace
              JOIN pg_catalog.pg_class parent ON parent.oid = original.confrelid
              JOIN pg_catalog.pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
              JOIN pg_catalog.pg_attribute child_column
                ON child_column.attrelid = child.oid
               AND child_column.attnum = original.conkey[1]
              JOIN pg_catalog.pg_attribute parent_column
                ON parent_column.attrelid = parent.oid
               AND parent_column.attnum = original.confkey[1]
             WHERE original.contype = 'f'
               AND child_ns.nspname = 'public' AND parent_ns.nspname = 'public'
               AND cardinality(original.conkey) = 1
               AND parent_column.attname = 'id'
               AND child_column.attname <> 'organization_id'
               AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                            WHERE a.attrelid = child.oid
                              AND a.attname = 'organization_id' AND NOT a.attisdropped)
               AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                            WHERE a.attrelid = parent.oid
                              AND a.attname = 'organization_id' AND NOT a.attisdropped)
             ORDER BY child.relname, child_column.attname, parent.relname
          LOOP
            constraint_name := left(
              'fk_tenant_' || edge.child_table || '_' || edge.child_column, 52
            ) || '_' || substr(
              md5(edge.child_table || ':' || edge.child_column || ':' || edge.parent_table),
              1, 8
            );
            index_name := left(
              'ix_tenant_' || edge.child_table || '_' || edge.child_column, 52
            ) || '_' || substr(md5(edge.child_table || ':' || edge.child_column), 1, 8);
            IF NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_constraint WHERE conname = constraint_name
            ) THEN
              EXECUTE format(
                'ALTER TABLE public.%I ADD CONSTRAINT %I '
                'FOREIGN KEY (organization_id, %I) '
                'REFERENCES public.%I (organization_id, id) NOT VALID',
                edge.child_table, constraint_name, edge.child_column,
                edge.parent_table
              );
              EXECUTE format(
                'ALTER TABLE public.%I VALIDATE CONSTRAINT %I',
                edge.child_table, constraint_name
              );
            END IF;
            IF to_regclass('public.' || index_name) IS NULL THEN
              EXECUTE format(
                'CREATE INDEX %I ON public.%I (organization_id, %I)',
                index_name, edge.child_table, edge.child_column
              );
            END IF;
          END LOOP;
        END
        $tenant_fks$;
        """
    )


def _enable_forced_rls() -> None:
    _execute_sql_script(
        """
        CREATE OR REPLACE FUNCTION security.current_organization_id()
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        PARALLEL SAFE
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
          value text;
        BEGIN
          value := pg_catalog.current_setting('app.organization_id', true);
          IF value IS NULL OR value = '' OR value !~*
             '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN NULL;
          END IF;
          RETURN value::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
          RETURN NULL;
        END
        $function$;
        ALTER FUNCTION security.current_organization_id() OWNER TO hr_agent_broker;
        REVOKE ALL ON FUNCTION security.current_organization_id() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION security.current_organization_id()
          TO hr_agent_runtime, hr_agent_worker;

        DO $rls$
        DECLARE relation record;
        BEGIN
          FOR relation IN
            SELECT DISTINCT table_name
              FROM information_schema.columns
             WHERE table_schema = 'public' AND column_name = 'organization_id'
             ORDER BY table_name
          LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', relation.table_name);
            EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', relation.table_name);
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', relation.table_name);
            EXECUTE format(
              'CREATE POLICY tenant_isolation ON public.%I '
              'USING (organization_id = security.current_organization_id()) '
              'WITH CHECK (organization_id = security.current_organization_id())',
              relation.table_name
            );
          END LOOP;
          ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;
          ALTER TABLE public.organizations FORCE ROW LEVEL SECURITY;
          DROP POLICY IF EXISTS organization_isolation ON public.organizations;
          CREATE POLICY organization_isolation ON public.organizations
            USING (id = security.current_organization_id())
            WITH CHECK (id = security.current_organization_id());
        END
        $rls$;
        """
    )


def _create_workflow_producer() -> None:
    _execute_sql_script(
        """
        CREATE OR REPLACE FUNCTION security.materialize_workflow_activity()
        RETURNS trigger
        LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog
        AS $function$
        BEGIN
          INSERT INTO public.workflow_runs
            (id, organization_id, rule_id, candidate_id, position_id,
             source_activity_id, rule_name_snapshot, trigger_snapshot,
             dedupe_key, status, action_results, available_at, attempt_count,
             max_attempts, started_at, created_at)
          SELECT pg_catalog.gen_random_uuid(), activity_rule.organization_id,
                 activity_rule.id, NEW.candidate_id, NEW.position_id, NEW.id,
                 activity_rule.name, activity_rule.trigger,
                 'activity:' || NEW.id::text, 'queued', '[]'::jsonb,
                 NEW.occurred_at, 0, 3, NEW.occurred_at, pg_catalog.now()
            FROM public.workflow_rules AS activity_rule
           WHERE activity_rule.organization_id = NEW.organization_id
             AND activity_rule.is_active
             AND activity_rule.trigger = NEW.event_type
             AND activity_rule.starts_at <= NEW.occurred_at
             AND activity_rule.trigger NOT IN ('candidate_inactive', 'feedback_overdue')
          ON CONFLICT (rule_id, dedupe_key) DO NOTHING;
          RETURN NEW;
        END
        $function$;
        ALTER FUNCTION security.materialize_workflow_activity()
          OWNER TO hr_agent_broker;
        REVOKE ALL ON FUNCTION security.materialize_workflow_activity() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION security.materialize_workflow_activity()
          TO hr_agent_runtime, hr_agent_worker;
        DROP TRIGGER IF EXISTS trg_materialize_workflow_activity
          ON public.candidate_activities;
        CREATE TRIGGER trg_materialize_workflow_activity
          AFTER INSERT ON public.candidate_activities
          FOR EACH ROW EXECUTE FUNCTION security.materialize_workflow_activity();
        """
    )


def _create_bootstrap_functions() -> None:
    _execute_sql_script(
        """
        GRANT SELECT ON public.users, public.auth_refresh_tokens,
          public.auth_refresh_sessions, public.organization_api_keys,
          public.assessment_invitations, public.shortlist_links,
          public.interview_scheduling_links, public.interview_appointments,
          public.candidate_offers, public.referral_links, public.organizations,
          public.positions,
          public.communication_accounts, public.source_integrations,
          public.integration_connections, public.integration_oauth_states,
          public.user_access_tokens
          TO hr_agent_broker;

        CREATE OR REPLACE FUNCTION security.claim_user_by_id(p_user_id uuid)
        RETURNS TABLE(user_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT account.id, account.organization_id
            FROM public.users AS account
           WHERE account.id = p_user_id AND account.is_active
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_user_by_email(p_email text)
        RETURNS TABLE(user_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT account.id, account.organization_id
            FROM public.users AS account
           WHERE pg_catalog.lower(account.email) = pg_catalog.lower(p_email)
             AND account.is_active
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_refresh_token(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(
          token_id uuid, session_id uuid, user_id uuid, organization_id uuid
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT token.id, family.id, account.id, family.organization_id
            FROM public.auth_refresh_tokens AS token
            JOIN public.auth_refresh_sessions AS family
              ON family.id = token.session_id
             AND family.organization_id = token.organization_id
            JOIN public.users AS account
              ON account.id = family.user_id
             AND account.organization_id = family.organization_id
           WHERE token.token_digest = p_digest
             AND token.expires_at > p_now
             AND family.expires_at > p_now
             AND family.revoked_at IS NULL
             AND account.is_active
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_api_key(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(
          api_key_id uuid, organization_id uuid,
          created_by_user_id uuid, scopes text[]
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT api_key.id, api_key.organization_id,
                 api_key.created_by_user_id, api_key.scopes
            FROM public.organization_api_keys AS api_key
           WHERE api_key.key_digest = p_digest
             AND api_key.revoked_at IS NULL
             AND (api_key.expires_at IS NULL OR api_key.expires_at > p_now)
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_assessment_invitation(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT invitation.id, invitation.organization_id, invitation.application_id
            FROM public.assessment_invitations AS invitation
           WHERE invitation.token_digest = p_digest
             AND invitation.status = 'active'
             AND invitation.expires_at > p_now
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_shortlist_link(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT link.id, link.organization_id, link.position_id
            FROM public.shortlist_links AS link
           WHERE link.token = p_digest
             AND link.revoked_at IS NULL
             AND link.expires_at > p_now
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_scheduling_link(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT link.id, link.organization_id, link.position_id
            FROM public.interview_scheduling_links AS link
           WHERE link.token_digest = p_digest
             AND link.revoked_at IS NULL
             AND link.expires_at > p_now
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_scheduling_management(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT appointment.id, appointment.organization_id,
                 appointment.scheduling_link_id
            FROM public.interview_appointments AS appointment
           WHERE appointment.manage_token_digest = p_digest
             AND appointment.manage_token_revoked_at IS NULL
             AND appointment.manage_token_expires_at > p_now
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_offer(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT offer.id, offer.organization_id, offer.candidate_id
            FROM public.candidate_offers AS offer
           WHERE offer.token_digest = p_digest
             AND offer.status IN ('sent', 'accepted', 'declined')
             AND (offer.expires_at IS NULL OR offer.expires_at > p_now)
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_referral_link(
          p_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT link.id, link.organization_id, link.position_id
            FROM public.referral_links AS link
           WHERE link.token_digest = p_digest
             AND link.is_active
             AND (link.expires_at IS NULL OR link.expires_at > p_now)
             AND (link.max_applications IS NULL
                  OR link.application_count < link.max_applications)
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_assessment_invitation(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT invitation.id, invitation.organization_id, invitation.application_id
            FROM public.assessment_invitations AS invitation
           WHERE invitation.token_digest = p_digest LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_shortlist_link(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT link.id, link.organization_id, link.position_id
            FROM public.shortlist_links AS link
           WHERE link.token = p_digest LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_scheduling_link(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT link.id, link.organization_id, link.position_id
            FROM public.interview_scheduling_links AS link
           WHERE link.token_digest = p_digest LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_scheduling_management(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT appointment.id, appointment.organization_id,
                 appointment.scheduling_link_id
            FROM public.interview_appointments AS appointment
           WHERE appointment.manage_token_digest = p_digest LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_offer(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT offer.id, offer.organization_id, offer.candidate_id
            FROM public.candidate_offers AS offer
           WHERE offer.token_digest = p_digest LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_referral_link(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT link.id, link.organization_id, link.position_id
            FROM public.referral_links AS link
           WHERE link.token_digest = p_digest LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_public_organization(p_slug text)
        RETURNS TABLE(organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT organization.id
            FROM public.organizations AS organization
           WHERE organization.slug = p_slug
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_public_position(p_position_id uuid)
        RETURNS TABLE(organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT position.organization_id
            FROM public.positions AS position
           WHERE position.id = p_position_id
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_communication_account(p_account_id uuid)
        RETURNS TABLE(capability_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT account.id, account.organization_id
            FROM public.communication_accounts AS account
           WHERE account.id = p_account_id AND account.is_active
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_source_integration(p_integration_id uuid)
        RETURNS TABLE(capability_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT integration.id, integration.organization_id
            FROM public.source_integrations AS integration
           WHERE integration.id = p_integration_id AND integration.is_active
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_integration_connection(p_connection_id uuid)
        RETURNS TABLE(capability_id uuid, organization_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT connection.id, connection.organization_id
            FROM public.integration_connections AS connection
           WHERE connection.id = p_connection_id
             AND connection.status IN ('connected', 'degraded')
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.claim_integration_oauth_state(
          p_state_digest text, p_now timestamptz
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT oauth.id, oauth.organization_id, oauth.connection_id
            FROM public.integration_oauth_states AS oauth
           WHERE oauth.state_digest = p_state_digest
             AND oauth.consumed_at IS NULL
             AND oauth.expires_at > p_now
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_integration_oauth_state(
          p_state_digest text
        )
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT oauth.id, oauth.organization_id, oauth.connection_id
            FROM public.integration_oauth_states AS oauth
           WHERE oauth.state_digest = p_state_digest
           LIMIT 1
        $function$;

        CREATE OR REPLACE FUNCTION security.resolve_user_access_token(p_digest text)
        RETURNS TABLE(capability_id uuid, organization_id uuid, resource_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT token.id, token.organization_id, token.user_id
            FROM public.user_access_tokens AS token
           WHERE token.token_digest = p_digest
           LIMIT 1
        $function$;
        """
    )
    signatures = (
        "claim_user_by_id(uuid)",
        "claim_user_by_email(text)",
        "claim_refresh_token(text,timestamptz)",
        "claim_api_key(text,timestamptz)",
        "claim_assessment_invitation(text,timestamptz)",
        "claim_shortlist_link(text,timestamptz)",
        "claim_scheduling_link(text,timestamptz)",
        "claim_scheduling_management(text,timestamptz)",
        "claim_offer(text,timestamptz)",
        "claim_referral_link(text,timestamptz)",
        "resolve_assessment_invitation(text)",
        "resolve_shortlist_link(text)",
        "resolve_scheduling_link(text)",
        "resolve_scheduling_management(text)",
        "resolve_offer(text)",
        "resolve_referral_link(text)",
        "claim_public_organization(text)",
        "claim_public_position(uuid)",
        "claim_communication_account(uuid)",
        "claim_source_integration(uuid)",
        "claim_integration_connection(uuid)",
        "claim_integration_oauth_state(text,timestamptz)",
        "resolve_integration_oauth_state(text)",
        "resolve_user_access_token(text)",
    )
    for signature in signatures:
        op.execute(f"ALTER FUNCTION security.{signature} OWNER TO hr_agent_broker")
        op.execute(f"REVOKE ALL ON FUNCTION security.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION security.{signature} TO hr_agent_runtime"
        )


def _create_worker_functions() -> None:
    _execute_sql_script(
        """
        GRANT SELECT, INSERT, UPDATE ON public.worker_queue_leases,
          public.integration_jobs, public.integration_webhook_deliveries,
          public.communication_messages, public.interview_reminders,
          public.workflow_runs, public.workflow_rules, public.mission_steps,
          public.mission_step_attempts, public.hiring_missions,
          public.ai_organization_settings, public.worker_heartbeats,
          public.private_object_deletions
          TO hr_agent_broker;
        GRANT SELECT ON public.private_objects TO hr_agent_broker;

        CREATE OR REPLACE FUNCTION security.claim_integration_jobs(
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
            SELECT job.id, job.organization_id, pg_catalog.gen_random_uuid() AS token
              FROM public.integration_jobs AS job
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'integration_jobs'
               AND existing.work_id = job.id
             WHERE job.next_attempt_at <= p_now
               AND job.attempt_count < job.max_attempts
               AND (job.status IN ('queued', 'retrying')
                    OR (job.status = 'running' AND job.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY job.next_attempt_at, job.created_at, job.id
             FOR UPDATE OF job SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.integration_jobs AS job
               SET status = 'running', attempt_count = job.attempt_count + 1,
                   leased_at = p_now, leased_by = p_worker_id,
                   lease_expires_at = p_now + interval '120 seconds',
                   updated_at = p_now
              FROM candidates
             WHERE job.id = candidates.id
             RETURNING job.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'integration_jobs', candidates.id, candidates.organization_id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
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

        CREATE OR REPLACE FUNCTION security.claim_integration_webhook_deliveries(
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
            SELECT delivery.id, delivery.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.integration_webhook_deliveries AS delivery
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'integration_webhook_deliveries'
               AND existing.work_id = delivery.id
             WHERE delivery.status IN ('queued', 'retrying')
               AND delivery.next_attempt_at <= p_now
               AND delivery.attempt_count < delivery.max_attempts
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY delivery.next_attempt_at, delivery.created_at, delivery.id
             FOR UPDATE OF delivery SKIP LOCKED
             LIMIT p_limit
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'integration_webhook_deliveries', candidates.id,
                   candidates.organization_id, candidates.token, p_worker_id,
                   p_now, p_now + interval '120 seconds'
              FROM candidates
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
               AND message.attempt_count < 10
               AND ((message.status = 'queued' AND message.scheduled_for <= p_now)
                    OR (message.status = 'processing'
                        AND message.claimed_at < p_now - interval '5 minutes'))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY message.scheduled_for, message.created_at, message.id
             FOR UPDATE OF message SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.communication_messages AS message
               SET status = 'processing', claimed_at = p_now,
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

        CREATE OR REPLACE FUNCTION security.claim_interview_reminders(
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
            SELECT reminder.id, reminder.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.interview_reminders AS reminder
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'interview_reminders'
               AND existing.work_id = reminder.id
             WHERE reminder.status = 'pending' AND reminder.due_at <= p_now
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY reminder.due_at, reminder.created_at, reminder.id
             FOR UPDATE OF reminder SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.interview_reminders AS reminder
               SET claimed_at = p_now
              FROM candidates
             WHERE reminder.id = candidates.id
             RETURNING reminder.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'interview_reminders', candidates.id,
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
    _execute_sql_script(
        """
        CREATE OR REPLACE FUNCTION security.claim_workflow_runs(
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

          UPDATE public.workflow_runs AS run
             SET status = 'dead_letter', error_code = 'attempts_exhausted',
                 completed_at = p_now, leased_at = NULL, leased_by = NULL,
                 lease_token = NULL, lease_expires_at = NULL
           WHERE run.status = 'running' AND run.lease_expires_at <= p_now
             AND run.attempt_count >= run.max_attempts;
          UPDATE public.workflow_runs AS run
             SET status = 'cancelled', cancelled_at = p_now, completed_at = p_now,
                 leased_at = NULL, leased_by = NULL, lease_token = NULL,
                 lease_expires_at = NULL,
                 error_code = COALESCE(run.error_code, 'rule_disabled')
            FROM public.workflow_rules AS rule
           WHERE run.rule_id = rule.id AND run.organization_id = rule.organization_id
             AND NOT rule.is_active
             AND run.status IN ('queued', 'retrying', 'running');

          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT run.id, run.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.workflow_runs AS run
              JOIN public.workflow_rules AS rule
                ON rule.id = run.rule_id
               AND rule.organization_id = run.organization_id
               AND rule.is_active
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'workflow_runs'
               AND existing.work_id = run.id
             WHERE run.available_at <= p_now
               AND run.attempt_count < run.max_attempts
               AND (run.status IN ('queued', 'retrying')
                    OR (run.status = 'running' AND run.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY run.available_at, run.created_at, run.id
             FOR UPDATE OF run SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.workflow_runs AS run
               SET status = 'running', attempt_count = run.attempt_count + 1,
                   leased_at = p_now, leased_by = p_worker_id,
                   lease_token = candidates.token,
                   lease_expires_at = p_now + interval '120 seconds',
                   started_at = COALESCE(run.started_at, p_now),
                   completed_at = NULL, cancelled_at = NULL, error_code = NULL
              FROM candidates
             WHERE run.id = candidates.id
             RETURNING run.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'workflow_runs', candidates.id, candidates.organization_id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
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

        CREATE OR REPLACE FUNCTION security.claim_mission_steps(
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
            SELECT step.id, step.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.mission_steps AS step
              JOIN public.hiring_missions AS mission
                ON mission.id = step.mission_id
               AND mission.organization_id = step.organization_id
               AND mission.status IN ('planning', 'running')
              JOIN public.ai_organization_settings AS settings
                ON settings.organization_id = step.organization_id
               AND settings.feature_enabled
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'mission_steps'
               AND existing.work_id = step.id
             WHERE ((step.status = 'ready' AND step.available_at <= p_now)
                    OR (step.status = 'running'
                        AND step.lease_expires_at IS NOT NULL
                        AND step.lease_expires_at <= p_now))
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
               AND (SELECT pg_catalog.count(*)
                      FROM public.mission_step_attempts AS attempt
                     WHERE attempt.step_id = step.id
                       AND attempt.organization_id = step.organization_id)
                   < step.max_attempts
             ORDER BY step.available_at, step.created_at, step.id
             FOR UPDATE OF step SKIP LOCKED
             LIMIT p_limit
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'mission_steps', candidates.id, candidates.organization_id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
              FROM candidates
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

        CREATE OR REPLACE FUNCTION security.claim_private_object_deletions(
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
            SELECT deletion.id, deletion.organization_id,
                   pg_catalog.gen_random_uuid() AS token
              FROM public.private_object_deletions AS deletion
              JOIN public.private_objects AS object
                ON object.id = deletion.private_object_id
               AND object.organization_id = deletion.organization_id
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'private_object_deletions'
               AND existing.work_id = deletion.id
             WHERE deletion.completed_at IS NULL
               AND deletion.not_before <= p_now
               AND deletion.next_attempt_at <= p_now
               AND deletion.attempt_count < 10
               AND object.state = 'delete_pending'
               AND object.legal_hold_at IS NULL
               AND (deletion.claimed_at IS NULL
                    OR deletion.claimed_at < p_now - interval '120 seconds')
               AND (existing.work_id IS NULL OR existing.lease_expires_at <= p_now)
             ORDER BY deletion.next_attempt_at, deletion.requested_at, deletion.id
             FOR UPDATE OF deletion SKIP LOCKED
             LIMIT p_limit
          ), updated AS (
            UPDATE public.private_object_deletions AS deletion
               SET attempt_count = deletion.attempt_count + 1,
                   claimed_at = p_now,
                   claim_token = candidates.token,
                   updated_at = p_now
              FROM candidates
             WHERE deletion.id = candidates.id
             RETURNING deletion.id
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'private_object_deletions', candidates.id,
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

        CREATE OR REPLACE FUNCTION security.worker_queue_stats(p_now timestamptz)
        RETURNS TABLE(
          queue_name text, due_count bigint, oldest_due_at timestamptz,
          dlq_count bigint, oldest_dlq_at timestamptz
        )
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          SELECT 'integration_jobs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE job.next_attempt_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND job.cancelled_at IS NULL
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running'
                              AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN job.status = 'running' THEN job.lease_expires_at
                        ELSE job.next_attempt_at END
                 ) FILTER (
                   WHERE job.next_attempt_at <= p_now
                     AND job.attempt_count < job.max_attempts
                     AND job.cancelled_at IS NULL
                     AND (job.status IN ('queued', 'retrying')
                          OR (job.status = 'running'
                              AND job.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE job.status = 'dead_letter'),
                 pg_catalog.min(job.completed_at) FILTER (
                   WHERE job.status = 'dead_letter')
            FROM public.integration_jobs AS job
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'integration_jobs'
             AND active.work_id = job.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'integration_webhook_deliveries'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE delivery.next_attempt_at <= p_now
                     AND delivery.attempt_count < delivery.max_attempts
                     AND delivery.status IN ('queued', 'retrying')
                     AND active.work_id IS NULL),
                 pg_catalog.min(delivery.next_attempt_at) FILTER (
                   WHERE delivery.next_attempt_at <= p_now
                     AND delivery.attempt_count < delivery.max_attempts
                     AND delivery.status IN ('queued', 'retrying')
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE delivery.status = 'dead_letter'),
                 pg_catalog.min(delivery.updated_at) FILTER (
                   WHERE delivery.status = 'dead_letter')
            FROM public.integration_webhook_deliveries AS delivery
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'integration_webhook_deliveries'
             AND active.work_id = delivery.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'communication_messages'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.attempt_count < 10
                     AND ((message.status = 'queued'
                           AND message.scheduled_for <= p_now)
                          OR (message.status = 'processing'
                              AND message.claimed_at < p_now - interval '5 minutes'))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN message.status = 'processing'
                        THEN message.claimed_at + interval '5 minutes'
                        ELSE message.scheduled_for END
                 ) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.attempt_count < 10
                     AND ((message.status = 'queued'
                           AND message.scheduled_for <= p_now)
                          OR (message.status = 'processing'
                              AND message.claimed_at < p_now - interval '5 minutes'))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.status = 'failed'),
                 pg_catalog.min(message.failed_at) FILTER (
                   WHERE message.direction = 'outbound'
                     AND message.status = 'failed')
            FROM public.communication_messages AS message
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'communication_messages'
             AND active.work_id = message.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'interview_reminders'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE reminder.status = 'pending'
                     AND reminder.due_at <= p_now
                     AND active.work_id IS NULL),
                 pg_catalog.min(reminder.due_at) FILTER (
                   WHERE reminder.status = 'pending'
                     AND reminder.due_at <= p_now
                     AND active.work_id IS NULL),
                 0::bigint, NULL::timestamptz
            FROM public.interview_reminders AS reminder
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'interview_reminders'
             AND active.work_id = reminder.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'workflow_runs'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE rule.is_active
                     AND run.available_at <= p_now
                     AND run.attempt_count < run.max_attempts
                     AND (run.status IN ('queued', 'retrying')
                          OR (run.status = 'running'
                              AND run.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN run.status = 'running' THEN run.lease_expires_at
                        ELSE run.available_at END
                 ) FILTER (
                   WHERE rule.is_active
                     AND run.available_at <= p_now
                     AND run.attempt_count < run.max_attempts
                     AND (run.status IN ('queued', 'retrying')
                          OR (run.status = 'running'
                              AND run.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE run.status = 'dead_letter'),
                 pg_catalog.min(run.completed_at) FILTER (
                   WHERE run.status = 'dead_letter')
            FROM public.workflow_runs AS run
            JOIN public.workflow_rules AS rule
              ON rule.id = run.rule_id
             AND rule.organization_id = run.organization_id
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'workflow_runs'
             AND active.work_id = run.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'mission_steps'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE settings.feature_enabled
                     AND mission.status IN ('planning', 'running')
                     AND attempts.attempt_count < step.max_attempts
                     AND ((step.status = 'ready' AND step.available_at <= p_now)
                          OR (step.status = 'running'
                              AND step.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.min(
                   CASE WHEN step.status = 'running' THEN step.lease_expires_at
                        ELSE step.available_at END
                 ) FILTER (
                   WHERE settings.feature_enabled
                     AND mission.status IN ('planning', 'running')
                     AND attempts.attempt_count < step.max_attempts
                     AND ((step.status = 'ready' AND step.available_at <= p_now)
                          OR (step.status = 'running'
                              AND step.lease_expires_at <= p_now))
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (WHERE step.status = 'failed'),
                 pg_catalog.min(step.completed_at) FILTER (
                   WHERE step.status = 'failed')
            FROM public.mission_steps AS step
            JOIN public.hiring_missions AS mission
              ON mission.id = step.mission_id
             AND mission.organization_id = step.organization_id
            LEFT JOIN public.ai_organization_settings AS settings
              ON settings.organization_id = step.organization_id
            CROSS JOIN LATERAL (
              SELECT pg_catalog.count(*) AS attempt_count
                FROM public.mission_step_attempts AS attempt
               WHERE attempt.step_id = step.id
                 AND attempt.organization_id = step.organization_id
            ) AS attempts
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'mission_steps'
             AND active.work_id = step.id
             AND active.lease_expires_at > p_now
          UNION ALL
          SELECT 'private_object_deletions'::text,
                 pg_catalog.count(*) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.not_before <= p_now
                     AND deletion.next_attempt_at <= p_now
                     AND deletion.attempt_count < 10
                     AND object.state = 'delete_pending'
                     AND object.legal_hold_at IS NULL
                     AND (deletion.claimed_at IS NULL
                          OR deletion.claimed_at < p_now - interval '120 seconds')
                     AND active.work_id IS NULL),
                 pg_catalog.min(deletion.next_attempt_at) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.not_before <= p_now
                     AND deletion.next_attempt_at <= p_now
                     AND deletion.attempt_count < 10
                     AND object.state = 'delete_pending'
                     AND object.legal_hold_at IS NULL
                     AND (deletion.claimed_at IS NULL
                          OR deletion.claimed_at < p_now - interval '120 seconds')
                     AND active.work_id IS NULL),
                 pg_catalog.count(*) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.attempt_count >= 10),
                 pg_catalog.min(deletion.updated_at) FILTER (
                   WHERE deletion.completed_at IS NULL
                     AND deletion.attempt_count >= 10)
            FROM public.private_object_deletions AS deletion
            JOIN public.private_objects AS object
              ON object.id = deletion.private_object_id
             AND object.organization_id = deletion.organization_id
            LEFT JOIN public.worker_queue_leases AS active
              ON active.queue_name = 'private_object_deletions'
             AND active.work_id = deletion.id
             AND active.lease_expires_at > p_now
        $function$;

        CREATE OR REPLACE FUNCTION security.record_worker_heartbeat(
          p_worker_name text, p_worker_id text, p_process_started_at timestamptz,
          p_last_seen_at timestamptz, p_metadata jsonb
        )
        RETURNS void
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
          IF p_worker_name IS NULL OR pg_catalog.btrim(p_worker_name) = ''
             OR pg_catalog.length(p_worker_name) > 64
             OR p_worker_id IS NULL OR pg_catalog.btrim(p_worker_id) = ''
             OR pg_catalog.length(p_worker_id) > 255
             OR p_last_seen_at < p_process_started_at
             OR pg_catalog.octet_length(p_metadata::text) > 8192 THEN
            RAISE EXCEPTION 'invalid heartbeat arguments' USING ERRCODE = '22023';
          END IF;
          INSERT INTO public.worker_heartbeats
            (id, worker_name, worker_id, process_started_at, last_seen_at,
             metadata, created_at, updated_at)
          VALUES (pg_catalog.gen_random_uuid(), p_worker_name, p_worker_id,
                  p_process_started_at, p_last_seen_at,
                  COALESCE(p_metadata, '{}'::jsonb), p_last_seen_at, p_last_seen_at)
          ON CONFLICT (worker_id) DO UPDATE
            SET worker_name = EXCLUDED.worker_name,
                process_started_at = EXCLUDED.process_started_at,
                last_seen_at = EXCLUDED.last_seen_at,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at;
        END
        $function$;
        """
    )
    worker_signatures = (
        "claim_integration_jobs(text,integer,timestamptz)",
        "claim_integration_webhook_deliveries(text,integer,timestamptz)",
        "claim_communication_messages(text,integer,timestamptz)",
        "claim_interview_reminders(text,integer,timestamptz)",
        "claim_workflow_runs(text,integer,timestamptz)",
        "claim_mission_steps(text,integer,timestamptz)",
        "claim_private_object_deletions(text,integer,timestamptz)",
        "worker_queue_stats(timestamptz)",
        "record_worker_heartbeat(text,text,timestamptz,timestamptz,jsonb)",
    )
    for signature in worker_signatures:
        op.execute(f"ALTER FUNCTION security.{signature} OWNER TO hr_agent_broker")
        op.execute(f"REVOKE ALL ON FUNCTION security.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION security.{signature} TO hr_agent_worker"
        )
    op.execute(
        "GRANT EXECUTE ON FUNCTION security.worker_queue_stats(timestamptz) "
        "TO hr_agent_runtime"
    )


def _grant_runtime_roles() -> None:
    _execute_sql_script(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
          TO hr_agent_runtime, hr_agent_worker;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
          TO hr_agent_runtime, hr_agent_worker;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES
          TO hr_agent_runtime, hr_agent_worker;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT USAGE, SELECT ON SEQUENCES TO hr_agent_runtime, hr_agent_worker;
        """
    )


def _create_maintenance_functions() -> None:
    _execute_sql_script(
        """
        GRANT SELECT, UPDATE ON public.organizations TO hr_agent_broker;
        GRANT SELECT, INSERT, UPDATE, DELETE ON public.worker_queue_leases
          TO hr_agent_broker;
        GRANT DELETE ON public.auth_login_throttles, public.worker_heartbeats
          TO hr_agent_broker;

        CREATE OR REPLACE FUNCTION security.claim_maintenance_organizations(
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
            RAISE EXCEPTION 'invalid maintenance claim arguments'
              USING ERRCODE = '22023';
          END IF;
          RETURN QUERY
          WITH candidates AS MATERIALIZED (
            SELECT organization.id, pg_catalog.gen_random_uuid() AS token
              FROM public.organizations AS organization
              LEFT JOIN public.worker_queue_leases AS existing
                ON existing.queue_name = 'maintenance'
               AND existing.work_id = organization.id
             WHERE existing.work_id IS NULL OR existing.lease_expires_at <= p_now
             ORDER BY organization.id
             FOR UPDATE OF organization SKIP LOCKED
             LIMIT p_limit
          ), leased AS (
            INSERT INTO public.worker_queue_leases
              (queue_name, work_id, organization_id, lease_token, worker_id,
               leased_at, lease_expires_at)
            SELECT 'maintenance', candidates.id, candidates.id,
                   candidates.token, p_worker_id, p_now,
                   p_now + interval '120 seconds'
              FROM candidates
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

        CREATE OR REPLACE FUNCTION security.run_global_maintenance(p_now timestamptz)
        RETURNS TABLE(
          auth_login_throttles bigint, worker_heartbeats bigint,
          expired_worker_leases bigint
        )
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
          WITH deleted_throttles AS (
            DELETE FROM public.auth_login_throttles AS throttle
             WHERE throttle.updated_at < p_now - interval '2 days'
             RETURNING 1
          ), deleted_heartbeats AS (
            DELETE FROM public.worker_heartbeats AS heartbeat
             WHERE heartbeat.last_seen_at < p_now - interval '7 days'
             RETURNING 1
          ), deleted_leases AS (
            DELETE FROM public.worker_queue_leases AS lease
             WHERE lease.lease_expires_at < p_now - interval '1 hour'
             RETURNING 1
          )
          SELECT (SELECT pg_catalog.count(*) FROM deleted_throttles),
                 (SELECT pg_catalog.count(*) FROM deleted_heartbeats),
                 (SELECT pg_catalog.count(*) FROM deleted_leases)
        $function$;
        """
    )
    for signature in (
        "claim_maintenance_organizations(text,integer,timestamptz)",
        "run_global_maintenance(timestamptz)",
    ):
        op.execute(f"ALTER FUNCTION security.{signature} OWNER TO hr_agent_broker")
        op.execute(f"REVOKE ALL ON FUNCTION security.{signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION security.{signature} TO hr_agent_worker"
        )


def upgrade() -> None:
    _create_roles_and_security_schema()
    _add_auth_tenant_columns()
    _create_user_access_tokens()
    _add_model_governance_columns()
    _create_interview_assignments()
    _create_worker_queue_leases()
    _create_private_object_registry()
    _secure_appointment_management_tokens()
    _ensure_webhook_attempt_table()
    _make_workflow_runs_durable()
    _add_static_constraints_and_defaults()
    _create_tenant_constraints()
    _enable_forced_rls()
    _create_workflow_producer()
    _create_bootstrap_functions()
    _create_worker_functions()
    _create_maintenance_functions()
    _grant_runtime_roles()


def _drop_database_owned_tenant_objects() -> None:
    op.execute(
        """
        DO $drop_tenant_objects$
        DECLARE relation record;
        BEGIN
          FOR relation IN
            SELECT n.nspname AS schema_name, t.relname AS table_name,
                   c.conname AS object_name
              FROM pg_catalog.pg_constraint c
              JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
             WHERE n.nspname = 'public'
               AND (c.conname LIKE 'fk_tenant\\_%' ESCAPE '\\'
                    OR c.conname LIKE 'uq_tenant\\_%' ESCAPE '\\')
             ORDER BY CASE WHEN c.conname LIKE 'fk_tenant\\_%' ESCAPE '\\'
                           THEN 0 ELSE 1 END, t.relname, c.conname
          LOOP
            EXECUTE format('ALTER TABLE %I.%I DROP CONSTRAINT %I',
                           relation.schema_name, relation.table_name,
                           relation.object_name);
          END LOOP;
          FOR relation IN
            SELECT schemaname AS schema_name, tablename AS table_name,
                   indexname AS object_name
              FROM pg_catalog.pg_indexes
             WHERE schemaname = 'public'
               AND indexname LIKE 'ix_tenant\\_%' ESCAPE '\\'
             ORDER BY tablename, indexname
          LOOP
            EXECUTE format('DROP INDEX %I.%I', relation.schema_name,
                           relation.object_name);
          END LOOP;
        END
        $drop_tenant_objects$;
        """
    )


def downgrade() -> None:
    _execute_sql_script(
        """
        DO $disable_rls$
        DECLARE relation record;
        BEGIN
          FOR relation IN
            SELECT DISTINCT table_name
              FROM information_schema.columns
             WHERE table_schema = 'public' AND column_name = 'organization_id'
          LOOP
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I',
                           relation.table_name);
            EXECUTE format('ALTER TABLE public.%I NO FORCE ROW LEVEL SECURITY',
                           relation.table_name);
            EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY',
                           relation.table_name);
          END LOOP;
          DROP POLICY IF EXISTS organization_isolation ON public.organizations;
          ALTER TABLE public.organizations NO FORCE ROW LEVEL SECURITY;
          ALTER TABLE public.organizations DISABLE ROW LEVEL SECURITY;
        END
        $disable_rls$;
        DROP SCHEMA IF EXISTS security CASCADE;
        """
    )
    _drop_database_owned_tenant_objects()
    op.execute("DROP INDEX IF EXISTS public.uq_tenant_users_email_ci")
    op.execute("DROP TABLE IF EXISTS public.user_access_tokens")

    # ``IF EXISTS`` also lets developers safely downgrade a local database that
    # briefly ran an earlier, uncommitted form of this consolidated revision.
    _execute_sql_script(
        """
        ALTER TABLE public.task_submissions
          DROP CONSTRAINT IF EXISTS fk_task_submissions_tenant_private_object;
        ALTER TABLE public.task_submissions
          DROP CONSTRAINT IF EXISTS fk_task_submissions_private_object_id;
        DROP INDEX IF EXISTS public.ix_task_submissions_private_object_id;
        ALTER TABLE public.task_submissions DROP COLUMN IF EXISTS private_object_id;

        ALTER TABLE public.assessment_files
          DROP CONSTRAINT IF EXISTS fk_assessment_files_tenant_private_object;
        ALTER TABLE public.assessment_files
          DROP CONSTRAINT IF EXISTS fk_assessment_files_private_object_id;
        DROP INDEX IF EXISTS public.ix_assessment_files_private_object_id;
        ALTER TABLE public.assessment_files DROP COLUMN IF EXISTS private_object_id;

        ALTER TABLE public.candidates
          DROP CONSTRAINT IF EXISTS fk_candidates_tenant_private_object;
        ALTER TABLE public.candidates
          DROP CONSTRAINT IF EXISTS fk_candidates_private_object_id;
        DROP INDEX IF EXISTS public.ix_candidates_private_object_id;
        ALTER TABLE public.candidates DROP COLUMN IF EXISTS private_object_id;

        DROP TABLE IF EXISTS public.private_object_deletions;
        DROP TABLE IF EXISTS public.private_objects;
        """
    )
    _execute_sql_script(
        """
        DROP INDEX IF EXISTS public.ix_interview_appointments_manage_expiry;
        ALTER TABLE public.interview_appointments
          DROP CONSTRAINT IF EXISTS ck_appointment_manage_token_expiry;
        ALTER TABLE public.interview_appointments
          DROP CONSTRAINT IF EXISTS ck_appointment_manage_token_version;
        ALTER TABLE public.interview_appointments
          DROP COLUMN IF EXISTS manage_token_revoked_at;
        ALTER TABLE public.interview_appointments
          DROP COLUMN IF EXISTS manage_token_expires_at;
        ALTER TABLE public.interview_appointments
          DROP COLUMN IF EXISTS manage_token_version;
        """
    )

    op.alter_column(
        "enterprise_policies",
        "field_visibility",
        existing_type=postgresql.JSONB(),
        server_default=sa.text("'{\"compensation\":[\"admin\",\"hiring_manager\"]}'::jsonb"),
        existing_nullable=False,
    )
    op.drop_constraint("ck_permission_override_role", "permission_overrides", type_="check")
    op.drop_constraint("ck_users_role", "users", type_="check")

    op.drop_index("ix_workflow_runs_claimable", table_name="workflow_runs")
    for constraint in (
        "ck_workflow_run_cancelled_at",
        "ck_workflow_run_terminal_completed_at",
        "ck_workflow_run_running_lease",
        "ck_workflow_run_lease_tuple",
        "ck_workflow_run_attempts",
    ):
        op.drop_constraint(constraint, "workflow_runs", type_="check")
    op.drop_constraint("ck_workflow_run_status", "workflow_runs", type_="check")
    op.execute(
        "UPDATE public.workflow_runs SET status = 'failed', "
        "completed_at = COALESCE(completed_at, now()) "
        "WHERE status NOT IN ('completed', 'skipped', 'failed')"
    )
    op.create_check_constraint(
        "ck_workflow_run_status",
        "workflow_runs",
        "status IN ('processing', 'completed', 'skipped', 'failed')",
    )
    for column in (
        "cancelled_at",
        "lease_expires_at",
        "lease_token",
        "leased_by",
        "leased_at",
        "max_attempts",
        "attempt_count",
        "available_at",
    ):
        op.drop_column("workflow_runs", column)

    op.drop_index("ix_worker_queue_leases_org_queue", table_name="worker_queue_leases")
    op.drop_index("ix_worker_queue_leases_queue_expiry", table_name="worker_queue_leases")
    op.drop_table("worker_queue_leases")

    op.drop_index(
        "ix_interview_assignments_org_user_interview",
        table_name="interview_assignments",
    )
    op.drop_index(
        "ix_interview_assignments_interviewer_user_id",
        table_name="interview_assignments",
    )
    op.drop_index("ix_interview_assignments_interview_id", table_name="interview_assignments")
    op.drop_index(
        "ix_interview_assignments_organization_id", table_name="interview_assignments"
    )
    op.drop_table("interview_assignments")

    op.drop_constraint(
        "ck_org_model_managed_revocation",
        "organization_model_configurations",
        type_="check",
    )
    op.drop_constraint(
        "ck_org_model_managed_acceptance",
        "organization_model_configurations",
        type_="check",
    )
    op.drop_constraint(
        "fk_org_model_managed_acceptance_user",
        "organization_model_configurations",
        type_="foreignkey",
    )
    for column in (
        "managed_processing_revoked_at",
        "managed_processing_notice_version",
        "managed_processing_accepted_by_user_id",
        "managed_processing_accepted_at",
    ):
        op.drop_column("organization_model_configurations", column)

    for table in (
        "user_mfa_recovery_codes",
        "auth_refresh_tokens",
        "auth_refresh_sessions",
        "revoked_access_tokens",
    ):
        op.drop_constraint(
            f"fk_{table}_organization_id_organizations",
            table,
            type_="foreignkey",
        )
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        op.drop_column(table, "organization_id")
    # Cluster roles intentionally remain: they may be shared by other databases
    # and have no login privilege. Deployment automation owns role lifecycle.
