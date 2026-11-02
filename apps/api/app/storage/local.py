"""Owner-only local BlobStore used by tests and single-node development."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from app.storage.base import (
    BlobDeletionReceipt,
    BlobEntry,
    BlobListPage,
    BlobNotFoundError,
    BlobStat,
    BlobStore,
    BlobStoreError,
    blob_deletion_receipt,
    validate_blob_key,
    validate_blob_prefix,
)


class LocalBlobStore(BlobStore):
    def __init__(self, root: str | Path, *, initialize: bool = True):
        candidate = Path(root)
        if initialize:
            self.root = candidate.resolve()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self.root.chmod(0o700)
            except OSError:
                pass
        else:
            try:
                self.root = candidate.resolve(strict=True)
            except OSError as exc:
                raise BlobStoreError("Local object root is unavailable") from exc
            if not self.root.is_dir():
                raise BlobStoreError("Local object root is unavailable")
        try:
            root_metadata = self.root.stat()
        except OSError as exc:
            raise BlobStoreError("Local object root is unavailable") from exc
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise BlobStoreError("Local object root is unavailable")
        # A long-lived legacy worker can otherwise keep using the path after a
        # mounted volume is detached and the empty underlying mountpoint is
        # exposed.  The parent-directory probe below also detects a worker
        # instantiated after that detach; this identity closes the in-process
        # swap case without imposing mount semantics on ordinary BlobStore
        # ``stat`` calls.
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)

    def _path(self, key: str) -> Path:
        validated = validate_blob_key(key)
        path = (self.root / validated).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Object key escapes the storage root")
        return path

    def _open_verified_directory(self, parts: tuple[str, ...]) -> int:
        """Open one existing directory chain beneath the captured root.

        Missing object *parents* are an availability failure, not proof that a
        legacy object was erased.  This distinction is important for mounted
        legacy volumes: after a detach, the configured mountpoint can remain as
        an empty directory and make every old object appear absent.
        """

        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd: int | None = None
        try:
            try:
                directory_fd = os.open(self.root, directory_flags)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise BlobStoreError("Local object root is unavailable") from exc
            root_metadata = os.fstat(directory_fd)
            if (
                root_metadata.st_dev,
                root_metadata.st_ino,
            ) != self._root_identity:
                raise BlobStoreError("Local object root identity changed")
            for part in parts:
                try:
                    child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                except (FileNotFoundError, NotADirectoryError) as exc:
                    raise BlobStoreError(
                        "Local object parent directory is unavailable"
                    ) from exc
                os.close(directory_fd)
                directory_fd = child_fd
            return directory_fd
        except BaseException:
            if directory_fd is not None:
                os.close(directory_fd)
            raise

    async def assert_key_parent_available(self, key: str) -> None:
        """Fail if the captured root or the key's parent chain disappeared."""

        validated = validate_blob_key(key)
        parent_parts = tuple(Path(validated).parts[:-1])

        def _probe() -> None:
            directory_fd = self._open_verified_directory(parent_parts)
            os.close(directory_fd)

        await asyncio.to_thread(_probe)

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str,
    ) -> BlobStat:
        destination = self._path(key)

        def _copy() -> int:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(
                f".{destination.name}.{os.urandom(8).hex()}.tmp"
            )
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with (
                    os.fdopen(descriptor, "wb") as output,
                    source.open("rb") as incoming,
                ):
                    shutil.copyfileobj(incoming, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                return destination.stat().st_size
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        try:
            size = await asyncio.to_thread(_copy)
        except OSError as exc:
            raise BlobStoreError("Local object write failed") from exc
        return BlobStat(
            key=validate_blob_key(key), size_bytes=size, content_type=content_type
        )

    async def promote(self, source_key: str, destination_key: str) -> None:
        source = self._path(source_key)
        destination = self._path(destination_key)

        def _promote() -> None:
            if not source.is_file():
                raise BlobNotFoundError("Quarantined object is unavailable")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(source, destination)

        try:
            await asyncio.to_thread(_promote)
        except BlobNotFoundError:
            raise
        except OSError as exc:
            raise BlobStoreError("Local object promotion failed") from exc

    async def delete(self, key: str) -> BlobDeletionReceipt:
        path = self._path(key)

        def _delete() -> bool:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

        try:
            existed = await asyncio.to_thread(_delete)
        except OSError as exc:
            raise BlobStoreError("Local object deletion failed") from exc
        # Re-check rather than treating a successful unlink syscall as the
        # erasure boundary. This also makes an already-absent retry successful.
        if await self.stat(key) is not None:
            raise BlobStoreError("Local object deletion could not be verified")
        return blob_deletion_receipt(
            backend="local",
            verification="path_absent",
            key=key,
            versions_deleted=1 if existed else 0,
            delete_markers_deleted=0,
        )

    async def delete_if_snapshot_matches(
        self,
        key: str,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
        claim_id: UUID,
    ) -> Literal["deleted", "absent", "mismatch"]:
        """Atomically claim and digest-bind one legacy local-file deletion.

        Renaming the directory entry to a deterministic, same-directory
        tombstone closes the check-then-unlink race: a concurrent replacement
        at the original name is never the object we unlink. Directory-FD and
        ``O_NOFOLLOW`` operations also keep a late symlink swap from redirecting
        the erasure outside the reviewed storage tree. A crash after the rename
        is resumable through the stable claim tombstone.
        """

        validated = validate_blob_key(key)
        if not 1 <= expected_size_bytes <= 15 * 1024 * 1024:
            raise BlobStoreError("Legacy deletion snapshot size is invalid")
        if (
            len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise BlobStoreError("Legacy deletion snapshot digest is invalid")
        parts = Path(validated).parts
        filename = parts[-1]
        tombstone = f".{filename}.{claim_id}.legacy-delete"

        def _delete_claimed() -> Literal["deleted", "absent", "mismatch"]:
            directory_fd: int | None = None
            try:
                directory_fd = self._open_verified_directory(parts[:-1])

                def _metadata(name: str) -> os.stat_result | None:
                    try:
                        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        return None

                if _metadata(tombstone) is None:
                    if _metadata(filename) is None:
                        return "absent"
                    try:
                        os.rename(
                            filename,
                            tombstone,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                    except FileNotFoundError:
                        # Another idempotent worker may have completed the
                        # claim between lstat and rename.
                        if _metadata(tombstone) is None:
                            return "absent"

                def _restore_or_quarantine() -> None:
                    # Hard-link creation is no-replace. If a concurrent writer
                    # already recreated the original name, retain the claimed
                    # entry under its tombstone for operator inspection rather
                    # than overwriting or deleting either object.
                    try:
                        os.link(
                            tombstone,
                            filename,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    except OSError:
                        return
                    try:
                        os.unlink(tombstone, dir_fd=directory_fd)
                    except OSError:
                        # Both hard links still preserve the exact bytes.
                        return

                file_fd: int | None = None
                try:
                    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    file_fd = os.open(tombstone, file_flags, dir_fd=directory_fd)
                    before = os.fstat(file_fd)
                    if not stat.S_ISREG(before.st_mode):
                        _restore_or_quarantine()
                        return "mismatch"
                    digest = hashlib.sha256()
                    observed = 0
                    while True:
                        chunk = os.read(file_fd, 1024 * 1024)
                        if not chunk:
                            break
                        observed += len(chunk)
                        if observed > 15 * 1024 * 1024:
                            _restore_or_quarantine()
                            return "mismatch"
                        digest.update(chunk)
                    after = os.fstat(file_fd)
                except OSError:
                    _restore_or_quarantine()
                    return "mismatch"
                finally:
                    if file_fd is not None:
                        os.close(file_fd)

                stable = (
                    before.st_dev == after.st_dev
                    and before.st_ino == after.st_ino
                    and before.st_size == after.st_size == observed
                    and before.st_mtime_ns == after.st_mtime_ns
                    and before.st_ctime_ns == after.st_ctime_ns
                )
                matches = (
                    stable
                    and observed == expected_size_bytes
                    and digest.hexdigest() == expected_sha256
                )
                claimed = _metadata(tombstone)
                if (
                    not matches
                    or claimed is None
                    or claimed.st_dev != before.st_dev
                    or claimed.st_ino != before.st_ino
                ):
                    _restore_or_quarantine()
                    return "mismatch"

                os.unlink(tombstone, dir_fd=directory_fd)
                os.fsync(directory_fd)
                # A writer that recreated the original key after our atomic
                # claim owns different, unverified bytes. Leave it untouched
                # and keep the outbox open for explicit investigation.
                return "mismatch" if _metadata(filename) is not None else "deleted"
            except OSError as exc:
                raise BlobStoreError("Atomic local object deletion failed") from exc
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)

        return await asyncio.to_thread(_delete_claimed)

    async def stat(self, key: str) -> BlobStat | None:
        path = self._path(key)

        def _stat() -> int | None:
            try:
                metadata = path.stat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise BlobStoreError("Local object metadata read failed") from exc
            return metadata.st_size if stat.S_ISREG(metadata.st_mode) else None

        size = await asyncio.to_thread(_stat)
        return (
            None
            if size is None
            else BlobStat(key=validate_blob_key(key), size_bytes=size)
        )

    async def list_prefix(
        self,
        prefix: str,
        *,
        limit: int = 1000,
        continuation: str | None = None,
        start_after: str | None = None,
    ) -> BlobListPage:
        validated_prefix = validate_blob_prefix(prefix)
        if not 1 <= limit <= 1000:
            raise ValueError("List limit must be between 1 and 1000")
        if continuation is not None and start_after is not None:
            raise ValueError("Continuation and start-after are mutually exclusive")
        lexical_cursor = continuation or start_after
        if lexical_cursor is not None:
            validate_blob_key(lexical_cursor)
            if not lexical_cursor.startswith(validated_prefix):
                raise ValueError("Object-list cursor does not match object prefix")

        def _list() -> BlobListPage:
            prefix_root = self._path(validated_prefix[:-1])
            if not prefix_root.exists():
                return BlobListPage(())
            try:
                keys = sorted(
                    path.relative_to(self.root).as_posix()
                    for path in prefix_root.rglob("*")
                    if path.is_file()
                )
                if lexical_cursor is not None:
                    keys = [key for key in keys if key > lexical_cursor]
                page_keys = keys[: limit + 1]
                truncated = len(page_keys) > limit
                page_keys = page_keys[:limit]
                entries = tuple(
                    BlobEntry(
                        key=validate_blob_key(key),
                        size_bytes=(self.root / key).stat().st_size,
                        modified_at=datetime.fromtimestamp(
                            (self.root / key).stat().st_mtime,
                            tz=UTC,
                        ),
                    )
                    for key in page_keys
                )
            except OSError as exc:
                raise BlobStoreError("Local object listing failed") from exc
            return BlobListPage(
                entries,
                continuation=page_keys[-1] if truncated and page_keys else None,
            )

        return await asyncio.to_thread(_list)

    async def iter_bytes(
        self,
        key: str,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not path.is_file():
            raise BlobNotFoundError("Object is unavailable")
        try:
            stream = path.open("rb")
        except FileNotFoundError as exc:
            raise BlobNotFoundError("Object is unavailable") from exc
        except OSError as exc:
            raise BlobStoreError("Local object read failed") from exc
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(stream.read, chunk_size)
                except OSError as exc:
                    raise BlobStoreError("Local object read failed") from exc
                if not chunk:
                    break
                yield chunk
        finally:
            stream.close()

    def local_path(self, key: str) -> Path | None:
        path = self._path(key)
        return path if path.is_file() else None

    @asynccontextmanager
    async def materialize(self, key: str) -> AsyncIterator[Path]:
        path = self._path(key)
        if not path.is_file():
            raise BlobNotFoundError("Object is unavailable")
        yield path
