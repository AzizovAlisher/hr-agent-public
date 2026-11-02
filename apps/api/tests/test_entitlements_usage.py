"""Authoritative contract usage and mutation-boundary regression coverage."""

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request, Response, UploadFile
from pypdf import PdfWriter
from sqlalchemy import func, select
from starlette.datastructures import Headers

from app.config import get_settings
from app.db import SessionLocal
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.enterprise import EnterprisePolicy
from app.models.notification import Notification
from app.models.operations import OrganizationEntitlement, UsageLedgerEntry
from app.models.organization import Organization
from app.models.position import Position
from app.models.private_object import PrivateObject, PrivateObjectDeletion
from app.models.sourcing import SourceImportRun, SourceIntegration
from app.models.user import User
from app.problem import entitlement_problem_handler
from app.routers.auth import accept_user_invitation
from app.routers.candidates import delete_candidate, upload_file, upload_text
from app.routers.enterprise import restore_from_trash, run_retention
from app.routers.positions import create_position, delete_position, update_position
from app.routers.users import create_user, create_user_invitation
from app.schemas.auth import InvitationAcceptRequest, UserCreate, UserInvitationCreate
from app.schemas.candidates import TextUploadRequest
from app.schemas.enterprise import RetentionRunRequest
from app.schemas.positions import PositionCreate, PositionUpdate
from app.schemas.sourcing import SourceCandidateInput
from app.services.ai_hiring_attachments import import_ai_attachments
from app.services.entitlements import (
    EntitlementExceeded,
    UsageIdempotencyConflict,
    entitlement_snapshot,
    list_usage,
    record_usage,
)
from app.services.public_applications import submit_public_application
from app.services.sourcing import ingest_source_candidates
from app.storage import (
    DeletionSettlement,
    LocalBlobStore,
    enqueue_private_object_deletion,
    settle_private_object_deletion,
)

pytestmark = pytest.mark.asyncio


async def _tenant(db, label: str) -> tuple[Organization, User]:
    marker = uuid4().hex[:10]
    organization = Organization(name=label, slug=f"ent-{label.lower()}-{marker}")
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"admin-{marker}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add(admin)
    await db.commit()
    return organization, admin


def _pdf_upload(filename: str = "resume.pdf") -> UploadFile:
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return UploadFile(
        filename=filename,
        file=io.BytesIO(output.getvalue()),
        headers=Headers({"content-type": "application/pdf"}),
    )


