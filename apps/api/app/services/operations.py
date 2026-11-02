"""Dependency-aware readiness and dedicated-worker heartbeat utilities."""

from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.operations import WorkerHeartbeat
from app.services.public_abuse import public_rate_limit_health
from app.storage.base import BlobStore, BlobStoreError, private_object_key
from app.storage.scanner import (
    MalwareDetectedError,
    MalwareScanner,
    MalwareScannerUnavailableError,
)


@dataclass(frozen=True, slots=True)
class StorageProbeResult:
    ok: bool
    backend: str
    detail: str | None = None


class PrivateStorageProbe(Protocol):
    """Minimal probe contract implemented by local and S3 BlobStores."""

    async def probe(self) -> StorageProbeResult: ...


@dataclass(frozen=True, slots=True)
class ScannerProbeResult:
    ok: bool
    backend: str
    detail: str | None = None
    signature_version: int | None = None
    signature_updated_at: str | None = None
    signature_age_seconds: int | None = None


class ScannerProbe(Protocol):
    """Readiness probe for the configured malware-scanning boundary."""

    async def probe(self) -> ScannerProbeResult: ...


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    checked_at: datetime
    components: dict[str, dict[str, Any]]


class ReadinessProbeCache:
    """Per-replica single-flight cache for active dependency probes."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not 1 <= ttl_seconds <= 30:
            raise ValueError("readiness cache TTL must be between 1 and 30 seconds")
        self.ttl_seconds = ttl_seconds
        self.clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._snapshot: ReadinessSnapshot | None = None
        self._expires_at = 0.0

    async def get(
        self,
        probe: Callable[[], Awaitable[dict[str, dict[str, Any]]]],
    ) -> ReadinessSnapshot:
        current = self.clock()
        if self._snapshot is not None and current < self._expires_at:
            return self._snapshot
        async with self._lock:
            current = self.clock()
            if self._snapshot is not None and current < self._expires_at:
                return self._snapshot
            components = await probe()
            snapshot = ReadinessSnapshot(
                checked_at=datetime.now(UTC),
                components=components,
            )
            self._snapshot = snapshot
            self._expires_at = self.clock() + self.ttl_seconds
            return snapshot

    def reset_for_testing(self) -> None:
        self._snapshot = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()


readiness_probe_cache = ReadinessProbeCache()


class MalwareScannerReadinessProbe:
    """Exercise the configured scanner with an isolated benign payload."""

    def __init__(
        self,
        scanner: MalwareScanner,
        *,
        backend: str,
        max_signature_age_hours: int = 48,
    ) -> None:
        self.scanner = scanner
        self.backend = backend
        self.max_signature_age_hours = max_signature_age_hours

    async def probe(self) -> ScannerProbeResult:
        descriptor, raw_path = tempfile.mkstemp(prefix="hr-scanner-readiness-")
        source = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, b"hr-agent readiness probe\n")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            result = await self.scanner.scan(source)
            if result.result != "clean":
                return ScannerProbeResult(
                    False,
                    self.backend,
                    "Malware scanner returned an invalid readiness verdict",
                )
            if self.backend == "clamav":
                version_probe = getattr(self.scanner, "version", None)
                if not callable(version_probe):
                    return ScannerProbeResult(
                        False,
                        self.backend,
                        "Malware signature freshness is unavailable",
                    )
                version = await version_probe()
                age_seconds = int(
                    (datetime.now(UTC) - version.signature_updated_at).total_seconds()
                )
                max_age_seconds = self.max_signature_age_hours * 60 * 60
                version_data = {
                    "signature_version": version.signature_version,
                    "signature_updated_at": version.signature_updated_at.isoformat(),
                    "signature_age_seconds": max(0, age_seconds),
                }
                if age_seconds < -3600 or age_seconds > max_age_seconds:
                    return ScannerProbeResult(
                        False,
                        self.backend,
                        "Malware signatures are stale or invalid",
                        **version_data,
                    )
                return ScannerProbeResult(True, self.backend, **version_data)
        except (MalwareDetectedError, MalwareScannerUnavailableError, OSError):
            return ScannerProbeResult(
                False,
                self.backend,
                "Malware scanner readiness probe failed",
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            source.unlink(missing_ok=True)
        return ScannerProbeResult(True, self.backend)


class LocalPrivateStorageProbe:
    """Prove private storage create/read/delete behavior, not just mode bits."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def probe(self) -> StorageProbeResult:
        return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self) -> StorageProbeResult:
        try:
            root = self.root.resolve(strict=True)
            if not root.is_dir():
                return StorageProbeResult(
                    False, "local", "Private storage is not a directory"
                )
            name = f".readiness-{secrets.token_hex(16)}"
            target = root / name
            payload = secrets.token_bytes(32)
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    target,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(descriptor, payload)
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                if target.read_bytes() != payload:
                    return StorageProbeResult(
                        False,
                        "local",
                        "Private storage read-after-write verification failed",
                    )
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                target.unlink(missing_ok=True)
        except OSError:
            return StorageProbeResult(False, "local", "Private storage probe failed")
        return StorageProbeResult(True, "local")


