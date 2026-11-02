"""Backend-neutral object storage contracts and opaque key construction."""

from __future__ import annotations

import hashlib
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import UUID


class BlobStoreError(RuntimeError):
    """An object-store operation failed without exposing provider details."""


class BlobNotFoundError(BlobStoreError):
    pass


@dataclass(frozen=True, slots=True)
class BlobStat:
    key: str
    size_bytes: int
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class BlobEntry:
    key: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class BlobListPage:
    entries: tuple[BlobEntry, ...]
    continuation: str | None = None


@dataclass(frozen=True, slots=True)
class BlobDeletionReceipt:
    """Backend-neutral proof that one opaque key is no longer recoverable.

    The digest binds the backend verification result to the key without
    requiring deletion audit records to retain the key or provider version
    identifiers.  S3 implementations return only after an authoritative
    version listing is empty; local storage returns only after the path is
    absent.
    """

    backend: str
    verification: str
    versions_deleted: int
    delete_markers_deleted: int
    verified_at: datetime
    receipt_sha256: str


def blob_deletion_receipt(
    *,
    backend: str,
    verification: str,
    key: str,
    versions_deleted: int,
    delete_markers_deleted: int,
    verified_at: datetime | None = None,
) -> BlobDeletionReceipt:
    """Build a bounded receipt without exposing object/version identifiers."""

    validated = validate_blob_key(key)
    if not backend or len(backend) > 32 or not backend.replace("_", "").isalnum():
        raise ValueError("Invalid deletion receipt backend")
    if (
        not verification
        or len(verification) > 64
        or not verification.replace("_", "").isalnum()
    ):
        raise ValueError("Invalid deletion receipt verification")
    if versions_deleted < 0 or delete_markers_deleted < 0:
        raise ValueError("Invalid deletion receipt count")
    current = verified_at or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Deletion receipt time must include a timezone")
    current = current.astimezone(UTC)
    key_digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()
    material = "\n".join(
        (
            "blob-deletion-receipt-v1",
            backend,
            verification,
            key_digest,
            str(versions_deleted),
            str(delete_markers_deleted),
            current.isoformat(),
        )
    )
    return BlobDeletionReceipt(
        backend=backend,
        verification=verification,
        versions_deleted=versions_deleted,
        delete_markers_deleted=delete_markers_deleted,
        verified_at=current,
        receipt_sha256=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )


def validate_blob_key(value: str) -> str:
    """Return one normalized opaque key or fail before touching storage."""

    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Invalid object key")
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise ValueError("Invalid object key")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Invalid object key")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("Invalid object key")
    return normalized


def validate_blob_prefix(value: str) -> str:
    """Validate a bounded directory-like prefix used only for reconciliation."""

    if not isinstance(value, str) or not value.endswith("/"):
        raise ValueError("Invalid object prefix")
    validate_blob_key(value[:-1])
    return value


def private_object_key(
    organization_id: UUID,
    category: str,
    object_id: UUID,
    suffix: str,
) -> str:
    if not category.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Invalid private object category")
    if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise ValueError("Invalid object suffix")
    return validate_blob_key(
        f"private/{organization_id}/{category}/{object_id}{suffix.casefold()}"
    )


def quarantine_object_key(
    organization_id: UUID,
    category: str,
    object_id: UUID,
) -> str:
    if not category.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Invalid quarantine object category")
    return validate_blob_key(
        f"quarantine/{organization_id}/{category}/{object_id}.upload"
    )


def public_branding_key(
    organization_id: UUID,
    object_id: UUID,
    suffix: str,
) -> str:
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("Invalid public branding suffix")
    # Versioned keys avoid overwriting the currently committed logo before the
    # organization row points at the replacement.
    return validate_blob_key(f"logos/{organization_id}/{object_id}{suffix}")


def require_tenant_private_key(
    value: str,
    *,
    organization_id: UUID,
    category: str,
) -> str:
    key = validate_blob_key(value)
    prefix = f"private/{organization_id}/{category}/"
    if not key.startswith(prefix):
        raise ValueError("Object does not belong to the tenant/category")
    return key


class BlobStore(ABC):
    """Minimal store needed by intake, download, erasure, and scanners."""

    @abstractmethod
    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
    ) -> BlobStat: ...

    @abstractmethod
    async def promote(self, source_key: str, destination_key: str) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> BlobDeletionReceipt: ...

    @abstractmethod
    async def stat(self, key: str) -> BlobStat | None: ...

    @abstractmethod
    async def list_prefix(
        self,
        prefix: str,
        *,
        limit: int = 1000,
        continuation: str | None = None,
        start_after: str | None = None,
    ) -> BlobListPage: ...

    @abstractmethod
    def iter_bytes(
        self,
        key: str,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]: ...

    def public_url(self, key: str) -> str | None:
        return None

    def local_path(self, key: str) -> Path | None:
        return None

    @asynccontextmanager
    async def materialize(self, key: str) -> AsyncIterator[Path]:
        """Yield a private temporary path for parsers that require seeking."""

        validated = validate_blob_key(key)
        suffix = Path(validated).suffix
        descriptor, raw_path = tempfile.mkstemp(prefix="hr-blob-", suffix=suffix)
        path = Path(raw_path)
        os.chmod(path, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in self.iter_bytes(validated):
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            yield path
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def encoded_key_path(key: str) -> str:
    return quote(validate_blob_key(key), safe="/-_.~")