async def test_current_state_snapshots_use_live_tables_without_backfill(db):
    organization, admin = await _tenant(db, "Existing")
    inactive = User(
        organization_id=organization.id,
        email=f"inactive-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
        is_active=False,
    )
    open_position = Position(
        organization_id=organization.id, title="Open", status="open"
    )
    paused_position = Position(
        organization_id=organization.id, title="Paused", status="paused"
    )
    deleted_position = Position(
        organization_id=organization.id,
        title="Deleted open",
        status="open",
        deleted_at=datetime.now(UTC),
    )
    canonical = Candidate(
        organization_id=organization.id, source="manual", raw_cv="canonical"
    )
    deleted_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        raw_cv="deleted",
        deleted_at=datetime.now(UTC),
    )
    db.add_all(
        [
            inactive,
            open_position,
            paused_position,
            deleted_position,
            canonical,
            deleted_candidate,
        ]
    )
    await db.flush()
    alias = Candidate(
        organization_id=organization.id,
        source="manual",
        raw_cv="alias",
        merged_into_candidate_id=canonical.id,
        merged_at=datetime.now(UTC),
        merged_by_user_id=admin.id,
    )
    ready_id = uuid4()
    pending_id = uuid4()
    deleted_id = uuid4()
    db.add_all(
        [
            alias,
            PrivateObject(
                id=ready_id,
                organization_id=organization.id,
                category="candidates",
                object_key=f"private/{organization.id}/candidates/{ready_id}.pdf",
                original_filename="ready.pdf",
                media_type="application/pdf",
                size_bytes=100,
                sha256="a" * 64,
                state="ready",
                resource_type="candidate",
                resource_id=canonical.id,
                ready_at=datetime.now(UTC),
            ),
            PrivateObject(
                id=pending_id,
                organization_id=organization.id,
                category="candidates",
                object_key=f"private/{organization.id}/candidates/{pending_id}.pdf",
                original_filename="pending.pdf",
                media_type="application/pdf",
                size_bytes=50,
                sha256="b" * 64,
                state="delete_pending",
                resource_type="candidate",
                resource_id=canonical.id,
                ready_at=datetime.now(UTC),
            ),
            PrivateObject(
                id=deleted_id,
                organization_id=organization.id,
                category="candidates",
                object_key=f"private/{organization.id}/candidates/{deleted_id}.pdf",
                original_filename="deleted",
                media_type="application/octet-stream",
                size_bytes=500,
                sha256="c" * 64,
                state="deleted",
                resource_type="candidate",
                resource_id=canonical.id,
                ready_at=datetime.now(UTC),
                deleted_at=datetime.now(UTC),
            ),
        ]
    )
    # A stale/manual ledger entry must remain auditable without inflating a
    # current-state metric.
    await record_usage(
        db,
        organization_id=organization.id,
        metric="storage_bytes",
        amount=9_999,
        kind="adjustment",
        idempotency_key="stale-storage-adjustment",
        enforce_limit=False,
    )
    await db.commit()

    seats = await entitlement_snapshot(db, organization.id, "seats")
    jobs = await entitlement_snapshot(db, organization.id, "active_jobs")
    candidates = await entitlement_snapshot(db, organization.id, "candidates")
    storage = await entitlement_snapshot(db, organization.id, "storage_bytes")
    assert (seats.used, jobs.used, candidates.used, storage.used) == (1, 1, 1, 150)
    assert {seats.basis, jobs.basis, candidates.basis, storage.basis} == {
        "current_state"
    }
    assert storage.period == "contract"


async def test_entitlement_problem_rolls_back_domain_activity_and_ledger(db):
    organization, admin = await _tenant(db, "Blocked")
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="candidates",
            limit_value=0,
        )
    )
    await db.commit()

    with pytest.raises(EntitlementExceeded) as denied:
        await upload_text(
            TextUploadRequest(
                raw_text="A complete candidate profile with enough detail for intake.",
                source_ref="blocked",
            ),
            db=db,
            user=admin,
        )
    response = await entitlement_problem_handler(
        Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/candidates/upload/text",
                "headers": [],
            }
        ),
        denied.value,
    )
    await db.rollback()
    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/problem+json")
    payload = json.loads(response.body)
    assert {
        key: payload[key] for key in ("code", "metric", "limit", "used", "requested")
    } == {
        "code": "entitlement_exceeded",
        "metric": "candidates",
        "limit": 0,
        "used": 0,
        "requested": 1,
    }
    assert await db.scalar(select(func.count(Candidate.id))) == 0
    assert await db.scalar(select(func.count(CandidateActivity.id))) == 0
    assert (
        await db.scalar(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.metric == "candidates"
            )
        )
        == 0
    )


async def test_concurrent_provisioning_serializes_the_last_seat(db):
    organization, admin = await _tenant(db, "Seats")
    organization_id = organization.id
    admin_id = admin.id
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="seats",
            limit_value=2,
        )
    )
    await db.commit()

    async def provision(address: str):
        async with SessionLocal() as session:
            actor = await session.get(User, admin_id)
            assert actor is not None
            return await create_user(
                UserCreate(
                    email=address,
                    password="Safe-password-1",
                    role="recruiter",
                    locale="en",
                ),
                db=session,
                user=actor,
            )

    results = await asyncio.gather(
        provision("last-a@example.com"),
        provision("last-b@example.com"),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1, repr(results)
    denied = [item for item in results if isinstance(item, EntitlementExceeded)]
    assert len(denied) == 1
    assert (denied[0].limit, denied[0].used, denied[0].requested) == (2, 2, 1)
    db.expire_all()
    assert (
        await db.scalar(
            select(func.count(User.id)).where(
                User.organization_id == organization_id,
                User.is_active.is_(True),
            )
        )
        == 2
    )
    assert (
        await db.scalar(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric == "seats",
            )
        )
        == 1
    )