class BlobStorePrivateStorageProbe:
    """Exercise the configured object API with an isolated private probe key."""

    def __init__(self, store: BlobStore, *, backend: str) -> None:
        self.store = store
        self.backend = backend

    async def probe(self) -> StorageProbeResult:
        payload = secrets.token_bytes(32)
        key = private_object_key(uuid4(), "tasks", uuid4(), ".probe")
        descriptor, raw_path = tempfile.mkstemp(prefix="hr-storage-readiness-")
        source = Path(raw_path)
        write_attempted = False
        detail: str | None = None
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            write_attempted = True
            stored = await self.store.put_file(
                key,
                source,
                content_type="application/octet-stream",
            )
            if stored.size_bytes != len(payload):
                detail = "Private storage returned incorrect object metadata"
            elif (
                b"".join([chunk async for chunk in self.store.iter_bytes(key)])
                != payload
            ):
                detail = "Private storage read-after-write verification failed"
        except (BlobStoreError, OSError, ValueError):
            detail = "Private storage probe failed"
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            source.unlink(missing_ok=True)
            if write_attempted:
                try:
                    await self.store.delete(key)
                    if await self.store.stat(key) is not None:
                        detail = "Private storage delete verification failed"
                except BlobStoreError:
                    detail = "Private storage probe cleanup failed"
        return StorageProbeResult(detail is None, self.backend, detail)


async def touch_worker_heartbeat(
    db: AsyncSession,
    *,
    worker_name: str,
    worker_id: str,
    process_started_at: datetime,
    metadata: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    statement = insert(WorkerHeartbeat).values(
        worker_name=worker_name,
        worker_id=worker_id,
        process_started_at=process_started_at,
        last_seen_at=now,
        metadata_json=metadata or {},
    )
    statement = statement.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_id],
        set_={
            "worker_name": worker_name,
            "process_started_at": process_started_at,
            "last_seen_at": now,
            "metadata": metadata or {},
            "updated_at": now,
        },
    )
    await db.execute(statement)


def _component(
    status: str,
    detail: str | None = None,
    **data: Any,
) -> dict[str, Any]:
    return {"status": status, "detail": detail, "data": data}


def _expected_migration_heads() -> tuple[str, ...]:
    api_root = Path(__file__).resolve().parents[2]
    configuration = Config(str(api_root / "alembic.ini"))
    scripts = ScriptDirectory.from_config(configuration)
    return tuple(sorted(scripts.get_heads()))


def _required_worker_queues(settings) -> set[str]:
    queues = {
        "legacy-private-object-deletions",
        "maintenance",
        "private-object-deletions",
    }
    if settings.communications_worker_enabled:
        queues.add("communications")
    if settings.integration_worker_enabled:
        queues.add("integrations")
    if settings.scheduling_worker_enabled:
        queues.add("reminders")
    if settings.workflow_worker_enabled:
        queues.add("workflows")
    if settings.ai_worker_enabled:
        queues.add("ai-hiring")
    if settings.candidate_processing_worker_enabled:
        queues.add("candidate-processing")
    return queues


