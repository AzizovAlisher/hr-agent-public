"""Tenant-private object registry and durable deletion settlement.

The object store is not transactional.  Upload callers therefore promote the
blob first, then create a ``PrivateObject`` in the same database transaction as
the resource that references it.  A failed database transaction removes the
unregistered blob best-effort.  Committed objects are never deleted inline:
callers enqueue ``PrivateObjectDeletion`` and a durable worker performs the
idempotent physical delete before marking the registry row deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.services.entitlements import record_usage
from app.storage.base import (
    BlobStore,
    BlobStoreError,
    blob_deletion_receipt,
    require_tenant_private_key,
)
from app.storage.intake import StoredUpload

_RESOURCE_CATEGORY = {
    "candidate": "candidates",
    "assessment_file": "assessments",
    "task_submission": "tasks",
    "ai_attachment": "ai",
    "interview_recording": "recordings",
}
_DELETION_REASONS = {
    "candidate_erasure",
    "retention_expired",
    "resource_deleted",
    "upload_rejected",
    "replacement",
    "admin_request",
}


def private_object_category(resource_type: str) -> str | None:
    """Return the only reviewed namespace for a registry resource type."""

    return _RESOURCE_CATEGORY.get(resource_type)


class PrivateObjectRegistryError(RuntimeError):
    """A registry operation violated a closed tenant/resource contract."""


class StalePrivateObjectDeletionClaim(PrivateObjectRegistryError):
    """The deletion lease is absent, expired, or belongs to another worker."""


class DeletionSettlement(StrEnum):
    DELETED = "deleted"
    ALREADY_DELETED = "already_deleted"
    RETRY_SCHEDULED = "retry_scheduled"


@dataclass(frozen=True, slots=True)
class RegisteredBlob:
    """The reviewed metadata needed by private readers and parsers."""

    id: UUID
    key: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PrivateObjectRegistryError("Private-object time must include a timezone")
    return current.astimezone(UTC)


async def _register_ready_private_object(
    db: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: UUID,
    stored: StoredUpload[object],
    now: datetime | None = None,
    retention_until: datetime | None = None,
    enforce_storage_limit: bool,
    usage_metadata: dict[str, str],
) -> PrivateObject:
    """Stage one promoted, scanned object in the resource transaction."""

    category = private_object_category(resource_type)
    if category is None:
        raise PrivateObjectRegistryError("Unsupported private-object resource type")
    try:
        key = require_tenant_private_key(
            stored.key,
            organization_id=organization_id,
            category=category,
        )
    except ValueError as exc:
        raise PrivateObjectRegistryError(
            "Private object does not match its tenant/resource category"
        ) from exc
    ready_at = _utc(now)
    retention = _utc(retention_until) if retention_until is not None else None
    record_id = uuid4()
    # Reserve against the authoritative live-byte count before the registry
    # row is added.  The caller commits this event together with the resource
    # and registry binding, or rolls all three back and removes the promoted
    # blob through its existing reconciliation path.
    await record_usage(
        db,
        organization_id=organization_id,
        metric="storage_bytes",
        amount=stored.size_bytes,
        kind="usage",
        idempotency_key=f"private-object:promote:{record_id}",
        resource_type="private_object",
        resource_id=record_id,
        metadata={"category": category, **usage_metadata},
        enforce_limit=enforce_storage_limit,
    )
    record = PrivateObject(
        id=record_id,
        organization_id=organization_id,
        category=category,
        object_key=key,
        original_filename=stored.original_filename,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        state="ready",
        scanner_engine=stored.scan.engine[:64],
        scanner_version=None,
        scanner_verdict=stored.scan.result[:64],
        resource_type=resource_type,
        resource_id=resource_id,
        retention_until=retention,
        ready_at=ready_at,
    )
    db.add(record)
    # Resource rows do not declare ORM relationships to this registry.  Flush
    # the parent explicitly so PostgreSQL never sees a child FK first when a
    # resource and its object are created in the same unit of work.
    await db.flush([record])
    return record


async def register_ready_private_object(
    db: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: UUID,
    stored: StoredUpload[object],
    now: datetime | None = None,
    retention_until: datetime | None = None,
) -> PrivateObject:
    """Register a newly promoted object and enforce contract storage capacity."""

    return await _register_ready_private_object(
        db,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        stored=stored,
        now=now,
        retention_until=retention_until,
        enforce_storage_limit=True,
        usage_metadata={},
    )


async def register_migrated_ready_private_object(
    db: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: UUID,
    stored: StoredUpload[object],
    now: datetime | None = None,
    retention_until: datetime | None = None,
) -> PrivateObject:
    """Register one clean legacy cutover object without blocking on its cap.

    The bytes already belong to the tenant and therefore must be brought under
    the authoritative registry even when the contract currently reports zero
    remaining storage capacity.  The usage delta is still recorded; only the
    ceiling check is bypassed.  Requiring a real clean scanner verdict keeps
    this narrow path unusable for ordinary or erasure-only adoption.
    """

    if stored.scan.result != "clean" or stored.scan.engine == "legacy":
        raise PrivateObjectRegistryError(
            "Legacy cutover requires a newly scanned clean object"
        )
    return await _register_ready_private_object(
        db,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        stored=stored,
        now=now,
        retention_until=retention_until,
        enforce_storage_limit=False,
        usage_metadata={"purpose": "legacy_private_object_cutover"},
    )


async def adopt_legacy_private_object_for_erasure(
    db: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: UUID,
    stored: StoredUpload[object],
    now: datetime | None = None,
) -> PrivateObject:
    """Adopt one pre-existing blob only as an atomic deletion outbox item.

    Retention must account for a legacy object before deleting it, even when
    the tenant is already at or above its storage cap. This narrow path accepts
    only the erasure scanner marker and makes the object unreadable/deletion-
    pending in the same transaction; it cannot create usable storage.
    """

    if stored.scan.engine != "legacy" or stored.scan.result != "erasure-only":
        raise PrivateObjectRegistryError(
            "Cleanup adoption requires an erasure-only legacy object"
        )
    record = await _register_ready_private_object(
        db,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        stored=stored,
        now=now,
        retention_until=None,
        enforce_storage_limit=False,
        usage_metadata={"purpose": "legacy_erasure_adoption"},
    )
    deletion = await enqueue_private_object_deletion(
        db,
        organization_id=organization_id,
        private_object_id=record.id,
        reason="candidate_erasure",
        now=now,
    )
    if deletion is None or record.state != "delete_pending":
        raise PrivateObjectRegistryError(
            "Cleanup adoption did not create a deletion outbox item"
        )
    return record


async def registered_resource_blob(
    db: AsyncSession,
    *,
    organization_id: UUID,
    category: str,
    resource_type: str,
    resource_id: UUID,
    private_object_id: UUID,
) -> RegisteredBlob | None:
    """Resolve only an exact ready registry binding; never fall back on mismatch."""

    if private_object_category(resource_type) != category:
        raise PrivateObjectRegistryError("Private-object resource/category mismatch")
    record = (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.id == private_object_id,
                PrivateObject.organization_id == organization_id,
                PrivateObject.category == category,
                PrivateObject.resource_type == resource_type,
                PrivateObject.resource_id == resource_id,
                PrivateObject.state == "ready",
                PrivateObject.ready_at.is_not(None),
                or_(
                    PrivateObject.retention_until.is_(None),
                    PrivateObject.retention_until > datetime.now(UTC),
                ),
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    try:
        key = require_tenant_private_key(
            record.object_key,
            organization_id=organization_id,
            category=category,
        )
    except ValueError:
        # Treat corrupted metadata exactly like an unavailable object.  The key
        # must never reach a storage backend when its tenant prefix is invalid.
        return None
    return RegisteredBlob(
        id=record.id,
        key=key,
        original_filename=record.original_filename,
        media_type=record.media_type,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
    )


async def enqueue_private_object_deletion(
    db: AsyncSession,
    *,
    organization_id: UUID,
    private_object_id: UUID,
    reason: str,
    now: datetime | None = None,
    not_before: datetime | None = None,
) -> PrivateObjectDeletion | None:
    """Transactionally make an object unreadable and enqueue physical erasure.

    Locking the object serializes replacement/delete races.  The partial unique
    index on active outbox rows then makes repeated requests idempotent.
    """

    if reason not in _DELETION_REASONS:
        raise PrivateObjectRegistryError("Unsupported private-object deletion reason")
    requested_at = _utc(now)
    requested_eligibility = _utc(not_before) if not_before is not None else requested_at
    eligible_at = max(requested_at, requested_eligibility)
    record = (
        await db.execute(
            select(PrivateObject)
            .where(
                PrivateObject.id == private_object_id,
                PrivateObject.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    existing = (
        await db.execute(
            select(PrivateObjectDeletion)
            .where(
                PrivateObjectDeletion.organization_id == organization_id,
                PrivateObjectDeletion.private_object_id == private_object_id,
                PrivateObjectDeletion.completed_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    if record.state == "deleted":
        return None
    record.state = "delete_pending"
    deletion = PrivateObjectDeletion(
        id=uuid4(),
        organization_id=organization_id,
        private_object_id=private_object_id,
        object_key_snapshot=record.object_key,
        sha256_snapshot=record.sha256,
        reason=reason,
        requested_at=requested_at,
        not_before=eligible_at,
        next_attempt_at=eligible_at,
    )
    db.add(deletion)
    return deletion


def _retry_delay(attempt_count: int) -> timedelta:
    # The claim primitive increments attempt_count before processing.  Bound
    # retries at one hour; the schema dead-letters the item after ten attempts.
    seconds = min(3600, 30 * (2 ** max(0, min(attempt_count - 1, 7))))
    return timedelta(seconds=seconds)


async def settle_private_object_deletion(
    db: AsyncSession,
    *,
    deletion_id: UUID,
    organization_id: UUID,
    claim_token: UUID,
    store: BlobStore,
    now: datetime | None = None,
) -> DeletionSettlement:
    """Idempotently settle one already-claimed deletion in a tenant session.

    The caller must first validate the matching worker-queue lease.  This
    function additionally validates the tenant outbox claim token and object
    snapshot before any storage call.  A backend failure is persisted as a
    bounded retry without exposing provider text.  A successful physical
    delete followed by a database rollback is safe: object DELETE is
    idempotent and the next claim repeats it.
    """

    current = _utc(now)
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion)
            .where(
                PrivateObjectDeletion.id == deletion_id,
                PrivateObjectDeletion.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if deletion is None:
        raise StalePrivateObjectDeletionClaim("Private-object deletion is unavailable")
    if deletion.completed_at is not None:
        return DeletionSettlement.ALREADY_DELETED
    if deletion.claim_token != claim_token or deletion.claimed_at is None:
        raise StalePrivateObjectDeletionClaim("Private-object deletion claim is stale")

    record = (
        await db.execute(
            select(PrivateObject)
            .where(
                PrivateObject.id == deletion.private_object_id,
                PrivateObject.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    metadata_valid = (
        record is not None
        and record.state == "delete_pending"
        and record.legal_hold_at is None
        and record.object_key == deletion.object_key_snapshot
        and record.sha256 == deletion.sha256_snapshot
    )
    if not metadata_valid:
        deletion.claimed_at = None
        deletion.claim_token = None
        deletion.last_error_code = (
            "legal_hold"
            if record is not None and record.legal_hold_at
            else "metadata_mismatch"
        )
        deletion.next_attempt_at = current + _retry_delay(deletion.attempt_count)
        return DeletionSettlement.RETRY_SCHEDULED

    try:
        key = require_tenant_private_key(
            deletion.object_key_snapshot,
            organization_id=organization_id,
            category=record.category,
        )
    except ValueError:
        deletion.claimed_at = None
        deletion.claim_token = None
        deletion.last_error_code = "metadata_mismatch"
        deletion.next_attempt_at = current + _retry_delay(deletion.attempt_count)
        return DeletionSettlement.RETRY_SCHEDULED

    try:
        receipt = await store.delete(key)
    except BlobStoreError:
        deletion.claimed_at = None
        deletion.claim_token = None
        deletion.last_error_code = "blob_store_unavailable"
        deletion.next_attempt_at = current + _retry_delay(deletion.attempt_count)
        return DeletionSettlement.RETRY_SCHEDULED
    # Preserve compatibility with test/dummy stores written against the old
    # ``None`` return contract. Production local and S3 stores always return a
    # verification receipt.
    if receipt is None:
        receipt = blob_deletion_receipt(
            backend="compatibility",
            verification="delete_returned",
            key=key,
            versions_deleted=0,
            delete_markers_deleted=0,
        )

    record.state = "deleted"
    record.deleted_at = current
    # The tombstone retains only opaque reconciliation fields. Original names
    # commonly contain candidate PII and have no purpose after erasure.
    record.original_filename = "deleted"
    record.media_type = "application/octet-stream"
    record.scanner_engine = None
    record.scanner_version = None
    record.scanner_verdict = None
    deletion.completed_at = current
    deletion.claimed_at = None
    deletion.claim_token = None
    deletion.last_error_code = None
    deletion.next_attempt_at = current
    # A pending object remains part of current storage usage until physical
    # deletion succeeds.  Record the release in the same settlement
    # transaction; a crash after the object-store DELETE safely replays both.
    await record_usage(
        db,
        organization_id=organization_id,
        metric="storage_bytes",
        amount=-record.size_bytes,
        kind="refund",
        idempotency_key=f"private-object:delete:{deletion.id}",
        resource_type="private_object",
        resource_id=record.id,
        metadata={
            "reason": deletion.reason,
            "storage_deletion": {
                "backend": receipt.backend,
                "verification": receipt.verification,
                "versions_deleted": receipt.versions_deleted,
                "delete_markers_deleted": receipt.delete_markers_deleted,
                "verified_at": receipt.verified_at.isoformat(),
                "receipt_sha256": receipt.receipt_sha256,
            },
        },
        enforce_limit=False,
    )
    return DeletionSettlement.DELETED


__all__ = [
    "DeletionSettlement",
    "PrivateObjectRegistryError",
    "RegisteredBlob",
    "StalePrivateObjectDeletionClaim",
    "adopt_legacy_private_object_for_erasure",
    "enqueue_private_object_deletion",
    "private_object_category",
    "register_ready_private_object",
    "registered_resource_blob",
    "settle_private_object_deletion",
]
