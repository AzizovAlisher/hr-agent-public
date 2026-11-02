"""Crash, retry, idempotency, and tenant binding for legacy-file erasure."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models.organization import Organization
from app.models.private_object import LegacyPrivateObjectDeletion
from app.services.legacy_private_object_worker import (
    process_legacy_private_object_deletions_once,
)
from app.storage import BlobStoreError, LocalBlobStore
from app.storage.legacy import (
    LegacyStorageUnavailableError,
    UnsafeLegacyPrivateObjectError,
    configured_legacy_storage_root,
    enqueue_legacy_private_object_deletion,
)

pytestmark = pytest.mark.asyncio


class DeleteThenFailStore(LocalBlobStore):
    async def delete_if_snapshot_matches(self, key: str, **kwargs):
        await super().delete_if_snapshot_matches(key, **kwargs)
        raise BlobStoreError("simulated acknowledgement loss")


async def test_missing_explicit_legacy_mount_fails_without_creating_it(
    tmp_path,
    monkeypatch,
):
    missing = tmp_path / "not-mounted" / "legacy"
    monkeypatch.setattr(
        get_settings(),
        "legacy_private_storage_dir",
        str(missing),
    )
    with pytest.raises(LegacyStorageUnavailableError):
        configured_legacy_storage_root()
    assert not missing.exists()


async def _organization(db, label: str) -> Organization:
    organization = Organization(
        name=label,
        slug=f"legacy-delete-{label.lower()}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.commit()
    return organization


def _legacy_task_file(root: Path, organization_id: UUID, payload: bytes) -> Path:
    path = root / "task_files" / str(organization_id) / f"{uuid4()}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _enqueue(db, organization_id: UUID, path: Path, store: LocalBlobStore):
    resource_id = uuid4()
    deletion = await enqueue_legacy_private_object_deletion(
        db,
        organization_id=organization_id,
        category="tasks",
        resource_type="task_submission",
        resource_id=resource_id,
        legacy_reference=path,
        reason="replacement",
        store=store,
    )
    assert deletion is not None
    return deletion


async def test_database_rollback_never_deletes_or_leaves_an_outbox_row(db, tmp_path):
    organization = await _organization(db, "Rollback")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"rollback-safe")

    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.rollback()

    assert path.exists()
    assert await db.get(LegacyPrivateObjectDeletion, deletion_id) is None


async def test_lost_storage_ack_retries_idempotently_and_completes(db, tmp_path):
    organization = await _organization(db, "LostAck")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"idempotent-delete")
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()
    now = datetime.now(UTC)

    first = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:lost-ack",
        mode="off",
        store=DeleteThenFailStore(store.root),
        now=now,
    )
    assert first["retried"] == 1
    assert not path.exists()

    second = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:idempotent-retry",
        mode="off",
        store=store,
        now=now + timedelta(seconds=31),
    )
    assert second["deleted"] == 1
    async with SessionLocal() as verify:
        settled = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert settled is not None
        assert settled.completed_at is not None
        assert settled.attempt_count == 2


async def test_detached_volume_empty_mountpoint_retries_then_reattach_deletes(
    db,
    tmp_path,
):
    organization = await _organization(db, "DetachedMount")
    root = tmp_path / "legacy-mounted"
    store = LocalBlobStore(root)
    path = _legacy_task_file(store.root, organization.id, b"must survive detach")
    relative = path.relative_to(root)
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()
    now = datetime.now(UTC)

    # Emulate an unmounted volume whose underlying mountpoint still exists.
    detached_volume = tmp_path / "detached-volume"
    root.rename(detached_volume)
    root.mkdir()
    first = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:detached-empty-mountpoint",
        mode="off",
        store=LocalBlobStore(root),
        now=now,
    )

    assert first["retried"] == 1
    assert (detached_volume / relative).read_bytes() == b"must survive detach"
    async with SessionLocal() as verify:
        pending = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert pending is not None
        assert pending.completed_at is None
        assert pending.last_error_code == "legacy_storage_unavailable"

    root.rmdir()
    detached_volume.rename(root)
    second = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:reattached-volume",
        mode="off",
        store=LocalBlobStore(root),
        now=now + timedelta(seconds=31),
    )

    assert second["deleted"] == 1
    assert (root / relative).exists() is False
    async with SessionLocal() as verify:
        completed = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert completed is not None
        assert completed.completed_at is not None


async def test_changed_file_is_never_deleted_and_enters_bounded_retry(db, tmp_path):
    organization = await _organization(db, "Mismatch")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"original")
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()
    path.write_bytes(b"replacement-by-operator")

    result = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:mismatch",
        mode="off",
        store=store,
    )
    assert result["retried"] == 1
    assert path.read_bytes() == b"replacement-by-operator"
    async with SessionLocal() as verify:
        pending = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert pending is not None
        assert pending.completed_at is None
        assert pending.last_error_code == "legacy_object_mismatch"


async def test_atomic_claim_never_deletes_a_concurrent_path_replacement(
    db,
    tmp_path,
    monkeypatch,
):
    organization = await _organization(db, "AtomicReplacement")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"original")
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()

    from app.storage import local as local_storage

    original_rename = local_storage.os.rename

    def replace_after_claim(source, destination, *, src_dir_fd, dst_dir_fd):
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        descriptor = local_storage.os.open(
            source,
            local_storage.os.O_WRONLY
            | local_storage.os.O_CREAT
            | local_storage.os.O_EXCL,
            0o600,
            dir_fd=src_dir_fd,
        )
        try:
            local_storage.os.write(descriptor, b"concurrent replacement")
        finally:
            local_storage.os.close(descriptor)

    monkeypatch.setattr(local_storage.os, "rename", replace_after_claim)
    result = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:atomic-replacement",
        mode="off",
        store=store,
    )

    assert result["retried"] == 1
    assert path.read_bytes() == b"concurrent replacement"
    async with SessionLocal() as verify:
        pending = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert pending is not None
        assert pending.completed_at is None
        assert pending.last_error_code == "legacy_object_mismatch"


async def test_atomic_claim_never_follows_a_late_symlink_swap(
    db,
    tmp_path,
    monkeypatch,
):
    organization = await _organization(db, "AtomicSymlink")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"original")
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()
    outside = tmp_path / "outside-private-bytes.txt"
    outside.write_bytes(b"must survive")

    from app.storage import local as local_storage

    original_rename = local_storage.os.rename
    swapped = False

    def symlink_before_claim(source, destination, *, src_dir_fd, dst_dir_fd):
        nonlocal swapped
        if not swapped:
            swapped = True
            local_storage.os.unlink(source, dir_fd=src_dir_fd)
            local_storage.os.symlink(outside, source, dir_fd=src_dir_fd)
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(local_storage.os, "rename", symlink_before_claim)
    result = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:atomic-symlink",
        mode="off",
        store=store,
    )

    assert result["retried"] == 1
    assert outside.read_bytes() == b"must survive"
    assert path.is_symlink() or any(path.parent.glob(f".{path.name}.*.legacy-delete"))
    async with SessionLocal() as verify:
        pending = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert pending is not None
        assert pending.completed_at is None
        assert pending.last_error_code == "legacy_object_mismatch"


async def test_atomic_claim_resumes_a_crash_tombstone(db, tmp_path):
    organization = await _organization(db, "CrashResume")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"crash-resumable")
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()
    tombstone = path.with_name(
        f".{path.name}.{deletion_id}.legacy-delete"
    )
    path.rename(tombstone)

    result = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy:crash-resume",
        mode="off",
        store=store,
    )

    assert result["deleted"] == 1
    assert path.exists() is False
    assert tombstone.exists() is False
    async with SessionLocal() as verify:
        settled = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert settled is not None and settled.completed_at is not None


async def test_concurrent_workers_claim_one_legacy_deletion(db, tmp_path):
    organization = await _organization(db, "Concurrent")
    store = LocalBlobStore(tmp_path / "legacy")
    path = _legacy_task_file(store.root, organization.id, b"one-delete")
    deletion = await _enqueue(db, organization.id, path, store)
    deletion_id = deletion.id
    await db.commit()
    now = datetime.now(UTC)

    left, right = await asyncio.gather(
        process_legacy_private_object_deletions_once(
            SessionLocal,
            worker_id="legacy:left",
            mode="off",
            limit=1,
            store=store,
            now=now,
        ),
        process_legacy_private_object_deletions_once(
            SessionLocal,
            worker_id="legacy:right",
            mode="off",
            limit=1,
            store=store,
            now=now,
        ),
    )
    assert sorted((left["claimed"], right["claimed"])) == [0, 1]
    assert left["deleted"] + right["deleted"] == 1
    assert not path.exists()
    async with SessionLocal() as verify:
        settled = await verify.get(LegacyPrivateObjectDeletion, deletion_id)
        assert settled is not None and settled.completed_at == now


async def test_path_and_resource_contract_fail_closed(db, tmp_path):
    first = await _organization(db, "FirstTenant")
    second = await _organization(db, "SecondTenant")
    store = LocalBlobStore(tmp_path / "legacy")
    other_path = _legacy_task_file(store.root, second.id, b"other tenant")

    with pytest.raises(UnsafeLegacyPrivateObjectError):
        await enqueue_legacy_private_object_deletion(
            db,
            organization_id=first.id,
            category="tasks",
            resource_type="candidate",
            resource_id=uuid4(),
            legacy_reference=other_path,
            reason="replacement",
            store=store,
        )
    with pytest.raises(UnsafeLegacyPrivateObjectError):
        await enqueue_legacy_private_object_deletion(
            db,
            organization_id=first.id,
            category="tasks",
            resource_type="task_submission",
            resource_id=uuid4(),
            legacy_reference=other_path,
            reason="replacement",
            store=store,
        )
    rows = list((await db.execute(select(LegacyPrivateObjectDeletion))).scalars())
    assert rows == []
    assert other_path.exists()