_TENANT_ISOLATION_METADATA = text(
    """
    WITH connected_role AS (
      SELECT role.oid, role.rolsuper, role.rolbypassrls
      FROM pg_catalog.pg_roles AS role
      WHERE role.rolname = CURRENT_USER
    ), protected_tables AS (
      SELECT relation.oid AS table_oid,
             relation.relowner,
             relation.relrowsecurity,
             relation.relforcerowsecurity
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      WHERE namespace.nspname = 'public'
        AND relation.relkind IN ('r', 'p')
        AND (
          relation.relname = 'organizations'
          OR EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS attribute
            WHERE attribute.attrelid = relation.oid
              AND attribute.attname = 'organization_id'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
          )
        )
    )
    SELECT connected_role.rolsuper AS is_superuser,
           connected_role.rolbypassrls AS bypass_rls,
           count(protected_tables.table_oid)::bigint AS protected_table_count,
           count(protected_tables.table_oid) FILTER (
             WHERE protected_tables.relowner = connected_role.oid
           )::bigint AS owned_protected_table_count,
           count(protected_tables.table_oid) FILTER (
             WHERE NOT protected_tables.relrowsecurity
           )::bigint AS rls_disabled_table_count,
           count(protected_tables.table_oid) FILTER (
             WHERE NOT protected_tables.relforcerowsecurity
           )::bigint AS rls_not_forced_table_count
    FROM connected_role
    LEFT JOIN protected_tables ON TRUE
    GROUP BY connected_role.oid,
             connected_role.rolsuper,
             connected_role.rolbypassrls
    """
)


async def _tenant_isolation_component(
    db: AsyncSession,
    *,
    mode: str,
    required: bool,
) -> dict[str, Any]:
    if mode != "forced":
        return _component(
            "failed" if required else "degraded",
            "Tenant isolation verification requires forced RLS mode",
            mode="off" if mode == "off" else "invalid",
        )
    try:
        row = (await db.execute(_TENANT_ISOLATION_METADATA)).mappings().one()
    except Exception:
        return _component(
            "failed" if required else "degraded",
            "Tenant isolation metadata is unavailable",
        )

    data = {
        "is_superuser": bool(row["is_superuser"]),
        "bypass_rls": bool(row["bypass_rls"]),
        "protected_table_count": int(row["protected_table_count"] or 0),
        "owned_protected_table_count": int(row["owned_protected_table_count"] or 0),
        "rls_disabled_table_count": int(row["rls_disabled_table_count"] or 0),
        "rls_not_forced_table_count": int(row["rls_not_forced_table_count"] or 0),
    }
    unsafe = (
        data["is_superuser"]
        or data["bypass_rls"]
        or data["protected_table_count"] == 0
        or data["owned_protected_table_count"] > 0
        or data["rls_disabled_table_count"] > 0
        or data["rls_not_forced_table_count"] > 0
    )
    if unsafe:
        return _component(
            "failed" if required else "degraded",
            "Connected database role or protected-table RLS posture is unsafe",
            **data,
        )
    return _component("ok", **data)


async def _migration_component(db: AsyncSession) -> dict[str, Any]:
    try:
        expected = _expected_migration_heads()
        version_table = (
            await db.execute(text("SELECT to_regclass('public.alembic_version')"))
        ).scalar_one_or_none()
        if version_table is None:
            return _component(
                "failed",
                "Database migration history is unavailable",
                current_heads=[],
                expected_heads=list(expected),
            )
        current = tuple(
            sorted(
                (
                    await db.execute(
                        text(
                            "SELECT version_num FROM alembic_version ORDER BY version_num"
                        )
                    )
                ).scalars()
            )
        )
    except Exception:
        return _component("failed", "Migration head check failed")
    if current != expected:
        return _component(
            "failed",
            "Database migration head does not match the application",
            current_heads=list(current),
            expected_heads=list(expected),
        )
    return _component(
        "ok",
        current_heads=list(current),
        expected_heads=list(expected),
    )


