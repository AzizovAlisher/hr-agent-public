"""Safely cut pre-registry private files over to the primary BlobStore.

The command is a dry run unless ``--apply`` is present.  It never removes a
legacy source file.  Applying one item validates and scans a fresh copy,
promotes it into the tenant-private namespace, and atomically commits the
registry binding, resource reference, usage entry, and a digest-bound legacy
deletion outbox row.  The existing legacy worker owns eventual source removal.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import UploadFile
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers

from app.config import get_settings
from app.db import SessionLocal
from app.models.assessment import AssessmentFile
from app.models.candidate import Candidate
from app.models.interview_recording import InterviewRecording
from app.models.private_object import LegacyPrivateObjectDeletion, PrivateObject
from app.models.task_submission import TaskSubmission
from app.services.legacy_private_object_fence import (
    hold_legacy_private_object_cutover_fence,
)
from app.storage import (
    BlobStore,
    BlobStoreError,
    LegacyStorageUnavailableError,
    LocalBlobStore,
    MalwareDetectedError,
    MalwareScanner,
    MalwareScannerUnavailableError,
    UnsafeLegacyPrivateObjectError,
    enqueue_legacy_private_object_deletion,
    get_blob_store,
    get_legacy_blob_store,
    get_malware_scanner,
    register_migrated_ready_private_object,
)
from app.storage.intake import (
    StoredUpload,
    cleanup_failed_intake_key,
    quarantine_scan_promote,
)
from app.storage.legacy import (
    LegacyFileSnapshot,
    normalize_legacy_private_key,
    snapshot_legacy_file,
)
from app.storage.validation import (
    UploadValidationError,
    canonical_media_type,
    validate_upload_path_bounded,
)

MAX_PRIVATE_OBJECT_BYTES = 15 * 1024 * 1024
MAX_BATCH_SIZE = 500
MAX_FAILURE_SAMPLES = 100
MAX_ORPHAN_SAMPLES = 25


class LegacyCutoverError(RuntimeError):
    """A cutover precondition or atomic binding failed closed."""


class OperatorConnectionRequired(LegacyCutoverError):
    """The database role cannot globally inspect forced-RLS tenant rows."""


class LegacyObjectMissing(LegacyCutoverError):
    """A referenced legacy source is absent."""


class LegacyObjectChanged(LegacyCutoverError):
    """A source changed during validation or copy."""


class DuplicateLegacyBinding(LegacyCutoverError):
    """One legacy file is referenced by more than one resource."""


class CutoverInventoryChanged(LegacyCutoverError):
    """The legacy resource estate changed after its bounded preflight."""


class ConflictingLegacyDeletion(LegacyCutoverError):
    """A source is already reserved for a different erasure purpose."""


class CutoverCleanupFailed(LegacyCutoverError):
    """An unregistered promoted object could not be cleaned up."""


class CutoverOutcomeUncertain(LegacyCutoverError):
    """Database outcome could not be verified after a failed commit."""


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    resource_type: str
    category: str
    model: type[Any]
    path_attribute: str


RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec("candidate", "candidates", Candidate, "pdf_path"),
    ResourceSpec(
        "assessment_file", "assessments", AssessmentFile, "storage_path"
    ),
    ResourceSpec("task_submission", "tasks", TaskSubmission, "file_path"),
    ResourceSpec(
        "interview_recording", "recordings", InterviewRecording, "media_path"
    ),
)
SPEC_BY_RESOURCE_TYPE = {spec.resource_type: spec for spec in RESOURCE_SPECS}

ALLOWED_SUFFIXES = {
    "candidates": {".pdf", ".docx"},
    "assessments": {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg"},
    "tasks": {".pdf", ".docx", ".txt", ".md", ".csv"},
    "recordings": {".m4a", ".mp3", ".mp4", ".ogg", ".wav", ".webm"},
}
LEGACY_PREFIXES = ("cvs/", "assessment_files/", "task_files/", "recordings/")


@dataclass(frozen=True, slots=True)
class LegacyResource:
    resource_type: str
    category: str
    resource_id: UUID
    organization_id: UUID
    legacy_reference: str


@dataclass(slots=True)
class CutoverReport:
    mode: str
    scanned: int = 0
    validated: int = 0
    migrated: int = 0
    skipped_registered: int = 0
    failed: int = 0
    source_files: int = 0
    orphan_files: int = 0
    pending_deletion_obligations: int = 0
    orphan_key_digests: list[str] = field(default_factory=list)
    orphan_sample_truncated: bool = False
    orphan_scan_skipped: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    def failure(self, item: LegacyResource, exc: Exception) -> None:
        self.failed += 1
        if len(self.failures) < MAX_FAILURE_SAMPLES:
            self.failures.append(
                {
                    "resource_type": item.resource_type,
                    "resource_id": str(item.resource_id),
                    "code": _error_code(exc),
                }
            )

    def as_dict(self) -> dict[str, object]:
        status = (
            "failed"
            if self.failed
            else "dry_run_pass"
            if self.mode == "dry_run"
            else "applied"
        )
        return {
            "status": status,
            "mode": self.mode,
            "scanned": self.scanned,
            "validated": self.validated,
            "migrated": self.migrated,
            "skipped_registered": self.skipped_registered,
            "failed": self.failed,
            "failure_samples": self.failures,
            "failure_samples_truncated": self.failed > len(self.failures),
            "legacy_source_files": self.source_files,
            "legacy_orphan_files": self.orphan_files,
            "legacy_pending_deletion_obligations": (
                self.pending_deletion_obligations
            ),
            "legacy_orphan_key_digests": self.orphan_key_digests,
            "legacy_orphan_sample_truncated": self.orphan_sample_truncated,
            "legacy_orphan_scan_skipped": self.orphan_scan_skipped,
        }


def _error_code(exc: Exception) -> str:
    if isinstance(exc, UnsafeLegacyPrivateObjectError):
        return "unsafe_legacy_reference"
    if isinstance(exc, LegacyStorageUnavailableError):
        return "legacy_storage_unavailable"
    if isinstance(exc, LegacyObjectMissing):
        return "legacy_object_missing"
    if isinstance(exc, LegacyObjectChanged):
        return "legacy_object_changed"
    if isinstance(exc, DuplicateLegacyBinding):
        return "duplicate_legacy_binding"
    if isinstance(exc, CutoverInventoryChanged):
        return "cutover_inventory_changed"
    if isinstance(exc, ConflictingLegacyDeletion):
        return "conflicting_legacy_deletion"
    if isinstance(exc, UploadValidationError):
        return "unsafe_file_content"
    if isinstance(exc, MalwareDetectedError):
        return "malware_detected"
    if isinstance(exc, MalwareScannerUnavailableError):
        return "malware_scanner_unavailable"
    if isinstance(exc, CutoverCleanupFailed):
        return "promoted_object_cleanup_failed"
    if isinstance(exc, CutoverOutcomeUncertain):
        return "database_outcome_uncertain"
    if isinstance(exc, BlobStoreError):
        return "blob_store_unavailable"
    return "cutover_failed"


async def assert_operator_connection(db: AsyncSession) -> None:
    """Require a role capable of global enumeration under forced RLS."""

    row = (
        await db.execute(
            text(
                "SELECT current_user, (rolsuper OR rolbypassrls) "
                "FROM pg_roles WHERE rolname = current_user"
            ),
            execution_options={"skip_tenant_scope": True},
        )
    ).one()
    role, can_bypass_rls = str(row[0]), bool(row[1])
    if role in {"hr_agent_api_login", "hr_agent_worker_login"} or not can_bypass_rls:
        raise OperatorConnectionRequired(
            "Legacy cutover requires an operator database role with RLS bypass"
        )


def _resource_statement(
    spec: ResourceSpec,
    *,
    organization_id: UUID | None,
    after_id: UUID | None,
    limit: int,
):
    model = spec.model
    path_column = getattr(model, spec.path_attribute)
    conditions = [path_column.is_not(None), model.private_object_id.is_(None)]
    if spec.resource_type == "assessment_file":
        # Deleted assessment evidence already belongs to the durable deletion
        # outbox (or is an orphan requiring operator inspection). Never
        # resurrect it into a ready registry object during the cutover.
        conditions.append(model.status != "deleted")
    if organization_id is not None:
        conditions.append(model.organization_id == organization_id)
    if after_id is not None:
        conditions.append(model.id > after_id)
    return (
        select(model.id, model.organization_id, path_column)
        .where(*conditions)
        .order_by(model.id)
        .limit(limit)
        .execution_options(include_deleted=True, skip_tenant_scope=True)
    )


async def iter_legacy_resource_batches(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
    organization_id: UUID | None = None,
) -> AsyncIterator[list[LegacyResource]]:
    """Yield unregistered resources without materializing the full estate."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    for spec in RESOURCE_SPECS:
        cursor: UUID | None = None
        while True:
            async with session_factory() as db:
                rows = (
                    await db.execute(
                        _resource_statement(
                            spec,
                            organization_id=organization_id,
                            after_id=cursor,
                            limit=batch_size,
                        )
                    )
                ).all()
            if not rows:
                break
            yield [
                LegacyResource(
                    resource_type=spec.resource_type,
                    category=spec.category,
                    resource_id=resource_id,
                    organization_id=tenant_id,
                    legacy_reference=str(reference),
                )
                for resource_id, tenant_id, reference in rows
            ]
            cursor = rows[-1][0]