async def test_invitation_does_not_reserve_a_seat_and_acceptance_fails_atomically(db):
    organization, admin = await _tenant(db, "InvitationSeats")
    organization_id = organization.id
    entitlement = OrganizationEntitlement(
        organization_id=organization.id,
        metric="seats",
        limit_value=2,
    )
    db.add(entitlement)
    await db.commit()
    invitation = await create_user_invitation(
        UserInvitationCreate(
            email="pending-seat@example.com",
            role="recruiter",
            locale="en",
        ),
        response=Response(),
        db=db,
        user=admin,
    )
    invited = await db.get(User, invitation.user_id)
    assert invited is not None and invited.is_active is False
    assert (await entitlement_snapshot(db, organization_id, "seats")).used == 1

    await create_user(
        UserCreate(
            email="fills-last-seat@example.com",
            password="Safe-password-1",
            role="recruiter",
            locale="en",
        ),
        db=db,
        user=admin,
    )
    raw_token = invitation.access_url.rsplit("/", 1)[-1]
    with pytest.raises(EntitlementExceeded):
        await accept_user_invitation(
            raw_token,
            InvitationAcceptRequest(password="Accepted-password-1"),
            Response(),
            db,
        )
    await db.rollback()
    invited = await db.get(User, invitation.user_id)
    assert invited is not None and invited.is_active is False
    assert (await entitlement_snapshot(db, organization_id, "seats")).used == 2


async def test_candidate_delete_restore_is_race_safe_and_tenant_scoped(db):
    organization, admin = await _tenant(db, "CandidateLifecycle")
    _other_organization, other_admin = await _tenant(db, "OtherLifecycle")
    organization_id = organization.id
    admin_id = admin.id
    other_admin_id = other_admin.id
    created = await upload_text(
        TextUploadRequest(
            raw_text="A complete candidate profile for lifecycle testing.",
            source_ref="lifecycle",
        ),
        db=db,
        user=admin,
    )
    candidate_id = created.id
    assert (await entitlement_snapshot(db, organization_id, "candidates")).used == 1

    async def remove_once():
        async with SessionLocal() as session:
            actor = await session.get(User, admin_id)
            assert actor is not None
            return await delete_candidate(candidate_id, db=session, user=actor)

    delete_results = await asyncio.gather(
        remove_once(),
        remove_once(),
        return_exceptions=True,
    )
    assert sum(result is None for result in delete_results) == 1
    assert (
        sum(
            isinstance(result, HTTPException) and result.status_code == 404
            for result in delete_results
        )
        == 1
    )
    db.expire_all()
    assert (await entitlement_snapshot(db, organization_id, "candidates")).used == 0
    other_admin = await db.get(User, other_admin_id)
    assert other_admin is not None

    with pytest.raises(HTTPException) as cross_tenant:
        await restore_from_trash(
            "candidate",
            candidate_id,
            db=db,
            user=other_admin,
        )
    assert cross_tenant.value.status_code == 404
    await db.rollback()

    async def restore_once():
        async with SessionLocal() as session:
            actor = await session.get(User, admin_id)
            assert actor is not None
            return await restore_from_trash(
                "candidate",
                candidate_id,
                db=session,
                user=actor,
            )

    restore_results = await asyncio.gather(
        restore_once(),
        restore_once(),
        return_exceptions=True,
    )
    assert sum(result is None for result in restore_results) == 1
    assert (
        sum(
            isinstance(result, HTTPException) and result.status_code == 404
            for result in restore_results
        )
        == 1
    )
    db.expire_all()
    assert (await entitlement_snapshot(db, organization_id, "candidates")).used == 1
    events = list(
        (
            await db.execute(
                select(UsageLedgerEntry)
                .where(
                    UsageLedgerEntry.organization_id == organization_id,
                    UsageLedgerEntry.metric == "candidates",
                )
                .order_by(UsageLedgerEntry.occurred_at, UsageLedgerEntry.id)
            )
        ).scalars()
    )
    assert [event.amount for event in events] == [1, -1, 1]