async def _worker_component(
    db: AsyncSession,
    *,
    now: datetime,
    required: bool,
) -> dict[str, Any]:
    settings = get_settings()
    freshness_seconds = int(getattr(settings, "worker_freshness_seconds", 90))
    threshold = now - timedelta(seconds=freshness_seconds)
    expected = _required_worker_queues(settings)
    try:
        rows = list(
            (
                await db.execute(
                    select(WorkerHeartbeat).where(
                        WorkerHeartbeat.last_seen_at >= threshold
                    )
                )
            ).scalars()
        )
    except Exception:
        return _component(
            "failed" if required else "degraded",
            "Worker heartbeat check failed",
            required_queues=sorted(expected),
        )
    covered: set[str] = set()
    for row in rows:
        advertised = (row.metadata_json or {}).get("queues", [])
        if isinstance(advertised, list):
            covered.update(value for value in advertised if isinstance(value, str))
        if row.worker_name in expected:
            covered.add(row.worker_name)
    missing = expected - covered
    if missing:
        return _component(
            "failed" if required else "degraded",
            "Required worker queues have no recent heartbeat",
            required_queues=sorted(expected),
            missing_queues=sorted(missing),
            fresh_replicas=len(rows),
            freshness_seconds=freshness_seconds,
        )
    return _component(
        "ok",
        required_queues=sorted(expected),
        fresh_replicas=len(rows),
        freshness_seconds=freshness_seconds,
    )


