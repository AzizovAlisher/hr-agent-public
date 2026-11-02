"""Tenant, field-policy, audit, and lifecycle tests for recording media."""

from __future__ import annotations

import io
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers

from app.auth.jwt import encode_access_token
from app.config import get_settings
from app.main import app
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog, PermissionOverride
from app.models.interview_recording import InterviewRecording
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.user import User
from app.routers.recordings import (
    delete_recording,
    delete_recording_file,
    download_recording_file,
    upload_recording_file,
)
from app.schemas.enrichment import RecordingOut, RecordingUpdate
from app.storage import (
    DeletionSettlement,
    get_blob_store,
    settle_private_object_deletion,
)


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8_000)
        recording.writeframes(b"\x00\x00" * 80)
    return output.getvalue()


def _upload(
    filename: str = "interview.wav",
    content: bytes | None = None,
    media_type: str = "audio/wav",
) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(_wav() if content is None else content),
        headers=Headers({"content-type": media_type}),
    )


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    db,
    label: str,
) -> tuple[Organization, User, Candidate, InterviewRecording]:
    organization = Organization(
        name=f"Recording file {label}",
        slug=f"recording-file-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"recording-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"recording-candidate-{label}",
        raw_cv="Candidate profile for a private recorded interview.",
    )
    db.add_all([user, candidate])
    await db.flush()
    recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by=user.id,
        title=f"Secure interview {label}",
        status="pending",
    )
    db.add(recording)
    await db.commit()
    return organization, user, candidate, recording


async def _settle_deletion(
    db,
    *,
    private_object_id,
    organization_id,
    store,
) -> None:
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


async def test_recording_file_is_tenant_scoped_audited_replaceable_and_erasable(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    organization, recruiter, _candidate, recording = await _seed(db, "owner")
    _other_org, other_recruiter, _other_candidate, _other_recording = await _seed(
        db, "other"
    )

    saved = await upload_recording_file(
        recording.id,
        _upload(),
        db=db,
        user=recruiter,
    )
    first_key = saved.media_path
    first_object_id = saved.private_object_id
    assert first_key is not None
    assert first_object_id is not None
    assert first_key.startswith(f"private/{organization.id}/recordings/")
    first_object = await db.get(PrivateObject, first_object_id)
    assert first_object is not None
    assert first_object.category == "recordings"
    assert first_object.resource_type == "interview_recording"
    assert first_object.resource_id == recording.id

    public_shape = RecordingOut.model_validate(saved).model_dump()
    assert public_shape["has_media_file"] is True
    assert "media_path" not in public_shape
    assert "private_object_id" not in public_shape
    assert "object_key" not in public_shape

    response = await download_recording_file(recording.id, db=db, user=recruiter)
    assert b"".join([chunk async for chunk in response.body_iterator]) == _wav()
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    audit = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == organization.id,
                AuditLog.action == "download_recording_file",
                AuditLog.resource_id == recording.id,
            )
        )
    ).scalar_one()
    assert audit.metadata_json["sensitive_read"] is True

    with pytest.raises(HTTPException) as cross_tenant:
        await download_recording_file(recording.id, db=db, user=other_recruiter)
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
        await download_recording_file(recording.id, db=db, user=recruiter)
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

    replaced = await upload_recording_file(
        recording.id,
        _upload(),
        db=db,
        user=recruiter,
    )
    assert replaced.media_path != first_key
    assert replaced.private_object_id != first_object_id
    store = get_blob_store()
    await db.refresh(first_object)
    assert first_object.state == "delete_pending"
    assert await store.stat(first_key) is not None
    await _settle_deletion(
        db,
        private_object_id=first_object_id,
        organization_id=organization.id,
        store=store,
    )
    assert await store.stat(first_key) is None
    assert await store.stat(replaced.media_path) is not None

    final_key = replaced.media_path
    final_object_id = replaced.private_object_id
    assert final_key is not None and final_object_id is not None
    await delete_recording_file(recording.id, db=db, user=recruiter)
    await db.refresh(recording)
    assert recording.media_path is None
    assert recording.private_object_id is None
    assert await store.stat(final_key) is not None
    await _settle_deletion(
        db,
        private_object_id=final_object_id,
        organization_id=organization.id,
        store=store,
    )
    assert await store.stat(final_key) is None

    final = await upload_recording_file(
        recording.id,
        _upload(),
        db=db,
        user=recruiter,
    )
    deleted_key = final.media_path
    deleted_object_id = final.private_object_id
    assert deleted_key is not None and deleted_object_id is not None
    await delete_recording(recording.id, db=db, user=recruiter)
    assert await db.get(InterviewRecording, recording.id) is None
    assert await store.stat(deleted_key) is not None
    await _settle_deletion(
        db,
        private_object_id=deleted_object_id,
        organization_id=organization.id,
        store=store,
    )
    assert await store.stat(deleted_key) is None