async def enumerate_legacy_resources(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
    organization_id: UUID | None = None,
) -> list[LegacyResource]:
    """Compatibility collector for focused callers and tests.

    The production cutover path consumes ``iter_legacy_resource_batches``
    directly and therefore never retains the complete row estate.
    """

    items: list[LegacyResource] = []
    async for batch in iter_legacy_resource_batches(
        session_factory,
        batch_size=batch_size,
        organization_id=organization_id,
    ):
        items.extend(batch)
    return items


async def _legacy_ownership_index(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int,
    organization_id: UUID | None,
    store: LocalBlobStore,
) -> dict[str, int]:
    """Build one O(N) canonical-key index using bounded database reads."""

    ownership: dict[str, int] = {}
    async for batch in iter_legacy_resource_batches(
        session_factory,
        batch_size=batch_size,
        organization_id=organization_id,
    ):
        for item in batch:
            try:
                key = _normalized_key(item, store)
            except (UnsafeLegacyPrivateObjectError, LegacyStorageUnavailableError):
                # The processing pass records the item-specific closed failure.
                continue
            ownership[key] = ownership.get(key, 0) + 1
    return ownership


def _normalized_key(item: LegacyResource, store: LocalBlobStore) -> str:
    return normalize_legacy_private_key(
        item.legacy_reference,
        organization_id=item.organization_id,
        category=item.category,
        root=store.root,
    )


