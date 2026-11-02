"""Tenant-bound durable erasure for pre-registry local private files."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.private_object import LegacyPrivateObjectDeletion
from app.storage.base import BlobStoreError, validate_blob_key
from app.storage.local import LocalBlobStore

_RESOURCE_CATEGORY = {
    "candidate": "candidates",
    "assessment_file": "assessments",
    "task_submission": "tasks",
    "interview_recording": "recordings",
}
_CATEGORY_SUFFIXES = {
    "candidates": frozenset({".pdf", ".docx"}),
    "assessments": frozenset({".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"}),
    "tasks": frozenset({".pdf", ".docx", ".txt", ".md", ".csv"}),
    "recordings": frozenset({".m4a", ".mp3", ".mp4", ".ogg", ".wav", ".webm"}),
}
_DELETION_REASONS = {
    "candidate_erasure",
    "retention_expired",
    "resource_deleted",
    "replacement",
    "admin_request",
}
_MAX_BYTES = 15 * 1024 * 1024
_MAX_ATTEMPTS = 10


class LegacyPrivateObjectError(RuntimeError):
    """A legacy path could not satisfy the closed deletion contract."""


class LegacyStorageUnavailableError(LegacyPrivateObjectError):
    """The explicitly configured legacy storage mount is unavailable."""


class UnsafeLegacyPrivateObjectError(LegacyPrivateObjectError):
    """A legacy reference escaped its tenant/category namespace."""


@dataclass(frozen=True, slots=True)
class LegacyFileSnapshot:
    size_bytes: int
    sha256: str


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LegacyPrivateObjectError("Legacy deletion time must include a timezone")
    return current.astimezone(UTC)


def configured_legacy_storage_root() -> Path:
    """Resolve an explicit production mount or the local/test storage root."""

    settings = get_settings()
    configured = settings.legacy_private_storage_dir
    if configured is None:
        if settings.app_environment == "production":
            raise LegacyStorageUnavailableError(
                "Legacy private storage is not configured"
            )
        configured = settings.storage_dir
    try:
        root = Path(configured).resolve(strict=True)
    except OSError as exc:
        raise LegacyStorageUnavailableError(
            "Legacy private storage is unavailable"
        ) from exc
    if not root.is_dir() or root == Path(root.anchor) or len(root.parts) < 3:
        raise LegacyStorageUnavailableError("Legacy private storage root is unsafe")
    return root


def _expected_prefix(organization_id: UUID, category: str) -> tuple[str, ...]:
    if category == "candidates":
        return ("cvs",)
    if category == "assessments":
        return ("assessment_files", str(organization_id))
    if category == "tasks":
        return ("task_files", str(organization_id))
    if category == "recordings":
        return ("recordings", str(organization_id))
    raise UnsafeLegacyPrivateObjectError("Unsupported legacy object category")


def normalize_legacy_private_key(
    reference: str | Path,
    *,
    organization_id: UUID,
    category: str,
    root: Path | None = None,
) -> str:
    """Return one canonical relative legacy key after resolving symlinks."""

    try:
        storage_root = (root or configured_legacy_storage_root()).resolve(strict=True)
    except OSError as exc:
        raise LegacyStorageUnavailableError(
            "Legacy private storage is unavailable"
        ) from exc
    raw = Path(str(reference))
    # The oldest candidate rows were persisted relative to the API package as
    # ``storage/cvs/<uuid>.<ext>``.  Production mounts expose the contents of
    # that historical ``storage`` directory as the explicit legacy root.  Drop
    # exactly that one reviewed compatibility segment; do not accept arbitrary
    # root-name aliases or recursively strip repeated segments.
    if not raw.is_absolute() and raw.parts[:1] == ("storage",):
        raw = Path(*raw.parts[1:])
    try:
        relative_candidate = (
            raw.relative_to(storage_root) if raw.is_absolute() else raw
        )
    except ValueError as exc:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object is outside its storage root"
        ) from exc
    if any(part in {"", ".", ".."} for part in relative_candidate.parts):
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object key is invalid"
        )
    # Reject aliases even when they resolve to another file inside the mount.
    # A database reference must name the regular opaque file itself; accepting
    # a symlink would make later digest-bound deletion depend on mutable path
    # indirection. The content snapshot below still closes replacement races
    # between this lstat walk and the subsequent read.
    probe = storage_root
    for part in relative_candidate.parts:
        probe = probe / part
        try:
            metadata = probe.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise LegacyStorageUnavailableError(
                "Legacy private storage is unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeLegacyPrivateObjectError(
                "Legacy private object path contains a symbolic link"
            )
    candidate = storage_root / relative_candidate
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(storage_root)
    except (OSError, ValueError) as exc:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object is outside its storage root"
        ) from exc
    try:
        key = validate_blob_key(relative.as_posix())
    except ValueError as exc:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object key is invalid"
        ) from exc
    parts = Path(key).parts
    prefix = _expected_prefix(organization_id, category)
    if tuple(parts[:-1]) != prefix:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object does not match its tenant/category"
        )
    filename = Path(parts[-1])
    try:
        identifier = UUID(filename.stem)
    except ValueError as exc:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object name is not opaque"
        ) from exc
    if (
        filename.stem != str(identifier)
        or filename.suffix != filename.suffix.casefold()
    ):
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object name is not canonical"
        )
    if filename.suffix.casefold() not in _CATEGORY_SUFFIXES[category]:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object type is unsupported"
        )
    return key


async def snapshot_legacy_file(
    store: LocalBlobStore,
    key: str,
) -> LegacyFileSnapshot | None:
    """Hash one bounded file; repeat stat detects mutation during the read."""

    try:
        # A missing file beneath its still-present parent is an idempotently
        # absent object. A missing parent (most importantly, an empty exposed
        # mountpoint after the configured volume is detached) is storage
        # unavailability and must never be accepted as erasure proof.
        await store.assert_key_parent_available(key)
        before = await store.stat(key)
        if before is None:
            await store.assert_key_parent_available(key)
            return None
        if not 1 <= before.size_bytes <= _MAX_BYTES:
            raise UnsafeLegacyPrivateObjectError(
                "Legacy private object exceeds the validated size boundary"
            )
        digest = hashlib.sha256()
        observed = 0
        async for chunk in store.iter_bytes(key):
            observed += len(chunk)
            if observed > _MAX_BYTES:
                raise UnsafeLegacyPrivateObjectError(
                    "Legacy private object exceeds the validated size boundary"
                )
            digest.update(chunk)
        after = await store.stat(key)
        await store.assert_key_parent_available(key)
    except BlobStoreError as exc:
        raise LegacyStorageUnavailableError(
            "Legacy private storage is unavailable"
        ) from exc
    if after is None or observed != before.size_bytes or after.size_bytes != observed:
        raise LegacyStorageUnavailableError(
            "Legacy private object changed while it was inspected"
        )
    return LegacyFileSnapshot(size_bytes=observed, sha256=digest.hexdigest())


def get_legacy_blob_store(*, initialize: bool = True) -> LocalBlobStore:
    # Root existence is verified before LocalBlobStore can create anything.
    return LocalBlobStore(
        configured_legacy_storage_root(),
        initialize=initialize,
    )


async def enqueue_legacy_private_object_deletion(
    db: AsyncSession,
    *,
    organization_id: UUID,
    category: str,
    resource_type: str,
    resource_id: UUID,
    legacy_reference: str | Path,
    reason: str,
    now: datetime | None = None,
    not_before: datetime | None = None,
    store: LocalBlobStore | None = None,
) -> LegacyPrivateObjectDeletion | None:
    """Digest and enqueue without touching the file before database commit."""

    if _RESOURCE_CATEGORY.get(resource_type) != category:
        raise UnsafeLegacyPrivateObjectError(
            "Legacy private object resource/category mismatch"
        )
    if reason not in _DELETION_REASONS:
        raise LegacyPrivateObjectError("Unsupported legacy deletion reason")
    requested_at = _utc(now)
    eligible_at = max(
        requested_at,
        _utc(not_before) if not_before is not None else requested_at,
    )
    backend = store or get_legacy_blob_store()
    key = normalize_legacy_private_key(
        legacy_reference,
        organization_id=organization_id,
        category=category,
        root=backend.root,
    )
    snapshot = await snapshot_legacy_file(backend, key)
    if snapshot is None:
        return None
    existing = (
        await db.execute(
            select(LegacyPrivateObjectDeletion)
            .where(LegacyPrivateObjectDeletion.normalized_key == key)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        expected = (
            existing.organization_id == organization_id
            and existing.category == category
            and existing.resource_type == resource_type
            and existing.resource_id == resource_id
            and existing.size_bytes == snapshot.size_bytes
            and existing.sha256_snapshot == snapshot.sha256
        )
        if not expected or existing.completed_at is not None:
            raise UnsafeLegacyPrivateObjectError(
                "Legacy private object is already bound to different metadata"
            )
        existing.not_before = min(existing.not_before, eligible_at)
        existing.next_attempt_at = min(existing.next_attempt_at, eligible_at)
        return existing
    deletion = LegacyPrivateObjectDeletion(
        id=uuid4(),
        organization_id=organization_id,
        category=category,
        normalized_key=key,
        resource_type=resource_type,
        resource_id=resource_id,
        size_bytes=snapshot.size_bytes,
        sha256_snapshot=snapshot.sha256,
        reason=reason,
        requested_at=requested_at,
        not_before=eligible_at,
        next_attempt_at=eligible_at,
    )
    db.add(deletion)
    await db.flush([deletion])
    return deletion


def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(3600, 30 * (2 ** max(0, min(attempt_count - 1, 7))))
    return timedelta(seconds=seconds)


async def settle_legacy_private_object_deletion(
    db: AsyncSession,
    *,
    deletion_id: UUID,
    organization_id: UUID,
    claim_token: UUID,
    store: LocalBlobStore | None = None,
    now: datetime | None = None,
) -> str:
    """Verify the digest, delete idempotently, and persist bounded retry state."""

    current = _utc(now)
    deletion = (
        await db.execute(
            select(LegacyPrivateObjectDeletion)
            .where(
                LegacyPrivateObjectDeletion.id == deletion_id,
                LegacyPrivateObjectDeletion.organization_id == organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if deletion is None:
        raise LegacyPrivateObjectError("Legacy deletion is unavailable")
    if deletion.completed_at is not None:
        return "already_deleted"
    if deletion.claim_token != claim_token or deletion.claimed_at is None:
        raise LegacyPrivateObjectError("Legacy deletion claim is stale")
    try:
        backend = store or get_legacy_blob_store()
        key = normalize_legacy_private_key(
            deletion.normalized_key,
            organization_id=organization_id,
            category=deletion.category,
            root=backend.root,
        )
        outcome = await backend.delete_if_snapshot_matches(
            key,
            expected_size_bytes=deletion.size_bytes,
            expected_sha256=deletion.sha256_snapshot,
            claim_id=deletion.id,
        )
        if outcome == "mismatch":
            raise UnsafeLegacyPrivateObjectError(
                "Legacy private object no longer matches its deletion snapshot"
            )
    except UnsafeLegacyPrivateObjectError:
        deletion.claimed_at = None
        deletion.claim_token = None
        deletion.last_error_code = "legacy_object_mismatch"
        deletion.next_attempt_at = current + _retry_delay(deletion.attempt_count)
        return "retry_scheduled"
    except (LegacyStorageUnavailableError, BlobStoreError):
        deletion.claimed_at = None
        deletion.claim_token = None
        deletion.last_error_code = "legacy_storage_unavailable"
        deletion.next_attempt_at = current + _retry_delay(deletion.attempt_count)
        return "retry_scheduled"
    deletion.completed_at = current
    deletion.claimed_at = None
    deletion.claim_token = None
    deletion.last_error_code = None
    return "deleted"


__all__ = [
    "LegacyFileSnapshot",
    "LegacyPrivateObjectError",
    "LegacyStorageUnavailableError",
    "UnsafeLegacyPrivateObjectError",
    "configured_legacy_storage_root",
    "enqueue_legacy_private_object_deletion",
    "get_legacy_blob_store",
    "normalize_legacy_private_key",
    "settle_legacy_private_object_deletion",
    "snapshot_legacy_file",
]