async def test_rejected_recording_upload_and_path_injection_leave_no_reference(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    _organization, recruiter, _candidate, recording = await _seed(db, "rejected")

    with pytest.raises(HTTPException) as rejected:
        await upload_recording_file(
            recording.id,
            _upload(content=b"<svg><script>alert(1)</script></svg>"),
            db=db,
            user=recruiter,
        )
    assert rejected.value.status_code == 422
    await db.refresh(recording)
    assert recording.media_path is None
    assert recording.private_object_id is None
    assert not list((await db.execute(select(PrivateObject))).scalars())
    root = Path(get_settings().storage_dir)
    assert not root.exists() or not any(path.is_file() for path in root.rglob("*"))

    with pytest.raises(ValidationError):
        RecordingUpdate.model_validate({"media_path": "/tmp/caller-controlled.wav"})


async def test_recording_upload_uses_exact_catalog_permissions_at_http_boundary(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    organization, recruiter, _candidate, recording = await _seed(db, "policy")
    # Recording intake is governed by interviews.manage + candidates.documents,
    # not the unrelated broad candidates.write action.
    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="recruiter",
            permission="candidates.write",
            allowed=False,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed = await client.post(
            f"/recordings/{recording.id}/file",
            files={"file": ("interview.wav", _wav(), "audio/wav")},
            headers=_auth(recruiter),
        )
        assert allowed.status_code == 200
        assert allowed.json()["has_media_file"] is True
        assert "media_path" not in allowed.json()
        assert "private_object_id" not in allowed.json()

        db.add(
            PermissionOverride(
                organization_id=organization.id,
                role="recruiter",
                permission="interviews.manage",
                allowed=False,
            )
        )
        await db.commit()
        denied = await client.post(
            f"/recordings/{recording.id}/file",
            files={"file": ("interview.wav", _wav(), "audio/wav")},
            headers=_auth(recruiter),
        )
    assert denied.status_code == 403
    objects = list(
        (
            await db.execute(
                select(PrivateObject).where(
                    PrivateObject.organization_id == organization.id,
                    PrivateObject.resource_type == "interview_recording",
                    PrivateObject.resource_id == recording.id,
                )
            )
        ).scalars()
    )
    assert len(objects) == 1


async def test_recording_private_object_composite_fk_blocks_cross_tenant_binding(db):
    owner_org, owner, _owner_candidate, owner_recording = await _seed(db, "fk-owner")
    _other_org, _other, _other_candidate, other_recording = await _seed(db, "fk-other")

    stored = await upload_recording_file(
        owner_recording.id,
        _upload(),
        db=db,
        user=owner,
    )
    assert stored.private_object_id is not None

    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            other_recording.private_object_id = stored.private_object_id
            await db.flush()
    await db.refresh(other_recording)
    assert other_recording.private_object_id is None
    owner_object = await db.get(PrivateObject, stored.private_object_id)
    assert owner_object is not None
    assert owner_object.organization_id == owner_org.id