async def _snapshot_required(
    item: LegacyResource,
    store: LocalBlobStore,
) -> tuple[str, Path, LegacyFileSnapshot]:
    key = _normalized_key(item, store)
    source = store.local_path(key)
    if source is None:
        raise LegacyObjectMissing("Referenced legacy object is unavailable")
    snapshot = await snapshot_legacy_file(store, key)
    if snapshot is None:
        raise LegacyObjectMissing("Referenced legacy object is unavailable")
    return key, source, snapshot


async def _assert_unique_binding(
    db: AsyncSession,
    item: LegacyResource,
    store: LocalBlobStore,
    expected_key: str,
    *,
    preflight_ownership_count: int | None = None,
) -> None:
    if preflight_ownership_count is not None:
        if preflight_ownership_count != 1:
            raise DuplicateLegacyBinding(
                "One legacy object cannot be bound to multiple resources"
            )
        return
    spec = SPEC_BY_RESOURCE_TYPE[item.resource_type]
    model = spec.model
    path_column = getattr(model, spec.path_attribute)
    rows = (
        await db.execute(
            select(model.id, model.organization_id, path_column)
            .where(
                path_column.is_not(None),
                model.private_object_id.is_(None),
                model.id != item.resource_id,
            )
            .execution_options(include_deleted=True, skip_tenant_scope=True)
        )
    ).all()
    for resource_id, organization_id, reference in rows:
        other = LegacyResource(
            resource_type=item.resource_type,
            category=item.category,
            resource_id=resource_id,
            organization_id=organization_id,
            legacy_reference=str(reference),
        )
        try:
            other_key = _normalized_key(other, store)
        except UnsafeLegacyPrivateObjectError:
            continue
        if other_key == expected_key:
            raise DuplicateLegacyBinding(
                "One legacy object cannot be bound to multiple resources"
            )