_DIRECT_QUEUE_STATS = text(
    """
    SELECT 'integration_jobs'::text AS queue_name,
           count(*) FILTER (WHERE job.next_attempt_at <= :now
             AND job.attempt_count < job.max_attempts
             AND job.cancelled_at IS NULL
             AND (job.status IN ('queued','retrying')
               OR (job.status='running' AND job.lease_expires_at <= :now))
             AND active.work_id IS NULL)::bigint AS due_count,
           min(CASE WHEN job.status='running' THEN job.lease_expires_at
                    ELSE job.next_attempt_at END) FILTER (
             WHERE job.next_attempt_at <= :now
               AND job.attempt_count < job.max_attempts
               AND job.cancelled_at IS NULL
               AND (job.status IN ('queued','retrying')
                 OR (job.status='running' AND job.lease_expires_at <= :now))
               AND active.work_id IS NULL) AS oldest_due_at,
           count(*) FILTER (WHERE job.status = 'dead_letter')::bigint AS dlq_count,
           min(job.completed_at) FILTER (WHERE job.status = 'dead_letter') AS oldest_dlq_at
    FROM integration_jobs AS job
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='integration_jobs' AND active.work_id=job.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'integration_webhook_deliveries',
           count(*) FILTER (WHERE delivery.status IN ('queued','retrying')
             AND delivery.next_attempt_at <= :now
             AND delivery.attempt_count < delivery.max_attempts
             AND active.work_id IS NULL),
           min(delivery.next_attempt_at) FILTER (
             WHERE delivery.status IN ('queued','retrying')
               AND delivery.next_attempt_at <= :now
               AND delivery.attempt_count < delivery.max_attempts
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE delivery.status = 'dead_letter'),
           min(delivery.updated_at) FILTER (WHERE delivery.status = 'dead_letter')
    FROM integration_webhook_deliveries AS delivery
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='integration_webhook_deliveries'
     AND active.work_id=delivery.id AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'communication_messages',
           count(*) FILTER (WHERE message.direction='outbound'
             AND message.attempt_count < 10
             AND ((message.status='queued' AND message.scheduled_for <= :now)
               OR (message.status='processing'
                 AND message.claimed_at < :now - interval '5 minutes'))
             AND active.work_id IS NULL),
           min(CASE WHEN message.status='processing'
                    THEN message.claimed_at + interval '5 minutes'
                    ELSE message.scheduled_for END) FILTER (
             WHERE message.direction='outbound' AND message.attempt_count < 10
               AND ((message.status='queued' AND message.scheduled_for <= :now)
                 OR (message.status='processing'
                   AND message.claimed_at < :now - interval '5 minutes'))
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE message.direction='outbound' AND message.status='failed'),
           min(message.failed_at) FILTER (
             WHERE message.direction='outbound' AND message.status='failed')
    FROM communication_messages AS message
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='communication_messages' AND active.work_id=message.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'interview_reminders',
           count(*) FILTER (WHERE reminder.status='pending'
             AND reminder.due_at <= :now AND active.work_id IS NULL),
           min(reminder.due_at) FILTER (WHERE reminder.status='pending'
             AND reminder.due_at <= :now AND active.work_id IS NULL),
           0::bigint,
           NULL::timestamptz
    FROM interview_reminders AS reminder
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='interview_reminders' AND active.work_id=reminder.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'workflow_runs',
           count(*) FILTER (WHERE run.available_at <= :now
             AND run.attempt_count < run.max_attempts
             AND (run.status IN ('queued','retrying')
               OR (run.status='running' AND run.lease_expires_at <= :now))
             AND active.work_id IS NULL),
           min(CASE WHEN run.status='running' THEN run.lease_expires_at
                    ELSE run.available_at END) FILTER (
             WHERE run.available_at <= :now
               AND run.attempt_count < run.max_attempts
               AND (run.status IN ('queued','retrying')
                 OR (run.status='running' AND run.lease_expires_at <= :now))
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE run.status='dead_letter'),
           min(run.completed_at) FILTER (WHERE run.status='dead_letter')
    FROM workflow_runs AS run
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='workflow_runs' AND active.work_id=run.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'mission_steps',
           count(*) FILTER (WHERE settings.feature_enabled
             AND mission.status IN ('planning','running')
             AND attempts.attempt_count < step.max_attempts
             AND ((step.status='ready' AND step.available_at <= :now)
               OR (step.status='running' AND step.lease_expires_at <= :now))
             AND active.work_id IS NULL),
           min(CASE WHEN step.status='running' THEN step.lease_expires_at
                    ELSE step.available_at END) FILTER (
             WHERE settings.feature_enabled
               AND mission.status IN ('planning','running')
               AND attempts.attempt_count < step.max_attempts
               AND ((step.status='ready' AND step.available_at <= :now)
                 OR (step.status='running' AND step.lease_expires_at <= :now))
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE step.status='failed'),
           min(step.completed_at) FILTER (WHERE step.status='failed')
    FROM mission_steps AS step
    JOIN hiring_missions AS mission
      ON mission.id=step.mission_id AND mission.organization_id=step.organization_id
    LEFT JOIN ai_organization_settings AS settings
      ON settings.organization_id=step.organization_id
    CROSS JOIN LATERAL (
      SELECT count(*) AS attempt_count FROM mission_step_attempts AS attempt
      WHERE attempt.step_id=step.id AND attempt.organization_id=step.organization_id
    ) AS attempts
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='mission_steps' AND active.work_id=step.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'private_object_deletions',
           count(*) FILTER (WHERE deletion.completed_at IS NULL
             AND deletion.not_before <= :now
             AND deletion.next_attempt_at <= :now
             AND deletion.attempt_count < 10
             AND object.state = 'delete_pending'
             AND object.legal_hold_at IS NULL
             AND (deletion.claimed_at IS NULL
               OR deletion.claimed_at < :now - interval '120 seconds')
             AND active.work_id IS NULL),
           min(deletion.next_attempt_at) FILTER (
             WHERE deletion.completed_at IS NULL
               AND deletion.not_before <= :now
               AND deletion.next_attempt_at <= :now
               AND deletion.attempt_count < 10
               AND object.state = 'delete_pending'
               AND object.legal_hold_at IS NULL
               AND (deletion.claimed_at IS NULL
                 OR deletion.claimed_at < :now - interval '120 seconds')
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE deletion.completed_at IS NULL
             AND deletion.attempt_count >= 10),
           min(deletion.updated_at) FILTER (WHERE deletion.completed_at IS NULL
             AND deletion.attempt_count >= 10)
    FROM private_object_deletions AS deletion
    JOIN private_objects AS object
      ON object.id = deletion.private_object_id
     AND object.organization_id = deletion.organization_id
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='private_object_deletions' AND active.work_id=deletion.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'legacy_private_object_deletions',
           count(*) FILTER (WHERE deletion.completed_at IS NULL
             AND deletion.not_before <= :now
             AND deletion.next_attempt_at <= :now
             AND deletion.attempt_count < 10
             AND (deletion.claimed_at IS NULL
               OR deletion.claimed_at < :now - interval '120 seconds')
             AND active.work_id IS NULL),
           min(deletion.next_attempt_at) FILTER (
             WHERE deletion.completed_at IS NULL
               AND deletion.not_before <= :now
               AND deletion.next_attempt_at <= :now
               AND deletion.attempt_count < 10
               AND (deletion.claimed_at IS NULL
                 OR deletion.claimed_at < :now - interval '120 seconds')
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE deletion.completed_at IS NULL
             AND deletion.attempt_count >= 10),
           min(deletion.updated_at) FILTER (WHERE deletion.completed_at IS NULL
             AND deletion.attempt_count >= 10)
    FROM legacy_private_object_deletions AS deletion
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='legacy_private_object_deletions'
     AND active.work_id=deletion.id
     AND active.lease_expires_at > :now
    UNION ALL
    SELECT 'candidate_processing_jobs',
           count(*) FILTER (WHERE job.cancelled_at IS NULL
             AND job.available_at <= :now
             AND job.attempt_count < job.max_attempts
             AND (job.status IN ('queued','retrying')
               OR (job.status='running' AND job.lease_expires_at <= :now))
             AND active.work_id IS NULL),
           min(CASE WHEN job.status='running' THEN job.lease_expires_at
                    ELSE job.available_at END) FILTER (
             WHERE job.cancelled_at IS NULL
               AND job.available_at <= :now
               AND job.attempt_count < job.max_attempts
               AND (job.status IN ('queued','retrying')
                 OR (job.status='running' AND job.lease_expires_at <= :now))
               AND active.work_id IS NULL),
           count(*) FILTER (WHERE job.status='dead_letter'),
           min(job.completed_at) FILTER (WHERE job.status='dead_letter')
    FROM candidate_processing_jobs AS job
    LEFT JOIN worker_queue_leases AS active
      ON active.queue_name='candidate_processing_jobs' AND active.work_id=job.id
     AND active.lease_expires_at > :now
    """
)


