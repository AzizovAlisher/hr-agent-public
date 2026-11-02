"""PostgreSQL-only release gates for the migrated forced-RLS boundary.

These tests intentionally run only in the dedicated migrated-database job.  The
ordinary suite builds tables from SQLAlchemy metadata and uses RLS-off mode so
unit and API fixtures can be populated without broker bootstrap calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.auth.jwt import encode_access_token
from app.db import SessionLocal
from app.main import app
from app.models.enterprise import AuditLog
from app.models.operations import WorkerQueueLease
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.user import User
from app.storage import private_object_key

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("TENANT_RLS_MODE") != "forced",
        reason="Run against an Alembic-migrated database with TENANT_RLS_MODE=forced",
    ),
]


def _install_runtime_app_sessions(monkeypatch):
    """Run request sessions as the production non-bypass runtime role."""

    runtime_engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"role": "hr_agent_runtime"}},
    )
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", runtime_sessions)
    return runtime_engine


async def _seed_audit_tenants(db):
    marker = uuid4().hex
    organization = Organization(
        name="Audit RLS A", slug=f"audit-rls-a-{marker}"
    )
    other_organization = Organization(
        name="Audit RLS B", slug=f"audit-rls-b-{marker}"
    )
    db.add_all([organization, other_organization])
    await db.flush()
    actor = User(
        organization_id=organization.id,
        email=f"audit-actor-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    other_actor = User(
        organization_id=other_organization.id,
        email=f"audit-other-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add_all([actor, other_actor])
    await db.commit()
    return organization, actor, other_organization, other_actor


async def test_authenticated_mutation_audit_bootstraps_and_binds_runtime_tenant(
    db, monkeypatch
):
    organization, actor, other_organization, other_actor = await _seed_audit_tenants(
        db
    )

    runtime_engine = _install_runtime_app_sessions(monkeypatch)
    try:
        # This is the middleware's pre-fix query shape: an unbound runtime
        # session cannot see even the legitimate actor once RLS is forced.
        async with db_module.SessionLocal() as runtime:
            assert (
                await runtime.execute(select(User.id).where(User.id == actor.id))
            ).scalar_one_or_none() is None

        token = encode_access_token(actor.id, actor.role, actor.locale)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.patch(
                "/organizations/me",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "forced-rls-audit-test",
                },
                json={"name": "Audit RLS A renamed"},
            )
    finally:
        await runtime_engine.dispose()

    assert response.status_code == 200
    audit = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "update_my_org")
        )
    ).scalar_one()
    assert audit.organization_id == organization.id
    assert audit.actor_user_id == actor.id
    assert audit.actor_email_snapshot == actor.email
    assert audit.metadata_json == {"query_keys": []}
    assert audit.organization_id != other_organization.id
    assert audit.actor_user_id != other_actor.id


async def test_sensitive_read_audit_persists_in_bound_runtime_request(
    db, monkeypatch
):
    organization, actor, other_organization, _other_actor = await _seed_audit_tenants(
        db
    )
    runtime_engine = _install_runtime_app_sessions(monkeypatch)
    try:
        token = encode_access_token(actor.id, actor.role, actor.locale)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/enterprise/backup",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        await runtime_engine.dispose()

    assert response.status_code == 200
    audit = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "export_core_data")
        )
    ).scalar_one()
    assert audit.organization_id == organization.id
    assert audit.organization_id != other_organization.id
    assert audit.actor_user_id == actor.id
    assert audit.metadata_json["sensitive_read"] is True


async def test_every_tenant_relation_is_forced_and_relationships_are_tenant_bound(db):
    protected = list(
        (
            await db.execute(
                text(
                    """
                    SELECT relation.relname AS table_name,
                           relation.relrowsecurity AS rls_enabled,
                           relation.relforcerowsecurity AS rls_forced,
                           owner.rolname AS owner,
                           EXISTS (
                             SELECT 1 FROM pg_catalog.pg_policy AS policy
                              WHERE policy.polrelid = relation.oid
                           ) AS has_policy
                      FROM pg_catalog.pg_class AS relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      JOIN pg_catalog.pg_roles AS owner
                        ON owner.oid = relation.relowner
                     WHERE namespace.nspname = 'public'
                       AND relation.relkind IN ('r', 'p')
                       AND (
                         relation.relname = 'organizations'
                         OR EXISTS (
                           SELECT 1 FROM pg_catalog.pg_attribute AS attribute
                            WHERE attribute.attrelid = relation.oid
                              AND attribute.attname = 'organization_id'
                              AND NOT attribute.attisdropped
                         )
                       )
                     ORDER BY relation.relname
                    """
                )
            )
        ).mappings()
    )
    assert protected
    assert all(row["rls_enabled"] and row["rls_forced"] for row in protected)
    assert all(row["has_policy"] for row in protected)
    assert all(
        row["owner"] not in {"hr_agent_runtime", "hr_agent_worker"} for row in protected
    )

    runtime_role = (
        await db.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcanlogin "
                "FROM pg_catalog.pg_roles WHERE rolname = 'hr_agent_runtime'"
            )
        )
    ).one()
    assert tuple(runtime_role) == (False, False, False)

    unbound_relationships = list(
        (
            await db.execute(
                text(
                    """
                    SELECT child.relname AS child_table,
                           child_column.attname AS child_column,
                           parent.relname AS parent_table
                      FROM pg_catalog.pg_constraint AS original
                      JOIN pg_catalog.pg_class AS child
                        ON child.oid = original.conrelid
                      JOIN pg_catalog.pg_namespace AS child_namespace
                        ON child_namespace.oid = child.relnamespace
                      JOIN pg_catalog.pg_class AS parent
                        ON parent.oid = original.confrelid
                      JOIN pg_catalog.pg_namespace AS parent_namespace
                        ON parent_namespace.oid = parent.relnamespace
                      JOIN pg_catalog.pg_attribute AS child_column
                        ON child_column.attrelid = child.oid
                       AND child_column.attnum = original.conkey[1]
                      JOIN pg_catalog.pg_attribute AS parent_column
                        ON parent_column.attrelid = parent.oid
                       AND parent_column.attnum = original.confkey[1]
                     WHERE original.contype = 'f'
                       AND child_namespace.nspname = 'public'
                       AND parent_namespace.nspname = 'public'
                       AND pg_catalog.cardinality(original.conkey) = 1
                       AND child_column.attname <> 'organization_id'
                       AND parent_column.attname = 'id'
                       AND EXISTS (
                         SELECT 1 FROM pg_catalog.pg_attribute AS attribute
                          WHERE attribute.attrelid = child.oid
                            AND attribute.attname = 'organization_id'
                            AND NOT attribute.attisdropped
                       )
                       AND EXISTS (
                         SELECT 1 FROM pg_catalog.pg_attribute AS attribute
                          WHERE attribute.attrelid = parent.oid
                            AND attribute.attname = 'organization_id'
                            AND NOT attribute.attisdropped
                       )
                       AND NOT EXISTS (
                         SELECT 1
                           FROM pg_catalog.pg_constraint AS tenant_fk
                          WHERE tenant_fk.contype = 'f'
                            AND tenant_fk.conrelid = child.oid
                            AND tenant_fk.confrelid = parent.oid
                            AND (
                              SELECT pg_catalog.array_agg(attribute.attname ORDER BY key.ord)
                                FROM pg_catalog.unnest(tenant_fk.conkey)
                                  WITH ORDINALITY AS key(attnum, ord)
                                JOIN pg_catalog.pg_attribute AS attribute
                                  ON attribute.attrelid = tenant_fk.conrelid
                                 AND attribute.attnum = key.attnum
                            ) = ARRAY['organization_id', child_column.attname]::name[]
                            AND (
                              SELECT pg_catalog.array_agg(attribute.attname ORDER BY key.ord)
                                FROM pg_catalog.unnest(tenant_fk.confkey)
                                  WITH ORDINALITY AS key(attnum, ord)
                                JOIN pg_catalog.pg_attribute AS attribute
                                  ON attribute.attrelid = tenant_fk.confrelid
                                 AND attribute.attnum = key.attnum
                            ) = ARRAY['organization_id', 'id']::name[]
                       )
                     ORDER BY child.relname, child_column.attname
                    """
                )
            )
        ).mappings()
    )
    assert unbound_relationships == []


async def test_runtime_role_fails_closed_and_broker_claims_only_active_identity(db):
    organization_a = uuid4()
    organization_b = uuid4()
    candidate_a = uuid4()
    candidate_b = uuid4()
    active_user = uuid4()
    inactive_user = uuid4()
    marker = uuid4().hex

    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug) VALUES "
            "(:a, 'RLS A', :slug_a), (:b, 'RLS B', :slug_b)"
        ),
        {
            "a": organization_a,
            "b": organization_b,
            "slug_a": f"rls-a-{marker}",
            "slug_b": f"rls-b-{marker}",
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidates (id, organization_id, source, raw_cv) VALUES "
            "(:candidate_a, :organization_a, 'manual', 'tenant-a-canary'), "
            "(:candidate_b, :organization_b, 'manual', 'tenant-b-canary')"
        ),
        {
            "candidate_a": candidate_a,
            "organization_a": organization_a,
            "candidate_b": candidate_b,
            "organization_b": organization_b,
        },
    )
    await db.execute(
        text(
            "INSERT INTO users "
            "(id, organization_id, email, password_hash, role, locale, is_active) "
            "VALUES "
            "(:active_user, :organization_a, :active_email, 'unused', 'admin', 'en', true), "
            "(:inactive_user, :organization_b, :inactive_email, 'unused', 'admin', 'en', false)"
        ),
        {
            "active_user": active_user,
            "organization_a": organization_a,
            "active_email": f"active-{marker}@example.test",
            "inactive_user": inactive_user,
            "organization_b": organization_b,
            "inactive_email": f"inactive-{marker}@example.test",
        },
    )

    # SET LOCAL prevents the privileged migration fixture connection from
    # carrying this role into the pool after the test transaction ends.
    await db.execute(text("SET LOCAL ROLE hr_agent_runtime"))

    await db.execute(text("SELECT set_config('app.organization_id', '', true)"))
    assert (await db.execute(text("SELECT count(*) FROM candidates"))).scalar_one() == 0

    await db.execute(
        text("SELECT set_config('app.organization_id', 'not-a-uuid', true)")
    )
    assert (await db.execute(text("SELECT count(*) FROM candidates"))).scalar_one() == 0

    active_claim = (
        (
            await db.execute(
                text("SELECT * FROM security.claim_user_by_id(:user_id)"),
                {"user_id": active_user},
            )
        )
        .mappings()
        .one()
    )
    assert set(active_claim) == {"user_id", "organization_id"}
    assert active_claim["user_id"] == active_user
    assert active_claim["organization_id"] == organization_a
    assert (
        await db.execute(
            text("SELECT * FROM security.claim_user_by_id(:user_id)"),
            {"user_id": inactive_user},
        )
    ).one_or_none() is None

    await db.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_a)},
    )
    visible = list(
        (
            await db.execute(text("SELECT id, organization_id, raw_cv FROM candidates"))
        ).mappings()
    )
    assert visible == [
        {
            "id": candidate_a,
            "organization_id": organization_a,
            "raw_cv": "tenant-a-canary",
        }
    ]
    assert (
        await db.execute(
            text("SELECT count(*) FROM candidates WHERE id = :candidate_id"),
            {"candidate_id": candidate_b},
        )
    ).scalar_one() == 0

    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO candidates (id, organization_id, source) "
                    "VALUES (:candidate_id, :organization_id, 'manual')"
                ),
                {"candidate_id": uuid4(), "organization_id": organization_b},
            )


async def test_worker_role_can_run_global_maintenance_with_broker_read_grant():
    """The SECURITY DEFINER owner can inspect rows it conditionally deletes."""

    now = datetime.now(UTC)
    marker = uuid4().hex
    stale_id = uuid4()
    active_id = uuid4()
    stale_digest = hashlib.sha256(f"stale-{marker}".encode()).hexdigest()
    active_digest = hashlib.sha256(f"active-{marker}".encode()).hexdigest()

    async with SessionLocal() as privileged:
        has_select = (
            await privileged.execute(
                text(
                    "SELECT pg_catalog.has_table_privilege("
                    "'hr_agent_broker', 'public.auth_login_throttles', 'SELECT')"
                )
            )
        ).scalar_one()
        assert has_select is True
        await privileged.execute(
            text(
                "INSERT INTO auth_login_throttles "
                "(id, scope, identifier_digest, failed_attempts, window_started_at, "
                "updated_at) VALUES "
                "(:stale_id, 'account', :stale_digest, 1, :stale_at, :stale_at), "
                "(:active_id, 'account', :active_digest, 1, :active_at, :active_at)"
            ),
            {
                "stale_id": stale_id,
                "stale_digest": stale_digest,
                "stale_at": now - timedelta(days=3),
                "active_id": active_id,
                "active_digest": active_digest,
                "active_at": now,
            },
        )
        await privileged.commit()

    async with SessionLocal() as worker:
        await worker.execute(text("SET LOCAL ROLE hr_agent_worker"))
        removed = (
            (
                await worker.execute(
                    text("SELECT * FROM security.run_global_maintenance(:now)"),
                    {"now": now},
                )
            )
            .mappings()
            .one()
        )
        await worker.commit()

    assert removed["auth_login_throttles"] >= 1
    async with SessionLocal() as privileged:
        remaining = set(
            (
                await privileged.execute(
                    text(
                        "SELECT identifier_digest FROM auth_login_throttles "
                        "WHERE identifier_digest IN (:stale_digest, :active_digest)"
                    ),
                    {
                        "stale_digest": stale_digest,
                        "active_digest": active_digest,
                    },
                )
            ).scalars()
        )
    assert stale_digest not in remaining
    assert active_digest in remaining


async def test_recording_registry_binding_is_rls_scoped_and_tenant_bound(db):
    organization_a = uuid4()
    organization_b = uuid4()
    candidate_a = uuid4()
    candidate_b = uuid4()
    recording_a = uuid4()
    recording_b = uuid4()
    object_a = uuid4()
    object_b = uuid4()
    marker = uuid4().hex
    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug) VALUES "
            "(:a, 'Recording RLS A', :slug_a), "
            "(:b, 'Recording RLS B', :slug_b)"
        ),
        {
            "a": organization_a,
            "b": organization_b,
            "slug_a": f"recording-rls-a-{marker}",
            "slug_b": f"recording-rls-b-{marker}",
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidates (id, organization_id, source, raw_cv) VALUES "
            "(:candidate_a, :organization_a, 'manual', 'tenant-a-recording'), "
            "(:candidate_b, :organization_b, 'manual', 'tenant-b-recording')"
        ),
        {
            "candidate_a": candidate_a,
            "organization_a": organization_a,
            "candidate_b": candidate_b,
            "organization_b": organization_b,
        },
    )
    await db.execute(
        text(
            "INSERT INTO interview_recordings "
            "(id, organization_id, candidate_id, title, status) VALUES "
            "(:recording_a, :organization_a, :candidate_a, 'Tenant A', 'uploaded'), "
            "(:recording_b, :organization_b, :candidate_b, 'Tenant B', 'uploaded')"
        ),
        {
            "recording_a": recording_a,
            "organization_a": organization_a,
            "candidate_a": candidate_a,
            "recording_b": recording_b,
            "organization_b": organization_b,
            "candidate_b": candidate_b,
        },
    )
    key_a = private_object_key(organization_a, "recordings", object_a, ".webm")
    key_b = private_object_key(organization_b, "recordings", object_b, ".webm")
    await db.execute(
        text(
            "INSERT INTO private_objects "
            "(id, organization_id, category, object_key, original_filename, "
            "media_type, size_bytes, sha256, state, scanner_engine, "
            "scanner_verdict, resource_type, resource_id, ready_at) VALUES "
            "(:object_a, :organization_a, 'recordings', :key_a, 'a.webm', "
            "'video/webm', 4, :sha_a, 'ready', 'fake', 'clean', "
            "'interview_recording', :recording_a, now()), "
            "(:object_b, :organization_b, 'recordings', :key_b, 'b.webm', "
            "'video/webm', 4, :sha_b, 'ready', 'fake', 'clean', "
            "'interview_recording', :recording_b, now())"
        ),
        {
            "object_a": object_a,
            "organization_a": organization_a,
            "key_a": key_a,
            "sha_a": "a" * 64,
            "recording_a": recording_a,
            "object_b": object_b,
            "organization_b": organization_b,
            "key_b": key_b,
            "sha_b": "b" * 64,
            "recording_b": recording_b,
        },
    )
    await db.execute(
        text(
            "UPDATE interview_recordings SET private_object_id = :object_id "
            "WHERE id = :recording_id"
        ),
        {"recording_id": recording_a, "object_id": object_a},
    )
    await db.execute(
        text(
            "UPDATE interview_recordings SET private_object_id = :object_id "
            "WHERE id = :recording_id"
        ),
        {"recording_id": recording_b, "object_id": object_b},
    )

    await db.execute(text("SET LOCAL ROLE hr_agent_runtime"))
    await db.execute(
        text("SELECT set_config('app.organization_id', :organization_id, true)"),
        {"organization_id": str(organization_a)},
    )
    visible_recordings = list(
        (
            await db.execute(
                text(
                    "SELECT id, private_object_id FROM interview_recordings ORDER BY id"
                )
            )
        ).mappings()
    )
    assert visible_recordings == [{"id": recording_a, "private_object_id": object_a}]
    assert (
        await db.execute(
            text("SELECT count(*) FROM private_objects WHERE id = :object_id"),
            {"object_id": object_b},
        )
    ).scalar_one() == 0

    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE interview_recordings SET private_object_id = :object_b "
                    "WHERE id = :recording_a"
                ),
                {"recording_a": recording_a, "object_b": object_b},
            )


async def test_forced_queue_stats_match_claim_lease_and_expiry_semantics(db):
    now = datetime.now(UTC)
    marker = uuid4().hex
    organization = Organization(name="Queue RLS", slug=f"queue-rls-{marker}")
    db.add(organization)
    await db.flush()
    object_id = uuid4()
    key = private_object_key(organization.id, "tasks", object_id, ".bin")
    payload = b"forced queue stats"
    private_object = PrivateObject(
        id=object_id,
        organization_id=organization.id,
        category="tasks",
        object_key=key,
        original_filename="answer.bin",
        media_type="application/octet-stream",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        state="delete_pending",
        resource_type="task_submission",
        resource_id=uuid4(),
    )
    deletion = PrivateObjectDeletion(
        id=object_id,
        organization_id=organization.id,
        private_object_id=object_id,
        object_key_snapshot=key,
        sha256_snapshot=private_object.sha256,
        reason="resource_deleted",
        requested_at=now - timedelta(seconds=1),
        not_before=now - timedelta(seconds=1),
        next_attempt_at=now - timedelta(seconds=1),
    )
    lease = WorkerQueueLease(
        queue_name="private_object_deletions",
        work_id=object_id,
        organization_id=organization.id,
        lease_token=uuid4(),
        worker_id="forced-stats-test",
        leased_at=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    db.add_all([private_object, deletion, lease])
    await db.flush()

    await db.execute(text("SET LOCAL ROLE hr_agent_runtime"))
    active_rows = list(
        (
            await db.execute(
                text("SELECT * FROM security.worker_queue_stats(:now)"),
                {"now": now},
            )
        ).mappings()
    )
    active = next(
        row for row in active_rows if row["queue_name"] == "private_object_deletions"
    )
    assert active["due_count"] == 0

    expired_rows = list(
        (
            await db.execute(
                text("SELECT * FROM security.worker_queue_stats(:now)"),
                {"now": lease.lease_expires_at},
            )
        ).mappings()
    )
    expired = next(
        row for row in expired_rows if row["queue_name"] == "private_object_deletions"
    )
    assert expired["due_count"] == 1


async def test_legacy_deletion_and_reconciliation_cursor_are_tenant_isolated(db):
    now = datetime.now(UTC)
    organization_a = uuid4()
    organization_b = uuid4()
    deletion_a = uuid4()
    deletion_b = uuid4()
    resource_a = uuid4()
    resource_b = uuid4()
    file_a = uuid4()
    file_b = uuid4()
    marker = uuid4().hex
    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug) VALUES "
            "(:a, 'Legacy RLS A', :slug_a), (:b, 'Legacy RLS B', :slug_b)"
        ),
        {
            "a": organization_a,
            "b": organization_b,
            "slug_a": f"legacy-rls-a-{marker}",
            "slug_b": f"legacy-rls-b-{marker}",
        },
    )
    await db.execute(
        text(
            "INSERT INTO legacy_private_object_deletions "
            "(id, organization_id, category, normalized_key, resource_type, "
            "resource_id, size_bytes, sha256_snapshot, reason, requested_at, "
            "not_before, next_attempt_at) VALUES "
            "(:id_a, :organization_a, 'tasks', :key_a, 'task_submission', "
            ":resource_a, 4, :sha_a, 'replacement', :now, :now, :now), "
            "(:id_b, :organization_b, 'tasks', :key_b, 'task_submission', "
            ":resource_b, 4, :sha_b, 'replacement', :now, :now, :now)"
        ),
        {
            "id_a": deletion_a,
            "organization_a": organization_a,
            "key_a": f"task_files/{organization_a}/{file_a}.txt",
            "resource_a": resource_a,
            "sha_a": "a" * 64,
            "id_b": deletion_b,
            "organization_b": organization_b,
            "key_b": f"task_files/{organization_b}/{file_b}.txt",
            "resource_b": resource_b,
            "sha_b": "b" * 64,
            "now": now,
        },
    )
    await db.execute(
        text(
            "INSERT INTO private_storage_reconciliation_cursors "
            "(organization_id, namespace, cursor_key) VALUES "
            "(:organization_a, 'private', :cursor_a), "
            "(:organization_b, 'private', :cursor_b)"
        ),
        {
            "organization_a": organization_a,
            "cursor_a": f"private/{organization_a}/tasks/{file_a}.txt",
            "organization_b": organization_b,
            "cursor_b": f"private/{organization_b}/tasks/{file_b}.txt",
        },
    )
    await db.commit()

    async with SessionLocal() as runtime:
        await runtime.execute(text("SET LOCAL ROLE hr_agent_runtime"))
        await runtime.execute(
            text("SELECT set_config('app.organization_id', :tenant, true)"),
            {"tenant": str(organization_a)},
        )
        visible_deletions = list(
            (
                await runtime.execute(
                    text("SELECT id FROM legacy_private_object_deletions ORDER BY id")
                )
            ).scalars()
        )
        assert visible_deletions == [deletion_a]
        visible_cursors = list(
            (
                await runtime.execute(
                    text(
                        "SELECT organization_id FROM "
                        "private_storage_reconciliation_cursors"
                    )
                )
            ).scalars()
        )
        assert visible_cursors == [organization_a]
        with pytest.raises(DBAPIError):
            async with runtime.begin_nested():
                await runtime.execute(
                    text(
                        "INSERT INTO private_storage_reconciliation_cursors "
                        "(organization_id, namespace) VALUES "
                        "(:organization_id, 'registry')"
                    ),
                    {"organization_id": organization_b},
                )

    async def claim(worker_id: str):
        async with SessionLocal() as worker:
            await worker.execute(text("SET LOCAL ROLE hr_agent_worker"))
            rows = list(
                (
                    await worker.execute(
                        text(
                            "SELECT * FROM "
                            "security.claim_legacy_private_object_deletions"
                            "(:worker_id, 1, :now)"
                        ),
                        {"worker_id": worker_id, "now": now},
                    )
                ).mappings()
            )
            await worker.commit()
            return rows

    first, second = await asyncio.gather(claim("legacy-a"), claim("legacy-b"))
    claims = first + second
    assert len(claims) == 2
    assert {item["work_id"] for item in claims} == {deletion_a, deletion_b}
    assert {item["organization_id"] for item in claims} == {
        organization_a,
        organization_b,
    }
    assert all(
        set(item) == {"work_id", "organization_id", "lease_token", "lease_expires_at"}
        for item in claims
    )


async def test_candidate_processing_claim_is_tenant_bound_and_exactly_once(db):
    """The broker returns identity only and two workers cannot claim one job."""

    marker = uuid4().hex
    organization_a = uuid4()
    organization_b = uuid4()
    candidate_a = uuid4()
    candidate_b = uuid4()
    position_a = uuid4()
    position_b = uuid4()
    application_a = uuid4()
    application_b = uuid4()
    job_id = uuid4()

    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug) VALUES "
            "(:organization_a, 'Processing A', :slug_a), "
            "(:organization_b, 'Processing B', :slug_b)"
        ),
        {
            "organization_a": organization_a,
            "organization_b": organization_b,
            "slug_a": f"processing-a-{marker}",
            "slug_b": f"processing-b-{marker}",
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidates (id, organization_id, source) VALUES "
            "(:candidate_a, :organization_a, 'manual'), "
            "(:candidate_b, :organization_b, 'manual')"
        ),
        {
            "candidate_a": candidate_a,
            "organization_a": organization_a,
            "candidate_b": candidate_b,
            "organization_b": organization_b,
        },
    )
    await db.execute(
        text(
            "INSERT INTO positions (id, organization_id, title, status) VALUES "
            "(:position_a, :organization_a, 'Position A', 'open'), "
            "(:position_b, :organization_b, 'Position B', 'open')"
        ),
        {
            "position_a": position_a,
            "organization_a": organization_a,
            "position_b": position_b,
            "organization_b": organization_b,
        },
    )
    await db.execute(
        text(
            "INSERT INTO applications "
            "(id, organization_id, candidate_id, position_id, stage, source, "
            "cycle_number) VALUES "
            "(:application_a, :organization_a, :candidate_a, :position_a, "
            "'new', 'manual', 1), "
            "(:application_b, :organization_b, :candidate_b, :position_b, "
            "'new', 'manual', 1)"
        ),
        {
            "application_a": application_a,
            "organization_a": organization_a,
            "candidate_a": candidate_a,
            "position_a": position_a,
            "application_b": application_b,
            "organization_b": organization_b,
            "candidate_b": candidate_b,
            "position_b": position_b,
        },
    )

    with pytest.raises(DBAPIError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO candidate_processing_jobs "
                    "(id, organization_id, candidate_id, position_id, "
                    "application_id, kind, idempotency_key) VALUES "
                    "(:id, :organization_id, :candidate_id, :position_id, "
                    ":application_id, 'score', :idempotency_key)"
                ),
                {
                    "id": uuid4(),
                    "organization_id": organization_a,
                    "candidate_id": candidate_b,
                    "position_id": position_b,
                    "application_id": application_b,
                    "idempotency_key": f"cross-tenant-{marker}",
                },
            )

    await db.execute(
        text(
            "INSERT INTO candidate_processing_jobs "
            "(id, organization_id, candidate_id, position_id, application_id, "
            "kind, idempotency_key) VALUES "
            "(:id, :organization_id, :candidate_id, :position_id, "
            ":application_id, 'score', :idempotency_key)"
        ),
        {
            "id": job_id,
            "organization_id": organization_a,
            "candidate_id": candidate_a,
            "position_id": position_a,
            "application_id": application_a,
            "idempotency_key": f"valid-{marker}",
        },
    )
    await db.commit()

    async def claim(worker_id: str):
        async with SessionLocal() as session:
            await session.execute(text("SET LOCAL ROLE hr_agent_worker"))
            rows = list(
                (
                    await session.execute(
                        text(
                            "SELECT * FROM security.claim_candidate_processing_jobs"
                            "(:worker_id, 1, :now)"
                        ),
                        {"worker_id": worker_id, "now": datetime.now(UTC)},
                    )
                ).mappings()
            )
            await session.commit()
            return rows

    first, second = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    claims = first + second
    assert len(claims) == 1
    assert set(claims[0]) == {
        "work_id",
        "organization_id",
        "lease_token",
        "lease_expires_at",
    }
    assert claims[0]["work_id"] == job_id
    assert claims[0]["organization_id"] == organization_a

    async with SessionLocal() as session:
        await session.execute(text("SET LOCAL ROLE hr_agent_runtime"))
        await session.execute(
            text("SELECT set_config('app.organization_id', :tenant, true)"),
            {"tenant": str(organization_b)},
        )
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM candidate_processing_jobs WHERE id = :job_id"
                ),
                {"job_id": job_id},
            )
        ).scalar_one() == 0