async def validate_legacy_resource(
    item: LegacyResource,
    *,
    legacy_store: LocalBlobStore,
    scanner: MalwareScanner,
) -> None:
    """Run the non-mutating half of the production intake contract."""

    key, source, before = await _snapshot_required(item, legacy_store)
    suffix = Path(key).suffix.casefold()
    media_type = canonical_media_type(suffix)
    await validate_upload_path_bounded(
        source,
        filename=Path(key).name,
        media_type=media_type,
        allowed_suffixes=ALLOWED_SUFFIXES[item.category],
    )
    scan = await scanner.scan(source)
    if scan.result != "clean":
        raise MalwareDetectedError("Legacy object did not receive a clean verdict")
    after = await snapshot_legacy_file(legacy_store, key)
    if after != before:
        raise LegacyObjectChanged("Legacy object changed during validation")


async def _binding_committed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    item: LegacyResource,
    object_key: str,
    legacy_key: str,
) -> bool | None:
    """Return None when a failed commit cannot be resolved safely."""

    spec = SPEC_BY_RESOURCE_TYPE[item.resource_type]
    model = spec.model
    try:
        async with session_factory() as verify:
            resource = (
                await verify.execute(
                    select(model)
                    .where(
                        model.id == item.resource_id,
                        model.organization_id == item.organization_id,
                    )
                    .execution_options(include_deleted=True, skip_tenant_scope=True)
                )
            ).scalar_one_or_none()
            if resource is None or resource.private_object_id is None:
                return False
            registered = await verify.get(PrivateObject, resource.private_object_id)
            deletion = (
                await verify.execute(
                    select(LegacyPrivateObjectDeletion).where(
                        LegacyPrivateObjectDeletion.organization_id
                        == item.organization_id,
                        LegacyPrivateObjectDeletion.resource_type
                        == item.resource_type,
                        LegacyPrivateObjectDeletion.resource_id == item.resource_id,
                        LegacyPrivateObjectDeletion.normalized_key == legacy_key,
                    )
                )
            ).scalar_one_or_none()
            return bool(
                registered is not None
                and registered.organization_id == item.organization_id
                and registered.category == item.category
                and registered.object_key == object_key
                and registered.resource_type == item.resource_type
                and registered.resource_id == item.resource_id
                and registered.state == "ready"
                and getattr(resource, spec.path_attribute) == object_key
                and deletion is not None
                and deletion.reason == "replacement"
                and deletion.completed_at is None
            )
    except BaseException:  # Database uncertainty must retain the promoted object.
        return None


async def _resolve_binding_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    item: LegacyResource,
    object_key: str,
    legacy_key: str,
) -> bool | None:
    """Retry a bounded authoritative read after commit acknowledgement loss."""

    for _attempt in range(3):
        committed = await _binding_committed(
            session_factory,
            item=item,
            object_key=object_key,
            legacy_key=legacy_key,
        )
        if committed is not None:
            return committed
    return None


