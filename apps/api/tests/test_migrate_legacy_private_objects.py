"""Operator cutover coverage for pre-registry private files."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pypdf import PdfWriter
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db import SessionLocal
from app.models.application import Application
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentFile,
    AssessmentInvitation,
    AssessmentQuestion,
    AssessmentSection,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.interview_recording import InterviewRecording
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.private_object import LegacyPrivateObjectDeletion, PrivateObject
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.routers.candidates import download_candidate_cv
from app.scripts import migrate_legacy_private_objects as cutover
from app.services import uploads as upload_service
from app.storage import (
    BlobStoreError,
    FakeMalwareScanner,
    LocalBlobStore,
    S3BlobStore,
    enqueue_legacy_private_object_deletion,
)
from app.storage.scanner import ScanResult


def _pdf() -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


async def _legacy_candidate(db, legacy_root: Path, *, label: str) -> tuple[Candidate, Path]:
    organization = Organization(
        name=f"Legacy cutover {label}",
        slug=f"legacy-cutover-{label}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="storage_bytes",
            limit_value=0,
            period="contract",
        )
    )
    file_id = uuid4()
    path = legacy_root / "cvs" / f"{file_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pdf())
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"legacy candidate {label}",
        raw_cv="existing candidate data",
        pdf_path=f"storage/cvs/{file_id}.pdf",
    )
    db.add(candidate)
    await db.commit()
    return candidate, path


async def _private_keys(store: LocalBlobStore, organization_id) -> list[str]:
    page = await store.list_prefix(f"private/{organization_id}/", limit=100)
    return [entry.key for entry in page.entries]


async def _legacy_resources_for_every_spec(
    db,
    legacy_root: Path,
) -> tuple[Organization, list[object], dict[str, Path]]:
    """Create one valid pre-registry row and file for each cutover contract."""

    candidate, candidate_path = await _legacy_candidate(
        db, legacy_root, label="all-resource-specs"
    )
    organization = await db.get(Organization, candidate.organization_id)
    assert organization is not None
    position = Position(
        organization_id=organization.id,
        title="Legacy cutover position",
        status="open",
    )
    db.add(position)
    await db.flush()
    application = Application(
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source="manual",
    )
    db.add(application)
    await db.flush()

    template = AssessmentTemplate(
        organization_id=organization.id,
        name=f"Legacy template {uuid4().hex[:8]}",
    )
    db.add(template)
    await db.flush()
    version = AssessmentTemplateVersion(
        organization_id=organization.id,
        template_id=template.id,
        version=1,
        title="Legacy assessment",
        passing_percentage=Decimal("0"),
    )
    db.add(version)
    await db.flush()
    section = AssessmentSection(
        organization_id=organization.id,
        version_id=version.id,
        title="Evidence",
        position=0,
    )
    db.add(section)
    await db.flush()
    question = AssessmentQuestion(
        organization_id=organization.id,
        version_id=version.id,
        section_id=section.id,
        key="legacy-file",
        question_type="file",
        prompt="Upload legacy evidence",
        position=0,
    )
    db.add(question)
    await db.flush()
    expires_at = datetime.now(UTC) + timedelta(days=1)
    invitation = AssessmentInvitation(
        organization_id=organization.id,
        application_id=application.id,
        template_version_id=version.id,
        token_digest=uuid4().hex + uuid4().hex,
        token_prefix="legacy_cutover",
        expires_at=expires_at,
    )
    db.add(invitation)
    await db.flush()
    attempt = AssessmentAttempt(
        organization_id=organization.id,
        invitation_id=invitation.id,
        application_id=application.id,
        template_version_id=version.id,
        sequence=1,
        expires_at=expires_at,
    )
    db.add(attempt)
    await db.flush()

    assessment_id = uuid4()
    assessment_path = (
        legacy_root / "assessment_files" / str(organization.id) / f"{assessment_id}.txt"
    )
    assessment_path.parent.mkdir(parents=True, exist_ok=True)
    assessment_path.write_text("clean assessment evidence", encoding="utf-8")
    assessment = AssessmentFile(
        organization_id=organization.id,
        invitation_id=invitation.id,
        attempt_id=attempt.id,
        question_id=question.id,
        original_filename="legacy-evidence.txt",
        media_type="text/plain",
        size_bytes=assessment_path.stat().st_size,
        sha256=hashlib.sha256(assessment_path.read_bytes()).hexdigest(),
        storage_path=(
            f"assessment_files/{organization.id}/{assessment_id}.txt"
        ),
        status="ready",
    )

    task_id = uuid4()
    task_path = legacy_root / "task_files" / str(organization.id) / f"{task_id}.txt"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("clean technical-task evidence", encoding="utf-8")
    task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=candidate.id,
        title="Legacy technical task",
        submission_type="file",
        file_path=f"task_files/{organization.id}/{task_id}.txt",
        status="submitted",
    )

    recording_id = uuid4()
    recording_path = (
        legacy_root / "recordings" / str(organization.id) / f"{recording_id}.wav"
    )
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    # The smallest canonical RIFF/WAVE container accepted by the bounded parser.
    recording_path.write_bytes(b"RIFF" + (4).to_bytes(4, "little") + b"WAVE")
    recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=candidate.id,
        title="Legacy interview recording",
        status="uploaded",
        media_path=f"recordings/{organization.id}/{recording_id}.wav",
    )
    db.add_all([assessment, task, recording])
    await db.commit()
    return (
        organization,
        [candidate, assessment, task, recording],
        {
            "candidate": candidate_path,
            "assessment_file": assessment_path,
            "task_submission": task_path,
            "interview_recording": recording_path,
        },
    )


async def test_actual_storage_prefix_cutover_is_non_destructive_and_idempotent(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db, legacy_root, label="idempotent"
    )
    organization_id = candidate.organization_id
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    orphan = legacy_root / "cvs" / f"{uuid4()}.pdf"
    orphan.write_bytes(_pdf())

    dry_run = await cutover.run_cutover(
        apply=False,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert dry_run.scanned == 1
    assert dry_run.validated == 1
    assert dry_run.failed == 0
    assert dry_run.source_files == 2
    assert dry_run.orphan_files == 1
    assert await _private_keys(primary, organization_id) == []
    async with SessionLocal() as verify:
        after_dry_run = (
            await verify.execute(
                select(Candidate)
                .where(Candidate.id == candidate.id)
                .execution_options(include_deleted=True, skip_tenant_scope=True)
            )
        ).scalar_one_or_none()
        assert after_dry_run is not None
        assert after_dry_run.private_object_id is None
        assert after_dry_run.pdf_path == candidate.pdf_path

    applied = await cutover.run_cutover(
        apply=True,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert applied.migrated == 1
    assert applied.failed == 0
    assert applied.orphan_files == 1
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest

    await db.rollback()
    await db.refresh(candidate)
    migrated = candidate
    assert migrated is not None and migrated.private_object_id is not None
    assert migrated.pdf_path is not None
    assert migrated.pdf_path.startswith(
        f"private/{organization_id}/candidates/"
    )
    registered = await db.get(PrivateObject, migrated.private_object_id)
    assert registered is not None
    assert registered.object_key == migrated.pdf_path
    assert registered.sha256 == source_digest
    assert registered.scanner_verdict == "clean"
    assert await primary.stat(registered.object_key) is not None
    deletion = (
        await db.execute(
            select(LegacyPrivateObjectDeletion).where(
                LegacyPrivateObjectDeletion.resource_id == candidate.id
            )
        )
    ).scalar_one()
    assert deletion.normalized_key == source.relative_to(legacy_root).as_posix()
    assert deletion.reason == "replacement"
    assert deletion.completed_at is None
    usage = list(
        (
            await db.execute(
                select(UsageLedgerEntry).where(
                    UsageLedgerEntry.organization_id == organization_id,
                    UsageLedgerEntry.metric == "storage_bytes",
                )
            )
        ).scalars()
    )
    assert len(usage) == 1
    assert usage[0].amount == source.stat().st_size
    assert usage[0].metadata_json == {
        "category": "candidates",
        "purpose": "legacy_private_object_cutover",
    }

    rerun = await cutover.run_cutover(
        apply=True,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert rerun.scanned == 0
    assert rerun.migrated == 0
    assert rerun.failed == 0
    assert rerun.source_files == 2
    assert rerun.orphan_files == 1
    await db.rollback()
    usage_count = (
        await db.execute(
            select(func.count()).select_from(UsageLedgerEntry).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == "storage_bytes",
            )
        )
    ).scalar_one()
    assert usage_count == 1


async def test_failed_database_binding_cleans_primary_and_rolls_back_outbox(
    db,
    tmp_path: Path,
    monkeypatch,
):
    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(tmp_path / "primary-objects")
    candidate, source = await _legacy_candidate(db, legacy_root, label="rollback")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    async def fail_registration(*_args, **_kwargs):
        raise RuntimeError("simulated registry failure")

    monkeypatch.setattr(
        cutover, "register_migrated_ready_private_object", fail_registration
    )
    report = await cutover.run_cutover(
        apply=True,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures == [
        {
            "resource_type": "candidate",
            "resource_id": str(candidate.id),
            "code": "cutover_failed",
        }
    ]
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert await _private_keys(primary, candidate.organization_id) == []
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None
    assert unchanged.private_object_id is None
    assert unchanged.pdf_path == f"storage/cvs/{source.stem}.pdf"
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 0


async def test_conflicting_erasure_outbox_cannot_be_reused_for_cutover(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(tmp_path / "primary-objects")
    candidate, source = await _legacy_candidate(db, legacy_root, label="conflict")
    pending = await enqueue_legacy_private_object_deletion(
        db,
        organization_id=candidate.organization_id,
        category="candidates",
        resource_type="candidate",
        resource_id=candidate.id,
        legacy_reference=candidate.pdf_path or "",
        reason="candidate_erasure",
        store=legacy,
    )
    assert pending is not None
    await db.commit()

    report = await cutover.run_cutover(
        apply=True,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures[0]["code"] == "conflicting_legacy_deletion"
    assert source.is_file()
    assert await _private_keys(primary, candidate.organization_id) == []
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None and unchanged.private_object_id is None
    existing = await db.get(LegacyPrivateObjectDeletion, pending.id)
    assert existing is not None and existing.reason == "candidate_erasure"


async def test_symlink_escape_fails_closed_without_reading_or_promoting(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(tmp_path / "primary-objects")
    candidate, source = await _legacy_candidate(db, legacy_root, label="symlink")
    source.unlink()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_pdf())
    source.symlink_to(outside)

    report = await cutover.run_cutover(
        apply=False,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert report.validated == 0
    assert report.failed == 1
    assert report.failures is not None
    assert report.failures[0]["code"] == "unsafe_legacy_reference"
    assert outside.is_file()
    assert await _private_keys(primary, candidate.organization_id) == []
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None and unchanged.private_object_id is None


async def test_in_root_symlink_alias_is_not_a_legacy_object_identity(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(tmp_path / "primary-objects")
    candidate, source = await _legacy_candidate(db, legacy_root, label="alias")
    alias_target = source.with_name(f"{uuid4()}.pdf")
    source.replace(alias_target)
    source.symlink_to(alias_target.name)

    report = await cutover.run_cutover(
        apply=False,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )
    assert report.validated == 0
    assert report.failed == 1
    assert report.failures[0]["code"] == "unsafe_legacy_reference"
    assert alias_target.is_file()
    assert await _private_keys(primary, candidate.organization_id) == []
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None and unchanged.private_object_id is None


async def test_streaming_preflight_rejects_duplicate_legacy_ownership_once(
    db,
    tmp_path: Path,
    monkeypatch,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db, legacy_root, label="duplicate-ownership"
    )
    duplicate = Candidate(
        organization_id=candidate.organization_id,
        source="manual",
        source_ref="duplicate legacy owner",
        raw_cv="second resource must not adopt the same file",
        pdf_path=candidate.pdf_path,
    )
    db.add(duplicate)
    await db.commit()
    candidate_ids = (candidate.id, duplicate.id)
    legacy_reference = candidate.pdf_path

    async def forbidden_collector(*_args, **_kwargs):
        raise AssertionError("production cutover must stream bounded batches")

    monkeypatch.setattr(
        cutover,
        "enumerate_legacy_resources",
        forbidden_collector,
    )
    report = await cutover.run_cutover(
        apply=True,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )

    assert report.scanned == 2
    assert report.migrated == 0
    assert report.failed == 2
    assert {item["code"] for item in report.failures} == {
        "duplicate_legacy_binding"
    }
    assert source.is_file()
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    for identifier in candidate_ids:
        unchanged = await db.get(Candidate, identifier)
        assert unchanged is not None and unchanged.private_object_id is None
        assert unchanged.pdf_path == legacy_reference


async def test_tenant_filtered_cutover_rejects_cross_tenant_source_alias(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db,
        legacy_root,
        label="filtered-cross-tenant",
    )
    other_organization = Organization(
        name="Other legacy owner",
        slug=f"other-legacy-owner-{uuid4().hex[:8]}",
    )
    db.add(other_organization)
    await db.flush()
    other = Candidate(
        organization_id=other_organization.id,
        source="manual",
        source_ref="cross-tenant legacy owner",
        raw_cv="must not share physical legacy evidence",
        pdf_path=candidate.pdf_path,
    )
    db.add(other)
    await db.commit()

    report = await cutover.run_cutover(
        apply=True,
        organization_id=candidate.organization_id,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )

    assert report.scanned == 1
    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures[0]["code"] == "duplicate_legacy_binding"
    assert source.is_file()
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    await db.refresh(candidate)
    await db.refresh(other)
    assert candidate.private_object_id is None
    assert other.private_object_id is None
    assert candidate.pdf_path == other.pdf_path


async def test_dry_run_does_not_initialize_primary_or_chmod_legacy_root(
    db,
    tmp_path: Path,
    monkeypatch,
):
    legacy_root = tmp_path / "legacy-storage"
    candidate, _source = await _legacy_candidate(
        db,
        legacy_root,
        label="read-only-dry-run",
    )
    legacy_root.chmod(0o750)
    primary_root = tmp_path / "must-not-be-created"
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "legacy_private_storage_dir",
        str(legacy_root),
    )
    monkeypatch.setattr(settings, "blob_store_backend", "local")
    monkeypatch.setattr(settings, "storage_dir", str(primary_root))

    report = await cutover.run_cutover(
        apply=False,
        session_factory=SessionLocal,
        scanner=FakeMalwareScanner(),
    )

    assert report.failed == 0
    assert report.validated == 1
    assert primary_root.exists() is False
    assert legacy_root.stat().st_mode & 0o777 == 0o750
    await db.rollback()
    await db.refresh(candidate)
    assert candidate.private_object_id is None


async def test_cutover_migrates_every_resource_spec_under_one_atomic_contract(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary = LocalBlobStore(tmp_path / "primary-objects")
    legacy = LocalBlobStore(legacy_root)
    organization, resources, sources = await _legacy_resources_for_every_spec(
        db, legacy_root
    )
    source_digests = {
        resource_type: hashlib.sha256(path.read_bytes()).hexdigest()
        for resource_type, path in sources.items()
    }

    report = await cutover.run_cutover(
        apply=True,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )

    assert report.scanned == 4
    assert report.migrated == 4
    assert report.failed == 0
    assert report.source_files == 4
    assert report.orphan_files == 0
    assert set(sources) == {
        "candidate",
        "assessment_file",
        "task_submission",
        "interview_recording",
    }
    for resource_type, path in sources.items():
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source_digests[resource_type]

    await db.rollback()
    for resource in resources:
        await db.refresh(resource)
    registered = list(
        (
            await db.execute(
                select(PrivateObject).where(
                    PrivateObject.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert len(registered) == 4
    by_type = {item.resource_type: item for item in registered}
    assert set(by_type) == set(sources)
    resource_contracts = (
        (resources[0], "candidate", "candidates", "pdf_path"),
        (resources[1], "assessment_file", "assessments", "storage_path"),
        (resources[2], "task_submission", "tasks", "file_path"),
        (resources[3], "interview_recording", "recordings", "media_path"),
    )
    for resource, resource_type, category, path_attribute in resource_contracts:
        record = by_type[resource_type]
        assert resource.private_object_id == record.id
        assert record.resource_id == resource.id
        assert record.category == category
        assert record.object_key == getattr(resource, path_attribute)
        assert record.object_key.startswith(f"private/{organization.id}/{category}/")
        assert record.sha256 == source_digests[resource_type]
        assert await primary.stat(record.object_key) is not None
    assessment = resources[1]
    assert isinstance(assessment, AssessmentFile)
    assert assessment.status == "ready"
    assert assessment.media_type == "text/plain"
    assert assessment.scan_result == "fake:clean"
    assert (
        await db.execute(
            select(func.count()).select_from(LegacyPrivateObjectDeletion)
        )
    ).scalar_one() == 4
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 4


async def test_deleted_assessment_evidence_is_not_resurrected_by_cutover(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(tmp_path / "primary-objects")
    organization, resources, sources = await _legacy_resources_for_every_spec(
        db,
        legacy_root,
    )
    assessment = next(
        item for item in resources if isinstance(item, AssessmentFile)
    )
    pending = await enqueue_legacy_private_object_deletion(
        db,
        organization_id=organization.id,
        category="assessments",
        resource_type="assessment_file",
        resource_id=assessment.id,
        legacy_reference=assessment.storage_path,
        reason="resource_deleted",
        store=legacy,
    )
    assert pending is not None
    assessment.status = "deleted"
    assessment.deleted_at = datetime.now(UTC)
    await db.commit()

    report = await cutover.run_cutover(
        apply=True,
        batch_size=1,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )

    assert report.scanned == 3
    assert report.migrated == 3
    assert report.failed == 0
    assert report.source_files == 4
    assert report.orphan_files == 0
    assert report.pending_deletion_obligations == 4
    await db.rollback()
    await db.refresh(assessment)
    assert assessment.status == "deleted"
    assert assessment.private_object_id is None
    assert sources["assessment_file"].is_file()
    assert (
        await db.execute(
            select(func.count())
            .select_from(PrivateObject)
            .where(PrivateObject.resource_id == assessment.id)
        )
    ).scalar_one() == 0
    existing = await db.get(LegacyPrivateObjectDeletion, pending.id)
    assert existing is not None and existing.completed_at is None


async def test_source_change_during_scan_fails_without_binding_or_primary_residue(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(db, legacy_root, label="source-change")

    class MutatingScanner:
        async def scan(self, _path: Path) -> ScanResult:
            source.write_bytes(source.read_bytes() + b"source changed during scan")
            return ScanResult(engine="mutating-test", result="clean")

    report = await cutover.run_cutover(
        apply=True,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=MutatingScanner(),
    )

    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures == [
        {
            "resource_type": "candidate",
            "resource_id": str(candidate.id),
            "code": "legacy_object_changed",
        }
    ]
    assert source.is_file()
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None
    assert unchanged.private_object_id is None
    assert unchanged.pdf_path == f"storage/cvs/{source.stem}.pdf"
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 0


async def test_source_change_before_deletion_snapshot_cannot_commit_stale_primary(
    db,
    tmp_path: Path,
    monkeypatch,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db, legacy_root, label="source-change-before-outbox"
    )
    original = source.read_bytes()
    original_enqueue = cutover.enqueue_legacy_private_object_deletion

    async def replace_then_snapshot(*args, **kwargs):
        source.write_bytes(_pdf() + b"\n% externally replaced source\n")
        return await original_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        cutover,
        "enqueue_legacy_private_object_deletion",
        replace_then_snapshot,
    )
    report = await cutover.run_cutover(
        apply=True,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )

    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures[0]["code"] == "legacy_object_changed"
    assert source.read_bytes() != original
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None
    assert unchanged.private_object_id is None
    assert unchanged.pdf_path == f"storage/cvs/{source.stem}.pdf"
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 0


@pytest.mark.parametrize(
    ("verdict", "expected_code"),
    [
        ("infected", "malware_detected"),
        ("unavailable", "malware_scanner_unavailable"),
    ],
)
async def test_scanner_failure_leaves_legacy_source_and_no_cutover_residue(
    db,
    tmp_path: Path,
    verdict: str,
    expected_code: str,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(db, legacy_root, label=verdict)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    report = await cutover.run_cutover(
        apply=True,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(verdict=verdict),
    )

    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures[0] == {
        "resource_type": "candidate",
        "resource_id": str(candidate.id),
        "code": expected_code,
    }
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None and unchanged.private_object_id is None
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0


async def test_primary_promotion_failure_cleans_destination_and_quarantine(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)

    class PromoteThenFailStore(LocalBlobStore):
        async def promote(self, source_key: str, destination_key: str) -> None:
            await super().promote(source_key, destination_key)
            raise BlobStoreError("simulated lost promotion acknowledgement")

    primary = PromoteThenFailStore(primary_root)
    candidate, source = await _legacy_candidate(db, legacy_root, label="promotion-fail")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    report = await cutover.run_cutover(
        apply=True,
        session_factory=SessionLocal,
        legacy_store=legacy,
        primary_store=primary,
        scanner=FakeMalwareScanner(),
    )

    assert report.migrated == 0
    assert report.failed == 1
    assert report.failures[0]["code"] == "blob_store_unavailable"
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    unchanged = await db.get(Candidate, candidate.id)
    assert unchanged is not None and unchanged.private_object_id is None
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0


async def test_cutover_promotion_cancellation_leaves_no_primary_or_database_residue(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)

    class PromoteThenCancelStore(LocalBlobStore):
        async def promote(self, source_key: str, destination_key: str) -> None:
            await super().promote(source_key, destination_key)
            raise asyncio.CancelledError

    primary = PromoteThenCancelStore(primary_root)
    candidate, source = await _legacy_candidate(
        db,
        legacy_root,
        label="promotion-cancel",
    )
    item = (
        await cutover.enumerate_legacy_resources(SessionLocal, batch_size=1)
    )[0]

    with pytest.raises(asyncio.CancelledError):
        await cutover.apply_legacy_resource(
            SessionLocal,
            item,
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        )

    assert source.is_file()
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    await db.refresh(candidate)
    assert candidate.private_object_id is None
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0


async def test_cutover_post_promotion_validation_cancellation_cleans_primary(
    db,
    tmp_path: Path,
    monkeypatch,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db,
        legacy_root,
        label="post-promotion-cancel",
    )
    item = (
        await cutover.enumerate_legacy_resources(SessionLocal, batch_size=1)
    )[0]
    original_snapshot = cutover.snapshot_legacy_file
    calls = 0

    async def cancel_second_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return await original_snapshot(*args, **kwargs)

    monkeypatch.setattr(cutover, "snapshot_legacy_file", cancel_second_snapshot)
    with pytest.raises(asyncio.CancelledError):
        await cutover.apply_legacy_resource(
            SessionLocal,
            item,
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        )

    assert calls == 2
    assert source.is_file()
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    await db.refresh(candidate)
    assert candidate.private_object_id is None
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0


async def test_cutover_registration_cancellation_rolls_back_and_cleans_primary(
    db,
    tmp_path: Path,
    monkeypatch,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db,
        legacy_root,
        label="registration-cancel",
    )
    item = (
        await cutover.enumerate_legacy_resources(SessionLocal, batch_size=1)
    )[0]
    original_register = cutover.register_migrated_ready_private_object

    async def register_then_cancel(*args, **kwargs):
        await original_register(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        cutover,
        "register_migrated_ready_private_object",
        register_then_cancel,
    )
    with pytest.raises(asyncio.CancelledError):
        await cutover.apply_legacy_resource(
            SessionLocal,
            item,
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        )

    assert source.is_file()
    assert not any(path.is_file() for path in primary_root.rglob("*"))
    await db.rollback()
    await db.refresh(candidate)
    assert candidate.private_object_id is None
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 0
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 0


@pytest.mark.parametrize("commit_landed", [False, True])
async def test_cutover_commit_cancellation_resolves_authoritative_outcome(
    db,
    tmp_path: Path,
    commit_landed: bool,
):
    legacy_root = tmp_path / "legacy-storage"
    primary_root = tmp_path / "primary-objects"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(primary_root)
    candidate, source = await _legacy_candidate(
        db,
        legacy_root,
        label=f"commit-cancel-{commit_landed}",
    )
    item = (
        await cutover.enumerate_legacy_resources(SessionLocal, batch_size=1)
    )[0]

    class CancelledCommitSession:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def commit(self):
            if commit_landed:
                await self._inner.commit()
            raise asyncio.CancelledError

    calls = 0

    def session_factory():
        nonlocal calls
        calls += 1
        session = SessionLocal()
        return CancelledCommitSession(session) if calls == 1 else session

    with pytest.raises(asyncio.CancelledError):
        await cutover.apply_legacy_resource(
            session_factory,
            item,
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        )

    assert source.is_file()
    await db.rollback()
    await db.refresh(candidate)
    primary_keys = await _private_keys(primary, candidate.organization_id)
    private_count = (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one()
    deletion_count = (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one()
    usage_count = (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one()
    if commit_landed:
        assert candidate.private_object_id is not None
        assert len(primary_keys) == 1
        assert private_count == deletion_count == usage_count == 1
    else:
        assert candidate.private_object_id is None
        assert primary_keys == []
        assert private_count == deletion_count == usage_count == 0


async def test_external_cutover_cancellation_drains_commit_and_verifies_binding(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    primary = LocalBlobStore(tmp_path / "primary-objects")
    legacy = LocalBlobStore(legacy_root)
    candidate, source = await _legacy_candidate(
        db,
        legacy_root,
        label="external-commit-cancel",
    )
    item = (
        await cutover.enumerate_legacy_resources(SessionLocal, batch_size=1)
    )[0]
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()

    class PausedCommitSession:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def commit(self):
            commit_started.set()
            await release_commit.wait()
            await self._inner.commit()

    calls = 0

    def session_factory():
        nonlocal calls
        calls += 1
        session = SessionLocal()
        return PausedCommitSession(session) if calls == 1 else session

    task = asyncio.create_task(
        cutover.apply_legacy_resource(
            session_factory,
            item,
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        )
    )
    await commit_started.wait()
    task.cancel()
    release_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert source.is_file()
    await db.rollback()
    await db.refresh(candidate)
    assert candidate.private_object_id is not None
    assert len(await _private_keys(primary, candidate.organization_id)) == 1
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 1
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 1
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 1


async def test_concurrent_cutover_is_exact_once_for_registry_usage_and_outbox(
    db,
    tmp_path: Path,
):
    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = LocalBlobStore(tmp_path / "primary-objects")
    candidate, source = await _legacy_candidate(db, legacy_root, label="concurrent")
    items = await cutover.enumerate_legacy_resources(SessionLocal, batch_size=1)
    assert len(items) == 1

    outcomes = await asyncio.gather(
        cutover.apply_legacy_resource(
            SessionLocal,
            items[0],
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        ),
        cutover.apply_legacy_resource(
            SessionLocal,
            items[0],
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        ),
    )

    assert sorted(outcomes) == ["migrated", "skipped_registered"]
    assert source.is_file()
    await db.rollback()
    await db.refresh(candidate)
    assert candidate.private_object_id is not None
    assert len(await _private_keys(primary, candidate.organization_id)) == 1
    assert (
        await db.execute(select(func.count()).select_from(PrivateObject))
    ).scalar_one() == 1
    assert (
        await db.execute(select(func.count()).select_from(LegacyPrivateObjectDeletion))
    ).scalar_one() == 1
    assert (
        await db.execute(select(func.count()).select_from(UsageLedgerEntry))
    ).scalar_one() == 1


async def test_operator_guard_refuses_a_real_non_bypass_database_role(db):
    role_name = f"legacy_cutover_nobypass_{uuid4().hex}"
    await db.execute(
        text(
            f'CREATE ROLE "{role_name}" NOLOGIN NOSUPERUSER NOCREATEDB '
            "NOCREATEROLE NOINHERIT NOBYPASSRLS"
        )
    )
    await db.execute(text(f'GRANT "{role_name}" TO CURRENT_USER'))
    await db.execute(text(f'SET LOCAL ROLE "{role_name}"'))
    try:
        with pytest.raises(
            cutover.OperatorConnectionRequired,
            match="operator database role with RLS bypass",
        ):
            await cutover.assert_operator_connection(db)
    finally:
        await db.execute(text("RESET ROLE"))
        await db.rollback()


@pytest.mark.skipif(
    not os.environ.get("MINIO_TEST_ENDPOINT"),
    reason="Set MINIO_TEST_ENDPOINT and credentials for live compatibility",
)
async def test_live_minio_cutover_registry_and_download_route(
    db,
    tmp_path: Path,
    monkeypatch,
):
    """Exercise local legacy intake through a real S3-compatible primary."""

    legacy_root = tmp_path / "legacy-storage"
    legacy = LocalBlobStore(legacy_root)
    primary = S3BlobStore(
        endpoint=os.environ["MINIO_TEST_ENDPOINT"],
        bucket=os.environ["MINIO_TEST_BUCKET"],
        access_key=os.environ["MINIO_TEST_ACCESS_KEY"],
        secret_key=os.environ["MINIO_TEST_SECRET_KEY"],
    )
    candidate, source = await _legacy_candidate(db, legacy_root, label="minio")
    recruiter = User(
        organization_id=candidate.organization_id,
        email=f"legacy-minio-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    db.add(recruiter)
    await db.commit()
    expected = source.read_bytes()
    object_key: str | None = None
    try:
        report = await cutover.run_cutover(
            apply=True,
            session_factory=SessionLocal,
            legacy_store=legacy,
            primary_store=primary,
            scanner=FakeMalwareScanner(),
        )
        assert report.migrated == 1 and report.failed == 0
        await db.rollback()
        await db.refresh(candidate)
        assert candidate.private_object_id is not None
        registered = await db.get(PrivateObject, candidate.private_object_id)
        assert registered is not None
        object_key = registered.object_key
        metadata = await primary.stat(object_key)
        assert metadata is not None and metadata.size_bytes == len(expected)
        assert b"".join(
            [chunk async for chunk in primary.iter_bytes(object_key)]
        ) == expected

        monkeypatch.setattr(upload_service, "get_blob_store", lambda: primary)
        response = await download_candidate_cv(
            candidate.id,
            db=db,
            user=recruiter,
        )
        assert response.status_code == 200
        assert b"".join([chunk async for chunk in response.body_iterator]) == expected
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert source.is_file()
    finally:
        if object_key is not None:
            await primary.delete(object_key)
