"""Opaque AI-session attachment upload and canonical candidate import."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.config import get_settings
from app.models.ai_candidate_source import AiCandidateSourceObject
from app.models.ai_hiring import AiSession
from app.models.candidate import Candidate
from app.models.enterprise import AuditLog
from app.models.user import User
from app.services.activities import append_candidate_activity
from app.services.bounded_document_parser import (
    BoundedDocumentParseError,
    BoundedDocumentParseTimeout,
    extract_document_inventory,
    extract_document_text,
    extract_spreadsheet_inventory,
    extract_spreadsheet_rows,
)
from app.services.enterprise import require_field_access
from app.services.entitlements import TECHNICAL_LIMITS, record_usage
from app.services.uploads import (
    cleanup_promoted_upload_after_db_failure,
    private_blob_response,
    safe_download_filename,
    store_private_upload,
)
from app.storage import BlobStore, BlobStoreError, get_blob_store
from app.storage.registry import (
    enqueue_private_object_deletion,
    register_ready_private_object,
    registered_resource_blob,
)

MAX_AI_ATTACHMENT_BYTES = TECHNICAL_LIMITS["candidate_document_bytes"]
MAX_IMPORT_ROWS = TECHNICAL_LIMITS["synchronous_import_rows"]
_ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".csv", ".xlsx"}


class AiAttachmentInventoryError(RuntimeError):
    """A stored AI attachment cannot be completely inventoried."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _verify_materialized_attachment(
    path: Path,
    *,
    expected_size_bytes: int,
    expected_sha256: str,
) -> None:
    if expected_size_bytes < 0 or expected_size_bytes > MAX_AI_ATTACHMENT_BYTES:
        raise AiAttachmentInventoryError("size_limit_exceeded")
    try:
        if path.stat().st_size != expected_size_bytes:
            raise AiAttachmentInventoryError("integrity_failed")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except AiAttachmentInventoryError:
        raise
    except OSError as exc:
        raise AiAttachmentInventoryError("storage_unavailable") from exc
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256.casefold()):
        raise AiAttachmentInventoryError("integrity_failed")


async def extract_unsubmitted_ai_attachment_inventory(
    store: BlobStore,
    *,
    object_key: str,
    size_bytes: int,
    sha256: str,
) -> str:
    """Return bounded searchable text for every accepted AI attachment type.

    Upload calls this after promotion and before registry commit, which makes
    parser/inventory eligibility part of the acceptance boundary. Retention
    calls the same helper again so later storage loss or tampering still fails
    closed instead of silently leaving candidate data behind.
    """

    suffix = Path(object_key).suffix.casefold()
    try:
        async with store.materialize(object_key) as path:
            await asyncio.to_thread(
                _verify_materialized_attachment,
                path,
                expected_size_bytes=size_bytes,
                expected_sha256=sha256,
            )
            if suffix in {".pdf", ".docx"}:
                inventory_text = await extract_document_inventory(path, suffix)
            elif suffix == ".xlsx":
                inventory = await extract_spreadsheet_inventory(
                    path,
                    max_rows=MAX_IMPORT_ROWS,
                )
                # Keep cells and metadata as raw newline-delimited strings.
                # JSON escaping would turn a separator into the literal bytes
                # ``\n``; the trailing ``n`` can then be consumed as part of
                # an adjacent email token and defeat boundary-equality matching.
                inventory_values = [inventory.metadata_text]
                for row in inventory.rows:
                    for key, value in row.items():
                        inventory_values.append(str(key))
                        if value is not None:
                            inventory_values.append(str(value))
                inventory_text = "\n".join(inventory_values)
            elif suffix in {".txt", ".csv"}:
                try:
                    inventory_text = await asyncio.to_thread(
                        path.read_text,
                        encoding="utf-8-sig",
                    )
                except UnicodeDecodeError as exc:
                    raise AiAttachmentInventoryError("encoding_invalid") from exc
            else:
                raise AiAttachmentInventoryError("type_unsupported")
            # Re-hash after parsing so a mutable local/materialized object
            # cannot pass eligibility and then change during the child scan.
            await asyncio.to_thread(
                _verify_materialized_attachment,
                path,
                expected_size_bytes=size_bytes,
                expected_sha256=sha256,
            )
            return inventory_text
    except AiAttachmentInventoryError:
        raise
    except BoundedDocumentParseError as exc:
        code = (
            "row_limit_exceeded"
            if str(exc) == "spreadsheet_row_limit_exceeded"
            else "parse_failed"
        )
        raise AiAttachmentInventoryError(code) from exc
    except BoundedDocumentParseTimeout as exc:
        raise AiAttachmentInventoryError("parse_failed") from exc
    except (BlobStoreError, OSError) as exc:
        raise AiAttachmentInventoryError("storage_unavailable") from exc


