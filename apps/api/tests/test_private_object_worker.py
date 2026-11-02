"""Durable private-object deletion, retry, and concurrency contracts."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.db import SessionLocal
from app.models.operations import UsageLedgerEntry, WorkerQueueLease
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.services.private_object_worker import process_private_object_deletions_once
from app.storage import BlobStoreError, LocalBlobStore, S3BlobStore, private_object_key


pytestmark = pytest.mark.asyncio


class FailingDeleteStore(LocalBlobStore):
    async def delete(self, key: str) -> None:
        raise BlobStoreError("simulated storage outage")


async def _seed_deletion(
    db,
    tmp_path: Path,
    *,
    label: str,
) -> tuple[Organization, PrivateObject, PrivateObjectDeletion, LocalBlobStore]:
    marker = uuid4().hex[:10]
    organization = Organization(name=label, slug=f"{label.lower()}-{marker}")
    db.add(organization)
    await db.flush()
    object_id = uuid4()
    key = private_object_key(organization.id, "tasks", object_id, ".bin")
    payload = f"private:{marker}".encode()
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
        organization_id=organization.id,
        private_object_id=object_id,
        object_key_snapshot=key,
        sha256_snapshot=private_object.sha256,
        reason="resource_deleted",
    )
    db.add_all([private_object, deletion])
    await db.commit()

    store = LocalBlobStore(tmp_path / marker)
    source = tmp_path / f"{marker}.bin"
    source.write_bytes(payload)
    await store.put_file(key, source, content_type=private_object.media_type)
    source.unlink()
    return organization, private_object, deletion, store


async def test_deletion_updates_storage_registry_and_outbox_atomically(db, tmp_path):
    organization, private_object, deletion, store = await _seed_deletion(
        db,
        tmp_path,
        label="DeleteSuccess",
    )
    organization_id = organization.id
    object_id = private_object.id
    deletion_id = deletion.id
    object_key = private_object.object_key
    now = datetime.now(UTC)

    result = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="delete:success",
        mode="off",
        store=store,
        now=now,
    )

    assert result == {
        "claimed": 1,
        "deleted": 1,
        "retried": 0,
        "dead_lettered": 0,
        "deferred": 0,
        "abandoned": 0,
    }
    assert await store.stat(object_key) is None
    async with SessionLocal() as verify:
        stored_object = await verify.get(PrivateObject, object_id)
        stored_deletion = await verify.get(PrivateObjectDeletion, deletion_id)
        receipt_entry = (
            await verify.execute(
                select(UsageLedgerEntry).where(
                    UsageLedgerEntry.idempotency_key
                    == f"private-object:delete:{deletion_id}"
                )
            )
        ).scalar_one()
    assert stored_object is not None
    assert stored_object.organization_id == organization_id
    assert stored_object.state == "deleted"
    assert stored_object.deleted_at == now
    assert stored_deletion is not None
    assert stored_deletion.completed_at == now
    assert stored_deletion.attempt_count == 1
    assert stored_deletion.claimed_at is None
    assert stored_deletion.claim_token is None
    assert receipt_entry.metadata_json["storage_deletion"]["backend"] == "local"
    assert (
        receipt_entry.metadata_json["storage_deletion"]["verification"]
        == "path_absent"
    )
    assert receipt_entry.metadata_json["storage_deletion"]["versions_deleted"] == 1
    assert len(receipt_entry.metadata_json["storage_deletion"]["receipt_sha256"]) == 64


async def test_storage_failure_records_bounded_retry_and_releases_lease(db, tmp_path):
    _organization, private_object, deletion, store = await _seed_deletion(
        db,
        tmp_path,
        label="DeleteRetry",
    )
    deletion_id = deletion.id
    object_key = private_object.object_key
    now = datetime.now(UTC)
    failing = FailingDeleteStore(store.root)

    result = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="delete:retry",
        mode="off",
        store=failing,
        now=now,
    )

    assert result["retried"] == 1
    assert await store.stat(object_key) is not None
    async with SessionLocal() as verify:
        stored = await verify.get(PrivateObjectDeletion, deletion_id)
    assert stored is not None
    assert stored.attempt_count == 1
    assert stored.claimed_at is None
    assert stored.claim_token is None
    assert stored.last_error_code == "blob_store_unavailable"
    assert stored.next_attempt_at == now + timedelta(seconds=30)
    assert (
        await db.execute(
            select(WorkerQueueLease).where(
                WorkerQueueLease.queue_name == "private_object_deletions",
                WorkerQueueLease.work_id == deletion_id,
            )
        )
    ).scalar_one_or_none() is None


async def test_partial_version_purge_stays_pending_then_retries_to_verified_receipt(
    db,
    tmp_path,
):
    _organization, private_object, deletion, _local = await _seed_deletion(
        db,
        tmp_path,
        label="DeletePartialVersion",
    )
    object_id = private_object.id
    deletion_id = deletion.id
    key = private_object.object_key
    entries = {"new-version": False, "old-version": False, "marker": True}
    fail_once = True

    def listing() -> bytes:
        fragments = []
        for version_id, is_marker in entries.items():
            tag = "DeleteMarker" if is_marker else "Version"
            fragments.append(
                f"<{tag}><Key>{key}</Key><VersionId>{version_id}</VersionId>"
                f"</{tag}>"
            )
        return (
            '<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Prefix>{key}</Prefix>{''.join(fragments)}"
            "<IsTruncated>false</IsTruncated></ListVersionsResult>"
        ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_once
        if request.method == "GET":
            return httpx.Response(200, content=listing())
        if request.method == "DELETE":
            version_id = request.url.params.get("versionId")
            if version_id == "old-version" and fail_once:
                fail_once = False
                return httpx.Response(503)
            if version_id is not None:
                entries.pop(version_id, None)
                return httpx.Response(204)
        return httpx.Response(500)

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="hr-agent-private",
        access_key="access",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )
    now = datetime.now(UTC)

    first = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="delete:partial:first",
        mode="off",
        store=store,
        now=now,
    )

    assert first["retried"] == 1
    assert entries == {"old-version": False, "marker": True}
    async with SessionLocal() as verify:
        pending_object = await verify.get(PrivateObject, object_id)
        pending_deletion = await verify.get(PrivateObjectDeletion, deletion_id)
    assert pending_object is not None and pending_object.state == "delete_pending"
    assert pending_deletion is not None and pending_deletion.completed_at is None
    assert pending_deletion.last_error_code == "blob_store_unavailable"

    second = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="delete:partial:retry",
        mode="off",
        store=store,
        now=now + timedelta(seconds=31),
    )

    assert second["deleted"] == 1
    assert entries == {}
    async with SessionLocal() as verify:
        deleted_object = await verify.get(PrivateObject, object_id)
        completed_deletion = await verify.get(PrivateObjectDeletion, deletion_id)
        receipt_entry = (
            await verify.execute(
                select(UsageLedgerEntry).where(
                    UsageLedgerEntry.idempotency_key
                    == f"private-object:delete:{deletion_id}"
                )
            )
        ).scalar_one()
    assert deleted_object is not None and deleted_object.state == "deleted"
    assert completed_deletion is not None and completed_deletion.completed_at is not None
    receipt = receipt_entry.metadata_json["storage_deletion"]
    assert receipt["backend"] == "s3"
    assert receipt["verification"] == "version_listing_empty"
    assert receipt["versions_deleted"] == 1
    assert receipt["delete_markers_deleted"] == 1


async def test_tenth_storage_failure_enters_implicit_dead_letter(db, tmp_path):
    _organization, private_object, deletion, store = await _seed_deletion(
        db,
        tmp_path,
        label="DeleteDeadLetter",
    )
    deletion_id = deletion.id
    object_key = private_object.object_key
    deletion.attempt_count = 9
    await db.commit()
    now = datetime.now(UTC)

    result = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="delete:dead-letter",
        mode="off",
        store=FailingDeleteStore(store.root),
        now=now,
    )

    assert result["dead_lettered"] == 1
    assert await store.stat(object_key) is not None
    async with SessionLocal() as verify:
        stored = await verify.get(PrivateObjectDeletion, deletion_id)
    assert stored is not None
    assert stored.attempt_count == 10
    assert stored.completed_at is None
    assert stored.claimed_at is None
    assert stored.claim_token is None
    assert stored.last_error_code == "blob_store_unavailable"


async def test_two_workers_cannot_delete_the_same_object_twice(db, tmp_path):
    _organization, private_object, deletion, store = await _seed_deletion(
        db,
        tmp_path,
        label="DeleteConcurrent",
    )
    deletion_id = deletion.id
    object_key = private_object.object_key
    now = datetime.now(UTC)

    left, right = await asyncio.gather(
        process_private_object_deletions_once(
            SessionLocal,
            worker_id="delete:left",
            mode="off",
            limit=1,
            store=store,
            now=now,
        ),
        process_private_object_deletions_once(
            SessionLocal,
            worker_id="delete:right",
            mode="off",
            limit=1,
            store=store,
            now=now,
        ),
    )

    assert sorted([left["claimed"], right["claimed"]]) == [0, 1]
    assert left["deleted"] + right["deleted"] == 1
    assert await store.stat(object_key) is None
    async with SessionLocal() as verify:
        stored = await verify.get(PrivateObjectDeletion, deletion_id)
    assert stored is not None
    assert stored.completed_at == now
    assert stored.attempt_count == 1


async def test_forced_claim_function_drives_native_private_deletion_lease(db, tmp_path):
    available = await db.scalar(
        text(
            "SELECT to_regprocedure("
            "'security.claim_private_object_deletions(text,integer,timestamptz)')"
        )
    )
    if available is None:
        pytest.skip("Forced private deletion broker requires the migrated schema")
    _organization, private_object, deletion, store = await _seed_deletion(
        db,
        tmp_path,
        label="DeleteForced",
    )
    deletion_id = deletion.id
    object_key = private_object.object_key
    now = datetime.now(UTC)

    result = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="delete:forced",
        mode="forced",
        store=store,
        now=now,
    )

    assert result["claimed"] == 1
    assert result["deleted"] == 1
    async with SessionLocal() as verify:
        stored = await verify.get(PrivateObjectDeletion, deletion_id)
    assert stored is not None
    assert stored.completed_at == now
    assert stored.attempt_count == 1
    assert await store.stat(object_key) is None
