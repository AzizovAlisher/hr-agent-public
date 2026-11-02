"""Contract usage, idempotency, and operational-health regression coverage."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.operations import (
    OrganizationEntitlement,
    UsageLedgerEntry,
    WorkerHeartbeat,
    WorkerQueueLease,
)
from app.models.organization import Organization
from app.models.private_object import (
    LegacyPrivateObjectDeletion,
    PrivateObject,
    PrivateObjectDeletion,
)
from app.models.user import User
from app.routers import operations as operations_router
from app.services.entitlements import (
    TECHNICAL_LIMITS,
    EntitlementExceeded,
    entitlement_snapshot,
    record_usage,
)
from app.services.operations import (
    BlobStorePrivateStorageProbe,
    LocalPrivateStorageProbe,
    MalwareScannerReadinessProbe,
    ReadinessProbeCache,
    ScannerProbeResult,
    StorageProbeResult,
    _tenant_isolation_component,
    readiness_components,
    readiness_probe_cache,
    touch_worker_heartbeat,
)
from app.storage import LocalBlobStore, private_object_key
from app.storage.scanner import ClamAvVersion, FakeMalwareScanner

pytestmark = pytest.mark.asyncio


class IsolationMetadataResult:
    def __init__(self, row: dict) -> None:
        self.row = row

    def mappings(self):
        return self

    def one(self) -> dict:
        return self.row


class IsolationMetadataDb:
    def __init__(self, row: dict) -> None:
        self.row = row

    async def execute(self, _statement):
        return IsolationMetadataResult(self.row)


async def _seed(db, *, role: str = "admin"):
    marker = uuid4().hex[:10]
    organization = Organization(name="Metering", slug=f"metering-{marker}")
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"{role}-{marker}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
    )
    db.add(user)
    await db.commit()
    return organization, user


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
        mfa_verified=True,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_usage_is_idempotent_and_positive_limit_is_hard_cap(db):
    organization, _user = await _seed(db)
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="assessment_starts",
            limit_value=2,
            period="contract",
        )
    )
    await db.commit()

    first = await record_usage(
        db,
        organization_id=organization.id,
        metric="assessment_starts",
        amount=1,
        kind="usage",
        idempotency_key="attempt:first",
    )
    await db.commit()
    replay = await record_usage(
        db,
        organization_id=organization.id,
        metric="assessment_starts",
        amount=1,
        kind="usage",
        idempotency_key="attempt:first",
    )
    assert replay.id == first.id

    await record_usage(
        db,
        organization_id=organization.id,
        metric="assessment_starts",
        amount=1,
        kind="usage",
        idempotency_key="attempt:second",
    )
    await db.commit()
    with pytest.raises(EntitlementExceeded) as denied:
        await record_usage(
            db,
            organization_id=organization.id,
            metric="assessment_starts",
            amount=1,
            kind="usage",
            idempotency_key="attempt:third",
        )
    assert denied.value.limit == 2
    assert denied.value.used == 2
    await db.rollback()
    assert (await db.execute(select(UsageLedgerEntry))).scalars().all().__len__() == 2


async def test_null_is_unlimited_zero_is_blocked_and_refund_releases_usage(db):
    organization, _user = await _seed(db)
    organization_id = organization.id
    unlimited = await entitlement_snapshot(db, organization_id, "ai_tokens")
    assert unlimited.limit is None
    assert unlimited.remaining is None

    db.add(
        OrganizationEntitlement(
            organization_id=organization_id,
            metric="integration_runs",
            limit_value=0,
        )
    )
    await db.commit()
    with pytest.raises(EntitlementExceeded):
        await record_usage(
            db,
            organization_id=organization_id,
            metric="integration_runs",
            amount=1,
            kind="reserve",
            idempotency_key="connector:blocked",
        )
    await db.rollback()

    await record_usage(
        db,
        organization_id=organization_id,
        metric="ai_tokens",
        amount=500,
        kind="reserve",
        idempotency_key="ai:reserve",
    )
    await record_usage(
        db,
        organization_id=organization_id,
        metric="ai_tokens",
        amount=-200,
        kind="refund",
        idempotency_key="ai:refund",
        enforce_limit=False,
    )
    await db.commit()
    snapshot = await entitlement_snapshot(db, organization_id, "ai_tokens")
    assert snapshot.used == 300


async def test_entitlements_and_usage_are_tenant_scoped(db):
    first_org, first_admin = await _seed(db)
    second_org, second_admin = await _seed(db)
    await record_usage(
        db,
        organization_id=first_org.id,
        metric="storage_bytes",
        amount=1024,
        kind="usage",
        idempotency_key="file:first",
    )
    await record_usage(
        db,
        organization_id=second_org.id,
        metric="storage_bytes",
        amount=4096,
        kind="usage",
        idempotency_key="file:second",
    )
    await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/enterprise/entitlements", headers=_auth(first_admin))
        usage = await client.get("/enterprise/usage", headers=_auth(first_admin))
        second = await client.get(
            "/enterprise/entitlements", headers=_auth(second_admin)
        )

    assert first.status_code == 200
    assert first.json()["technical_limits"] == TECHNICAL_LIMITS
    first_storage = next(
        item
        for item in first.json()["entitlements"]
        if item["metric"] == "storage_bytes"
    )
    second_storage = next(
        item
        for item in second.json()["entitlements"]
        if item["metric"] == "storage_bytes"
    )
    # Storage is a current-state metric. Historical/manual ledger events stay
    # tenant-scoped and auditable, but cannot inflate the authoritative live
    # private-object byte count.
    assert first_storage["used"] == 0
    assert second_storage["used"] == 0
    assert first_storage["basis"] == "current_state"
    assert [item["amount"] for item in usage.json()["items"]] == [1024]


async def test_worker_heartbeat_upserts_without_duplicate_replica(db):
    started = datetime.now(UTC)
    await touch_worker_heartbeat(
        db,
        worker_name="durable-work",
        worker_id="worker:test:1",
        process_started_at=started,
        metadata={"pid": 1},
    )
    await db.commit()
    await touch_worker_heartbeat(
        db,
        worker_name="durable-work",
        worker_id="worker:test:1",
        process_started_at=started,
        metadata={"pid": 1, "tick": 2},
    )
    await db.commit()
    rows = (await db.execute(select(WorkerHeartbeat))).scalars().all()
    assert len(rows) == 1
    assert rows[0].metadata_json["tick"] == 2


async def test_private_storage_probe_performs_round_trip_without_orphan(tmp_path):
    result = await LocalPrivateStorageProbe(tmp_path).probe()
    assert result == StorageProbeResult(ok=True, backend="local", detail=None)
    assert list(tmp_path.iterdir()) == []


async def test_readiness_probe_cache_is_single_flight_and_bounded():
    clock = [100.0]
    cache = ReadinessProbeCache(ttl_seconds=5, clock=lambda: clock[0])
    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"database": {"status": "ok", "detail": None, "data": {}}}

    snapshots = await asyncio.gather(*(cache.get(probe) for _ in range(40)))
    assert calls == 1
    assert len({snapshot.checked_at for snapshot in snapshots}) == 1

    clock[0] += 4.9
    assert (await cache.get(probe)).components["database"]["status"] == "ok"
    assert calls == 1

    clock[0] += 0.2
    await cache.get(probe)
    assert calls == 2


async def test_public_readiness_is_single_flight_redacted_and_detail_is_authorized(
    db, monkeypatch
):
    _organization, admin = await _seed(db)
    calls = 0
    private_marker = "internal-migration-and-queue-detail"

    async def probe(_session):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {
            "database": {
                "status": "ok",
                "detail": private_marker,
                "data": {"current_heads": ["internal-head"]},
            }
        }

    monkeypatch.setattr(operations_router, "readiness_components", probe)
    readiness_probe_cache.reset_for_testing()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            public_responses = await asyncio.gather(
                *(client.get("/health/ready") for _ in range(30))
            )
            detailed = await client.get(
                "/enterprise/operations/readiness", headers=_auth(admin)
            )
        assert calls == 1
        assert all(response.status_code == 200 for response in public_responses)
        assert all(
            set(response.json()) == {"status", "checked_at"}
            for response in public_responses
        )
        assert all(private_marker not in response.text for response in public_responses)
        assert len(
            {response.json()["checked_at"] for response in public_responses}
        ) == 1
        assert detailed.status_code == 200
        assert detailed.json()["components"]["database"]["detail"] == private_marker
    finally:
        readiness_probe_cache.reset_for_testing()


async def test_blob_store_probe_verifies_object_round_trip_and_cleanup(tmp_path):
    store = LocalBlobStore(tmp_path / "objects")
    result = await BlobStorePrivateStorageProbe(store, backend="test-s3").probe()
    assert result == StorageProbeResult(ok=True, backend="test-s3", detail=None)
    assert [path for path in store.root.rglob("*") if path.is_file()] == []


async def test_malware_scanner_readiness_probe_is_live_and_cleans_up():
    scanner = FakeMalwareScanner()
    result = await MalwareScannerReadinessProbe(
        scanner,
        backend="fake",
    ).probe()
    assert result == ScannerProbeResult(ok=True, backend="fake", detail=None)
    assert len(scanner.scanned_paths) == 1
    assert not scanner.scanned_paths[0].exists()


async def test_malware_scanner_readiness_fails_closed_when_unavailable():
    result = await MalwareScannerReadinessProbe(
        FakeMalwareScanner(verdict="unavailable"),
        backend="clamav",
    ).probe()
    assert result == ScannerProbeResult(
        ok=False,
        backend="clamav",
        detail="Malware scanner readiness probe failed",
    )


async def test_malware_scanner_readiness_enforces_signature_freshness():
    class VersionedScanner(FakeMalwareScanner):
        def __init__(self, updated_at: datetime):
            super().__init__()
            self.updated_at = updated_at

        async def version(self) -> ClamAvVersion:
            return ClamAvVersion(
                engine_version="test",
                signature_version=28_101,
                signature_updated_at=self.updated_at,
            )

    current = datetime.now(UTC)
    fresh = await MalwareScannerReadinessProbe(
        VersionedScanner(current - timedelta(hours=1)),
        backend="clamav",
        max_signature_age_hours=48,
    ).probe()
    assert fresh.ok is True
    assert fresh.signature_version == 28_101
    assert fresh.signature_age_seconds is not None
    assert 3_500 <= fresh.signature_age_seconds <= 3_700

    stale = await MalwareScannerReadinessProbe(
        VersionedScanner(current - timedelta(hours=49)),
        backend="clamav",
        max_signature_age_hours=48,
    ).probe()
    assert stale.ok is False
    assert stale.detail == "Malware signatures are stale or invalid"


async def test_tenant_isolation_readiness_accepts_only_safe_role_and_forced_tables():
    result = await _tenant_isolation_component(
        IsolationMetadataDb(
            {
                "is_superuser": False,
                "bypass_rls": False,
                "protected_table_count": 73,
                "owned_protected_table_count": 0,
                "rls_disabled_table_count": 0,
                "rls_not_forced_table_count": 0,
            }
        ),
        mode="forced",
        required=True,
    )
    assert result["status"] == "ok"
    assert result["data"]["protected_table_count"] == 73


async def test_tenant_isolation_readiness_fails_closed_without_identity_leakage():
    result = await _tenant_isolation_component(
        IsolationMetadataDb(
            {
                "is_superuser": True,
                "bypass_rls": True,
                "protected_table_count": 73,
                "owned_protected_table_count": 2,
                "rls_disabled_table_count": 1,
                "rls_not_forced_table_count": 3,
            }
        ),
        mode="forced",
        required=True,
    )
    assert result["status"] == "failed"
    assert result["detail"] == (
        "Connected database role or protected-table RLS posture is unsafe"
    )
    assert set(result["data"]) == {
        "is_superuser",
        "bypass_rls",
        "protected_table_count",
        "owned_protected_table_count",
        "rls_disabled_table_count",
        "rls_not_forced_table_count",
    }


async def test_off_mode_private_deletion_stats_exclude_legal_holds(db):
    organization, _user = await _seed(db)
    now = datetime.now(UTC)
    object_id = uuid4()
    payload = b"legal hold"
    key = private_object_key(organization.id, "tasks", object_id, ".bin")
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
        legal_hold_at=now,
    )
    db.add(private_object)
    db.add(
        PrivateObjectDeletion(
            organization_id=organization.id,
            private_object_id=object_id,
            object_key_snapshot=key,
            sha256_snapshot=private_object.sha256,
            reason="resource_deleted",
            requested_at=now - timedelta(seconds=1),
            not_before=now - timedelta(seconds=1),
            next_attempt_at=now - timedelta(seconds=1),
        )
    )
    leased_object_id = uuid4()
    leased_key = private_object_key(
        organization.id,
        "tasks",
        leased_object_id,
        ".bin",
    )
    leased_object = PrivateObject(
        id=leased_object_id,
        organization_id=organization.id,
        category="tasks",
        object_key=leased_key,
        original_filename="leased.bin",
        media_type="application/octet-stream",
        size_bytes=len(payload),
        sha256=hashlib.sha256(b"leased payload").hexdigest(),
        state="delete_pending",
        resource_type="task_submission",
        resource_id=uuid4(),
    )
    db.add(leased_object)
    db.add(
        PrivateObjectDeletion(
            id=leased_object_id,
            organization_id=organization.id,
            private_object_id=leased_object_id,
            object_key_snapshot=leased_key,
            sha256_snapshot=leased_object.sha256,
            reason="resource_deleted",
            requested_at=now - timedelta(seconds=1),
            not_before=now - timedelta(seconds=1),
            next_attempt_at=now - timedelta(seconds=1),
        )
    )
    active_lease = WorkerQueueLease(
        queue_name="private_object_deletions",
        work_id=leased_object_id,
        organization_id=organization.id,
        lease_token=uuid4(),
        worker_id="readiness-test",
        leased_at=now - timedelta(seconds=1),
        lease_expires_at=now + timedelta(seconds=30),
    )
    legacy_file_id = uuid4()
    db.add(
        LegacyPrivateObjectDeletion(
            organization_id=organization.id,
            category="tasks",
            normalized_key=(f"task_files/{organization.id}/{legacy_file_id}.txt"),
            resource_type="task_submission",
            resource_id=uuid4(),
            size_bytes=4,
            sha256_snapshot="c" * 64,
            reason="replacement",
            requested_at=now - timedelta(seconds=1),
            not_before=now - timedelta(seconds=1),
            next_attempt_at=now - timedelta(seconds=1),
        )
    )
    db.add(active_lease)
    await db.commit()

    class HealthyStorage:
        async def probe(self) -> StorageProbeResult:
            return StorageProbeResult(ok=True, backend="test")

    components = await readiness_components(
        db,
        storage_probe=HealthyStorage(),
        now=now,
    )
    private_queue = next(
        row
        for row in components["queues"]["data"]["queues"]
        if row["queue"] == "private_object_deletions"
    )
    assert private_queue["due_count"] == 0
    legacy_queue = next(
        row
        for row in components["queues"]["data"]["queues"]
        if row["queue"] == "legacy_private_object_deletions"
    )
    assert legacy_queue["due_count"] == 1

    active_lease.lease_expires_at = now
    await db.commit()
    components = await readiness_components(
        db,
        storage_probe=HealthyStorage(),
        now=now,
    )
    private_queue = next(
        row
        for row in components["queues"]["data"]["queues"]
        if row["queue"] == "private_object_deletions"
    )
    assert private_queue["due_count"] == 1


async def test_readiness_reports_exact_migration_heads_workers_and_queue_state(db):
    try:
        migrated = await db.scalar(text("SELECT count(*) FROM alembic_version"))
    except Exception:
        await db.rollback()
        pytest.skip("Exact-head readiness requires the migrated test schema")
    if not migrated:
        pytest.skip("Exact-head readiness requires the migrated test schema")

    started = datetime.now(UTC)
    await touch_worker_heartbeat(
        db,
        worker_name="durable-work",
        worker_id="worker:readiness",
        process_started_at=started,
        metadata={
            "queues": [
                "maintenance",
                "legacy-private-object-deletions",
                "private-object-deletions",
                "communications",
                "integrations",
                "reminders",
                "workflows",
                "ai-hiring",
                "candidate-processing",
            ]
        },
    )
    await db.commit()

    class HealthyStorage:
        async def probe(self) -> StorageProbeResult:
            return StorageProbeResult(ok=True, backend="test")

    class HealthyScanner:
        async def probe(self) -> ScannerProbeResult:
            return ScannerProbeResult(ok=True, backend="test")

    components = await readiness_components(
        db,
        storage_probe=HealthyStorage(),
        scanner_probe=HealthyScanner(),
    )
    assert components["database"]["status"] == "ok"
    assert components["migrations"]["status"] == "ok"
    current_heads = components["migrations"]["data"]["current_heads"]
    expected_heads = components["migrations"]["data"]["expected_heads"]
    assert current_heads == expected_heads
    assert len(expected_heads) == 1
    assert components["private_storage"] == {
        "status": "ok",
        "detail": None,
        "data": {"backend": "test"},
    }
    assert components["malware_scanner"] == {
        "status": "ok",
        "detail": None,
        "data": {"backend": "test"},
    }
    assert components["workers"]["status"] == "ok"
    assert components["queues"]["status"] == "ok"