def _inventory_http_error(exc: AiAttachmentInventoryError) -> HTTPException:
    if exc.code == "row_limit_exceeded":
        return HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            (
                "AI attachment inventory is limited to "
                f"{MAX_IMPORT_ROWS} spreadsheet rows."
            ),
        )
    if exc.code == "size_limit_exceeded":
        return HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            (
                "AI attachment exceeds the "
                f"{MAX_AI_ATTACHMENT_BYTES // (1024 * 1024)} MB upload limit."
            ),
        )
    if exc.code == "storage_unavailable":
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Private file storage is temporarily unavailable.",
        )
    if exc.code == "integrity_failed":
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "Attachment integrity check failed",
        )
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        (
            "Attachment contains unsupported embedded or interactive content; "
            "upload a text-only PDF/DOCX, TXT/CSV, or single-sheet XLSX."
        ),
    )


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: dict[str, Any]) -> str:
    body = _b64_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = hmac.new(
        get_settings().jwt_secret.encode(), body.encode(), hashlib.sha256
    ).digest()
    return f"{body}.{_b64_encode(signature)}"


def _verify(token: str) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(
            get_settings().jwt_secret.encode(), body.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64_decode(signature)):
            raise ValueError
        payload = json.loads(_b64_decode(body))
        if int(payload["exp"]) < int(datetime.now(UTC).timestamp()):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid attachment token"
        ) from exc


def _legacy_attachment_path(
    organization_id: UUID, session_id: UUID, attachment_id: UUID, suffix: str
) -> Path:
    root = (
        Path(get_settings().storage_dir)
        / "ai_uploads"
        / str(organization_id)
        / str(session_id)
    )
    return root / f"{attachment_id}{suffix}"


