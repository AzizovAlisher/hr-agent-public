"""Registry dual-write, authoritative binding, and deletion-outbox tests."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from pypdf import PdfWriter
from sqlalchemy import select
from starlette.datastructures import Headers

from app.config import get_settings
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.user import User
from app.routers.candidates import download_candidate_cv, upload_file
from app.services.uploads import (
    cleanup_promoted_upload_after_db_failure,
    resolve_resource_private_blob_reference,
    store_private_upload,
)
from app.storage import (
    BlobStoreError,
    DeletionSettlement,
    LocalBlobStore,
    StalePrivateObjectDeletionClaim,
    enqueue_private_object_deletion,
    register_ready_private_object,
    settle_private_object_deletion,
)


def _pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def _upload(filename: str = "resume.pdf") -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(_pdf()),
        headers=Headers({"content-type": "application/pdf"}),
    )


async def _actor(db, label: str) -> tuple[Organization, User]:
    organization = Organization(
        name=f"Private objects {label}",
        slug=f"private-objects-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"private-{label}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(user)
    await db.commit()
    return organization, user


async def test_candidate_registry_binding_is_authoritative_and_tenant_scoped(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    organization, recruiter = await _actor(db, "owner")
    _other_organization, other_recruiter = await _actor(db, "other")

    response_model = await upload_file(
        file=_upload(),
        source_ref="Ada Lovelace",
        db=db,
        user=recruiter,
    )
    candidate = await db.get(Candidate, response_model.id)
    assert candidate is not None and candidate.private_object_id is not None
    private_object = await db.get(PrivateObject, candidate.private_object_id)
    assert private_object is not None
    assert private_object.organization_id == organization.id
    assert private_object.resource_type == "candidate"
    assert private_object.resource_id == candidate.id
    assert private_object.state == "ready"
    assert private_object.object_key == candidate.pdf_path
    assert private_object.scanner_verdict == "clean"
    assert response_model.pdf_path == f"/candidates/{candidate.id}/cv"
    assert private_object.object_key not in str(response_model.model_dump())
    assert "private_object_id" not in response_model.model_dump()

    # A compatibility-column edit cannot redirect a registered resource.  The
    # download must use the exact ready registry row instead.
    candidate.pdf_path = f"private/{organization.id}/candidates/{uuid4()}.pdf"
    await db.commit()
    download = await download_candidate_cv(
        candidate.id,
        db=db,
        user=recruiter,
    )
    assert b"".join([chunk async for chunk in download.body_iterator]) == _pdf()

    wrong_resource = await resolve_resource_private_blob_reference(
        db,
        organization_id=organization.id,
        category="candidates",
        resource_type="candidate",
        resource_id=uuid4(),
        private_object_id=candidate.private_object_id,
        legacy_reference=private_object.object_key,
    )
    assert wrong_resource is None
    with pytest.raises(HTTPException) as cross_tenant:
        await download_candidate_cv(candidate.id, db=db, user=other_recruiter)
    assert cross_tenant.value.status_code == 404


async def test_manual_candidate_upload_uses_safe_filename_as_initial_label(
    db,
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "objects"))
    _organization, recruiter = await _actor(db, "filename-label")
    response_model = await upload_file(
        file=_upload("Сайидазизхон Абдуллаев - CV.pdf"),
        source_ref=None,
        db=db,
        user=recruiter,
    )
    assert response_model.source_ref == "Сайидазизхон Абдуллаев - CV"


class _UnavailableDeleteStore(LocalBlobStore):
    async def delete(self, key: str) -> None:
        raise BlobStoreError("provider details must remain internal")


async def test_deletion_outbox_is_idempotent_retriable_and_physically_settled(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, _user = await _actor(db, "delete")
    stored = await store_private_upload(
        _upload(),
        organization_id=organization.id,
        category="candidates",
        allowed_suffixes={".pdf"},
        max_bytes=15 * 1024 * 1024,
        label="CV file",
    )
    candidate = Candidate(
        id=uuid4(),
        organization_id=organization.id,
        source="manual",
        source_ref="deletion candidate",
        raw_cv="Deletion candidate profile",
        pdf_path=stored.key,
    )
    private_object = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="candidate",
        resource_id=candidate.id,
        stored=stored,
    )
    candidate.private_object_id = private_object.id
    db.add(candidate)
    await db.commit()

    first = await enqueue_private_object_deletion(
        db,
        organization_id=organization.id,
        private_object_id=private_object.id,
        reason="candidate_erasure",
    )
    second = await enqueue_private_object_deletion(
        db,
        organization_id=organization.id,
        private_object_id=private_object.id,
        reason="admin_request",
    )
    assert first is not None and second is not None and first.id == second.id
    await db.commit()
    await db.refresh(private_object)
    assert private_object.state == "delete_pending"
    assert await LocalBlobStore(root).stat(stored.key) is not None

    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(PrivateObjectDeletion.id == first.id)
        )
    ).scalar_one()
    first_token = uuid4()
    deletion.claimed_at = datetime.now(UTC)
    deletion.claim_token = first_token
    deletion.attempt_count = 1
    await db.flush()
    retry = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization.id,
        claim_token=first_token,
        store=_UnavailableDeleteStore(root),
    )
    assert retry == DeletionSettlement.RETRY_SCHEDULED
    assert deletion.last_error_code == "blob_store_unavailable"
    assert deletion.claim_token is None and deletion.claimed_at is None
    await db.commit()

    with pytest.raises(StalePrivateObjectDeletionClaim):
        await settle_private_object_deletion(
            db,
            deletion_id=deletion.id,
            organization_id=organization.id,
            claim_token=first_token,
            store=LocalBlobStore(root),
        )

    second_token = uuid4()
    deletion.claimed_at = datetime.now(UTC)
    deletion.claim_token = second_token
    deletion.attempt_count = 2
    await db.flush()
    settled = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization.id,
        claim_token=second_token,
        store=LocalBlobStore(root),
    )
    assert settled == DeletionSettlement.DELETED
    await db.commit()
    await db.refresh(private_object)
    assert private_object.state == "deleted"
    assert private_object.deleted_at is not None
    assert private_object.original_filename == "deleted"
    assert private_object.scanner_engine is None
    assert deletion.completed_at is not None
    assert await LocalBlobStore(root).stat(stored.key) is None

    replay = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization.id,
        claim_token=uuid4(),
        store=LocalBlobStore(root),
    )
    assert replay == DeletionSettlement.ALREADY_DELETED


async def test_legal_hold_blocks_physical_deletion_even_after_claim(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "legal-hold"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, _user = await _actor(db, "legal-hold")
    stored = await store_private_upload(
        _upload("held.pdf"),
        organization_id=organization.id,
        category="candidates",
        allowed_suffixes={".pdf"},
        max_bytes=15 * 1024 * 1024,
        label="CV file",
    )
    private_object = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="candidate",
        resource_id=uuid4(),
        stored=stored,
    )
    private_object.legal_hold_at = datetime.now(UTC)
    await db.commit()
    deletion = await enqueue_private_object_deletion(
        db,
        organization_id=organization.id,
        private_object_id=private_object.id,
        reason="candidate_erasure",
    )
    assert deletion is not None
    token = uuid4()
    deletion.claimed_at = datetime.now(UTC)
    deletion.claim_token = token
    deletion.attempt_count = 1
    await db.flush()

    result = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization.id,
        claim_token=token,
        store=LocalBlobStore(root),
    )
    assert result == DeletionSettlement.RETRY_SCHEDULED
    assert deletion.last_error_code == "legal_hold"
    assert await LocalBlobStore(root).stat(stored.key) is not None
    await db.commit()
    await db.refresh(private_object)
    assert private_object.state == "delete_pending"
    assert private_object.deleted_at is None


async def test_failed_commit_cleanup_preserves_landed_row_and_removes_orphan(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "commit-uncertainty"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, _user = await _actor(db, "commit-uncertainty")
    landed = await store_private_upload(
        _upload("landed.pdf"),
        organization_id=organization.id,
        category="candidates",
        allowed_suffixes={".pdf"},
        max_bytes=15 * 1024 * 1024,
        label="CV file",
    )
    landed_record = await register_ready_private_object(
        db,
        organization_id=organization.id,
        resource_type="candidate",
        resource_id=uuid4(),
        stored=landed,
    )
    await db.commit()

    await cleanup_promoted_upload_after_db_failure(
        landed.key,
        organization_id=organization.id,
        category="candidates",
        private_object_id=landed_record.id,
    )
    assert await LocalBlobStore(root).stat(landed.key) is not None

    orphan = await store_private_upload(
        _upload("orphan.pdf"),
        organization_id=organization.id,
        category="candidates",
        allowed_suffixes={".pdf"},
        max_bytes=15 * 1024 * 1024,
        label="CV file",
    )
    await cleanup_promoted_upload_after_db_failure(
        orphan.key,
        organization_id=organization.id,
        category="candidates",
        private_object_id=uuid4(),
    )
    assert await LocalBlobStore(root).stat(orphan.key) is None
