"""Enterprise retention covers primary, derived, legacy, and held files."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from pypdf import PdfWriter
from sqlalchemy import select
from starlette.datastructures import Headers

from app.config import get_settings
from app.db import SessionLocal
from app.models.candidate import Candidate
from app.models.enterprise import EnterprisePolicy
from app.models.interview_recording import InterviewRecording
from app.models.organization import Organization
from app.models.private_object import (
    LegacyPrivateObjectDeletion,
    PrivateObject,
    PrivateObjectDeletion,
)
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.routers.candidates import upload_file
from app.routers.enterprise import run_retention
from app.schemas.enterprise import RetentionRunRequest
from app.services.legacy_private_object_worker import (
    process_legacy_private_object_deletions_once,
)
from app.services.private_object_worker import process_private_object_deletions_once
from app.services.uploads import store_private_upload
from app.storage import (
    LocalBlobStore,
    enqueue_legacy_private_object_deletion,
    register_ready_private_object,
)

pytestmark = pytest.mark.asyncio


def _pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def _webm() -> bytes:
    def element(element_id: int, payload: bytes) -> bytes:
        encoded_id = element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
        assert len(payload) < 127
        return encoded_id + bytes([0x80 | len(payload)]) + payload

    header = element(0x1A45DFA3, element(0x4282, b"webm"))
    info = element(0x1549A966, element(0x2AD7B1, b"\x0fB@"))
    tracks = element(
        0x1654AE6B,
        element(
            0xAE,
            element(0xD7, b"\x01") + element(0x83, b"\x02") + element(0x86, b"A_OPUS"),
        ),
    )
    cluster = element(
        0x1F43B675,
        element(0xE7, b"\x00") + element(0xA3, b"\x81\x00\x00\x80\xf8\xff\xfe"),
    )
    segment_payload = info + tracks + cluster
    return (
        header
        + b"\x18\x53\x80\x67"
        + bytes([0x80 | len(segment_payload)])
        + segment_payload
    )


def _upload(name: str, content: bytes, media_type: str) -> UploadFile:
    return UploadFile(
        filename=name,
        file=io.BytesIO(content),
        headers=Headers({"content-type": media_type}),
    )


async def test_retention_erases_registered_derived_and_legacy_files_but_honors_hold(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name="Retention objects", slug=f"retention-objects-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"retention-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    await db.commit()
    old = datetime.now(UTC) - timedelta(days=60)
    store = LocalBlobStore(root)

    primary_out = await upload_file(
        file=_upload("primary.pdf", _pdf(), "application/pdf"),
        source_ref="Primary",
        db=db,
        user=admin,
    )
    primary = await db.get(Candidate, primary_out.id)
    assert primary is not None and primary.private_object_id is not None
    primary.deleted_at = old
    primary_object = await db.get(PrivateObject, primary.private_object_id)
    assert primary_object is not None
    completed_tombstone = PrivateObject(
        organization_id=organization.id,
        category="candidates",
        object_key=(f"private/{organization.id}/candidates/{uuid4()}.pdf"),
        original_filename="candidate-sensitive-name.pdf",
        media_type="application/pdf",
        size_bytes=1,
        sha256="a" * 64,
        state="deleted",
        scanner_engine="historical-scanner",
        scanner_version="candidate-linked-version",
        scanner_verdict="clean",
        resource_type="candidate",
        resource_id=primary.id,
        ready_at=old,
        deleted_at=old,
    )
    db.add(completed_tombstone)
    migrated_source_path = root / "cvs" / f"{uuid4()}.pdf"
    migrated_source_path.parent.mkdir(parents=True, exist_ok=True)
    migrated_source_bytes = _pdf()
    migrated_source_path.write_bytes(migrated_source_bytes)
    migrated_source_deletion = await enqueue_legacy_private_object_deletion(
        db,
        organization_id=organization.id,
        category="candidates",
        resource_type="candidate",
        resource_id=primary.id,
        legacy_reference=migrated_source_path,
        reason="replacement",
        not_before=datetime.now(UTC) + timedelta(days=30),
        store=store,
    )
    assert migrated_source_deletion is not None
    migrated_source_deletion.attempt_count = 10
    migrated_source_deletion.last_error_code = "legacy_storage_unavailable"
    await db.flush()

    task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=primary.id,
        title="Derived task",
        submission_type="file",
        status="submitted",
    )
    db.add(task)
    await db.flush()
    task_upload = await store_private_upload(
        _upload("task.txt", b"derived private task", "text/plain"),
        organization_id=organization.id,
        category="tasks",
        allowed_suffixes={".txt"},
        max_bytes=15 * 1024 * 1024,
        store=store,
    )
    task_object = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="task_submission",
        resource_id=task.id,
        stored=task_upload,
    )
    task.private_object_id = task_object.id
    task.file_path = task_upload.key

    recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=primary.id,
        created_by=admin.id,
        title="Registered derived recording",
        status="uploaded",
    )
    db.add(recording)
    await db.flush()
    recording_upload = await store_private_upload(
        _upload(
            "interview.webm",
            _webm(),
            "video/webm",
        ),
        organization_id=organization.id,
        category="recordings",
        allowed_suffixes={".webm"},
        max_bytes=15 * 1024 * 1024,
        store=store,
    )
    recording_object = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="interview_recording",
        resource_id=recording.id,
        stored=recording_upload,
    )
    recording.private_object_id = recording_object.id
    recording.media_path = recording_upload.key

    recording_path = root / "recordings" / str(organization.id) / f"{uuid4()}.webm"
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    recording_path.write_bytes(b"legacy owned recording")
    legacy_recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=primary.id,
        created_by=admin.id,
        title="Derived recording",
        status="uploaded",
        media_path=str(recording_path),
    )
    db.add(legacy_recording)

    legacy_path = root / "cvs" / f"{uuid4()}.pdf"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(_pdf())
    legacy = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Legacy",
        raw_cv="legacy candidate",
        pdf_path=str(legacy_path),
        deleted_at=old,
    )
    db.add(legacy)

    held_out = await upload_file(
        file=_upload("held.pdf", _pdf(), "application/pdf"),
        source_ref="Held",
        db=db,
        user=admin,
    )
    held_candidate = await db.get(Candidate, held_out.id)
    assert held_candidate is not None and held_candidate.private_object_id is not None
    held_candidate.deleted_at = old
    held_object = await db.get(PrivateObject, held_candidate.private_object_id)
    assert held_object is not None
    held_recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=held_candidate.id,
        created_by=admin.id,
        title="Held derived recording",
        status="uploaded",
    )
    db.add(held_recording)
    await db.flush()
    held_recording_upload = await store_private_upload(
        _upload(
            "held-interview.webm",
            _webm(),
            "video/webm",
        ),
        organization_id=organization.id,
        category="recordings",
        allowed_suffixes={".webm"},
        max_bytes=15 * 1024 * 1024,
        store=store,
    )
    held_recording_object = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="interview_recording",
        resource_id=held_recording.id,
        stored=held_recording_upload,
    )
    held_recording.private_object_id = held_recording_object.id
    held_recording.media_path = held_recording_upload.key
    # A hold on derived recording media protects its candidate root and every
    # other owned object in the retention closure.
    held_recording_object.legal_hold_at = datetime.now(UTC)
    await db.commit()
    erased_resource_ids = {
        primary.id,
        task.id,
        recording.id,
        legacy.id,
        legacy_recording.id,
    }

    # A cutover source that no longer matches its deletion snapshot cannot be
    # silently rebound or stranded after the subject row is purged. The whole
    # retention unit fails closed and remains retryable after operator repair.
    migrated_source_path.write_bytes(migrated_source_bytes + b"changed")
    with pytest.raises(HTTPException) as mismatch:
        await run_retention(
            RetentionRunRequest(
                confirmation="PURGE",
                include_active_candidates=False,
            ),
            db=db,
            user=admin,
        )
    assert mismatch.value.status_code == 409
    # The router rolled the failed transaction back, which intentionally
    # expires ORM state. Reload every fixture used by the retry assertions.
    for item in (
        organization,
        admin,
        primary,
        primary_object,
        completed_tombstone,
        migrated_source_deletion,
        task,
        task_object,
        recording,
        recording_object,
        legacy_recording,
        legacy,
        held_candidate,
        held_object,
        held_recording,
        held_recording_object,
    ):
        await db.refresh(item)
    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == primary.id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is not None
    assert migrated_source_deletion.reason == "replacement"
    assert migrated_source_deletion.attempt_count == 10
    migrated_source_path.write_bytes(migrated_source_bytes)

    result = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )
    assert result.deleted_candidates == 2
    # Database erasure commits with both legacy paths durably queued. Physical
    # removal is deliberately asynchronous and survives storage outages.
    assert legacy_path.exists() is True
    assert recording_path.exists() is True
    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == held_candidate.id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is not None
    await db.refresh(held_object)
    assert held_object.state == "ready"
    await db.refresh(held_recording_object)
    assert held_recording_object.state == "ready"

    deletions = list(
        (
            await db.execute(
                select(PrivateObjectDeletion).where(
                    PrivateObjectDeletion.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert {item.private_object_id for item in deletions} == {
        primary_object.id,
        task_object.id,
        recording_object.id,
    }
    await db.refresh(primary_object)
    await db.refresh(task_object)
    await db.refresh(recording_object)
    await db.refresh(completed_tombstone)
    assert {
        primary_object.resource_id,
        task_object.resource_id,
        recording_object.resource_id,
        completed_tombstone.resource_id,
    }.isdisjoint(erased_resource_ids)
    assert completed_tombstone.state == "deleted"
    assert completed_tombstone.original_filename == "erased"
    assert completed_tombstone.media_type == "application/octet-stream"
    assert completed_tombstone.scanner_engine is None
    assert completed_tombstone.scanner_version is None
    assert completed_tombstone.scanner_verdict is None
    assert await store.stat(primary_object.object_key) is not None
    assert await store.stat(task_object.object_key) is not None
    assert await store.stat(recording_object.object_key) is not None

    legacy_deletions = list(
        (
            await db.execute(
                select(LegacyPrivateObjectDeletion).where(
                    LegacyPrivateObjectDeletion.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert {item.normalized_key for item in legacy_deletions} == {
        legacy_path.relative_to(root).as_posix(),
        migrated_source_path.relative_to(root).as_posix(),
        recording_path.relative_to(root).as_posix(),
    }
    assert {item.resource_id for item in legacy_deletions}.isdisjoint(
        erased_resource_ids
    )
    await db.refresh(migrated_source_deletion)
    assert migrated_source_deletion.reason == "candidate_erasure"
    assert migrated_source_deletion.not_before <= datetime.now(UTC)
    assert migrated_source_deletion.attempt_count == 0
    assert migrated_source_deletion.claimed_at is None
    assert migrated_source_deletion.claim_token is None
    assert migrated_source_deletion.last_error_code is None
    legacy_counts = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="retention-legacy-private-files",
        mode="off",
        store=store,
    )
    assert legacy_counts["deleted"] == 3
    assert legacy_path.exists() is False
    assert migrated_source_path.exists() is False
    assert recording_path.exists() is False

    counts = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="retention-private-files",
        mode="off",
        store=store,
    )
    assert counts["deleted"] == 3
    assert await store.stat(primary_object.object_key) is None
    assert await store.stat(task_object.object_key) is None
    assert await store.stat(recording_object.object_key) is None
    assert await store.stat(held_object.object_key) is not None
    assert await store.stat(held_recording_object.object_key) is not None


async def test_retention_empty_detached_mount_fails_before_pseudonymization_then_retries(
    db,
    tmp_path: Path,
    monkeypatch,
):
    primary_root = tmp_path / "primary-objects"
    legacy_root = tmp_path / "legacy-mounted"
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_dir", str(primary_root))
    monkeypatch.setattr(settings, "legacy_private_storage_dir", str(legacy_root))
    organization = Organization(
        name="Retention detached legacy mount",
        slug=f"retention-detached-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"retention-detached-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    await db.commit()

    uploaded = await upload_file(
        file=_upload("registered.pdf", _pdf(), "application/pdf"),
        source_ref="Detached legacy retention",
        db=db,
        user=admin,
    )
    candidate = await db.get(Candidate, uploaded.id)
    assert candidate is not None and candidate.private_object_id is not None
    candidate.deleted_at = datetime.now(UTC) - timedelta(days=60)
    legacy_store = LocalBlobStore(legacy_root)
    legacy_path = legacy_root / "cvs" / f"{uuid4()}.pdf"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(_pdf())
    pending = await enqueue_legacy_private_object_deletion(
        db,
        organization_id=organization.id,
        category="candidates",
        resource_type="candidate",
        resource_id=candidate.id,
        legacy_reference=legacy_path,
        reason="replacement",
        store=legacy_store,
    )
    assert pending is not None
    pending_id = pending.id
    candidate_id = candidate.id
    original_resource_id = pending.resource_id
    relative = legacy_path.relative_to(legacy_root)
    await db.commit()

    detached_volume = tmp_path / "detached-volume"
    legacy_root.rename(detached_volume)
    legacy_root.mkdir()
    with pytest.raises(HTTPException) as unavailable:
        await run_retention(
            RetentionRunRequest(
                confirmation="PURGE",
                include_active_candidates=False,
            ),
            db=db,
            user=admin,
        )
    assert unavailable.value.status_code == 409
    assert (detached_volume / relative).is_file()
    remaining_candidate = (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none()
    assert remaining_candidate is not None
    unchanged_pending = await db.get(LegacyPrivateObjectDeletion, pending_id)
    assert unchanged_pending is not None
    assert unchanged_pending.resource_id == original_resource_id
    assert unchanged_pending.reason == "replacement"
    assert unchanged_pending.completed_at is None

    legacy_root.rmdir()
    detached_volume.rename(legacy_root)
    await db.refresh(admin)
    result = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )

    assert result.deleted_candidates == 1
    erased_candidate = (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none()
    assert erased_candidate is None
    adopted_pending = await db.get(LegacyPrivateObjectDeletion, pending_id)
    assert adopted_pending is not None
    assert adopted_pending.resource_id != original_resource_id
    assert adopted_pending.reason == "candidate_erasure"
    assert adopted_pending.completed_at is None
    assert (legacy_root / relative).is_file()