async def apply_legacy_resource(
    session_factory: async_sessionmaker[AsyncSession],
    item: LegacyResource,
    *,
    legacy_store: LocalBlobStore,
    primary_store: BlobStore,
    scanner: MalwareScanner,
    preflight_legacy_key: str | None = None,
    preflight_ownership_count: int | None = None,
) -> str:
    """Apply one resource as an independently atomic, idempotent unit."""

    spec = SPEC_BY_RESOURCE_TYPE[item.resource_type]
    model = spec.model
    promoted: StoredUpload[object] | None = None
    legacy_key: str | None = None
    commit_task: asyncio.Task[None] | None = None
    db = session_factory()
    try:
        current = (
            await db.execute(
                select(model)
                .where(
                    model.id == item.resource_id,
                    model.organization_id == item.organization_id,
                )
                .with_for_update()
                .execution_options(include_deleted=True, skip_tenant_scope=True)
            )
        ).scalar_one_or_none()
        if current is None or current.private_object_id is not None:
            await db.rollback()
            return "skipped_registered"
        reference = getattr(current, spec.path_attribute)
        if reference is None:
            await db.rollback()
            return "skipped_registered"
        locked_item = LegacyResource(
            resource_type=item.resource_type,
            category=item.category,
            resource_id=item.resource_id,
            organization_id=item.organization_id,
            legacy_reference=str(reference),
        )
        legacy_key, source, before = await _snapshot_required(
            locked_item, legacy_store
        )
        if (
            preflight_legacy_key is not None
            and legacy_key != preflight_legacy_key
        ):
            raise CutoverInventoryChanged(
                "Legacy resource changed after the cutover preflight"
            )
        await _assert_unique_binding(
            db,
            locked_item,
            legacy_store,
            legacy_key,
            preflight_ownership_count=preflight_ownership_count,
        )
        suffix = Path(legacy_key).suffix.casefold()
        media_type = canonical_media_type(suffix)
        upload = UploadFile(
            filename=Path(legacy_key).name,
            file=source.open("rb"),
            headers=Headers({"content-type": media_type}),
        )
        try:
            promoted = await quarantine_scan_promote(
                upload,
                organization_id=item.organization_id,
                category=item.category,
                allowed_suffixes=ALLOWED_SUFFIXES[item.category],
                max_bytes=MAX_PRIVATE_OBJECT_BYTES,
                store=primary_store,
                scanner=scanner,
            )
        finally:
            await upload.close()
        after = await snapshot_legacy_file(legacy_store, legacy_key)
        if (
            after != before
            or promoted.size_bytes != before.size_bytes
            or promoted.sha256 != before.sha256
        ):
            raise LegacyObjectChanged("Legacy object changed during cutover")

        deletion = await enqueue_legacy_private_object_deletion(
            db,
            organization_id=item.organization_id,
            category=item.category,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            legacy_reference=legacy_key,
            reason="replacement",
            store=legacy_store,
        )
        if deletion is None:
            raise LegacyObjectMissing("Referenced legacy object is unavailable")
        if deletion.reason != "replacement":
            raise ConflictingLegacyDeletion(
                "Legacy source already has a conflicting deletion purpose"
            )
        # The source is outside PostgreSQL's locking domain. It may be
        # replaced after the post-promotion snapshot above but before the
        # deletion helper takes its own digest-bound snapshot. Never commit a
        # canonical object containing version A alongside an outbox row that
        # authorizes deleting version B: retain the legacy binding and remove
        # the unregistered promoted copy instead.
        if (
            deletion.size_bytes != before.size_bytes
            or deletion.sha256_snapshot != before.sha256
            or deletion.size_bytes != promoted.size_bytes
            or deletion.sha256_snapshot != promoted.sha256
        ):
            raise LegacyObjectChanged("Legacy object changed during cutover")
        private_object = await register_migrated_ready_private_object(
            db,
            organization_id=item.organization_id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            stored=promoted,
        )
        setattr(current, spec.path_attribute, promoted.key)
        current.private_object_id = private_object.id
        if isinstance(current, AssessmentFile):
            current.media_type = promoted.media_type
            current.size_bytes = promoted.size_bytes
            current.sha256 = promoted.sha256
            current.status = "ready"
            current.scan_result = (
                f"{promoted.scan.engine}:{promoted.scan.result}"[:64]
            )
        await db.flush()
        # Do not let task cancellation interrupt the driver exactly where the
        # commit outcome becomes unknowable. The failure path drains this task
        # and proves the authoritative binding before deciding whether the
        # promoted object must be retained or erased.
        commit_task = asyncio.create_task(db.commit())
        await asyncio.shield(commit_task)
        return "migrated"
    except BaseException as exc:
        if commit_task is not None:
            while not commit_task.done():
                try:
                    await asyncio.shield(commit_task)
                except BaseException:
                    continue
        try:
            await db.rollback()
        except BaseException:
            pass
        if promoted is None:
            raise
        committed = (
            await _resolve_binding_outcome(
                session_factory,
                item=item,
                object_key=promoted.key,
                legacy_key=legacy_key or "",
            )
            if legacy_key is not None
            else False
        )
        if committed is True:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return "migrated"
        if committed is None:
            if isinstance(exc, asyncio.CancelledError):
                exc.add_note("Cutover database outcome could not be verified")
                raise
            raise CutoverOutcomeUncertain(
                "Cutover database outcome could not be verified"
            ) from exc
        try:
            await cleanup_failed_intake_key(
                primary_store,
                promoted.key,
                preserve_cancellation=not isinstance(
                    exc,
                    asyncio.CancelledError,
                ),
            )
        except BaseException as cleanup_exc:
            if isinstance(exc, asyncio.CancelledError):
                exc.add_note(str(cleanup_exc))
                raise exc
            raise CutoverCleanupFailed(
                "Promoted object cleanup could not be verified"
            ) from cleanup_exc
        raise
    finally:
        await db.close()


