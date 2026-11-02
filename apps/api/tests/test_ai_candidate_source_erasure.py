"""AI-import provenance is idempotent, tenant-bound, and erased with candidates."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import UploadFile
from sqlalchemy import func, select
from starlette.datastructures import Headers

from app.config import get_settings
from app.db import SessionLocal
from app.models.ai_candidate_source import AiCandidateSourceObject
from app.models.ai_hiring import AiOrganizationSettings, AiSession
from app.models.candidate import Candidate
from app.models.enterprise import EnterprisePolicy
from app.models.organization import Organization
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.user import User
from app.routers.enterprise import run_retention
from app.schemas.enterprise import RetentionRunRequest
from app.services.ai_hiring_attachments import (
    import_ai_attachments,
    resolve_attachment_references,
    save_ai_attachment,
)
from app.services.private_object_worker import process_private_object_deletions_once
from app.storage import LocalBlobStore

pytestmark = pytest.mark.asyncio


def _upload(canary: str) -> UploadFile:
    return UploadFile(
        filename="candidate.txt",
        file=io.BytesIO(
            f"{canary}: Python and PostgreSQL delivery experience.".encode()
        ),
        headers=Headers({"content-type": "text/plain"}),
    )


async def test_ai_source_is_linked_once_and_erased_with_candidate(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name="AI provenance", slug=f"ai-provenance-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"ai-provenance-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    db.add(
        AiOrganizationSettings(
            organization_id=organization.id,
            feature_enabled=True,
        )
    )
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        status="active",
    )
    db.add(session)
    await db.commit()

    canary = f"ERASE-{uuid4()}"
    store = LocalBlobStore(root)
    upload = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_upload(canary),
        store=store,
    )
    reference = {"id": upload["id"], "upload_token": upload["upload_token"]}
    resolved = await resolve_attachment_references(
        db,
        user=admin,
        session_id=session.id,
        references=[reference],
    )
    first = await import_ai_attachments(db, admin, attachments=resolved, store=store)
    replay = await import_ai_attachments(db, admin, attachments=resolved, store=store)
    await db.commit()

    assert len(first["imported_candidate_ids"]) == 1
    candidate_id = UUID(first["imported_candidate_ids"][0])
    assert replay["imported_candidate_ids"] == []
    assert replay["duplicate_candidate_ids"] == [str(candidate_id)]
    assert (
        await db.execute(
            select(func.count(AiCandidateSourceObject.id)).where(
                AiCandidateSourceObject.organization_id == organization.id,
                AiCandidateSourceObject.candidate_id == candidate_id,
                AiCandidateSourceObject.private_object_id == UUID(upload["id"]),
            )
        )
    ).scalar_one() == 1

    candidate = await db.get(Candidate, candidate_id)
    source_object = await db.get(PrivateObject, UUID(upload["id"]))
    assert candidate is not None and canary in (candidate.raw_cv or "")
    assert source_object is not None
    assert await store.stat(source_object.object_key) is not None
    candidate.deleted_at = datetime.now(UTC) - timedelta(days=60)
    await db.commit()

    result = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )
    assert result.deleted_candidates == 1
    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is None
    assert (
        await db.execute(
            select(AiCandidateSourceObject).where(
                AiCandidateSourceObject.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none() is None
    deletion = (
        await db.execute(
            select(PrivateObjectDeletion).where(
                PrivateObjectDeletion.private_object_id == source_object.id
            )
        )
    ).scalar_one()
    assert deletion.reason == "candidate_erasure"

    counts = await process_private_object_deletions_once(
        SessionLocal,
        worker_id="ai-source-erasure",
        mode="off",
        store=store,
    )
    assert counts["deleted"] == 1
    assert await store.stat(source_object.object_key) is None


async def test_ai_source_legal_hold_blocks_candidate_erasure(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization = Organization(
        name="AI provenance hold", slug=f"ai-hold-{uuid4().hex[:8]}"
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"ai-hold-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    db.add(
        AiOrganizationSettings(
            organization_id=organization.id,
            feature_enabled=True,
        )
    )
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            candidate_retention_days=365,
            deleted_retention_days=30,
            audit_retention_days=90,
        )
    )
    session = AiSession(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        status="active",
    )
    db.add(session)
    await db.commit()

    store = LocalBlobStore(root)
    upload = await save_ai_attachment(
        db,
        admin,
        session.id,
        upload=_upload(f"HOLD-{uuid4()}"),
        store=store,
    )
    resolved = await resolve_attachment_references(
        db,
        user=admin,
        session_id=session.id,
        references=[{"id": upload["id"], "upload_token": upload["upload_token"]}],
    )
    imported = await import_ai_attachments(db, admin, attachments=resolved, store=store)
    candidate_id = UUID(imported["imported_candidate_ids"][0])
    candidate = await db.get(Candidate, candidate_id)
    source_object = await db.get(PrivateObject, UUID(upload["id"]))
    assert candidate is not None and source_object is not None
    candidate.deleted_at = datetime.now(UTC) - timedelta(days=60)
    source_object.legal_hold_at = datetime.now(UTC)
    await db.commit()

    result = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=False,
        ),
        db=db,
        user=admin,
    )
    assert result.deleted_candidates == 0
    assert (
        await db.execute(
            select(Candidate)
            .where(Candidate.id == candidate_id)
            .execution_options(include_deleted=True)
        )
    ).scalar_one_or_none() is not None
    await db.refresh(source_object)
    assert source_object.state == "ready"
    assert await store.stat(source_object.object_key) is not None
