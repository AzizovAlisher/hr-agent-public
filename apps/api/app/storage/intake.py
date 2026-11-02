"""Bounded quarantine → validate → scan → atomic-promote upload pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from fastapi import UploadFile

from app.storage.base import (
    BlobStore,
    BlobStoreError,
    private_object_key,
    public_branding_key,
    quarantine_object_key,
)
from app.storage.scanner import MalwareScanner, ScanResult
from app.storage.validation import (
    UploadValidationError,
    canonical_media_type,
    validate_upload_path_bounded,
)

Inspection = TypeVar("Inspection")


class UploadTooLargeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredUpload(Generic[Inspection]):
    key: str
    original_filename: str
    suffix: str
    media_type: str
    size_bytes: int
    sha256: str
    scan: ScanResult
    inspection: Inspection | None = None


def safe_original_filename(value: str | None) -> str:
    raw = Path((value or "attachment").replace("\x00", "")).name
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", "_", raw).strip(" .")
    return (cleaned or "attachment")[:255]


async def cleanup_failed_intake_key(
    store: BlobStore,
    key: str,
    *,
    preserve_cancellation: bool = True,
) -> None:
    """Obtain verified deletion even when the first acknowledgement is lost.

    Cancellation is a ``BaseException`` and a storage provider can surface it
    after the delete already landed. Retrying the versioned/random intake key
    is idempotent and lets the backend return its authoritative deletion
    receipt. A plain metadata ``stat`` is deliberately insufficient for a
    versioned store, where the current key can be hidden while old versions
    remain recoverable.
    """

    last_error: BaseException | None = None
    cancellation: asyncio.CancelledError | None = None
    for _attempt in range(3):
        try:
            await store.delete(key)
        except BaseException as exc:  # Includes task/provider cancellation.
            last_error = exc
            if isinstance(exc, asyncio.CancelledError):
                cancellation = cancellation or exc
        else:
            if cancellation is not None and preserve_cancellation:
                raise cancellation
            return
    raise BlobStoreError(
        "Failed intake object cleanup could not be verified"
    ) from last_error


async def quarantine_scan_promote(
    upload: UploadFile,
    *,
    organization_id: UUID,
    category: str,
    allowed_suffixes: set[str],
    max_bytes: int,
    store: BlobStore,
    scanner: MalwareScanner,
    inspector: Callable[[Path, str], Inspection] | None = None,
    public_branding: bool = False,
    chunk_size: int = 1024 * 1024,
) -> StoredUpload[Inspection]:
    """Store no unscanned byte in a final namespace.

    The inbound stream is copied to an owner-only spool with an explicit byte
    ceiling and incremental digest. A quarantine object exists before scanner
    invocation. Any validation, scanner, parser, promotion, or caller-facing
    failure removes quarantine and spool; no final key is returned until atomic
    promotion succeeds.
    """

    if max_bytes < 1 or max_bytes > 15 * 1024 * 1024:
        raise ValueError("Upload limit must be between 1 byte and 15 MiB")
    object_id = uuid4()
    quarantine_key = quarantine_object_key(organization_id, category, object_id)
    original_filename = safe_original_filename(upload.filename)
    declared_media_type = (
        (upload.content_type or "application/octet-stream")
        .split(";", 1)[0]
        .strip()
        .casefold()
        or "application/octet-stream"
    )[:255]
    descriptor, raw_path = tempfile.mkstemp(prefix="hr-upload-", suffix=".spool")
    spool = Path(raw_path)
    os.chmod(spool, 0o600)
    total = 0
    digest = hashlib.sha256()
    final_key: str | None = None
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                read_size = min(chunk_size, max_bytes + 1 - total)
                if read_size <= 0:
                    read_size = 1
                chunk = await upload.read(read_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLargeError(
                        f"Upload exceeds the {max_bytes}-byte limit"
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())

        suffix = await validate_upload_path_bounded(
            spool,
            filename=original_filename,
            media_type=declared_media_type,
            allowed_suffixes=allowed_suffixes,
        )
        safe_media_type = canonical_media_type(suffix)
        await store.put_file(
            quarantine_key,
            spool,
            content_type=safe_media_type,
        )
        scan = await scanner.scan(spool)
        inspection = (
            await asyncio.to_thread(inspector, spool, suffix)
            if inspector is not None
            else None
        )
        final_key = (
            public_branding_key(organization_id, object_id, suffix)
            if public_branding
            else private_object_key(organization_id, category, object_id, suffix)
        )
        # Shield keeps caller cancellation from interrupting a provider copy at
        # an unknowable point. If cancellation arrives, the child promotion is
        # drained below before both possible keys are erased and cancellation
        # is propagated to the caller.
        promotion = asyncio.create_task(store.promote(quarantine_key, final_key))
        try:
            await asyncio.shield(promotion)
        except BaseException:
            while not promotion.done():
                try:
                    await asyncio.shield(promotion)
                except BaseException:
                    continue
            raise
        stored = StoredUpload(
            key=final_key,
            original_filename=original_filename,
            suffix=suffix,
            media_type=safe_media_type,
            size_bytes=total,
            sha256=digest.hexdigest(),
            scan=scan,
            inspection=inspection,
        )
        # S3 promotion deliberately treats a quarantine-delete outage as a
        # recoverable copy success. Intake has a stronger boundary: before it
        # returns, the quarantine key is authoritatively absent.
        await cleanup_failed_intake_key(store, quarantine_key)
        return stored
    except BaseException as exc:
        # A provider can create the destination and then return a copy error or
        # lose the response. Destination keys are random/versioned, so it is
        # safe and necessary to delete whenever promotion was attempted.
        cleanup_error: BaseException | None = None
        if final_key is not None:
            try:
                await cleanup_failed_intake_key(store, final_key)
            except BaseException as cleanup_exc:
                cleanup_error = cleanup_exc
        try:
            await cleanup_failed_intake_key(store, quarantine_key)
        except BaseException as cleanup_exc:
            cleanup_error = cleanup_error or cleanup_exc
        if cleanup_error is not None:
            if isinstance(exc, asyncio.CancelledError):
                exc.add_note(str(cleanup_error))
                raise
            raise cleanup_error from exc
        raise
    finally:
        try:
            spool.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "StoredUpload",
    "UploadTooLargeError",
    "UploadValidationError",
    "cleanup_failed_intake_key",
    "quarantine_scan_promote",
    "safe_original_filename",
]