async def test_position_open_close_delete_lifecycle_releases_and_reuses_capacity(db):
    organization, admin = await _tenant(db, "Jobs")
    organization_id = organization.id
    admin_id = admin.id
    db.add(
        OrganizationEntitlement(
            organization_id=organization.id,
            metric="active_jobs",
            limit_value=1,
        )
    )
    await db.commit()

    first = await create_position(
        PositionCreate(title="First", status="open"), db=db, user=admin
    )
    first_id = first.id
    with pytest.raises(EntitlementExceeded):
        await create_position(
            PositionCreate(title="Blocked", status="open"), db=db, user=admin
        )
    await db.rollback()
    admin = await db.get(User, admin_id)
    assert admin is not None
    await update_position(first_id, PositionUpdate(status="paused"), db=db, user=admin)
    second = await create_position(
        PositionCreate(title="Second", status="open"), db=db, user=admin
    )
    # A no-op replay never emits another usage event.
    before = await db.scalar(select(func.count(UsageLedgerEntry.id)))
    await update_position(second.id, PositionUpdate(status="open"), db=db, user=admin)
    assert await db.scalar(select(func.count(UsageLedgerEntry.id))) == before
    await delete_position(second.id, db=db, user=admin)
    assert (await entitlement_snapshot(db, organization_id, "active_jobs")).used == 0

    reopened = await update_position(
        first_id, PositionUpdate(status="open"), db=db, user=admin
    )
    assert reopened.status == "open"
    assert (await entitlement_snapshot(db, organization_id, "active_jobs")).used == 1


async def test_source_import_meters_requested_rows_but_not_duplicate_candidates(db):
    organization, admin = await _tenant(db, "Imports")
    organization_id = organization.id
    position = Position(
        organization_id=organization.id,
        title="Import target",
        status="open",
        created_by=admin.id,
    )
    integration = SourceIntegration(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        provider="csv",
        name="Entitlement import",
        configuration={},
        status="ready",
    )
    db.add_all(
        [
            position,
            integration,
            OrganizationEntitlement(
                organization_id=organization.id,
                metric="candidates",
                limit_value=1,
            ),
            OrganizationEntitlement(
                organization_id=organization.id,
                metric="candidate_import_rows",
                limit_value=2,
            ),
        ]
    )
    await db.commit()
    row = SourceCandidateInput(
        external_id="duplicate-row",
        position_id=position.id,
        name="One person",
        phone="+992900000001",
        cv_text="Python and PostgreSQL experience",
    )
    run = await ingest_source_candidates(
        db,
        integration=integration,
        items=[row, row],
        actor_user_id=admin.id,
        idempotency_key="import-two-rows",
    )
    assert (run.requested_count, run.created_count, run.duplicate_count) == (2, 1, 1)
    replay = await ingest_source_candidates(
        db,
        integration=integration,
        items=[row, row],
        actor_user_id=admin.id,
        idempotency_key="import-two-rows",
    )
    assert replay.id == run.id
    assert (await entitlement_snapshot(db, organization.id, "candidates")).used == 1
    assert (
        await entitlement_snapshot(db, organization.id, "candidate_import_rows")
    ).used == 2

    with pytest.raises(EntitlementExceeded):
        await ingest_source_candidates(
            db,
            integration=integration,
            items=[row.model_copy(update={"external_id": "another-request"})],
            actor_user_id=admin.id,
            idempotency_key="third-row",
        )
    await db.rollback()
    assert (
        await db.scalar(
            select(func.count(SourceImportRun.id)).where(
                SourceImportRun.organization_id == organization_id
            )
        )
        == 1
    )


