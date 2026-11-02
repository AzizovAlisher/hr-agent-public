"""Validated private-object intake, download, and compatibility helpers."""

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import tenant_session_scope
from app.engine.parsing import extract_text_from_docx, extract_text_from_pdf
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.private_object import PrivateObject
from app.services.entitlements import TECHNICAL_LIMITS
from app.storage import (
    BlobStore,
    BlobStoreError,
    LegacyStorageUnavailableError,
    MalwareDetectedError,
    MalwareScanner,
    MalwareScannerUnavailableError,
    UnsafeLegacyPrivateObjectError,
    enqueue_legacy_private_object_deletion,
    get_blob_store,
    get_public_blob_store,
    get_malware_scanner,
    require_tenant_private_key,
    validate_blob_key,
)
from app.storage.intake import (
    StoredUpload,
    UploadTooLargeError,
    quarantine_scan_promote,
)
from app.storage.registry import (
    enqueue_private_object_deletion,
    registered_resource_blob,
)
from app.storage.validation import (
    UploadValidationError,
    canonical_media_type,
    validate_upload_bytes,
)


@dataclass(frozen=True, slots=True)
class CandidateFileUpload:
    """A parsed candidate plus the metadata needed for registry dual-write."""

    candidate: Candidate
    stored: StoredUpload[str]


def validate_upload_content(
    *,
    filename: str,
    media_type: str,
    content: bytes,
    allowed_suffixes: set[str],
) -> str:
    """Compatibility wrapper around the same hardened path validator."""

    try:
        return validate_upload_bytes(
            filename=filename,
            media_type=media_type,
            content=content,
            allowed_suffixes=allowed_suffixes,
        )
    except UploadValidationError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def remove_private_upload(path: str | Path | None) -> None:
    """Best-effort cleanup for a file written by the current transaction."""

    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        # Cleanup must not replace the primary parser/database error. Orphan
        # sweeps remain a separate operational safeguard.
        pass