async def _tracked_legacy_keys(
    session_factory: async_sessionmaker[AsyncSession],
) -> set[str]:
    async with session_factory() as db:
        return set(
            (
                await db.execute(
                    select(LegacyPrivateObjectDeletion.normalized_key).where(
                        LegacyPrivateObjectDeletion.completed_at.is_(None)
                    )
                )
            ).scalars()
        )


async def _report_orphans(
    report: CutoverReport,
    *,
    legacy_store: LocalBlobStore,
    tracked_keys: set[str],
) -> None:
    for prefix in LEGACY_PREFIXES:
        continuation: str | None = None
        while True:
            page = await legacy_store.list_prefix(
                prefix, limit=MAX_BATCH_SIZE, continuation=continuation
            )
            for entry in page.entries:
                report.source_files += 1
                if entry.key in tracked_keys:
                    continue
                report.orphan_files += 1
                digest = hashlib.sha256(entry.key.encode("utf-8")).hexdigest()
                if len(report.orphan_key_digests) < MAX_ORPHAN_SAMPLES:
                    report.orphan_key_digests.append(digest)
                else:
                    report.orphan_sample_truncated = True
            continuation = page.continuation
            if continuation is None:
                break


async def run_cutover(
    *,
    apply: bool,
    batch_size: int = 50,
    organization_id: UUID | None = None,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    legacy_store: LocalBlobStore | None = None,
    primary_store: BlobStore | None = None,
    scanner: MalwareScanner | None = None,
) -> CutoverReport:
    """Validate or apply every legacy resource and return a bounded report."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if legacy_store is None and get_settings().legacy_private_storage_dir is None:
        raise LegacyStorageUnavailableError(
            "LEGACY_PRIVATE_STORAGE_DIR must explicitly identify the legacy mount"
        )
    # Dry-run construction must not create directories or chmod the explicit
    # legacy mount. The primary store is not touched or even initialized until
    # an applying run actually needs it.
    legacy = legacy_store or get_legacy_blob_store(initialize=apply)
    primary = primary_store or (get_blob_store() if apply else None)
    malware_scanner = scanner or get_malware_scanner()
    async with hold_legacy_private_object_cutover_fence(session_factory) as operator_db:
        await assert_operator_connection(operator_db)

        # Ownership is deliberately global even for a tenant-filtered apply.
        # The oldest candidate keys are not tenant-namespaced, so another
        # tenant may otherwise own the same physical source.
        ownership = await _legacy_ownership_index(
            session_factory,
            batch_size=batch_size,
            organization_id=None,
            store=legacy,
        )
        report = CutoverReport(mode="apply" if apply else "dry_run")
        tracked_keys = await _tracked_legacy_keys(session_factory)
        tracked_keys.update(ownership)
        if organization_id is None:
            await _report_orphans(
                report,
                legacy_store=legacy,
                tracked_keys=tracked_keys,
            )
        else:
            report.orphan_scan_skipped = True

        async for batch in iter_legacy_resource_batches(
            session_factory,
            batch_size=batch_size,
            organization_id=organization_id,
        ):
            for item in batch:
                report.scanned += 1
                try:
                    preflight_key = _normalized_key(item, legacy)
                    ownership_count = ownership.get(preflight_key)
                    if ownership_count is None:
                        raise CutoverInventoryChanged(
                            "Legacy resource changed after the cutover preflight"
                        )
                    if apply:
                        if primary is None:  # Defensive type/runtime invariant.
                            raise LegacyCutoverError(
                                "Primary object storage is unavailable"
                            )
                        outcome = await apply_legacy_resource(
                            session_factory,
                            item,
                            legacy_store=legacy,
                            primary_store=primary,
                            scanner=malware_scanner,
                            preflight_legacy_key=preflight_key,
                            preflight_ownership_count=ownership_count,
                        )
                        if outcome == "migrated":
                            report.migrated += 1
                        else:
                            report.skipped_registered += 1
                    else:
                        async with session_factory() as db:
                            await _assert_unique_binding(
                                db,
                                item,
                                legacy,
                                preflight_key,
                                preflight_ownership_count=ownership_count,
                            )
                        await validate_legacy_resource(
                            item,
                            legacy_store=legacy,
                            scanner=malware_scanner,
                        )
                        report.validated += 1
                except Exception as exc:
                    # Report every item without abandoning the bounded pass.
                    report.failure(item, exc)
        # This is an end-of-run operational obligation, not merely a preflight
        # count: a successful apply creates one replacement deletion row for
        # every migrated source. Operators must keep the legacy mount until it
        # reaches zero.
        report.pending_deletion_obligations = len(
            await _tracked_legacy_keys(session_factory)
        )
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and cut legacy local private files over to the primary "
            "tenant object store; defaults to a non-mutating dry run"
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply validated cutovers (legacy sources are never deleted here)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50, choices=range(1, MAX_BATCH_SIZE + 1)
    )
    parser.add_argument(
        "--organization-id",
        type=UUID,
        default=None,
        help="Restrict resources to one tenant (global orphan scan is skipped)",
    )
    return parser


async def _run(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    report = await run_cutover(
        apply=bool(args.apply),
        batch_size=int(args.batch_size),
        organization_id=args.organization_id,
    )
    payload = report.as_dict()
    return payload, 1 if report.failed else 0


def main() -> None:
    args = _parser().parse_args()
    try:
        result, exit_code = asyncio.run(_run(args))
    except (LegacyCutoverError, OSError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "code": _error_code(exc)}),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