async def test_duplicate_public_applicant_reuses_candidate_without_another_charge(db):
    organization, admin = await _tenant(db, "Public")
    first = Position(
        organization_id=organization.id,
        title="First public job",
        status="open",
        created_by=admin.id,
    )
    second = Position(
        organization_id=organization.id,
        title="Second public job",
        status="open",
        created_by=admin.id,
    )
    db.add_all(
        [
            first,
            second,
            OrganizationEntitlement(
                organization_id=organization.id,
                metric="candidates",
                limit_value=1,
            ),
        ]
    )
    await db.commit()
    submitted = {
        "name": "Same Applicant",
        "phone": "+992900001111",
        "email": "same.applicant@example.com",
        "source": "careers",
        "file": None,
        "raw_text": "A detailed public candidate profile with Python experience.",
        "db": db,
    }
    first_result = await submit_public_application(position=first, **submitted)
    replay = await submit_public_application(position=first, **submitted)
    another_job = await submit_public_application(position=second, **submitted)
    assert replay.candidate_id == first_result.candidate_id
    assert another_job.candidate_id == first_result.candidate_id
    assert replay.application_created is False
    assert another_job.application_created is True
    assert (await entitlement_snapshot(db, organization.id, "candidates")).used == 1
    assert (
        await db.scalar(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization.id,
                UsageLedgerEntry.metric == "candidates",
            )
        )
        == 1
    )

    notification_count = await db.scalar(select(func.count(Notification.id)))
    with pytest.raises(EntitlementExceeded):
        await submit_public_application(
            position=first,
            name="Different Applicant",
            phone="+992900001112",
            email="different.applicant@example.com",
            source="careers",
            file=None,
            raw_text="Another complete public candidate profile with Go experience.",
            db=db,
        )
    assert await db.scalar(select(func.count(Candidate.id))) == 1
    assert await db.scalar(select(func.count(Notification.id))) == notification_count


async def test_storage_limit_cleanup_and_physical_deletion_release(
    db,
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "objects"
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))
    organization, recruiter = await _tenant(db, "Storage")
    organization_id = organization.id
    recruiter_id = recruiter.id
    entitlement = OrganizationEntitlement(
        organization_id=organization.id,
        metric="storage_bytes",
        limit_value=0,
    )
    db.add(entitlement)
    await db.commit()
    entitlement_id = entitlement.id

    with pytest.raises(EntitlementExceeded):
        await upload_file(_pdf_upload(), None, db=db, user=recruiter)
    await db.rollback()
    assert await db.scalar(select(func.count(Candidate.id))) == 0
    assert await db.scalar(select(func.count(PrivateObject.id))) == 0
    assert [path for path in root.rglob("*") if path.is_file()] == []

    entitlement = await db.get(OrganizationEntitlement, entitlement_id)
    assert entitlement is not None
    entitlement.limit_value = None
    await db.commit()
    recruiter = await db.get(User, recruiter_id)
    assert recruiter is not None
    uploaded = await upload_file(
        _pdf_upload("accepted.pdf"), None, db=db, user=recruiter
    )
    candidate = await db.get(Candidate, uploaded.id)
    assert candidate is not None and candidate.private_object_id is not None
    private_object = await db.get(PrivateObject, candidate.private_object_id)
    assert private_object is not None
    initial = await entitlement_snapshot(db, organization_id, "storage_bytes")
    assert initial.used == private_object.size_bytes

    deletion = await enqueue_private_object_deletion(
        db,
        organization_id=organization_id,
        private_object_id=private_object.id,
        reason="admin_request",
    )
    assert deletion is not None
    await db.commit()
    assert (
        await entitlement_snapshot(db, organization_id, "storage_bytes")
    ).used == initial.used
    deletion = await db.get(PrivateObjectDeletion, deletion.id)
    assert deletion is not None
    claim = uuid4()
    deletion.claimed_at = datetime.now(UTC)
    deletion.claim_token = claim
    deletion.attempt_count = 1
    await db.flush()
    result = await settle_private_object_deletion(
        db,
        deletion_id=deletion.id,
        organization_id=organization_id,
        claim_token=claim,
        store=LocalBlobStore(root),
    )
    assert result == DeletionSettlement.DELETED
    await db.commit()
    assert (await entitlement_snapshot(db, organization_id, "storage_bytes")).used == 0
    storage_events = list(
        (
            await db.execute(
                select(UsageLedgerEntry)
                .where(
                    UsageLedgerEntry.organization_id == organization_id,
                    UsageLedgerEntry.metric == "storage_bytes",
                )
                .order_by(UsageLedgerEntry.occurred_at)
            )
        ).scalars()
    )
    assert [event.amount for event in storage_events] == [initial.used, -initial.used]


