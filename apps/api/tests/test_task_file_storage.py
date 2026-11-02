"""Tenant, field-policy, audit, and lifecycle tests for task attachments."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers

from app.config import get_settings
from app.db import SessionLocal
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog, PermissionOverride
from app.models.organization import Organization
from app.models.private_object import (
    LegacyPrivateObjectDeletion,
    PrivateObject,
    PrivateObjectDeletion,
)
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.routers.tasks import delete_task_file, download_task_file, upload_task_file
from app.services.legacy_private_object_worker import (
    process_legacy_private_object_deletions_once,
)
from app.storage import (
    BlobStoreError,
    DeletionSettlement,
    LocalBlobStore,
    get_blob_store,
    settle_private_object_deletion,
)


class DeleteThenFailStore(LocalBlobStore):
    async def delete_if_snapshot_matches(self, key: str, **kwargs):
        await super().delete_if_snapshot_matches(key, **kwargs)
        raise BlobStoreError("simulated lost storage acknowledgement")


async def _settle_deletion(db, *, private_object_id, organization_id, store):
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.organization_id == organization_id,
                PrivateObjectDeletion.private_object_id == private_object_id,
                PrivateObjectDeletion.completed_at.is_(None),
            )
        )
    ).scalar_one()
    token = uuid4()
    deletion.claimed_at = datetime.now(UTC)
    deletion.claim_token = token
    deletion.attempt_count += 1
    await db.flush()
    result = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization_id,
        claim_token=token,
        store=store,
    )
    await db.commit()
    assert result == DeletionSettlement.DELETED


async def _seed(db, label: str) -> tuple[Organization, User, TaskSubmission]:
    organization = Organization(
        name=f"Task file {label}",
        slug=f"task-file-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"task-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{label}",
        raw_cv="Candidate profile with enough detail for a technical task.",
    )
    db.add_all([user, candidate])
    await db.flush()
    task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by=user.id,
        title=f"Secure task {label}",
        submission_type="file",
        status="sent",
    )
    db.add(task)
    await db.commit()
    return organization, user, task


def _upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(content),
        headers=Headers({"content-type": "text/plain"}),
    )


async def test_task_file_is_tenant_scoped_audited_replaceable_and_erasable(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    organization, recruiter, task = await _seed(db, "owner")
    _other_org, other_recruiter, _other_task = await _seed(db, "other")

    saved = await upload_task_file(
        task.id,
        _upload("answer.txt", b"first private task submission"),
        db=db,
        user=recruiter,
    )
    first_key = saved.file_path
    first_object_id = saved.private_object_id
    assert first_key is not None
    assert first_object_id is not None
    assert first_key.startswith(f"private/{organization.id}/tasks/")

    response = await download_task_file(task.id, db=db, user=recruiter)
    assert b"".join([chunk async for chunk in response.body_iterator]) == (
        b"first private task submission"
    )
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["x-content-type-options"] == "nosniff"
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.action == "download_task_file",
                AuditLog.resource_id == task.id,
            )
        )
    ).scalar_one()
    assert audit.metadata_json["sensitive_read"] is True

    with pytest.raises(HTTPException) as cross_tenant:
        await download_task_file(task.id, db=db, user=other_recruiter)
    assert cross_tenant.value.status_code == 404

    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="recruiter",
            permission="candidates.documents",
            allowed=False,
        )
    )
    await db.commit()
    with pytest.raises(HTTPException) as field_denied:
        await download_task_file(task.id, db=db, user=recruiter)
    assert field_denied.value.status_code == 403
    override = (
        await db.execute(
            select(PermissionOverride).where(
                PermissionOverride.organization_id == organization.id,
                PermissionOverride.role == "recruiter",
                PermissionOverride.permission == "candidates.documents",
            )
        )
    ).scalar_one()
    await db.delete(override)
    await db.commit()

    replaced = await upload_task_file(
        task.id,
        _upload("answer.txt", b"replacement private task submission"),
        db=db,
        user=recruiter,
    )
    assert replaced.file_path != first_key
    store = get_blob_store()
    first_object = await db.get(PrivateObject, first_object_id)
    assert first_object is not None and first_object.state == "delete_pending"
    assert await store.stat(first_key) is not None
    await _settle_deletion(
        db,
        private_object_id=first_object_id,
        organization_id=organization.id,
        store=store,
    )
    assert await store.stat(first_key) is None
    assert await store.stat(replaced.file_path) is not None

    final_key = replaced.file_path
    final_object_id = replaced.private_object_id
    assert final_object_id is not None
    await delete_task_file(task.id, db=db, user=recruiter)
    await db.refresh(task)
    assert task.file_path is None
    assert await store.stat(final_key) is not None
    await _settle_deletion(
        db,
        private_object_id=final_object_id,
        organization_id=organization.id,
        store=store,
    )
    assert await store.stat(final_key) is None


async def test_rejected_task_upload_leaves_no_object_or_model_reference(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    _organization, recruiter, task = await _seed(db, "rejected")
    malicious = UploadFile(
        filename="answer.png",
        file=io.BytesIO(b"<svg><script>alert(1)</script></svg>"),
        headers=Headers({"content-type": "image/png"}),
    )
    with pytest.raises(HTTPException) as rejected:
        await upload_task_file(task.id, malicious, db=db, user=recruiter)
    assert rejected.value.status_code == 415
    await db.refresh(task)
    assert task.file_path is None
    root = Path(get_settings().storage_dir)
    assert not any(path.is_file() for path in root.rglob("*"))


async def test_legacy_replacement_commit_survives_ambiguous_storage_failure(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, recruiter, task = await _seed(db, "legacy-replacement")
    legacy_path = root / "task_files" / str(organization.id) / f"{uuid4()}.txt"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b"validated legacy submission")
    task.file_path = str(legacy_path)
    task.private_object_id = None
    await db.commit()

    replaced = await upload_task_file(
        task.id,
        _upload("replacement.txt", b"new durable submission"),
        db=db,
        user=recruiter,
    )
    assert replaced.file_path != str(legacy_path)
    assert replaced.private_object_id is not None
    assert legacy_path.exists()
    deletion = (
        await db.execute(
            select(LegacyPrivateObjectDeletion).where(
                LegacyPrivateObjectDeletion.organization_id == organization.id,
                LegacyPrivateObjectDeletion.resource_id == task.id,
            )
        )
    ).scalar_one()
    assert deletion.reason == "replacement"

    now = datetime.now(UTC)
    first = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy-replacement:lost-ack",
        mode="off",
        store=DeleteThenFailStore(root),
        now=now,
    )
    assert first["retried"] == 1
    assert not legacy_path.exists()
    async with SessionLocal() as verify:
        pending = await verify.get(LegacyPrivateObjectDeletion, deletion.id)
        assert pending is not None
        assert pending.completed_at is None
        assert pending.last_error_code == "legacy_storage_unavailable"

    second = await process_legacy_private_object_deletions_once(
        SessionLocal,
        worker_id="legacy-replacement:retry",
        mode="off",
        store=LocalBlobStore(root),
        now=now + timedelta(seconds=31),
    )
    assert second["deleted"] == 1
    async with SessionLocal() as verify:
        completed = await verify.get(LegacyPrivateObjectDeletion, deletion.id)
        assert completed is not None and completed.completed_at is not None