async def _queue_component(
    db: AsyncSession,
    *,
    now: datetime,
    mode: str,
    required: bool,
) -> dict[str, Any]:
    settings = get_settings()
    warning_seconds = int(getattr(settings, "worker_queue_warning_age_seconds", 300))
    failure_seconds = int(getattr(settings, "worker_queue_failure_age_seconds", 900))
    try:
        if mode == "forced":
            result = await db.execute(
                text(
                    "SELECT queue_name, due_count, oldest_due_at, "
                    "dlq_count, oldest_dlq_at "
                    "FROM security.worker_queue_stats(:now) "
                    "UNION ALL SELECT queue_name, due_count, oldest_due_at, "
                    "dlq_count, oldest_dlq_at "
                    "FROM security.candidate_processing_queue_stats(:now) "
                    "UNION ALL SELECT queue_name, due_count, oldest_due_at, "
                    "dlq_count, oldest_dlq_at "
                    "FROM security.legacy_private_object_deletion_queue_stats(:now)"
                ),
                {"now": now},
            )
        elif mode == "off":
            result = await db.execute(_DIRECT_QUEUE_STATS, {"now": now})
        else:
            raise RuntimeError("Worker RLS mode is not configured")
        rows = list(result.mappings())
    except Exception:
        return _component(
            "failed" if required else "degraded",
            "Worker queue statistics are unavailable",
        )

    queues: list[dict[str, Any]] = []
    worst_age = 0.0
    dlq_total = 0
    for row in rows:
        oldest_due = row["oldest_due_at"]
        age = max(0.0, (now - oldest_due).total_seconds()) if oldest_due else None
        if age is not None:
            worst_age = max(worst_age, age)
        dlq_total += int(row["dlq_count"] or 0)
        queues.append(
            {
                "queue": row["queue_name"],
                "due_count": int(row["due_count"] or 0),
                "oldest_due_age_seconds": round(age, 3) if age is not None else None,
                "dlq_count": int(row["dlq_count"] or 0),
                "oldest_dlq_at": row["oldest_dlq_at"].isoformat()
                if row["oldest_dlq_at"]
                else None,
            }
        )
    if worst_age >= failure_seconds:
        status = "failed"
        detail = "A durable queue exceeded the maximum processing age"
    elif worst_age >= warning_seconds or dlq_total:
        status = "degraded"
        detail = "Durable queue backlog or dead-letter records require attention"
    else:
        status = "ok"
        detail = None
    return _component(
        status,
        detail,
        queues=queues,
        warning_age_seconds=warning_seconds,
        failure_age_seconds=failure_seconds,
        dlq_total=dlq_total,
    )