async def test_active_retention_purge_releases_once_but_soft_purge_does_not(db):
    organization, admin = await _tenant(db, "Retention")
    active_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="old active candidate",
        created_at=datetime.now(UTC) - timedelta(days=400),
    )
    soft_deleted_candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="old deleted candidate",
        deleted_at=datetime.now(UTC) - timedelta(days=60),
        deleted_by_user_id=admin.id,
    )
    db.add_all(
        [
            active_candidate,
            soft_deleted_candidate,
            EnterprisePolicy(
                organization_id=organization.id,
                candidate_retention_days=365,
                deleted_retention_days=30,
                audit_retention_days=90,
            ),
        ]
    )
    await db.commit()
    assert (await entitlement_snapshot(db, organization.id, "candidates")).used == 1

    first = await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=True,
        ),
        db=db,
        user=admin,
    )
    assert first.old_active_candidates == 1
    assert first.deleted_candidates == 1
    assert (await entitlement_snapshot(db, organization.id, "candidates")).used == 0
    release_events = list(
        (
            await db.execute(
                select(UsageLedgerEntry).where(
                    UsageLedgerEntry.organization_id == organization.id,
                    UsageLedgerEntry.metric == "candidates",
                    UsageLedgerEntry.kind == "refund",
                )
            )
        ).scalars()
    )
    assert [event.amount for event in release_events] == [-1]

    # A repeated purge has no live row to release, and the already-soft-deleted
    # row was never counted in current usage in the first place.
    await run_retention(
        RetentionRunRequest(
            confirmation="PURGE",
            include_active_candidates=True,
        ),
        db=db,
        user=admin,
    )
    assert (
        await db.scalar(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization.id,
                UsageLedgerEntry.metric == "candidates",
                UsageLedgerEntry.kind == "refund",
            )
        )
        == 1
    )


async def test_usage_idempotency_conflict_does_not_append_an_event(db):
    organization, _admin = await _tenant(db, "Idempotency")
    resource_id = uuid4()
    await record_usage(
        db,
        organization_id=organization.id,
        metric="candidate_import_rows",
        amount=2,
        kind="usage",
        idempotency_key="import:idempotency-conflict",
        resource_type="source_import",
        resource_id=resource_id,
        metadata={"source": "csv"},
    )
    await db.commit()
    with pytest.raises(UsageIdempotencyConflict):
        await record_usage(
            db,
            organization_id=organization.id,
            metric="candidate_import_rows",
            amount=2,
            kind="usage",
            idempotency_key="import:idempotency-conflict",
            resource_type="source_import",
            resource_id=resource_id,
            metadata={"source": "different"},
        )
    assert (
        await db.scalar(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization.id
            )
        )
        == 1
    )


