"""Safe dry-run and bounded cleanup of orphaned private storage."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.ai_hiring import AiSession
from app.models.candidate import Candidate
from app.models.interview_recording import InterviewRecording
from app.models.organization import Organization
from app.models.private_object import (
    PrivateObject,
    PrivateObjectDeletion,
    PrivateStorageReconciliationCursor,
)
from app.storage import BlobStoreError, LocalBlobStore, S3BlobStore, private_object_key
from app.storage.reconciliation import reconcile_private_storage

pytestmark = pytest.mark.asyncio


async def _put(store: LocalBlobStore, tmp_path: Path, key: str, payload: bytes) -> None:
    source = tmp_path / f"source-{uuid4().hex}"
    source.write_bytes(payload)
    await store.put_file(key, source, content_type="text/plain")
    source.unlink()


async def test_reconciliation_dry_run_cleanup_outbox_and_legal_hold(db, tmp_path):
    organization = Organization(
        name="Reconciliation", slug=f"reconciliation-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    store = LocalBlobStore(tmp_path / "objects")
    now = datetime.now(UTC)
    stale = now - timedelta(days=2)

    orphan_id = uuid4()
    orphan_key = private_object_key(organization.id, "candidates", orphan_id, ".txt")
    held_id = uuid4()
    held_key = private_object_key(organization.id, "tasks", held_id, ".txt")
    orphan = PrivateObject(
        id=orphan_id,
        organization_id=organization.id,
        category="candidates",
        object_key=orphan_key,
        original_filename="orphan.txt",
        media_type="text/plain",
        size_bytes=6,
        sha256="a" * 64,
        state="ready",
        scanner_engine="fake",
        scanner_verdict="clean",
        resource_type="candidate",
        resource_id=uuid4(),
        ready_at=stale,
    )
    held = PrivateObject(
        id=held_id,
        organization_id=organization.id,
        category="tasks",
        object_key=held_key,
        original_filename="held.txt",
        media_type="text/plain",
        size_bytes=4,
        sha256="b" * 64,
        state="ready",
        scanner_engine="fake",
        scanner_verdict="clean",
        resource_type="task_submission",
        resource_id=uuid4(),
        ready_at=stale,
        retention_until=stale,
        legal_hold_at=stale,
    )
    db.add_all([orphan, held])
    await db.commit()
    await _put(store, tmp_path, orphan_key, b"orphan")
    await _put(store, tmp_path, held_key, b"held")

    loose_key = private_object_key(organization.id, "assessments", uuid4(), ".txt")
    quarantine_key = f"quarantine/{organization.id}/tasks/{uuid4()}.upload"
    await _put(store, tmp_path, loose_key, b"loose")
    await _put(store, tmp_path, quarantine_key, b"quarantine")
    old_epoch = stale.timestamp()
    os.utime((tmp_path / "objects" / loose_key), (old_epoch, old_epoch))
    os.utime((tmp_path / "objects" / quarantine_key), (old_epoch, old_epoch))

    preview = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=True,
        now=now,
        stale_after=timedelta(hours=24),
    )
    assert preview.orphaned_registry_objects == 2
    assert preview.expired_registry_objects == 1
    assert preview.registry_deletions_planned == 1
    assert preview.registry_deletions_enqueued == 0
    assert preview.legal_holds_skipped == 1
    assert preview.stale_quarantine_objects == 1
    assert preview.unregistered_private_objects == 1
    assert await store.stat(loose_key) is not None
    assert await store.stat(quarantine_key) is not None
    assert not list((await db.execute(select(PrivateObjectDeletion))).scalars())

    applied = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=False,
        now=now,
        stale_after=timedelta(hours=24),
    )
    await db.commit()
    assert applied.registry_deletions_enqueued == 1
    assert applied.quarantine_objects_deleted == 1
    assert applied.unregistered_private_objects_deleted == 1
    await db.refresh(orphan)
    await db.refresh(held)
    assert orphan.state == "delete_pending"
    assert held.state == "ready"
    assert await store.stat(loose_key) is None
    assert await store.stat(quarantine_key) is None
    assert await store.stat(held_key) is not None


async def test_expired_ai_attachment_uses_dedicated_category_and_outbox(
    db,
    tmp_path,
):
    organization = Organization(
        name="AI reconciliation",
        slug=f"ai-reconciliation-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    session = AiSession(
        organization_id=organization.id,
        status="active",
    )
    db.add(session)
    await db.flush()

    now = datetime.now(UTC)
    stale = now - timedelta(days=2)
    attachment_id = uuid4()
    key = private_object_key(organization.id, "ai", attachment_id, ".txt")
    attachment = PrivateObject(
        id=attachment_id,
        organization_id=organization.id,
        category="ai",
        object_key=key,
        original_filename="candidate.txt",
        media_type="text/plain",
        size_bytes=10,
        sha256="c" * 64,
        state="ready",
        scanner_engine="fake",
        scanner_verdict="clean",
        resource_type="ai_attachment",
        resource_id=session.id,
        ready_at=stale,
        retention_until=stale,
    )
    db.add(attachment)
    await db.commit()

    store = LocalBlobStore(tmp_path / "ai-objects")
    await _put(store, tmp_path, key, b"candidate\n")
    metrics = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=False,
        now=now,
        stale_after=timedelta(hours=24),
    )
    await db.commit()

    assert metrics.expired_registry_objects == 1
    assert metrics.orphaned_registry_objects == 0
    assert metrics.unsupported_resource_types == 0
    assert metrics.registry_deletions_enqueued == 1
    await db.refresh(attachment)
    assert attachment.state == "delete_pending"
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id == attachment.id
            )
        )
    ).scalar_one()
    assert deletion.reason == "retention_expired"
    assert await store.stat(key) is not None


async def test_expired_recording_is_recognized_and_uses_deletion_outbox(
    db,
    tmp_path,
):
    organization = Organization(
        name="Recording reconciliation",
        slug=f"recording-reconciliation-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        raw_cv="Recording reconciliation candidate",
    )
    db.add(candidate)
    await db.flush()
    recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=candidate.id,
        title="Reconciled interview",
        status="uploaded",
    )
    db.add(recording)
    await db.flush()

    now = datetime.now(UTC)
    stale = now - timedelta(days=2)
    object_id = uuid4()
    key = private_object_key(organization.id, "recordings", object_id, ".webm")
    recording_object = PrivateObject(
        id=object_id,
        organization_id=organization.id,
        category="recordings",
        object_key=key,
        original_filename="interview.webm",
        media_type="video/webm",
        size_bytes=9,
        sha256="e" * 64,
        state="ready",
        scanner_engine="fake",
        scanner_verdict="clean",
        resource_type="interview_recording",
        resource_id=recording.id,
        ready_at=stale,
        retention_until=stale,
    )
    db.add(recording_object)
    await db.commit()

    store = LocalBlobStore(tmp_path / "recording-objects")
    await _put(store, tmp_path, key, b"recording")
    metrics = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=False,
        now=now,
        stale_after=timedelta(hours=24),
    )
    await db.commit()

    assert metrics.expired_registry_objects == 1
    assert metrics.orphaned_registry_objects == 0
    assert metrics.unsupported_resource_types == 0
    assert metrics.unsafe_keys_skipped == 0
    assert metrics.registry_deletions_enqueued == 1
    await db.refresh(recording_object)
    assert recording_object.state == "delete_pending"
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id == recording_object.id
            )
        )
    ).scalar_one()
    assert deletion.reason == "retention_expired"
    assert await store.stat(key) is not None


async def test_reconciliation_fails_closed_on_resource_category_mismatch(
    db,
    tmp_path,
):
    organization = Organization(
        name="Unsafe reconciliation metadata",
        slug=f"unsafe-reconciliation-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    now = datetime.now(UTC)
    stale = now - timedelta(days=2)
    object_id = uuid4()
    key = private_object_key(organization.id, "tasks", object_id, ".txt")
    mismatched = PrivateObject(
        id=object_id,
        organization_id=organization.id,
        category="tasks",
        object_key=key,
        original_filename="mismatched.txt",
        media_type="text/plain",
        size_bytes=10,
        sha256="d" * 64,
        state="ready",
        scanner_engine="fake",
        scanner_verdict="clean",
        resource_type="candidate",
        resource_id=uuid4(),
        ready_at=stale,
        retention_until=stale,
    )
    db.add(mismatched)
    await db.commit()
    store = LocalBlobStore(tmp_path / "mismatched-objects")
    await _put(store, tmp_path, key, b"mismatch\n")

    metrics = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=False,
        now=now,
        stale_after=timedelta(hours=24),
    )
    await db.commit()

    assert metrics.expired_registry_objects == 1
    assert metrics.orphaned_registry_objects == 1
    assert metrics.unsafe_keys_skipped == 1
    assert metrics.registry_deletions_enqueued == 0
    await db.refresh(mismatched)
    assert mismatched.state == "ready"
    assert await store.stat(key) is not None


async def test_s3_listing_is_signed_bounded_and_tenant_prefixed():
    organization_id = uuid4()
    key = private_object_key(organization_id, "tasks", uuid4(), ".txt")
    start_after = private_object_key(
        organization_id,
        "tasks",
        UUID(int=0),
        ".txt",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params["list-type"] == "2"
        assert request.url.params["prefix"] == f"private/{organization_id}/"
        assert request.url.params["start-after"] == start_after
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
        return httpx.Response(
            200,
            content=(
                "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
                f"<Contents><Key>{key}</Key><LastModified>2026-08-20T00:00:00Z"
                "</LastModified><Size>4</Size></Contents><IsTruncated>false"
                "</IsTruncated></ListBucketResult>"
            ).encode(),
        )

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="private-files",
        access_key="access",
        secret_key="secret-value",
        transport=httpx.MockTransport(handler),
    )
    page = await store.list_prefix(
        f"private/{organization_id}/",
        limit=10,
        start_after=start_after,
    )
    assert [entry.key for entry in page.entries] == [key]
    assert page.continuation is None


async def test_s3_listing_rejects_non_monotonic_start_after_response():
    organization_id = uuid4()
    start_after = private_object_key(
        organization_id,
        "tasks",
        UUID(int=2),
        ".txt",
    )
    earlier = private_object_key(
        organization_id,
        "tasks",
        UUID(int=1),
        ".txt",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                "<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
                f"<Contents><Key>{earlier}</Key><LastModified>2026-08-20T00:00:00Z"
                "</LastModified><Size>4</Size></Contents><IsTruncated>false"
                "</IsTruncated></ListBucketResult>"
            ).encode(),
        )

    store = S3BlobStore(
        endpoint="https://minio.example.test",
        bucket="private-files",
        access_key="access",
        secret_key="secret-value",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BlobStoreError):
        await store.list_prefix(
            f"private/{organization_id}/",
            start_after=start_after,
        )


async def test_persisted_prefix_cursor_eventually_wraps_to_new_earlier_keys(
    db,
    tmp_path,
):
    organization = Organization(
        name="Cursor tenant",
        slug=f"cursor-tenant-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.commit()
    store = LocalBlobStore(tmp_path / "cursor-objects")
    now = datetime.now(UTC)
    stale = now - timedelta(days=2)

    def key(number: int) -> str:
        return private_object_key(
            organization.id,
            "tasks",
            UUID(int=number),
            ".txt",
        )

    for number in (100, 200, 300):
        await _put(store, tmp_path, key(number), str(number).encode())
        os.utime(store.root / key(number), (stale.timestamp(), stale.timestamp()))

    first = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=False,
        now=now,
        stale_after=timedelta(hours=24),
        max_objects=1,
    )
    await db.commit()
    assert first.unregistered_private_objects_deleted == 1
    assert await store.stat(key(100)) is None
    cursor = await db.get(
        PrivateStorageReconciliationCursor,
        (organization.id, "private"),
    )
    assert cursor is not None and cursor.cursor_key == key(100)

    # Arrives lexically behind the persisted cursor. It must be discovered
    # after the current bounded cycle reaches the end and wraps.
    await _put(store, tmp_path, key(50), b"earlier")
    os.utime(store.root / key(50), (stale.timestamp(), stale.timestamp()))
    deleted: list[int] = []
    for expected in (200, 300, 50):
        metrics = await reconcile_private_storage(
            db,
            organization_id=organization.id,
            store=store,
            dry_run=False,
            now=now,
            stale_after=timedelta(hours=24),
            max_objects=1,
        )
        await db.commit()
        assert metrics.unregistered_private_objects_deleted == 1
        assert await store.stat(key(expected)) is None
        deleted.append(expected)
    assert deleted == [200, 300, 50]
    cursor = await db.get(
        PrivateStorageReconciliationCursor,
        (organization.id, "private"),
    )
    assert cursor is not None
    assert cursor.cursor_key is None
    assert cursor.cycle_count >= 1
    assert cursor.last_completed_at == now


async def test_dry_run_does_not_create_or_advance_durable_cursors(db, tmp_path):
    organization = Organization(
        name="Dry-run cursor",
        slug=f"dry-run-cursor-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.commit()
    store = LocalBlobStore(tmp_path / "dry-run-cursor")
    now = datetime.now(UTC)
    stale = now - timedelta(days=2)
    keys = [
        private_object_key(organization.id, "tasks", UUID(int=value), ".txt")
        for value in (10, 20)
    ]
    for object_key in keys:
        await _put(store, tmp_path, object_key, b"stale")
        os.utime(
            store.root / object_key,
            (stale.timestamp(), stale.timestamp()),
        )

    preview = await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=True,
        now=now,
        stale_after=timedelta(hours=24),
        max_objects=1,
    )
    assert preview.unregistered_private_objects == 1
    assert (
        await db.execute(
            select(PrivateStorageReconciliationCursor).where(
                PrivateStorageReconciliationCursor.organization_id == organization.id
            )
        )
    ).scalars().all() == []

    await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=False,
        now=now,
        stale_after=timedelta(hours=24),
        max_objects=1,
    )
    await db.commit()
    cursor = await db.get(
        PrivateStorageReconciliationCursor,
        (organization.id, "private"),
    )
    assert cursor is not None and cursor.cursor_key == keys[0]
    before = cursor.cursor_key

    await reconcile_private_storage(
        db,
        organization_id=organization.id,
        store=store,
        dry_run=True,
        now=now,
        stale_after=timedelta(hours=24),
        max_objects=1,
    )
    await db.refresh(cursor)
    assert cursor.cursor_key == before


async def test_registry_cursor_advances_across_every_bounded_page(db, tmp_path):
    organization = Organization(
        name="Registry cursor",
        slug=f"registry-cursor-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    now = datetime.now(UTC)
    keys: list[str] = []
    for number in (10, 20, 30):
        candidate = Candidate(
            organization_id=organization.id,
            source="manual",
            raw_cv=f"registry cursor candidate {number}",
        )
        db.add(candidate)
        await db.flush()
        object_id = UUID(int=number)
        object_key = private_object_key(
            organization.id,
            "candidates",
            object_id,
            ".pdf",
        )
        keys.append(object_key)
        db.add(
            PrivateObject(
                id=object_id,
                organization_id=organization.id,
                category="candidates",
                object_key=object_key,
                original_filename=f"{number}.pdf",
                media_type="application/pdf",
                size_bytes=1,
                sha256=f"{number // 10}" * 64,
                state="ready",
                scanner_engine="fake",
                scanner_verdict="clean",
                resource_type="candidate",
                resource_id=candidate.id,
                ready_at=now,
            )
        )
    await db.commit()
    store = LocalBlobStore(tmp_path / "registry-cursor")

    observed: list[str | None] = []
    for _index in range(3):
        metrics = await reconcile_private_storage(
            db,
            organization_id=organization.id,
            store=store,
            dry_run=False,
            now=now,
            stale_after=timedelta(hours=24),
            max_objects=1,
        )
        await db.commit()
        assert metrics.registry_scanned == 1
        cursor = await db.get(
            PrivateStorageReconciliationCursor,
            (organization.id, "registry"),
        )
        assert cursor is not None
        observed.append(cursor.cursor_key)
        db.expire(cursor)
    assert observed == [keys[0], keys[1], None]
