"""Bounded tenant-private object reconciliation for durable maintenance claims."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_hiring import AiSession
from app.models.assessment import AssessmentFile
from app.models.candidate import Candidate
from app.models.interview_recording import InterviewRecording
from app.models.private_object import (
    PrivateObject,
    PrivateStorageReconciliationCursor,
)
from app.models.task_submission import TaskSubmission
from app.storage.base import BlobStore, require_tenant_private_key
from app.storage.registry import (
    enqueue_private_object_deletion,
    private_object_category,
)

_PRIVATE_CATEGORIES = frozenset(
    {"candidates", "assessments", "tasks", "ai", "recordings"}
)
_QUARANTINE_CATEGORIES = _PRIVATE_CATEGORIES | {"branding"}


@dataclass(slots=True)
class ReconciliationMetrics:
    dry_run: bool
    registry_scanned: int = 0
    expired_registry_objects: int = 0
    orphaned_registry_objects: int = 0
    registry_deletions_planned: int = 0
    registry_deletions_enqueued: int = 0
    legal_holds_skipped: int = 0
    unsupported_resource_types: int = 0
    missing_registered_objects: int = 0
    quarantine_scanned: int = 0
    stale_quarantine_objects: int = 0
    quarantine_objects_deleted: int = 0
    private_objects_scanned: int = 0
    unregistered_private_objects: int = 0
    unregistered_private_objects_deleted: int = 0
    unsafe_keys_skipped: int = 0
    listing_truncated: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Reconciliation time must include a timezone")
    return current.astimezone(UTC)


async def _locked_cursor(
    db: AsyncSession,
    *,
    organization_id: UUID,
    namespace: str,
    dry_run: bool,
) -> PrivateStorageReconciliationCursor | None:
    """Read progress, creating and locking it only for an applying scan."""

    if not dry_run:
        await db.execute(
            insert(PrivateStorageReconciliationCursor)
            .values(organization_id=organization_id, namespace=namespace)
            .on_conflict_do_nothing(index_elements=["organization_id", "namespace"])
        )
    statement = select(PrivateStorageReconciliationCursor).where(
        PrivateStorageReconciliationCursor.organization_id == organization_id,
        PrivateStorageReconciliationCursor.namespace == namespace,
    )
    if not dry_run:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


def _complete_cursor_cycle(
    cursor: PrivateStorageReconciliationCursor | None,
    *,
    current: datetime,
) -> None:
    if cursor is None:
        return
    cursor.cursor_key = None
    cursor.cycle_count += 1
    cursor.last_completed_at = current


async def _existing_resource_ids(
    db: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_ids: set[UUID],
) -> set[UUID] | None:
    if not resource_ids:
        return set()
    if resource_type == "candidate":
        statement = (
            select(Candidate.id)
            .where(
                Candidate.organization_id == organization_id,
                Candidate.id.in_(resource_ids),
            )
            .execution_options(include_deleted=True)
        )
    elif resource_type == "assessment_file":
        statement = select(AssessmentFile.id).where(
            AssessmentFile.organization_id == organization_id,
            AssessmentFile.id.in_(resource_ids),
        )
    elif resource_type == "task_submission":
        statement = select(TaskSubmission.id).where(
            TaskSubmission.organization_id == organization_id,
            TaskSubmission.id.in_(resource_ids),
        )
    elif resource_type == "ai_attachment":
        # AI attachments bind ``resource_id`` to their owning session. Their
        # registry UUID remains the externally visible attachment identifier.
        statement = select(AiSession.id).where(
            AiSession.organization_id == organization_id,
            AiSession.id.in_(resource_ids),
        )
    elif resource_type == "interview_recording":
        statement = select(InterviewRecording.id).where(
            InterviewRecording.organization_id == organization_id,
            InterviewRecording.id.in_(resource_ids),
        )
    else:
        return None
    return set((await db.execute(statement)).scalars())


def _safe_private_key(key: str, organization_id: UUID) -> bool:
    parts = PurePosixPath(key).parts
    if len(parts) != 4 or parts[0] != "private" or parts[1] != str(organization_id):
        return False
    category = parts[2]
    if category not in _PRIVATE_CATEGORIES:
        return False
    try:
        require_tenant_private_key(
            key,
            organization_id=organization_id,
            category=category,
        )
        UUID(PurePosixPath(parts[3]).stem)
    except (ValueError, TypeError):
        return False
    return True


def _safe_quarantine_key(key: str, organization_id: UUID) -> bool:
    parts = PurePosixPath(key).parts
    if (
        len(parts) != 4
        or parts[0] != "quarantine"
        or parts[1] != str(organization_id)
        or parts[2] not in _QUARANTINE_CATEGORIES
        or PurePosixPath(parts[3]).suffix != ".upload"
    ):
        return False
    try:
        UUID(PurePosixPath(parts[3]).stem)
    except (ValueError, TypeError):
        return False
    return True


async def _reconcile_registry(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store: BlobStore,
    current: datetime,
    dry_run: bool,
    max_objects: int,
    metrics: ReconciliationMetrics,
) -> None:
    cursor = await _locked_cursor(
        db,
        organization_id=organization_id,
        namespace="registry",
        dry_run=dry_run,
    )
    start_after = cursor.cursor_key if cursor is not None else None

    async def load(after: str | None) -> list[PrivateObject]:
        statement = select(PrivateObject).where(
            PrivateObject.organization_id == organization_id,
            PrivateObject.state == "ready",
        )
        if after is not None:
            statement = statement.where(PrivateObject.object_key > after)
        return list(
            (
                await db.execute(
                    statement.order_by(PrivateObject.object_key).limit(max_objects + 1)
                )
            ).scalars()
        )

    rows = await load(start_after)
    # A cursor can legitimately point past the current end after earlier rows
    # were erased. Wrap immediately so one maintenance interval is not wasted.
    if not rows and start_after is not None:
        if not dry_run:
            _complete_cursor_cycle(cursor, current=current)
        rows = await load(None)
    has_more = len(rows) > max_objects
    if has_more:
        metrics.listing_truncated = True
        rows = rows[:max_objects]
    metrics.registry_scanned = len(rows)

    resource_ids: dict[str, set[UUID]] = {}
    for row in rows:
        resource_ids.setdefault(row.resource_type, set()).add(row.resource_id)
    existing: dict[str, set[UUID] | None] = {}
    for resource_type, ids in resource_ids.items():
        existing[resource_type] = await _existing_resource_ids(
            db,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_ids=ids,
        )

    for row in rows:
        owner_ids = existing[row.resource_type]
        unsupported = owner_ids is None
        expected_category = private_object_category(row.resource_type)
        metadata_safe = (
            not unsupported
            and expected_category == row.category
            and _safe_private_key(row.object_key, organization_id)
        )
        expired = row.retention_until is not None and row.retention_until <= current
        orphaned = owner_ids is not None and row.resource_id not in owner_ids
        if expired:
            metrics.expired_registry_objects += 1
        if orphaned:
            metrics.orphaned_registry_objects += 1
        if unsupported:
            metrics.unsupported_resource_types += 1
        if not metadata_safe:
            # Unknown resource types, category mismatches, and corrupted keys
            # are operator-visible but never probed or deleted automatically.
            # This keeps future resource types and tenant-boundary corruption
            # fail closed until their ownership contract is reviewed.
            metrics.unsafe_keys_skipped += 1
            continue

        if await store.stat(row.object_key) is None:
            metrics.missing_registered_objects += 1
        if not expired and not orphaned:
            continue
        if row.legal_hold_at is not None:
            metrics.legal_holds_skipped += 1
            continue
        metrics.registry_deletions_planned += 1
        if not dry_run:
            await enqueue_private_object_deletion(
                db,
                organization_id=organization_id,
                private_object_id=row.id,
                reason="retention_expired" if expired else "resource_deleted",
                now=current,
            )
            metrics.registry_deletions_enqueued += 1

    if not dry_run and cursor is not None:
        if has_more and rows:
            cursor.cursor_key = rows[-1].object_key
        else:
            _complete_cursor_cycle(cursor, current=current)


async def _reconcile_prefix(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store: BlobStore,
    prefix: str,
    cutoff: datetime,
    current: datetime,
    dry_run: bool,
    max_objects: int,
    quarantine: bool,
    namespace: str,
    metrics: ReconciliationMetrics,
) -> None:
    cursor = await _locked_cursor(
        db,
        organization_id=organization_id,
        namespace=namespace,
        dry_run=dry_run,
    )
    remaining = max_objects
    start_after = cursor.cursor_key if cursor is not None else None
    wrapped = False
    reached_end = False
    while remaining > 0:
        page = await store.list_prefix(
            prefix,
            limit=min(remaining, 1000),
            start_after=start_after,
        )
        if quarantine:
            metrics.quarantine_scanned += len(page.entries)
        else:
            metrics.private_objects_scanned += len(page.entries)
        if not page.entries:
            if start_after is not None and not wrapped:
                if not dry_run:
                    _complete_cursor_cycle(cursor, current=current)
                start_after = None
                wrapped = True
                continue
            reached_end = True
            break

        registered_keys: set[str] = set()
        if not quarantine:
            registered_keys = set(
                (
                    await db.execute(
                        select(PrivateObject.object_key).where(
                            PrivateObject.organization_id == organization_id,
                            PrivateObject.object_key.in_(
                                [entry.key for entry in page.entries]
                            ),
                        )
                    )
                ).scalars()
            )
        for entry in page.entries:
            if entry.modified_at > cutoff:
                continue
            safe = (
                _safe_quarantine_key(entry.key, organization_id)
                if quarantine
                else _safe_private_key(entry.key, organization_id)
            )
            if not safe:
                metrics.unsafe_keys_skipped += 1
                continue
            if quarantine:
                metrics.stale_quarantine_objects += 1
                if not dry_run:
                    await store.delete(entry.key)
                    metrics.quarantine_objects_deleted += 1
            elif entry.key not in registered_keys:
                metrics.unregistered_private_objects += 1
                if not dry_run:
                    await store.delete(entry.key)
                    metrics.unregistered_private_objects_deleted += 1
        remaining -= len(page.entries)
        start_after = page.entries[-1].key
        if page.continuation is None:
            reached_end = True
            break
    if not dry_run and cursor is not None:
        if reached_end:
            _complete_cursor_cycle(cursor, current=current)
        elif start_after is not None:
            cursor.cursor_key = start_after
    if not reached_end:
        metrics.listing_truncated = True


async def reconcile_private_storage(
    db: AsyncSession,
    *,
    organization_id: UUID,
    store: BlobStore | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
    stale_after: timedelta | None = None,
    max_objects: int | None = None,
) -> ReconciliationMetrics:
    """Inspect one tenant and optionally enqueue/delete only proven stale data.

    Callers must provide a tenant-bound session. Registered objects always use
    the durable deletion outbox; only aged quarantine and unregistered provider
    objects are deleted directly because no durable registry identity exists.
    """

    current = _utc(now)
    settings = get_settings()
    age = stale_after or timedelta(hours=settings.storage_reconciliation_stale_hours)
    limit = max_objects or settings.storage_reconciliation_scan_limit
    if age < timedelta(hours=1):
        raise ValueError("Reconciliation stale age must be at least one hour")
    if not 1 <= limit <= 5000:
        raise ValueError("Reconciliation scan limit must be between 1 and 5000")
    backend = store
    if backend is None:
        from app.storage import get_blob_store

        backend = get_blob_store()
    metrics = ReconciliationMetrics(dry_run=dry_run)
    await _reconcile_registry(
        db,
        organization_id=organization_id,
        store=backend,
        current=current,
        dry_run=dry_run,
        max_objects=limit,
        metrics=metrics,
    )
    cutoff = current - age
    await _reconcile_prefix(
        db,
        organization_id=organization_id,
        store=backend,
        prefix=f"quarantine/{organization_id}/",
        cutoff=cutoff,
        current=current,
        dry_run=dry_run,
        max_objects=limit,
        quarantine=True,
        namespace="quarantine",
        metrics=metrics,
    )
    await _reconcile_prefix(
        db,
        organization_id=organization_id,
        store=backend,
        prefix=f"private/{organization_id}/",
        cutoff=cutoff,
        current=current,
        dry_run=dry_run,
        max_objects=limit,
        quarantine=False,
        namespace="private",
        metrics=metrics,
    )
    return metrics


__all__ = ["ReconciliationMetrics", "reconcile_private_storage"]