async def test_usage_cursor_is_stable_and_never_crosses_tenants(db):
    organization, _admin = await _tenant(db, "UsageCursor")
    other_organization, _other_admin = await _tenant(db, "UsageCursorOther")
    for index in range(3):
        await record_usage(
            db,
            organization_id=organization.id,
            metric="candidate_import_rows",
            amount=index + 1,
            kind="usage",
            idempotency_key=f"cursor:{index}",
        )
    await record_usage(
        db,
        organization_id=other_organization.id,
        metric="candidate_import_rows",
        amount=99,
        kind="usage",
        idempotency_key="cursor:other-tenant",
    )
    await db.commit()

    first, cursor = await list_usage(
        db,
        organization_id=organization.id,
        metric="candidate_import_rows",
        limit=2,
        cursor=None,
    )
    assert len(first) == 2
    assert cursor is not None
    second, terminal = await list_usage(
        db,
        organization_id=organization.id,
        metric="candidate_import_rows",
        limit=2,
        cursor=cursor,
    )
    assert len(second) == 1
    assert terminal is None
    assert {row.id for row in first}.isdisjoint(row.id for row in second)
    assert {row.amount for row in [*first, *second]} == {1, 2, 3}

    with pytest.raises(HTTPException) as malformed:
        await list_usage(
            db,
            organization_id=organization.id,
            metric=None,
            limit=2,
            cursor="not-a-cursor",
        )
    assert malformed.value.status_code == 422
    assert malformed.value.detail == {
        "code": "cursor_invalid",
        "detail": "Invalid usage cursor",
    }

    with pytest.raises(HTTPException) as different_filter:
        await list_usage(
            db,
            organization_id=organization.id,
            metric=None,
            limit=2,
            cursor=cursor,
        )
    assert different_filter.value.status_code == 422

    with pytest.raises(HTTPException) as different_tenant:
        await list_usage(
            db,
            organization_id=other_organization.id,
            metric="candidate_import_rows",
            limit=2,
            cursor=cursor,
        )
    assert different_tenant.value.status_code == 422


async def test_ai_attachment_import_limit_is_aggregate_and_rolls_back(
    db,
    tmp_path: Path,
    monkeypatch,
):
    organization, admin = await _tenant(db, "AI aggregate import")
    organization_id = organization.id
    store = LocalBlobStore(tmp_path / "objects")
    attachments: list[dict] = []
    for index in range(2):
        attachment_id = uuid4()
        content = f"candidate {index}".encode()
        source = tmp_path / f"candidate-{index}.txt"
        source.write_bytes(content)
        key = f"private/{organization_id}/ai/{attachment_id}.txt"
        await store.put_file(key, source, content_type="text/plain")
        digest = hashlib.sha256(content).hexdigest()
        db.add(
            PrivateObject(
                id=attachment_id,
                organization_id=organization_id,
                category="ai",
                object_key=key,
                original_filename=source.name,
                media_type="text/plain",
                size_bytes=len(content),
                sha256=digest,
                state="ready",
                resource_type="ai_attachment",
                resource_id=uuid4(),
                ready_at=datetime.now(UTC),
            )
        )
        attachments.append(
            {
                "id": str(attachment_id),
                "key": key,
                "filename": source.name,
                "suffix": ".txt",
                "sha256": digest,
            }
        )
    await db.commit()
    monkeypatch.setattr(
        "app.services.ai_hiring_attachments.MAX_IMPORT_ROWS",
        1,
    )

    with pytest.raises(HTTPException) as failure:
        await import_ai_attachments(db, admin, attachments=attachments, store=store)
    assert failure.value.status_code == 413
    await db.rollback()

    assert (
        await db.execute(
            select(func.count(Candidate.id)).where(
                Candidate.organization_id == organization_id
            )
        )
    ).scalar_one() == 0
    assert (
        await db.execute(
            select(func.count(UsageLedgerEntry.id)).where(
                UsageLedgerEntry.organization_id == organization_id,
                UsageLedgerEntry.metric.in_(("candidates", "candidate_import_rows")),
            )
        )
    ).scalar_one() == 0