async def readiness_components(
    db: AsyncSession,
    *,
    storage_probe: PrivateStorageProbe | None = None,
    scanner_probe: ScannerProbe | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    settings = get_settings()
    current = now or datetime.now(UTC)
    required = (
        settings.app_environment == "production" or settings.worker_readiness_required
    )
    mode = getattr(settings, "tenant_rls_mode", None)
    components: dict[str, dict[str, Any]] = {}
    database_ok = False
    try:
        await db.execute(text("SELECT 1"))
        database_ok = True
        components["database"] = _component("ok")
    except Exception:
        components["database"] = _component(
            "failed",
            "Database connectivity check failed",
        )

    if database_ok:
        components["migrations"] = await _migration_component(db)
        components["tenant_isolation"] = await _tenant_isolation_component(
            db,
            mode=mode,
            required=required,
        )
    else:
        components["migrations"] = _component(
            "failed",
            "Migration state is unavailable without the database",
        )
        components["tenant_isolation"] = _component(
            "failed" if required else "degraded",
            "Tenant isolation state is unavailable without the database",
        )

    if storage_probe is None:
        if settings.blob_store_backend == "s3":
            from app.storage import get_blob_store

            storage_probe = BlobStorePrivateStorageProbe(
                get_blob_store(),
                backend="s3",
            )
        else:
            configured = getattr(settings, "private_storage_dir", settings.storage_dir)
            storage_probe = LocalPrivateStorageProbe(Path(configured))
    storage_result = await storage_probe.probe()
    components["private_storage"] = _component(
        "ok" if storage_result.ok else "failed",
        storage_result.detail,
        backend=storage_result.backend,
    )

    if scanner_probe is None:
        from app.storage import get_malware_scanner

        scanner_probe = MalwareScannerReadinessProbe(
            get_malware_scanner(),
            backend=settings.malware_scanner_backend,
            max_signature_age_hours=settings.clamav_max_signature_age_hours,
        )
    scanner_result = await scanner_probe.probe()
    components["malware_scanner"] = _component(
        "ok" if scanner_result.ok else "failed",
        scanner_result.detail,
        backend=scanner_result.backend,
        **{
            key: value
            for key, value in {
                "signature_version": scanner_result.signature_version,
                "signature_updated_at": scanner_result.signature_updated_at,
                "signature_age_seconds": scanner_result.signature_age_seconds,
            }.items()
            if value is not None
        },
    )

    limiter_ok, limiter_backend = await public_rate_limit_health()
    components["public_rate_limits"] = _component(
        "ok"
        if limiter_ok
        else "failed"
        if settings.app_environment == "production"
        else "degraded",
        None if limiter_ok else "Public abuse-protection backend is unavailable",
        backend=limiter_backend,
        distributed=limiter_backend == "valkey",
    )

    if database_ok:
        components["workers"] = await _worker_component(
            db,
            now=current,
            required=required,
        )
        components["queues"] = await _queue_component(
            db,
            now=current,
            mode=mode,
            required=required,
        )
    else:
        components["workers"] = _component(
            "failed" if required else "degraded",
            "Worker freshness is unavailable without the database",
        )
        components["queues"] = _component(
            "failed" if required else "degraded",
            "Queue state is unavailable without the database",
        )
    return components