def write_private_upload(path: str | Path, content: bytes) -> None:
    """Atomically create a private file with owner-only permissions."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        remove_private_upload(temporary)


def private_download_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


def safe_content_disposition(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")[:120]
    return f'attachment; filename="{cleaned or "download.bin"}"'


async def private_blob_response(
    key: str,
    *,
    filename: str,
    media_type: str,
    store: BlobStore | None = None,
) -> StreamingResponse:
    backend = store or get_blob_store()
    try:
        metadata = await backend.stat(key)
    except BlobStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private file storage is temporarily unavailable.",
        ) from exc
    if metadata is None:
        raise HTTPException(status.HTTP_410_GONE, "Private file is unavailable")
    headers = {
        **private_download_headers(),
        "Content-Disposition": safe_content_disposition(filename),
        "Content-Length": str(metadata.size_bytes),
    }
    return StreamingResponse(
        backend.iter_bytes(key),
        media_type=media_type,
        headers=headers,
    )


def resolve_private_blob_reference(
    stored_reference: str | Path,
    *,
    organization_id: UUID,
    category: str,
) -> str | None:
    """Resolve a canonical key or a constrained local legacy path."""

    raw = str(stored_reference)
    try:
        return require_tenant_private_key(
            raw,
            organization_id=organization_id,
            category=category,
        )
    except ValueError:
        pass
    if get_settings().blob_store_backend != "local":
        return None
    root = Path(get_settings().storage_dir).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        path = candidate.resolve()
    except OSError:
        return None
    legacy_roots = {
        "candidates": (root / "cvs").resolve(),
        "assessments": (root / "assessment_files" / str(organization_id)).resolve(),
        "tasks": (root / "task_files" / str(organization_id)).resolve(),
        "recordings": (root / "recordings" / str(organization_id)).resolve(),
    }
    allowed_root = legacy_roots.get(category)
    if allowed_root is None or not path.is_relative_to(allowed_root):
        return None
    try:
        return validate_blob_key(path.relative_to(root).as_posix())
    except (ValueError, OSError):
        return None


def resolve_candidate_private_path(
    stored_path: str | Path,
    *,
    organization_id: UUID,
) -> Path | None:
    """Resolve a stored CV only inside an approved private storage subtree."""

    key = resolve_private_blob_reference(
        stored_path,
        organization_id=organization_id,
        category="candidates",
    )
    return get_blob_store().local_path(key) if key is not None else None


def safe_download_filename(value: str | None, *, fallback: str, suffix: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", value or fallback).strip("._")[:80]
    if suffix and base.casefold().endswith(suffix.casefold()):
        base = base[: -len(suffix)].rstrip("._")
    return f"{base or fallback}{suffix}"


def validated_private_media_type(key: str, *, allowed_suffixes: set[str]) -> str:
    suffix = Path(key).suffix.casefold()
    if suffix not in {value.casefold() for value in allowed_suffixes}:
        raise HTTPException(status.HTTP_410_GONE, "Private file is unavailable")
    try:
        return canonical_media_type(suffix)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_410_GONE,
            "Private file is unavailable",
        ) from exc


def _storage_path(suffix: str) -> Path:
    base = Path(get_settings().storage_dir) / "cvs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{uuid.uuid4()}{suffix}"


async def store_private_upload(
    upload: UploadFile,
    *,
    organization_id: UUID,
    category: str,
    allowed_suffixes: set[str],
    max_bytes: int,
    label: str = "File",
    inspector: Callable[[Path, str], Any] | None = None,
    public_branding: bool = False,
    store: BlobStore | None = None,
    scanner: MalwareScanner | None = None,
) -> StoredUpload[Any]:
    """Run the common fail-closed upload boundary with API-safe errors."""

    backend = store or get_blob_store()
    try:
        return await quarantine_scan_promote(
            upload,
            organization_id=organization_id,
            category=category,
            allowed_suffixes=allowed_suffixes,
            max_bytes=max_bytes,
            store=backend,
            scanner=scanner or get_malware_scanner(),
            inspector=inspector,
            public_branding=public_branding,
        )
    except UploadTooLargeError as exc:
        display_limit = (
            f"{max_bytes // (1024 * 1024)} MiB"
            if max_bytes % (1024 * 1024) == 0
            else f"{max_bytes} bytes"
        )
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"{label} exceeds the {display_limit} upload limit.",
        ) from exc
    except UploadValidationError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except MalwareDetectedError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{label} was rejected by security scanning.",
        ) from exc
    except MalwareScannerUnavailableError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "File security scanning is temporarily unavailable.",
        ) from exc
    except BlobStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private file storage is temporarily unavailable.",
        ) from exc


async def candidate_from_upload(
    upload: UploadFile,
    *,
    organization_id: UUID,
    source_ref: str | None = None,
    max_bytes: int = TECHNICAL_LIMITS["candidate_document_bytes"],
    store: BlobStore | None = None,
    scanner: MalwareScanner | None = None,
) -> CandidateFileUpload:
    stored = await store_private_upload(
        upload,
        organization_id=organization_id,
        category="candidates",
        allowed_suffixes={".pdf", ".docx"},
        max_bytes=max_bytes,
        label="CV file",
        store=store,
        scanner=scanner,
    )
    resolved_source_ref = (source_ref or "").strip()
    if not resolved_source_ref:
        # Manual uploads otherwise render as an opaque candidate UUID until a
        # recruiter edits or enriches the record. The safe original filename
        # is already bounded and is the best available human label while the
        # durable parser extracts the document text.
        resolved_source_ref = Path(stored.original_filename).stem.strip()
    return CandidateFileUpload(
        candidate=Candidate(
            id=uuid.uuid4(),
            organization_id=organization_id,
            source="manual",
            source_ref=resolved_source_ref[:255] or None,
            # Text extraction is a durable, resource-bounded worker job. The
            # API only validates, scans, promotes, and registers the object.
            raw_cv=None,
            pdf_path=stored.key,
        ),
        stored=stored,
    )


async def resolve_resource_private_blob_reference(
    db: AsyncSession,
    *,
    organization_id: UUID,
    category: str,
    resource_type: str,
    resource_id: UUID,
    private_object_id: UUID | None,
    legacy_reference: str | Path | None,
) -> str | None:
    """Resolve the registry binding, with one-release legacy compatibility.

    Once a resource has ``private_object_id``, a missing/mismatched registry
    row is a hard failure and the text path is ignored.  This prevents a stale
    or tampered compatibility column from bypassing the authoritative binding.
    """

    if private_object_id is not None:
        registered = await registered_resource_blob(
            db,
            organization_id=organization_id,
            category=category,
            resource_type=resource_type,
            resource_id=resource_id,
            private_object_id=private_object_id,
        )
        return registered.key if registered is not None else None
    if legacy_reference is None:
        return None
    return resolve_private_blob_reference(
        legacy_reference,
        organization_id=organization_id,
        category=category,
    )


async def enqueue_or_delete_private_object(
    db: AsyncSession,
    *,
    organization_id: UUID,
    category: str,
    resource_type: str,
    resource_id: UUID,
    private_object_id: UUID | None,
    legacy_reference: str | Path | None,
    reason: str,
) -> None:
    """Transactionally queue registered or verified pre-registry erasure."""

    if private_object_id is not None:
        queued = await enqueue_private_object_deletion(
            db,
            organization_id=organization_id,
            private_object_id=private_object_id,
            reason=reason,
        )
        if queued is None:
            raise HTTPException(
                status.HTTP_410_GONE,
                "Private file metadata is unavailable.",
            )
        return
    if legacy_reference is not None:
        try:
            await enqueue_legacy_private_object_deletion(
                db,
                organization_id=organization_id,
                category=category,
                resource_type=resource_type,
                resource_id=resource_id,
                legacy_reference=legacy_reference,
                reason=reason,
            )
        except UnsafeLegacyPrivateObjectError as exc:
            raise HTTPException(
                status.HTTP_410_GONE,
                "Private file metadata is unavailable.",
            ) from exc
        except LegacyStorageUnavailableError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Legacy private file storage is temporarily unavailable.",
            ) from exc


async def remove_private_object(
    reference: str | Path | None,
    *,
    organization_id: UUID,
    category: str,
    store: BlobStore | None = None,
) -> None:
    if not reference:
        return
    key = resolve_private_blob_reference(
        reference,
        organization_id=organization_id,
        category=category,
    )
    if key is None:
        return
    try:
        await (store or get_blob_store()).delete(key)
    except BlobStoreError:
        # Callers performing rollback cleanup must preserve the primary error.
        pass


async def cleanup_promoted_upload_after_db_failure(
    reference: str | Path | None,
    *,
    organization_id: UUID,
    category: str,
    private_object_id: UUID | None,
    store: BlobStore | None = None,
) -> None:
    """Delete only after positively proving a failed commit did not land.

    A database commit can succeed server-side and still raise when its response
    is lost. Blind cleanup in that case would remove a blob referenced by a
    committed ``ready`` row. If verification is unavailable, leave the random
    object for reconciliation instead of risking destructive inconsistency.
    """

    if not reference:
        return
    if private_object_id is not None:
        try:
            async with tenant_session_scope(
                organization_id=organization_id,
                identity_kind="background",
                role="storage-reconciliation",
            ) as verification:
                landed = (
                    await verification.execute(
                        select(PrivateObject.id).where(
                            PrivateObject.id == private_object_id,
                            PrivateObject.organization_id == organization_id,
                            PrivateObject.object_key == str(reference),
                        )
                    )
                ).scalar_one_or_none()
        except Exception:
            return
        if landed is not None:
            return
    await remove_private_object(
        reference,
        organization_id=organization_id,
        category=category,
        store=store,
    )


async def delete_private_object(
    reference: str | Path,
    *,
    organization_id: UUID,
    category: str,
    store: BlobStore | None = None,
) -> None:
    """Delete a tenant object or fail without falsely recording erasure."""

    key = resolve_private_blob_reference(
        reference,
        organization_id=organization_id,
        category=category,
    )
    if key is None:
        raise HTTPException(status.HTTP_410_GONE, "Private file is unavailable")
    try:
        await (store or get_blob_store()).delete(key)
    except BlobStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private file deletion is temporarily unavailable.",
        ) from exc


def candidate_from_pdf(pdf_bytes: bytes, *, source_ref: str | None = None) -> Candidate:
    validate_upload_content(
        filename="candidate.pdf",
        media_type="application/pdf",
        content=pdf_bytes,
        allowed_suffixes={".pdf"},
    )
    path = _storage_path(".pdf")
    write_private_upload(path, pdf_bytes)
    try:
        text = extract_text_from_pdf(path)
    except Exception:
        remove_private_upload(path)
        raise
    return Candidate(
        source="manual",
        source_ref=source_ref,
        raw_cv=text,
        pdf_path=str(path),
    )


def candidate_from_docx(
    docx_bytes: bytes, *, source_ref: str | None = None
) -> Candidate:
    validate_upload_content(
        filename="candidate.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        content=docx_bytes,
        allowed_suffixes={".docx"},
    )
    # Persist the original .docx for completeness (no PDF conversion on Day 1).
    path = _storage_path(".docx")
    write_private_upload(path, docx_bytes)
    # Includes table cells, not just paragraphs — a paragraph-only pass
    # silently produces empty text on table-based CV layouts.
    try:
        text = extract_text_from_docx(path)
    except Exception:
        remove_private_upload(path)
        raise

    return Candidate(
        source="manual",
        source_ref=source_ref,
        raw_cv=text,
        pdf_path=str(path),
    )


def candidate_from_text(raw_text: str, *, source_ref: str | None = None) -> Candidate:
    return Candidate(
        source="manual",
        source_ref=source_ref,
        raw_cv=raw_text.strip(),
    )


def candidate_from_hh_json(hh: dict) -> Candidate:
    """Accept a single HeadHunter resume JSON object (already fetched).

    OAuth + Celery scraping are deferred — this endpoint just ingests JSON the
    user provides. Maps HH fields to a text representation the engine can score
    and stores the original payload in `parsed_cv`.
    """
    text = _hh_json_to_text(hh)
    source_ref = str(hh.get("id") or hh.get("alternate_url") or "")
    return Candidate(
        source="hh",
        source_ref=source_ref or None,
        raw_cv=text,
        parsed_cv=hh,
    )


def _hh_json_to_text(hh: dict) -> str:
    parts: list[str] = []

    name = " ".join(
        filter(None, [hh.get("first_name"), hh.get("middle_name"), hh.get("last_name")])
    )
    if name:
        parts.append(f"Имя: {name}")
    if title := hh.get("title"):
        parts.append(f"Должность: {title}")
    if age := hh.get("age"):
        parts.append(f"Возраст: {age}")
    if (salary := hh.get("salary")) and isinstance(salary, dict):
        amount, currency = salary.get("amount"), salary.get("currency")
        if amount:
            parts.append(f"Зарплата: {amount} {currency or ''}".strip())
    if (area := hh.get("area")) and isinstance(area, dict):
        parts.append(f"Город: {area.get('name', '')}")

    if skills := hh.get("skill_set"):
        parts.append(f"Ключевые навыки: {', '.join(skills)}")
    if (skills_text := hh.get("skills")) and isinstance(skills_text, str):
        parts.append(f"О себе: {skills_text}")

    if experience := hh.get("experience"):
        parts.append("\nОпыт работы:")
        for exp in experience or []:
            company = exp.get("company") or ""
            position = exp.get("position") or ""
            start = exp.get("start") or ""
            end = exp.get("end") or "по настоящее время"
            description = exp.get("description") or ""
            parts.append(f"- {company} ({start} – {end}): {position}")
            if description:
                parts.append(f"  {description}")

    if education := hh.get("education"):
        parts.append("\nОбразование:")
        primary = education.get("primary") if isinstance(education, dict) else education
        for ed in primary or []:
            name = ed.get("name") or ""
            organization = ed.get("organization") or ""
            year = ed.get("year") or ""
            result = ed.get("result") or ""
            parts.append(
                f"- {name} {organization} {year} {result}".strip().replace("  ", " ")
            )

    return "\n".join(p for p in parts if p)


def ensure_storage_dir() -> None:
    if get_settings().blob_store_backend != "local":
        # S3-backed production containers need no writable application tree;
        # intake spools use the operating system's private temporary directory.
        return
    root = Path(get_settings().storage_dir)
    for path in (root, root / "cvs", root / "logos"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass


# ---------- org logo storage ----------

LOGO_MAX_BYTES = 2 * 1024 * 1024
LOGO_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}


async def save_org_logo(*, org_id: UUID, upload: UploadFile) -> str:
    """Scan and atomically promote one logo into the public-only namespace."""

    stored = await store_private_upload(
        upload,
        organization_id=org_id,
        category="branding",
        allowed_suffixes=LOGO_ALLOWED_EXT,
        max_bytes=LOGO_MAX_BYTES,
        label="Logo",
        public_branding=True,
        store=get_public_blob_store(),
    )
    return stored.key


async def remove_org_logo(*, org_id: UUID, logo_path: str) -> None:
    """Delete exactly this tenant's public-branding object, or fail closed."""

    try:
        key = validate_blob_key(logo_path)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_410_GONE, "Organization logo is unavailable"
        ) from exc
    current_prefix = f"logos/{org_id}/"
    legacy_prefix = f"logos/{org_id}."
    if (
        not (key.startswith(current_prefix) or key.startswith(legacy_prefix))
        or Path(key).suffix not in LOGO_ALLOWED_EXT
    ):
        raise HTTPException(status.HTTP_410_GONE, "Organization logo is unavailable")
    try:
        await get_public_blob_store().delete(key)
    except BlobStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Branding storage is temporarily unavailable.",
        ) from exc


async def cleanup_org_logo_after_db_failure(*, org_id: UUID, logo_path: str) -> None:
    """Preserve a logo if an uncertain commit actually made it authoritative."""

    try:
        async with tenant_session_scope(
            organization_id=org_id,
            identity_kind="background",
            role="storage-reconciliation",
        ) as verification:
            landed = (
                await verification.execute(
                    select(Organization.id).where(
                        Organization.id == org_id,
                        Organization.logo_path == logo_path,
                    )
                )
            ).scalar_one_or_none()
    except Exception:
        return
    if landed is not None:
        return
    try:
        await remove_org_logo(org_id=org_id, logo_path=logo_path)
    except HTTPException:
        pass


def public_logo_url(logo_path: str | None) -> str | None:
    if not logo_path:
        return None
    try:
        key = validate_blob_key(logo_path)
    except ValueError:
        return None
    if not key.startswith("logos/"):
        return None
    return get_public_blob_store().public_url(key)
