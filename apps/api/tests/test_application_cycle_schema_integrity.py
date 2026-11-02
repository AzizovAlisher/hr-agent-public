"""Direct PostgreSQL regressions for exact application-cycle ownership."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


pytestmark = pytest.mark.asyncio


APPLICATION_TABLES = (
    "candidate_activities",
    "candidate_processing_jobs",
    "chat_sessions",
    "communication_threads",
    "interview_recordings",
    "interview_scheduling_links",
    "interview_appointments",
    "source_candidate_receipts",
    "task_submissions",
    "workflow_runs",
)

PARENT_FK_CATALOG_SQL = (
    "SELECT constraint_row.conname, "
    "pg_get_constraintdef(constraint_row.oid) AS definition, "
    "constraint_row.confdeltype::text AS confdeltype "
    "FROM pg_catalog.pg_constraint AS constraint_row "
    "WHERE constraint_row.contype = 'f' "
    "AND constraint_row.conrelid::regclass::text = ANY(:tables) "
    "AND constraint_row.confrelid IN "
    "('candidates'::regclass, 'positions'::regclass) "
    "ORDER BY constraint_row.conname"
)


async def _run_alembic(database_url: str, action: str, revision: str) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url,
            "TEST_DATABASE_URL": database_url,
            "OPENROUTER_API_KEY": "",
            "VLLM_BASE_URL": "",
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        action,
        revision,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode(errors="replace")


async def _seed_application_pairs(db):
    ids = {name: uuid4() for name in (
        "organization",
        "candidate",
        "other_candidate",
        "position",
        "other_position",
        "application",
        "other_candidate_application",
        "other_position_application",
        "user",
    )}
    marker = uuid4().hex
    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (:id, :name, :slug)"
        ),
        {
            "id": ids["organization"],
            "name": f"Cycle schema {marker}",
            "slug": f"cycle-schema-{marker}",
        },
    )
    await db.execute(
        text(
            "INSERT INTO users "
            "(id, organization_id, email, password_hash, role, locale) "
            "VALUES (:id, :organization_id, :email, 'not-a-real-hash', "
            "'recruiter', 'en')"
        ),
        {
            "id": ids["user"],
            "organization_id": ids["organization"],
            "email": f"cycle-{marker}@example.test",
        },
    )
    for key in ("candidate", "other_candidate"):
        await db.execute(
            text(
                "INSERT INTO candidates (id, organization_id, source, source_ref) "
                "VALUES (:id, :organization_id, 'manual', :source_ref)"
            ),
            {
                "id": ids[key],
                "organization_id": ids["organization"],
                "source_ref": key,
            },
        )
    for key in ("position", "other_position"):
        await db.execute(
            text(
                "INSERT INTO positions "
                "(id, organization_id, title, status, sensitivity) "
                "VALUES (:id, :organization_id, :title, 'open', 'public')"
            ),
            {
                "id": ids[key],
                "organization_id": ids["organization"],
                "title": key,
            },
        )
    for application_key, candidate_key, position_key in (
        ("application", "candidate", "position"),
        ("other_candidate_application", "other_candidate", "position"),
        ("other_position_application", "candidate", "other_position"),
    ):
        await db.execute(
            text(
                "INSERT INTO applications "
                "(id, organization_id, candidate_id, position_id, stage, source, "
                " cycle_number, applied_at) "
                "VALUES (:id, :organization_id, :candidate_id, :position_id, "
                " 'new', 'manual', 1, :applied_at)"
            ),
            {
                "id": ids[application_key],
                "organization_id": ids["organization"],
                "candidate_id": ids[candidate_key],
                "position_id": ids[position_key],
                "applied_at": datetime.now(UTC) - timedelta(days=1),
            },
        )
    await db.commit()
    return ids


async def _seed_application_artifacts(db, ids):
    artifact_ids = {table: uuid4() for table in APPLICATION_TABLES}
    now = datetime.now(UTC)
    common = {
        "organization_id": ids["organization"],
        "candidate_id": ids["candidate"],
        "position_id": ids["position"],
        "application_id": ids["application"],
    }
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " event_type) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, 'erasure_matrix')"
        ),
        {"id": artifact_ids["candidate_activities"], **common},
    )
    await db.execute(
        text(
            "INSERT INTO candidate_processing_jobs "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " kind, status, idempotency_key, available_at) "
            "VALUES (:id, :organization_id, :candidate_id, :position_id, "
            " :application_id, 'score', 'queued', :key, :now)"
        ),
        {
            "id": artifact_ids["candidate_processing_jobs"],
            "key": f"erasure-{artifact_ids['candidate_processing_jobs']}",
            "now": now,
            **common,
        },
    )
    await db.execute(
        text(
            "INSERT INTO chat_sessions "
            "(id, organization_id, user_id, scope, candidate_id, position_id, "
            " application_id) VALUES (:id, :organization_id, :user_id, 'candidate', "
            " :candidate_id, :position_id, :application_id)"
        ),
        {
            "id": artifact_ids["chat_sessions"],
            "user_id": ids["user"],
            **common,
        },
    )
    await db.execute(
        text(
            "INSERT INTO communication_threads "
            "(id, organization_id, candidate_id, position_id, application_id, channel) "
            "VALUES (:id, :organization_id, :candidate_id, :position_id, "
            " :application_id, 'email')"
        ),
        {"id": artifact_ids["communication_threads"], **common},
    )
    await db.execute(
        text(
            "INSERT INTO interview_recordings "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " title, status) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, 'Erasure recording', 'pending')"
        ),
        {"id": artifact_ids["interview_recordings"], **common},
    )
    await db.execute(
        text(
            "INSERT INTO interview_scheduling_links "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " token_digest, duration_minutes, slot_interval_minutes, "
            " min_notice_minutes, booking_window_days, buffer_minutes, location_mode, "
            " reminder_offsets, expires_at) VALUES (:id, :organization_id, "
            " :candidate_id, :position_id, :application_id, :token, 60, 30, 120, "
            " 14, 0, 'video', '[1440,60]'::jsonb, :expires_at)"
        ),
        {
            "id": artifact_ids["interview_scheduling_links"],
            "token": uuid4().hex * 2,
            "expires_at": now + timedelta(days=7),
            **common,
        },
    )
    await db.execute(
        text(
            "INSERT INTO interview_appointments "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " starts_at, ends_at, candidate_timezone, location_mode, "
            " position_title_snapshot, manage_token_digest, manage_token_expires_at, "
            " idempotency_key) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, :starts_at, :ends_at, 'UTC', 'video', "
            " 'Erasure role', :token, :token_expires_at, :key)"
        ),
        {
            "id": artifact_ids["interview_appointments"],
            "starts_at": now + timedelta(days=2),
            "ends_at": now + timedelta(days=2, hours=1),
            "token": uuid4().hex * 2,
            "token_expires_at": now + timedelta(days=30),
            "key": uuid4().hex,
            **common,
        },
    )
    await db.execute(
        text(
            "INSERT INTO source_candidate_receipts "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " external_id, provider_snapshot, received_at) VALUES (:id, "
            " :organization_id, :candidate_id, :position_id, :application_id, "
            " :external_id, 'test', :now)"
        ),
        {
            "id": artifact_ids["source_candidate_receipts"],
            "external_id": f"erasure-{uuid4()}",
            "now": now,
            **common,
        },
    )
    await db.execute(
        text(
            "INSERT INTO task_submissions "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " title, submission_type, status) VALUES (:id, :organization_id, "
            " :candidate_id, :position_id, :application_id, 'Erasure task', "
            " 'text', 'sent')"
        ),
        {"id": artifact_ids["task_submissions"], **common},
    )
    await db.execute(
        text(
            "INSERT INTO workflow_runs "
            "(id, organization_id, rule_id_snapshot, rule_version_snapshot, "
            " candidate_id, position_id, application_id, rule_name_snapshot, "
            " trigger_snapshot, conditions_snapshot, actions_snapshot, dedupe_key, "
            " status, available_at, started_at) VALUES (:id, :organization_id, "
            " :rule_id, 1, :candidate_id, :position_id, :application_id, "
            " 'Erasure workflow', 'candidate_updated', '{}'::jsonb, '[]'::jsonb, "
            " :key, 'queued', :now, :now)"
        ),
        {
            "id": artifact_ids["workflow_runs"],
            "rule_id": uuid4(),
            "key": f"erasure-{uuid4()}",
            "now": now,
            **common,
        },
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()
    return artifact_ids


async def _seed_non_application_artifacts(db, ids):
    now = datetime.now(UTC)
    rows = {
        "candidate_only": {
            table: uuid4()
            for table in APPLICATION_TABLES
            if table != "interview_scheduling_links"
        },
        "position_only": {
            table: uuid4()
            for table in ("chat_sessions", "source_candidate_receipts", "workflow_runs")
        },
        "global": {
            table: uuid4()
            for table in ("chat_sessions", "source_candidate_receipts", "workflow_runs")
        },
    }
    candidate_only = rows["candidate_only"]
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, event_type) "
            "VALUES (:id, :organization_id, :candidate_id, 'candidate_only')"
        ),
        {
            "id": candidate_only["candidate_activities"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidate_processing_jobs "
            "(id, organization_id, candidate_id, kind, status, idempotency_key, "
            " available_at) VALUES (:id, :organization_id, :candidate_id, "
            " 'parse_cv', 'queued', :key, :now)"
        ),
        {
            "id": candidate_only["candidate_processing_jobs"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "key": f"candidate-only-{uuid4()}",
            "now": now,
        },
    )
    await db.execute(
        text(
            "INSERT INTO chat_sessions "
            "(id, organization_id, user_id, scope, candidate_id) "
            "VALUES (:id, :organization_id, :user_id, 'candidate', :candidate_id)"
        ),
        {
            "id": candidate_only["chat_sessions"],
            "organization_id": ids["organization"],
            "user_id": ids["user"],
            "candidate_id": ids["candidate"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO communication_threads "
            "(id, organization_id, candidate_id, channel) "
            "VALUES (:id, :organization_id, :candidate_id, 'email')"
        ),
        {
            "id": candidate_only["communication_threads"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO interview_recordings "
            "(id, organization_id, candidate_id, title, status) "
            "VALUES (:id, :organization_id, :candidate_id, "
            "'Candidate-only recording', 'pending')"
        ),
        {
            "id": candidate_only["interview_recordings"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO interview_appointments "
            "(id, organization_id, candidate_id, starts_at, ends_at, "
            " candidate_timezone, location_mode, position_title_snapshot, "
            " manage_token_digest, manage_token_expires_at, idempotency_key) "
            "VALUES (:id, :organization_id, :candidate_id, :starts_at, :ends_at, "
            "'UTC', 'video', 'General screen', :token, :token_expires_at, :key)"
        ),
        {
            "id": candidate_only["interview_appointments"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "starts_at": now + timedelta(days=3),
            "ends_at": now + timedelta(days=3, hours=1),
            "token": uuid4().hex * 2,
            "token_expires_at": now + timedelta(days=30),
            "key": uuid4().hex,
        },
    )
    await db.execute(
        text(
            "INSERT INTO source_candidate_receipts "
            "(id, organization_id, candidate_id, external_id, provider_snapshot, "
            " received_at) VALUES (:id, :organization_id, :candidate_id, "
            " :external_id, 'test', :now)"
        ),
        {
            "id": candidate_only["source_candidate_receipts"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "external_id": f"candidate-only-{uuid4()}",
            "now": now,
        },
    )
    await db.execute(
        text(
            "INSERT INTO task_submissions "
            "(id, organization_id, candidate_id, title, submission_type, status) "
            "VALUES (:id, :organization_id, :candidate_id, 'Candidate-only task', "
            " 'text', 'sent')"
        ),
        {
            "id": candidate_only["task_submissions"],
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
        },
    )

    workflow_insert = text(
        "INSERT INTO workflow_runs "
        "(id, organization_id, rule_id_snapshot, rule_version_snapshot, candidate_id, "
        " position_id, rule_name_snapshot, trigger_snapshot, conditions_snapshot, "
        " actions_snapshot, dedupe_key, status, available_at, started_at) "
        "VALUES (:id, :organization_id, :rule_id, 1, :candidate_id, :position_id, "
        " :name, 'manual', '{}'::jsonb, '[]'::jsonb, :key, 'queued', :now, :now)"
    )
    await db.execute(
        workflow_insert,
        {
            "id": candidate_only["workflow_runs"],
            "organization_id": ids["organization"],
            "rule_id": uuid4(),
            "candidate_id": ids["candidate"],
            "position_id": None,
            "name": "Candidate-only workflow",
            "key": f"candidate-only-{uuid4()}",
            "now": now,
        },
    )

    for category, candidate_id, position_id in (
        ("position_only", None, ids["position"]),
        ("global", None, None),
    ):
        await db.execute(
            text(
                "INSERT INTO chat_sessions "
                "(id, organization_id, user_id, scope, candidate_id, position_id) "
                "VALUES (:id, :organization_id, :user_id, 'free', "
                " :candidate_id, :position_id)"
            ),
            {
                "id": rows[category]["chat_sessions"],
                "organization_id": ids["organization"],
                "user_id": ids["user"],
                "candidate_id": candidate_id,
                "position_id": position_id,
            },
        )
        await db.execute(
            text(
                "INSERT INTO source_candidate_receipts "
                "(id, organization_id, candidate_id, position_id, external_id, "
                " provider_snapshot, received_at) VALUES (:id, :organization_id, "
                " :candidate_id, :position_id, :external_id, 'test', :now)"
            ),
            {
                "id": rows[category]["source_candidate_receipts"],
                "organization_id": ids["organization"],
                "candidate_id": candidate_id,
                "position_id": position_id,
                "external_id": f"{category}-{uuid4()}",
                "now": now,
            },
        )
        await db.execute(
            workflow_insert,
            {
                "id": rows[category]["workflow_runs"],
                "organization_id": ids["organization"],
                "rule_id": uuid4(),
                "candidate_id": candidate_id,
                "position_id": position_id,
                "name": f"{category} workflow",
                "key": f"{category}-{uuid4()}",
                "now": now,
            },
        )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()
    return rows


async def _expect_integrity_error(db, statement: str, parameters: dict) -> None:
    savepoint = await db.begin_nested()
    try:
        with pytest.raises(IntegrityError):
            await db.execute(text(statement), parameters)
            await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        if savepoint.is_active:
            await savepoint.rollback()
    await db.execute(text("SET CONSTRAINTS ALL DEFERRED"))


async def _assert_artifact_presence(db, artifacts, *, present: bool) -> None:
    expected = 1 if present else 0
    for table, artifact_id in artifacts.items():
        assert (
            await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE id = :id"),
                {"id": artifact_id},
            )
        ) == expected


async def test_migrated_catalog_has_cycle_edges_indexes_triggers_and_rls(db):
    revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "e18c4f6a0b32"

    columns = dict(
        (
            await db.execute(
                text(
                    "SELECT table_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'application_id' "
                    "AND table_name = ANY(:tables)"
                ),
                {"tables": list(APPLICATION_TABLES)},
            )
        ).all()
    )
    assert set(columns) == set(APPLICATION_TABLES)
    assert columns["interview_scheduling_links"] == "NO"
    assert all(
        columns[table] == "YES"
        for table in APPLICATION_TABLES
        if table != "interview_scheduling_links"
    )

    required_constraints = {
        "fk_candidate_activity_exact_application",
        "fk_candidate_processing_exact_application",
        "fk_chat_session_exact_application",
        "fk_communication_thread_exact_application",
        "fk_interview_recording_exact_application",
        "fk_scheduling_link_exact_application",
        "fk_appointment_exact_application",
        "fk_source_receipt_exact_application",
        "fk_task_submission_exact_application",
        "fk_workflow_run_exact_application",
        "fk_interview_exact_application",
        "fk_mission_assessment_exact_plan",
        "fk_mission_assessment_exact_agent_run",
        "fk_evidence_claim_exact_assessment",
        "fk_evidence_claim_exact_agent_run",
    }
    constraints = {
        row.name: row
        for row in (
            await db.execute(
                text(
                    "SELECT conname AS name, condeferrable, condeferred "
                    "FROM pg_catalog.pg_constraint "
                    "WHERE conname = ANY(:names)"
                ),
                {"names": list(required_constraints)},
            )
        ).all()
    }
    assert set(constraints) == required_constraints
    assert all(row.condeferrable and row.condeferred for row in constraints.values())

    tenant_indexes = set(
        (
            await db.execute(
                text(
                    "SELECT tablename FROM pg_catalog.pg_indexes "
                    "WHERE schemaname = 'public' "
                    "AND tablename = ANY(:tables) "
                    "AND indexname LIKE '%org_application%' "
                    "AND indexdef LIKE '%(organization_id, application_id%'"
                ),
                {"tables": list(APPLICATION_TABLES)},
            )
        ).scalars()
    )
    assert tenant_indexes == set(APPLICATION_TABLES)

    parent_foreign_keys = (
        await db.execute(
            text(PARENT_FK_CATALOG_SQL),
            {"tables": list(APPLICATION_TABLES)},
        )
    ).all()
    assert parent_foreign_keys
    assert all(row.confdeltype == "c" for row in parent_foreign_keys)

    active_score_index = await db.scalar(
        text(
            "SELECT indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'public' "
            "AND tablename = 'candidate_processing_jobs' "
            "AND indexname = 'uq_candidate_processing_active_score'"
        )
    )
    assert active_score_index is not None
    assert "UNIQUE INDEX" in active_score_index
    assert "(organization_id, application_id)" in active_score_index
    assert "kind" in active_score_index
    assert "score" in active_score_index
    assert "status" in active_score_index

    active_parse_index = await db.scalar(
        text(
            "SELECT indexdef FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'public' "
            "AND tablename = 'candidate_processing_jobs' "
            "AND indexname = 'uq_candidate_processing_active_parse'"
        )
    )
    assert active_parse_index is not None
    assert "(organization_id, candidate_id)" in active_parse_index

    trigger_rows = (
        await db.execute(
            text(
                "SELECT trigger.tgname, trigger.tgdeferrable, trigger.tginitdeferred "
                "FROM pg_catalog.pg_trigger AS trigger "
                "WHERE trigger.tgname = ANY(:names)"
            ),
            {
                "names": [
                    "trg_communication_message_thread_consistency",
                    "trg_communication_thread_message_consistency",
                    "trg_appointment_link_interview_consistency",
                    "trg_scheduling_link_appointment_consistency",
                ]
            },
        )
    ).all()
    assert len(trigger_rows) == 4
    assert all(row.tgdeferrable and row.tginitdeferred for row in trigger_rows)

    rls_rows = (
        await db.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_catalog.pg_class "
                "WHERE relname = ANY(:tables)"
            ),
            {"tables": list(APPLICATION_TABLES)},
        )
    ).all()
    assert len(rls_rows) == len(APPLICATION_TABLES)
    assert all(row.relrowsecurity and row.relforcerowsecurity for row in rls_rows)


async def test_migration_cancels_inactive_cycle_job_and_restores_indexes_safely(db):
    source_url = make_url(os.environ["TEST_DATABASE_URL"])
    database_name = f"cycle_index_{uuid4().hex}_test"
    admin_url = source_url.set(database="postgres")
    migration_url = source_url.set(database=database_name)
    migration_url_string = migration_url.render_as_string(hide_password=False)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    migration_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        await _run_alembic(
            migration_url_string, "upgrade", "c85f2a6d1e43"
        )
        organization_id = uuid4()
        candidate_id = uuid4()
        position_id = uuid4()
        old_application_id = uuid4()
        active_application_id = uuid4()
        stale_job_id = uuid4()
        old_applied_at = datetime(2025, 1, 1, tzinfo=UTC)
        stale_job_created_at = datetime(2025, 1, 15, tzinfo=UTC)
        stale_job_updated_at = datetime(2025, 1, 16, tzinfo=UTC)
        closed_at = datetime(2025, 2, 1, tzinfo=UTC)
        archived_at = datetime(2025, 2, 2, tzinfo=UTC)
        active_applied_at = datetime(2025, 3, 1, tzinfo=UTC)

        migration_engine = create_async_engine(migration_url)
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) "
                    "VALUES (:id, 'Cycle migration', :slug)"
                ),
                {"id": organization_id, "slug": f"cycle-{uuid4().hex}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO candidates "
                    "(id, organization_id, source, source_ref) "
                    "VALUES (:id, :organization_id, 'manual', 'cycle-migration')"
                ),
                {"id": candidate_id, "organization_id": organization_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO positions "
                    "(id, organization_id, title, status, sensitivity) "
                    "VALUES (:id, :organization_id, 'Cycle role', 'open', 'public')"
                ),
                {"id": position_id, "organization_id": organization_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO applications "
                    "(id, organization_id, candidate_id, position_id, stage, source, "
                    " cycle_number, applied_at, closed_at, archived_at) "
                    "VALUES (:id, :organization_id, :candidate_id, :position_id, "
                    " 'rejected', 'manual', 1, :applied_at, :closed_at, :archived_at)"
                ),
                {
                    "id": old_application_id,
                    "organization_id": organization_id,
                    "candidate_id": candidate_id,
                    "position_id": position_id,
                    "applied_at": old_applied_at,
                    "closed_at": closed_at,
                    "archived_at": archived_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO applications "
                    "(id, organization_id, candidate_id, position_id, stage, source, "
                    " cycle_number, applied_at) "
                    "VALUES (:id, :organization_id, :candidate_id, :position_id, "
                    " 'new', 'manual', 2, :applied_at)"
                ),
                {
                    "id": active_application_id,
                    "organization_id": organization_id,
                    "candidate_id": candidate_id,
                    "position_id": position_id,
                    "applied_at": active_applied_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO candidate_processing_jobs "
                    "(id, organization_id, candidate_id, position_id, kind, status, "
                    " idempotency_key, attempt_count, max_attempts, available_at, "
                    " leased_at, leased_by, lease_expires_at, created_at, updated_at) "
                    "VALUES (:id, :organization_id, :candidate_id, :position_id, "
                    " 'score', 'running', :key, 1, 3, :created_at, :updated_at, "
                    " 'legacy-worker', :lease_expires_at, :created_at, :updated_at)"
                ),
                {
                    "id": stale_job_id,
                    "organization_id": organization_id,
                    "candidate_id": candidate_id,
                    "position_id": position_id,
                    "key": f"stale-{stale_job_id}",
                    "created_at": stale_job_created_at,
                    "updated_at": stale_job_updated_at,
                    "lease_expires_at": datetime(2025, 4, 1, tzinfo=UTC),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO worker_queue_leases "
                    "(queue_name, work_id, organization_id, lease_token, worker_id, "
                    " leased_at, lease_expires_at) VALUES "
                    "('candidate_processing_jobs', :work_id, :organization_id, "
                    " :lease_token, 'legacy-worker', :leased_at, :lease_expires_at)"
                ),
                {
                    "work_id": stale_job_id,
                    "organization_id": organization_id,
                    "lease_token": uuid4(),
                    "leased_at": stale_job_updated_at,
                    "lease_expires_at": datetime(2025, 4, 1, tzinfo=UTC),
                },
            )
            legacy_parent_fks = {
                row.conname: (row.definition, row.confdeltype)
                for row in (
                    await connection.execute(
                        text(PARENT_FK_CATALOG_SQL),
                        {"tables": list(APPLICATION_TABLES)},
                    )
                ).all()
            }
            legacy_materializer = await connection.scalar(
                text(
                    "SELECT pg_get_functiondef("
                    "'security.materialize_workflow_activity()'::regprocedure)"
                )
            )
        await migration_engine.dispose()
        migration_engine = None

        await _run_alembic(
            migration_url_string, "upgrade", "d96a4c8e2f70"
        )
        migration_engine = create_async_engine(migration_url)
        async with migration_engine.connect() as connection:
            stale_job = (
                await connection.execute(
                    text(
                        "SELECT application_id, status, cancelled_at, completed_at, "
                        "leased_at, leased_by, lease_expires_at, failure_code, "
                        "failure_detail, result_summary FROM candidate_processing_jobs "
                        "WHERE id = :id"
                    ),
                    {"id": stale_job_id},
                )
            ).one()
            assert stale_job.application_id == old_application_id
            assert stale_job.status == "cancelled"
            assert stale_job.cancelled_at == archived_at
            assert stale_job.completed_at == archived_at
            assert stale_job.leased_at is None
            assert stale_job.leased_by is None
            assert stale_job.lease_expires_at is None
            assert stale_job.failure_code == "application_cycle_inactive"
            assert len(stale_job.failure_detail) <= 160
            assert stale_job.result_summary == {
                "status": "cancelled",
                "reason": "application_cycle_inactive",
                "application_id": str(old_application_id),
            }
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM worker_queue_leases "
                        "WHERE queue_name = 'candidate_processing_jobs' "
                        "AND work_id = :id"
                    ),
                    {"id": stale_job_id},
                )
            ) == 0
            cycle_index = await connection.scalar(
                text(
                    "SELECT indexdef FROM pg_catalog.pg_indexes "
                    "WHERE indexname = 'uq_candidate_processing_active_score'"
                )
            )
            assert "(organization_id, application_id)" in cycle_index
            upgraded_materializer = await connection.scalar(
                text(
                    "SELECT pg_get_functiondef("
                    "'security.materialize_workflow_activity()'::regprocedure)"
                )
            )
            assert upgraded_materializer != legacy_materializer
            assert "application_id" in upgraded_materializer
            assert "NEW.application_id" in upgraded_materializer

        active_job_id = uuid4()
        active_created_at = datetime(2025, 3, 2, tzinfo=UTC)
        score_insert = text(
            "INSERT INTO candidate_processing_jobs "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " kind, status, idempotency_key, available_at, created_at, updated_at) "
            "VALUES (:id, :organization_id, :candidate_id, :position_id, "
            " :application_id, 'score', 'queued', :key, :created_at, "
            " :created_at, :created_at)"
        )
        async with migration_engine.begin() as connection:
            await connection.execute(
                score_insert,
                {
                    "id": active_job_id,
                    "organization_id": organization_id,
                    "candidate_id": candidate_id,
                    "position_id": position_id,
                    "application_id": active_application_id,
                    "key": f"active-{active_job_id}",
                    "created_at": active_created_at,
                },
            )
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        async with migration_engine.connect() as connection:
            transaction = await connection.begin()
            try:
                with pytest.raises(IntegrityError):
                    await connection.execute(
                        score_insert,
                        {
                            "id": uuid4(),
                            "organization_id": organization_id,
                            "candidate_id": candidate_id,
                            "position_id": position_id,
                            "application_id": active_application_id,
                            "key": f"duplicate-{uuid4()}",
                            "created_at": active_created_at,
                        },
                    )
            finally:
                await transaction.rollback()

        downgrade_stale_id = uuid4()
        downgrade_stale_created_at = datetime(2025, 1, 20, tzinfo=UTC)
        async with migration_engine.begin() as connection:
            await connection.execute(
                score_insert,
                {
                    "id": downgrade_stale_id,
                    "organization_id": organization_id,
                    "candidate_id": candidate_id,
                    "position_id": position_id,
                    "application_id": old_application_id,
                    "key": f"downgrade-{downgrade_stale_id}",
                    "created_at": downgrade_stale_created_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO worker_queue_leases "
                    "(queue_name, work_id, organization_id, lease_token, worker_id, "
                    " leased_at, lease_expires_at) VALUES "
                    "('candidate_processing_jobs', :work_id, :organization_id, "
                    " :lease_token, 'downgrade-worker', :leased_at, :lease_expires_at)"
                ),
                {
                    "work_id": downgrade_stale_id,
                    "organization_id": organization_id,
                    "lease_token": uuid4(),
                    "leased_at": downgrade_stale_created_at,
                    "lease_expires_at": datetime(2025, 4, 1, tzinfo=UTC),
                },
            )
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

        await migration_engine.dispose()
        migration_engine = None
        await _run_alembic(
            migration_url_string, "downgrade", "c85f2a6d1e43"
        )
        migration_engine = create_async_engine(migration_url)
        async with migration_engine.connect() as connection:
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'candidate_processing_jobs' "
                    "AND column_name = 'application_id')"
                )
            )
            legacy_index = await connection.scalar(
                text(
                    "SELECT indexdef FROM pg_catalog.pg_indexes "
                    "WHERE indexname = 'uq_candidate_processing_active_score'"
                )
            )
            assert "(organization_id, candidate_id, position_id)" in legacy_index
            restored_parent_fks = {
                row.conname: (row.definition, row.confdeltype)
                for row in (
                    await connection.execute(
                        text(PARENT_FK_CATALOG_SQL),
                        {"tables": list(APPLICATION_TABLES)},
                    )
                ).all()
            }
            assert restored_parent_fks == legacy_parent_fks
            restored_materializer = await connection.scalar(
                text(
                    "SELECT pg_get_functiondef("
                    "'security.materialize_workflow_activity()'::regprocedure)"
                )
            )
            assert restored_materializer == legacy_materializer
            statuses = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT id, status FROM candidate_processing_jobs "
                            "WHERE id = ANY(:ids)"
                        ),
                        {"ids": [active_job_id, downgrade_stale_id]},
                    )
                ).all()
            )
            assert statuses == {
                active_job_id: "queued",
                downgrade_stale_id: "cancelled",
            }
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM worker_queue_leases "
                        "WHERE queue_name = 'candidate_processing_jobs' "
                        "AND work_id = :id"
                    ),
                    {"id": downgrade_stale_id},
                )
            ) == 0
            post_downgrade_state = (
                await connection.execute(
                    text(
                        "SELECT id, status, cancelled_at, completed_at, leased_at, "
                        "leased_by, lease_expires_at, failure_code, failure_detail, "
                        "result_summary, created_at, updated_at "
                        "FROM candidate_processing_jobs ORDER BY id"
                    )
                )
            ).all()

        await migration_engine.dispose()
        migration_engine = None
        await _run_alembic(
            migration_url_string, "upgrade", "d96a4c8e2f70"
        )
        migration_engine = create_async_engine(migration_url)
        async with migration_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "d96a4c8e2f70"
            cycle_index = await connection.scalar(
                text(
                    "SELECT indexdef FROM pg_catalog.pg_indexes "
                    "WHERE indexname = 'uq_candidate_processing_active_score'"
                )
            )
            assert "(organization_id, application_id)" in cycle_index
            upgraded_parent_fks = (
                await connection.execute(
                    text(PARENT_FK_CATALOG_SQL),
                    {"tables": list(APPLICATION_TABLES)},
                )
            ).all()
            assert upgraded_parent_fks
            assert all(row.confdeltype == "c" for row in upgraded_parent_fks)
            reupgraded_materializer = await connection.scalar(
                text(
                    "SELECT pg_get_functiondef("
                    "'security.materialize_workflow_activity()'::regprocedure)"
                )
            )
            assert reupgraded_materializer == upgraded_materializer
            assert (
                await connection.scalar(
                    text(
                        "SELECT application_id FROM candidate_processing_jobs "
                        "WHERE id = :id"
                    ),
                    {"id": active_job_id},
                )
            ) == active_application_id
            post_reupgrade_state = (
                await connection.execute(
                    text(
                        "SELECT id, status, cancelled_at, completed_at, leased_at, "
                        "leased_by, lease_expires_at, failure_code, failure_detail, "
                        "result_summary, created_at, updated_at "
                        "FROM candidate_processing_jobs ORDER BY id"
                    )
                )
            ).all()
            assert post_reupgrade_state == post_downgrade_state
    finally:
        if migration_engine is not None:
            await migration_engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()


async def test_application_pair_rows_fail_closed_and_global_rows_remain_valid(db):
    ids = await _seed_application_pairs(db)
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " event_type) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, 'test_exact_cycle')"
        ),
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["application"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, event_type) "
            "VALUES (:id, :organization_id, :candidate_id, 'test_global')"
        ),
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
        },
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _expect_integrity_error(
        db,
        "INSERT INTO candidate_activities "
        "(id, organization_id, candidate_id, position_id, application_id, event_type) "
        "VALUES (:id, :organization_id, :candidate_id, :position_id, "
        ":application_id, 'test_cross_pair')",
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["other_candidate_application"],
        },
    )
    await _expect_integrity_error(
        db,
        "INSERT INTO candidate_activities "
        "(id, organization_id, candidate_id, position_id, event_type) "
        "VALUES (:id, :organization_id, :candidate_id, :position_id, 'test_missing')",
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
        },
    )


async def test_workflow_materializer_preserves_activity_application_cycle(db):
    ids = await _seed_application_pairs(db)
    rule_id = uuid4()
    exact_activity_id = uuid4()
    global_activity_id = uuid4()
    forged_activity_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO workflow_rules "
            "(id, organization_id, created_by_user_id, name, trigger, conditions, "
            " actions, version, is_active, starts_at) VALUES (:id, "
            " :organization_id, :user_id, 'Application-cycle materializer', "
            " 'candidate_added', '{}'::jsonb, '[]'::jsonb, 1, true, :starts_at)"
        ),
        {
            "id": rule_id,
            "organization_id": ids["organization"],
            "user_id": ids["user"],
            "starts_at": datetime.now(UTC) - timedelta(days=2),
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " event_type) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, 'candidate_added')"
        ),
        {
            "id": exact_activity_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["application"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, event_type) "
            "VALUES (:id, :organization_id, :candidate_id, 'candidate_added')"
        ),
        {
            "id": global_activity_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
        },
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    materialized = {
        row.source_activity_id: row
        for row in (
            await db.execute(
                text(
                    "SELECT source_activity_id, candidate_id, position_id, "
                    "application_id FROM workflow_runs "
                    "WHERE rule_id = :rule_id ORDER BY source_activity_id"
                ),
                {"rule_id": rule_id},
            )
        ).all()
    }
    assert set(materialized) == {exact_activity_id, global_activity_id}
    assert materialized[exact_activity_id].candidate_id == ids["candidate"]
    assert materialized[exact_activity_id].position_id == ids["position"]
    assert materialized[exact_activity_id].application_id == ids["application"]
    assert materialized[global_activity_id].candidate_id == ids["candidate"]
    assert materialized[global_activity_id].position_id is None
    assert materialized[global_activity_id].application_id is None

    await _expect_integrity_error(
        db,
        "INSERT INTO candidate_activities "
        "(id, organization_id, candidate_id, position_id, application_id, event_type) "
        "VALUES (:id, :organization_id, :candidate_id, :position_id, "
        ":application_id, 'candidate_added')",
        {
            "id": forged_activity_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["other_candidate_application"],
        },
    )
    assert (
        await db.scalar(
            text(
                "SELECT count(*) FROM workflow_runs "
                "WHERE source_activity_id = :activity_id"
            ),
            {"activity_id": forged_activity_id},
        )
    ) == 0


async def test_direct_application_delete_cascades_all_cycle_artifacts(db):
    ids = await _seed_application_pairs(db)
    application_artifacts = await _seed_application_artifacts(db, ids)
    non_application = await _seed_non_application_artifacts(db, ids)

    await db.execute(
        text("DELETE FROM applications WHERE id = :id"),
        {"id": ids["application"]},
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _assert_artifact_presence(db, application_artifacts, present=False)
    for artifacts in non_application.values():
        await _assert_artifact_presence(db, artifacts, present=True)
    assert (
        await db.scalar(
            text("SELECT count(*) FROM candidates WHERE id = :id"),
            {"id": ids["candidate"]},
        )
    ) == 1

    assert (
        await db.scalar(
            text("SELECT count(*) FROM positions WHERE id = :id"),
            {"id": ids["position"]},
        )
    ) == 1


async def test_candidate_erasure_cascades_all_owned_artifacts_and_preserves_global(db):
    ids = await _seed_application_pairs(db)
    application_artifacts = await _seed_application_artifacts(db, ids)
    non_application = await _seed_non_application_artifacts(db, ids)

    await db.execute(
        text("DELETE FROM candidates WHERE id = :id"),
        {"id": ids["candidate"]},
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _assert_artifact_presence(db, application_artifacts, present=False)
    await _assert_artifact_presence(
        db, non_application["candidate_only"], present=False
    )
    await _assert_artifact_presence(
        db, non_application["position_only"], present=True
    )
    await _assert_artifact_presence(db, non_application["global"], present=True)
    assert (
        await db.scalar(
            text("SELECT count(*) FROM positions WHERE id = :id"),
            {"id": ids["position"]},
        )
    ) == 1

async def test_position_erasure_cascades_all_owned_artifacts_and_preserves_global(db):
    ids = await _seed_application_pairs(db)
    application_artifacts = await _seed_application_artifacts(db, ids)
    non_application = await _seed_non_application_artifacts(db, ids)

    await db.execute(
        text("DELETE FROM positions WHERE id = :id"),
        {"id": ids["position"]},
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _assert_artifact_presence(db, application_artifacts, present=False)
    await _assert_artifact_presence(
        db, non_application["candidate_only"], present=True
    )
    await _assert_artifact_presence(
        db, non_application["position_only"], present=False
    )
    await _assert_artifact_presence(db, non_application["global"], present=True)
    assert (
        await db.scalar(
            text("SELECT count(*) FROM candidates WHERE id = :id"),
            {"id": ids["candidate"]},
        )
    ) == 1


async def test_deferred_application_edge_allows_atomic_merge_repoint(db):
    ids = await _seed_application_pairs(db)
    activity_id = uuid4()
    merged_candidate_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO candidate_activities "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " event_type) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, 'merge_repoint')"
        ),
        {
            "id": activity_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["application"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidates (id, organization_id, source, source_ref) "
            "VALUES (:id, :organization_id, 'manual', 'merged-candidate')"
        ),
        {
            "id": merged_candidate_id,
            "organization_id": ids["organization"],
        },
    )

    # Merge services repoint dependents before the authoritative Application row.
    # The deferred exact edge validates the final transaction state, not write order.
    await db.execute(
        text(
            "UPDATE candidate_activities SET candidate_id = :candidate_id "
            "WHERE id = :id"
        ),
        {"candidate_id": merged_candidate_id, "id": activity_id},
    )
    await db.execute(
        text(
            "UPDATE applications SET candidate_id = :candidate_id "
            "WHERE id = :id"
        ),
        {"candidate_id": merged_candidate_id, "id": ids["application"]},
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    assert (
        await db.scalar(
            text(
                "SELECT count(*) FROM candidate_activities AS activity "
                "JOIN applications AS application "
                "ON application.organization_id = activity.organization_id "
                "AND application.id = activity.application_id "
                "AND application.candidate_id = activity.candidate_id "
                "AND application.position_id = activity.position_id "
                "WHERE activity.id = :id"
            ),
            {"id": activity_id},
        )
    ) == 1

    # Reversal exercises the opposite write order and proves that erasing the
    # abandoned merge target cannot cascade the restored application cycle.
    await db.execute(
        text(
            "UPDATE applications SET candidate_id = :candidate_id "
            "WHERE id = :id"
        ),
        {"candidate_id": ids["candidate"], "id": ids["application"]},
    )
    await db.execute(
        text(
            "UPDATE candidate_activities SET candidate_id = :candidate_id "
            "WHERE id = :id"
        ),
        {"candidate_id": ids["candidate"], "id": activity_id},
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()
    await db.execute(
        text("DELETE FROM candidates WHERE id = :id"),
        {"id": merged_candidate_id},
    )
    await db.commit()
    assert (
        await db.scalar(
            text(
                "SELECT count(*) FROM candidate_activities "
                "WHERE id = :id AND candidate_id = :candidate_id"
            ),
            {"id": activity_id, "candidate_id": ids["candidate"]},
        )
    ) == 1


async def test_forced_rls_runtime_cannot_link_to_another_tenant_cycle(db):
    tenant = await _seed_application_pairs(db)
    other_tenant = await _seed_application_pairs(db)
    runtime_engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"server_settings": {"role": "hr_agent_runtime"}},
    )
    marker = uuid4()
    try:
        with pytest.raises(IntegrityError):
            async with runtime_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.organization_id', :value, true)"),
                    {"value": str(tenant["organization"])},
                )
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM applications")
                    )
                ) == 3
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM applications WHERE id = :id"),
                        {"id": other_tenant["application"]},
                    )
                ) == 0
                await connection.execute(
                    text(
                        "INSERT INTO candidate_activities "
                        "(id, organization_id, candidate_id, position_id, "
                        " application_id, event_type) VALUES (:id, :organization_id, "
                        " :candidate_id, :position_id, :application_id, 'rls_cross_cycle')"
                    ),
                    {
                        "id": marker,
                        "organization_id": tenant["organization"],
                        "candidate_id": tenant["candidate"],
                        "position_id": tenant["position"],
                        "application_id": other_tenant["application"],
                    },
                )
                await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    finally:
        await runtime_engine.dispose()

    assert (
        await db.scalar(
            text("SELECT count(*) FROM candidate_activities WHERE id = :id"),
            {"id": marker},
        )
    ) == 0
async def test_communication_and_scheduling_validate_the_final_transaction_state(db):
    ids = await _seed_application_pairs(db)
    contact_one, contact_two, thread_id, message_id = (uuid4() for _ in range(4))
    for contact_id, value in ((contact_one, "one@example.test"), (contact_two, "two@example.test")):
        await db.execute(
            text(
                "INSERT INTO candidate_contact_points "
                "(id, organization_id, candidate_id, channel, value, normalized_value) "
                "VALUES (:id, :organization_id, :candidate_id, 'email', :value, :value)"
            ),
            {
                "id": contact_id,
                "organization_id": ids["organization"],
                "candidate_id": ids["candidate"],
                "value": value,
            },
        )
    await db.execute(
        text(
            "INSERT INTO communication_threads "
            "(id, organization_id, candidate_id, contact_point_id, position_id, "
            " application_id, channel) VALUES (:id, :organization_id, :candidate_id, "
            " :contact, :position_id, :application_id, 'email')"
        ),
        {
            "id": thread_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "contact": contact_one,
            "position_id": ids["position"],
            "application_id": ids["application"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO communication_messages "
            "(id, organization_id, thread_id, candidate_id, contact_point_id, "
            " direction, channel, status, body) VALUES (:id, :organization_id, "
            " :thread_id, :candidate_id, :contact, 'outbound', 'email', 'queued', 'ok')"
        ),
        {
            "id": message_id,
            "organization_id": ids["organization"],
            "thread_id": thread_id,
            "candidate_id": ids["candidate"],
            "contact": contact_one,
        },
    )
    # Child-first repointing is allowed because only the final state is checked.
    await db.execute(
        text("UPDATE communication_messages SET contact_point_id = :contact WHERE id = :id"),
        {"contact": contact_two, "id": message_id},
    )
    await db.execute(
        text("UPDATE communication_threads SET contact_point_id = :contact WHERE id = :id"),
        {"contact": contact_two, "id": thread_id},
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _expect_integrity_error(
        db,
        "INSERT INTO communication_messages "
        "(id, organization_id, thread_id, candidate_id, contact_point_id, "
        " direction, channel, status, body) VALUES (:id, :organization_id, "
        " :thread_id, :candidate_id, :contact, 'outbound', 'email', 'queued', 'bad')",
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "thread_id": thread_id,
            "candidate_id": ids["candidate"],
            "contact": contact_one,
        },
    )

    interview_id, link_id = uuid4(), uuid4()
    await db.execute(
        text(
            "INSERT INTO interviews "
            "(id, organization_id, candidate_id, position_id, application_id, status) "
            "VALUES (:id, :organization_id, :candidate_id, :position_id, "
            " :application_id, 'draft')"
        ),
        {
            "id": interview_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["application"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO interview_scheduling_links "
            "(id, organization_id, candidate_id, position_id, application_id, "
            " interview_id, token_digest, duration_minutes, slot_interval_minutes, "
            " min_notice_minutes, booking_window_days, buffer_minutes, location_mode, "
            " reminder_offsets, expires_at) VALUES (:id, :organization_id, :candidate_id, "
            " :position_id, :application_id, :interview_id, :token, 60, 30, 120, 14, "
            " 0, 'video', '[1440,60]'::jsonb, :expires_at)"
        ),
        {
            "id": link_id,
            "organization_id": ids["organization"],
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["application"],
            "interview_id": interview_id,
            "token": uuid4().hex * 2,
            "expires_at": datetime.now(UTC) + timedelta(days=7),
        },
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _expect_integrity_error(
        db,
        "INSERT INTO interview_appointments "
        "(id, organization_id, scheduling_link_id, candidate_id, position_id, "
        " application_id, interview_id, starts_at, ends_at, candidate_timezone, "
        " location_mode, position_title_snapshot, manage_token_digest, "
        " manage_token_expires_at, idempotency_key) VALUES (:id, :organization_id, "
        " :link_id, :candidate_id, :position_id, :application_id, NULL, :starts_at, "
        " :ends_at, 'UTC', 'video', 'Role', :token, :token_expires, :key)",
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "link_id": link_id,
            "candidate_id": ids["candidate"],
            "position_id": ids["position"],
            "application_id": ids["application"],
            "starts_at": datetime.now(UTC) + timedelta(days=2),
            "ends_at": datetime.now(UTC) + timedelta(days=2, hours=1),
            "token": uuid4().hex * 2,
            "token_expires": datetime.now(UTC) + timedelta(days=30),
            "key": uuid4().hex,
        },
    )


async def test_mission_evidence_rejects_cross_mission_plan_and_agent_run(db):
    ids = await _seed_application_pairs(db)
    mission_ids = [uuid4(), uuid4()]
    plan_ids = [uuid4(), uuid4()]
    run_ids = [uuid4(), uuid4()]
    for ordinal in range(2):
        session_id = uuid4()
        await db.execute(
            text(
                "INSERT INTO ai_sessions (id, organization_id) "
                "VALUES (:id, :organization_id)"
            ),
            {"id": session_id, "organization_id": ids["organization"]},
        )
        await db.execute(
            text(
                "INSERT INTO hiring_missions "
                "(id, organization_id, session_id, title, original_request, correlation_id) "
                "VALUES (:id, :organization_id, :session_id, :title, 'request', :correlation)"
            ),
            {
                "id": mission_ids[ordinal],
                "organization_id": ids["organization"],
                "session_id": session_id,
                "title": f"mission-{ordinal}",
                "correlation": uuid4(),
            },
        )
        await db.execute(
            text(
                "INSERT INTO mission_plan_versions "
                "(id, organization_id, mission_id, version, input_hash) "
                "VALUES (:id, :organization_id, :mission_id, 1, :input_hash)"
            ),
            {
                "id": plan_ids[ordinal],
                "organization_id": ids["organization"],
                "mission_id": mission_ids[ordinal],
                "input_hash": str(ordinal) * 64,
            },
        )
        await db.execute(
            text(
                "INSERT INTO ai_agent_runs "
                "(id, organization_id, mission_id, agent_type, input_hash, correlation_id) "
                "VALUES (:id, :organization_id, :mission_id, 'assessment', "
                " :input_hash, :correlation)"
            ),
            {
                "id": run_ids[ordinal],
                "organization_id": ids["organization"],
                "mission_id": mission_ids[ordinal],
                "input_hash": str(ordinal + 2) * 64,
                "correlation": uuid4(),
            },
        )
    assessment_id = uuid4()
    await db.execute(
        text(
            "INSERT INTO mission_candidate_assessments "
            "(id, organization_id, mission_id, plan_version_id, plan_version, "
            " candidate_id, agent_run_id, assessment_version, overall_score, "
            " confidence, recommendation) VALUES (:id, :organization_id, :mission_id, "
            " :plan_id, 1, :candidate_id, :run_id, 1, 80, 'high', 'fit')"
        ),
        {
            "id": assessment_id,
            "organization_id": ids["organization"],
            "mission_id": mission_ids[0],
            "plan_id": plan_ids[0],
            "candidate_id": ids["candidate"],
            "run_id": run_ids[0],
        },
    )
    await db.execute(
        text(
            "INSERT INTO evidence_claims "
            "(id, organization_id, mission_id, plan_version_id, assessment_id, "
            " candidate_id, agent_run_id, criterion_key, claim, source_type, stance, "
            " confidence, content_hash) VALUES (:id, :organization_id, :mission_id, "
            " :plan_id, :assessment_id, :candidate_id, :run_id, 'criterion', 'claim', "
            " 'candidate', 'supports', 0.9, :content_hash)"
        ),
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "mission_id": mission_ids[0],
            "plan_id": plan_ids[0],
            "assessment_id": assessment_id,
            "candidate_id": ids["candidate"],
            "run_id": run_ids[0],
            "content_hash": "a" * 64,
        },
    )
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.commit()

    await _expect_integrity_error(
        db,
        "INSERT INTO mission_candidate_assessments "
        "(id, organization_id, mission_id, plan_version_id, plan_version, "
        " candidate_id, agent_run_id, assessment_version, overall_score, confidence, "
        " recommendation) VALUES (:id, :organization_id, :mission_id, :plan_id, 1, "
        " :candidate_id, :run_id, 2, 70, 'medium', 'possible_fit')",
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "mission_id": mission_ids[0],
            "plan_id": plan_ids[0],
            "candidate_id": ids["candidate"],
            "run_id": run_ids[1],
        },
    )
    await _expect_integrity_error(
        db,
        "INSERT INTO evidence_claims "
        "(id, organization_id, mission_id, plan_version_id, assessment_id, "
        " candidate_id, agent_run_id, criterion_key, claim, source_type, stance, "
        " confidence, content_hash) VALUES (:id, :organization_id, :mission_id, "
        " :plan_id, :assessment_id, :candidate_id, :run_id, 'other', 'bad', "
        " 'candidate', 'supports', 0.8, :content_hash)",
        {
            "id": uuid4(),
            "organization_id": ids["organization"],
            "mission_id": mission_ids[0],
            "plan_id": plan_ids[0],
            "assessment_id": assessment_id,
            "candidate_id": ids["candidate"],
            "run_id": run_ids[1],
            "content_hash": "b" * 64,
        },
    )