async def save_ai_attachment(
    db: AsyncSession,
    user: User,
    session_id: UUID,
    *,
    upload: UploadFile,
    store: BlobStore | None = None,
) -> dict[str, Any]:
    await require_field_access(db, user, FieldClass.AI_CONTEXT)
    organization_id = user.organization_id
    session = (
        await db.execute(
            select(AiSession).where(
                AiSession.id == session_id,
                AiSession.organization_id == organization_id,
                AiSession.status == "active",
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI session not found")
    backend = store or get_blob_store()
    stored = await store_private_upload(
        upload,
        organization_id=organization_id,
        category="ai",
        allowed_suffixes=_ALLOWED_SUFFIXES,
        max_bytes=MAX_AI_ATTACHMENT_BYTES,
        label="AI attachment",
        store=backend,
    )
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    private_object = None
    try:
        try:
            await extract_unsubmitted_ai_attachment_inventory(
                backend,
                object_key=stored.key,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
        except AiAttachmentInventoryError as exc:
            raise _inventory_http_error(exc) from exc
        private_object = await register_ready_private_object(
            db,
            organization_id=organization_id,
            resource_type="ai_attachment",
            resource_id=session_id,
            stored=stored,
            retention_until=expires_at,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        await cleanup_promoted_upload_after_db_failure(
            stored.key,
            organization_id=organization_id,
            category="ai",
            private_object_id=(
                private_object.id if private_object is not None else None
            ),
            store=backend,
        )
        raise
    payload = {
        "id": str(private_object.id),
        "org": str(organization_id),
        "session": str(session_id),
        "filename": stored.original_filename,
        "media_type": stored.media_type,
        "size_bytes": stored.size_bytes,
        "sha256": stored.sha256,
        "suffix": stored.suffix,
        "exp": int(expires_at.timestamp()),
    }
    return {
        "id": str(private_object.id),
        "filename": stored.original_filename,
        "media_type": payload["media_type"],
        "size_bytes": stored.size_bytes,
        "sha256": stored.sha256,
        "upload_token": _sign(payload),
    }


async def resolve_attachment_references(
    db: AsyncSession,
    *,
    user: User,
    session_id: UUID,
    references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    await require_field_access(db, user, FieldClass.AI_CONTEXT)
    resolved: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    for reference in references:
        payload = _verify(str(reference.get("upload_token") or ""))
        try:
            attachment_id = UUID(str(reference.get("id")))
            payload_id = UUID(str(payload["id"]))
            payload_organization_id = UUID(str(payload["org"]))
            payload_session_id = UUID(str(payload["session"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Invalid attachment reference",
            ) from exc
        if (
            payload_id != attachment_id
            or payload_organization_id != user.organization_id
            or payload_session_id != session_id
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Attachment scope mismatch"
            )
        if attachment_id in seen:
            continue
        seen.add(attachment_id)
        registered = await registered_resource_blob(
            db,
            organization_id=user.organization_id,
            category="ai",
            resource_type="ai_attachment",
            resource_id=session_id,
            private_object_id=attachment_id,
        )
        if registered is not None:
            if (
                registered.original_filename != payload.get("filename")
                or registered.media_type != payload.get("media_type")
                or registered.size_bytes != int(payload.get("size_bytes", -1))
                or registered.sha256 != payload.get("sha256")
                or Path(registered.key).suffix.casefold()
                != str(payload.get("suffix", "")).casefold()
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Attachment integrity check failed",
                )
            resolved.append({**payload, "key": registered.key, "registered": True})
            continue

        # One-release local compatibility for tokens minted before registry
        # cutover. The signed tenant/session/id/suffix tuple still constrains
        # the path, and production S3 deployments never consult local files.
        if get_settings().blob_store_backend != "local":
            raise HTTPException(status.HTTP_410_GONE, "Attachment has expired")
        path = _legacy_attachment_path(
            user.organization_id, session_id, attachment_id, payload["suffix"]
        )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise HTTPException(status.HTTP_410_GONE, "Attachment has expired") from exc
        if hashlib.sha256(content).hexdigest() != payload["sha256"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Attachment integrity check failed"
            )
        root = Path(get_settings().storage_dir).resolve()
        resolved.append(
            {
                **payload,
                "key": path.resolve().relative_to(root).as_posix(),
                "registered": False,
            }
        )
    return resolved


def _normalize_cv(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _fingerprint(value: str) -> str:
    return hashlib.md5(_normalize_cv(value).encode(), usedforsecurity=False).hexdigest()


async def _text_from_document(
    item: dict[str, Any], path: Path
) -> list[tuple[str, str]]:
    suffix = item["suffix"]
    if suffix in {".pdf", ".docx"}:
        try:
            return [(item["filename"], await extract_document_text(path, suffix))]
        except (BoundedDocumentParseError, BoundedDocumentParseTimeout) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Attachment could not be parsed safely",
            ) from exc
    if suffix == ".txt":
        try:
            return [(item["filename"], path.read_text(encoding="utf-8-sig"))]
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "TXT attachment must be UTF-8"
            ) from exc
    if suffix == ".csv":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "CSV attachment must be UTF-8"
            ) from exc
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            if len(rows) >= MAX_IMPORT_ROWS:
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"Candidate import is limited to {MAX_IMPORT_ROWS} rows",
                )
            rows.append(row)
    else:
        try:
            rows = await extract_spreadsheet_rows(path, max_rows=MAX_IMPORT_ROWS)
        except BoundedDocumentParseError as exc:
            if str(exc) == "spreadsheet_row_limit_exceeded":
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"Candidate import is limited to {MAX_IMPORT_ROWS} rows",
                ) from exc
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Attachment could not be parsed safely",
            ) from exc
        except BoundedDocumentParseTimeout as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Attachment could not be parsed safely",
            ) from exc
    output: list[tuple[str, str]] = []
    for index, row in enumerate(rows, start=2):
        if not isinstance(row, dict):
            continue
        normalized = {
            str(key or "").strip().lower(): value for key, value in row.items()
        }
        name = str(
            normalized.get("name") or normalized.get("имя") or f"row {index}"
        ).strip()
        preferred = next(
            (
                normalized[key]
                for key in ("cv_text", "resume", "резюме", "summary", "profile")
                if normalized.get(key)
            ),
            None,
        )
        if preferred is not None:
            cv_text = str(preferred)
        else:
            cv_text = "\n".join(
                f"{key}: {value}"
                for key, value in normalized.items()
                if value not in (None, "")
            )
        if cv_text.strip():
            output.append((name[:255], cv_text.strip()))
    return output


async def import_ai_attachments(
    db: AsyncSession,
    user: User,
    *,
    attachments: list[dict[str, Any]],
    store: BlobStore | None = None,
) -> dict[str, Any]:
    backend = store or get_blob_store()
    imported: list[str] = []
    duplicates: list[str] = []
    requested_rows = 0
    for attachment in attachments:
        try:
            async with backend.materialize(attachment["key"]) as path:
                content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if content_hash != attachment["sha256"]:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "Attachment integrity check failed",
                    )
                extracted = await _text_from_document(attachment, path)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status.HTTP_410_GONE,
                "Attachment is unavailable",
            ) from exc
        attachment_rows = sum(1 for _, cv_text in extracted if cv_text.strip())
        if requested_rows + attachment_rows > MAX_IMPORT_ROWS:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"Candidate import is limited to {MAX_IMPORT_ROWS} rows",
            )
        requested_rows += attachment_rows
        for row_index, (source_ref, cv_text) in enumerate(extracted):
            if not cv_text.strip():
                continue
            fingerprint = _fingerprint(cv_text)
            await record_usage(
                db,
                organization_id=user.organization_id,
                metric="candidate_import_rows",
                amount=1,
                kind="usage",
                idempotency_key=(
                    f"candidate-import:ai:{attachment['id']}:{row_index}:"
                    f"{fingerprint[:24]}"
                ),
                resource_type="ai_attachment",
                resource_id=UUID(str(attachment["id"])),
            )
            existing = (
                await db.execute(
                    select(Candidate).where(
                        Candidate.organization_id == user.organization_id,
                        Candidate.duplicate_cv_fingerprint == fingerprint,
                        Candidate.merged_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                duplicates.append(str(existing.id))
                candidate = existing
            else:
                candidate = Candidate(
                    id=uuid4(),
                    organization_id=user.organization_id,
                    source="ai_assistant",
                    source_ref=source_ref,
                    raw_cv=cv_text,
                )
                await record_usage(
                    db,
                    organization_id=user.organization_id,
                    metric="candidates",
                    amount=1,
                    kind="usage",
                    idempotency_key=f"candidate:ai:{candidate.id}",
                    resource_type="candidate",
                    resource_id=candidate.id,
                )
                db.add(candidate)
                await db.flush()
                append_candidate_activity(
                    db,
                    organization_id=user.organization_id,
                    candidate_id=candidate.id,
                    actor_user_id=user.id,
                    actor_kind="ai",
                    event_type="candidate_added",
                    subject_type="candidate",
                    subject_id=candidate.id,
                    payload={"source": "ai_assistant"},
                )
                imported.append(str(candidate.id))
            # Preserve provenance even when the import deduplicates to an
            # existing person. The unique edge makes attachment replay and
            # concurrent message submission idempotent.
            await db.execute(
                pg_insert(AiCandidateSourceObject)
                .values(
                    organization_id=user.organization_id,
                    candidate_id=candidate.id,
                    private_object_id=UUID(str(attachment["id"])),
                    source_ref=source_ref,
                )
                .on_conflict_do_nothing(constraint="uq_ai_candidate_source_object_edge")
            )
        db.add(
            AuditLog(
                organization_id=user.organization_id,
                actor_user_id=user.id,
                actor_email_snapshot=user.email,
                action="read_ai_attachment",
                resource_type="ai_attachment",
                resource_id=UUID(str(attachment["id"])),
                path_template="/ai/v1/sessions/{session_id}/messages",
                method="POST",
                status_code=200,
                request_id=uuid4(),
                metadata_json={"sensitive_read": True},
                occurred_at=datetime.now(UTC),
            )
        )
    return {
        "imported_candidate_ids": imported,
        "duplicate_candidate_ids": list(dict.fromkeys(duplicates)),
    }


async def download_ai_attachment(
    db: AsyncSession,
    user: User,
    *,
    session_id: UUID,
    attachment_id: UUID,
    store: BlobStore | None = None,
) -> StreamingResponse:
    await require_field_access(db, user, FieldClass.AI_CONTEXT)
    session = (
        await db.execute(
            select(AiSession.id).where(
                AiSession.id == session_id,
                AiSession.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "AI session not found")
    registered = await registered_resource_blob(
        db,
        organization_id=user.organization_id,
        category="ai",
        resource_type="ai_attachment",
        resource_id=session_id,
        private_object_id=attachment_id,
    )
    if registered is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    response = await private_blob_response(
        registered.key,
        filename=safe_download_filename(
            registered.original_filename,
            fallback="ai-attachment",
            suffix=Path(registered.key).suffix.casefold(),
        ),
        media_type=registered.media_type,
        store=store,
    )
    db.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
            action="download_ai_attachment",
            resource_type="ai_attachment",
            resource_id=attachment_id,
            path_template=("/ai/v1/sessions/{session_id}/attachments/{attachment_id}"),
            method="GET",
            status_code=200,
            request_id=uuid4(),
            metadata_json={"sensitive_read": True},
            occurred_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return response


async def delete_ai_attachment(
    db: AsyncSession,
    user: User,
    *,
    session_id: UUID,
    attachment_id: UUID,
) -> None:
    await require_field_access(db, user, FieldClass.AI_CONTEXT)
    registered = await registered_resource_blob(
        db,
        organization_id=user.organization_id,
        category="ai",
        resource_type="ai_attachment",
        resource_id=session_id,
        private_object_id=attachment_id,
    )
    if registered is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    await enqueue_private_object_deletion(
        db,
        organization_id=user.organization_id,
        private_object_id=attachment_id,
        reason="resource_deleted",
    )
    await db.commit()


def public_attachment_metadata(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "filename": item["filename"],
            "media_type": item["media_type"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in items
    ]
