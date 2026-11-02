"""Durable regional-sourcing operations on the canonical integration runtime.

The job table stores only tenant-local identifiers. Provider negotiation,
resume, contact, and public-post payloads are decrypted or fetched inside the
worker and never copied into references, result summaries, or logs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.position import Position
from app.models.sourcing import SourceIntegration, SourcePublication
from app.schemas.sourcing import SourceCandidateInput
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.cursors import delete_cursor, load_cursor, store_cursor
from app.services.integrations.jobs import (
    IntegrationJobError,
    enqueue_job,
    load_job_payload,
)
from app.services.integrations.providers import HeadHunterAdapter
from app.services.source_import_files import SourceFileImportError, parse_source_file
from app.services.sourcing import (
    SourceImportError,
    ingest_source_candidates,
    source_connection_ready,
    validate_source_connection,
)

_MAX_RESUME_TEXT = 100_000
_MAX_COLLECTIONS = 20
_MAX_PAGES = 20
_MAX_RECORDS = 500
MAX_DURABLE_LOCAL_IMPORT_ROWS = 5_000
MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES = 40 * 1024 * 1024
MAX_DURABLE_LOCAL_IMPORT_FILE_BYTES = 25 * 1024 * 1024
_LOCAL_IMPORT_CHUNK_ROWS = 100
_TAG = re.compile(r"<[^>]{0,500}>")
_SPACE = re.compile(r"\s+")


async def enqueue_source_provider_job(
    db: AsyncSession,
    *,
    integration: SourceIntegration,
    connection: IntegrationConnection,
    actor_user_id: UUID | None,
    operation: str,
    idempotency_key: str,
    references: dict[str, str | int | bool | None],
    sensitive_payload: dict[str, Any],
    cipher: IntegrationCipher,
    required_capability: str,
    max_attempts: int = 3,
    max_payload_bytes: int = 512_000,
) -> tuple[IntegrationJob, bool]:
    """Validate the source binding before creating one encrypted provider job."""

    validate_source_connection(
        integration.provider,
        connection,
        organization_id=integration.organization_id,
    )
    if integration.connection_id != connection.id:
        raise IntegrationJobError("source_connection_mismatch")
    if not integration.is_active:
        raise IntegrationJobError("source_integration_disabled")
    if not source_connection_ready(
        integration.provider,
        connection,
        required_capability=required_capability,
    ):
        if connection.status != "connected":
            raise IntegrationJobError("connection_not_ready")
        raise IntegrationJobError("connection_capability_mismatch")
    return await enqueue_job(
        db,
        connection=connection,
        actor_user_id=actor_user_id,
        operation=operation,
        idempotency_key=idempotency_key,
        references=references,
        sensitive_payload=sensitive_payload,
        cipher=cipher,
        max_attempts=max_attempts,
        max_payload_bytes=max_payload_bytes,
    )


async def execute_sourcing_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    connection: IntegrationConnection,
    adapter: Any,
    credentials: dict[str, Any],
    cipher: IntegrationCipher,
) -> dict[str, Any] | None:
    """Execute sourcing-only jobs; return ``None`` for the generic runtime."""

    references = job.reference_payload or {}
    if references.get("source_kind") in {
        "local_candidate_import",
        "local_candidate_file_import",
    }:
        return await _execute_local_candidate_import(
            db,
            job=job,
            connection=connection,
            cipher=cipher,
        )
    if references.get("source_kind") != "hh_candidate_import":
        return None
    if job.operation != "candidate.import" or connection.provider != "hh_ru":
        raise IntegrationJobError("source_job_contract_invalid")
    if not isinstance(adapter, HeadHunterAdapter) and not all(
        hasattr(adapter, name)
        for name in (
            "list_negotiation_collections",
            "list_negotiations",
            "get_negotiation",
            "get_resume",
        )
    ):
        raise IntegrationJobError("provider_adapter_unavailable")

    source_integration_id = _reference_uuid(references, "source_integration_id")
    position_id = _reference_uuid(references, "position_id")
    integration = (
        await db.execute(
            select(SourceIntegration).where(
                SourceIntegration.id == source_integration_id,
                SourceIntegration.organization_id == job.organization_id,
                SourceIntegration.connection_id == connection.id,
                SourceIntegration.provider == "hh",
                SourceIntegration.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == job.organization_id,
                Position.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if integration is None or position is None:
        raise IntegrationJobError("source_job_resource_not_found")

    payload = load_job_payload(job, cipher=cipher)
    vacancy_id = _bounded_identifier(payload.get("vacancy_id"), 255)
    requested_collections = _collection_ids(payload.get("collection_ids"))
    max_pages = _bounded_integer(payload.get("max_pages"), 1, _MAX_PAGES)
    max_records = _bounded_integer(payload.get("max_records"), 1, _MAX_RECORDS)
    resume_from_cursor = bool(payload.get("resume_from_cursor", True))

    raw_collections = await adapter.list_negotiation_collections(
        credentials,
        vacancy_id=vacancy_id,
    )
    collection_values = raw_collections.get("collections")
    if not isinstance(collection_values, list):
        raise IntegrationJobError("provider_response_invalid")
    collection_ids: list[str] = []
    for value in collection_values:
        if not isinstance(value, dict):
            continue
        try:
            collection_id = _bounded_identifier(value.get("id"), 96)
        except IntegrationJobError:
            continue
        if requested_collections and collection_id not in requested_collections:
            continue
        if collection_id not in collection_ids:
            collection_ids.append(collection_id)
        if len(collection_ids) >= _MAX_COLLECTIONS:
            break
    if requested_collections:
        missing = set(requested_collections) - set(collection_ids)
        if missing:
            raise IntegrationJobError("hh_collection_not_found")

    cursor_name = f"hh.import.{integration.id.hex}"
    collection_index = 0
    page = 0
    if resume_from_cursor:
        cursor = await load_cursor(
            db,
            connection=connection,
            cursor_name=cursor_name,
            cipher=cipher,
        )
        if isinstance(cursor, dict) and cursor.get("job_id") == str(job.id):
            collection_index = _bounded_integer(
                cursor.get("collection_index", 0), 0, len(collection_ids)
            )
            page = _bounded_integer(cursor.get("page", 0), 0, max_pages)

    scanned = imported = duplicate = rejected = pages_fetched = 0
    for index in range(collection_index, len(collection_ids)):
        collection_id = collection_ids[index]
        current_page = page if index == collection_index else 0
        while current_page < max_pages and scanned < max_records:
            requested_page_size = min(100, max_records - scanned)
            page_payload = await adapter.list_negotiations(
                credentials,
                vacancy_id=vacancy_id,
                collection_id=collection_id,
                page=current_page,
                per_page=requested_page_size,
            )
            pages_fetched += 1
            raw_items = page_payload.get("items")
            if not isinstance(raw_items, list):
                raise IntegrationJobError("provider_response_invalid")
            if not raw_items:
                break

            normalized: list[SourceCandidateInput] = []
            for raw_item in raw_items:
                if scanned >= max_records:
                    break
                scanned += 1
                if not isinstance(raw_item, dict):
                    rejected += 1
                    continue
                try:
                    normalized.append(
                        await _normalize_hh_negotiation(
                            adapter,
                            credentials=credentials,
                            raw_item=raw_item,
                            collection_id=collection_id,
                            position_id=position.id,
                        )
                    )
                except (IntegrationJobError, ValidationError):
                    # One malformed provider record is isolated; transport and
                    # authentication failures still propagate from the adapter.
                    rejected += 1

            if normalized:
                run = await ingest_source_candidates(
                    db,
                    integration=integration,
                    items=normalized,
                    actor_user_id=job.created_by_user_id,
                    idempotency_key=(f"hh:{job.id}:{collection_id}:{current_page}"),
                    commit=False,
                )
                imported += run.created_count
                duplicate += run.duplicate_count
                rejected += run.failed_count

            next_page = current_page + 1
            await store_cursor(
                db,
                connection=connection,
                cursor_name=cursor_name,
                value={
                    "job_id": str(job.id),
                    "collection_index": index,
                    "page": next_page,
                },
                cipher=cipher,
            )
            pages = page_payload.get("pages")
            if isinstance(pages, int) and pages >= 0:
                if next_page >= pages:
                    break
            elif len(raw_items) < requested_page_size:
                break
            current_page = next_page
        page = 0
        await store_cursor(
            db,
            connection=connection,
            cursor_name=cursor_name,
            value={
                "job_id": str(job.id),
                "collection_index": index + 1,
                "page": 0,
            },
            cipher=cipher,
        )
        if scanned >= max_records:
            break

    await delete_cursor(db, connection=connection, cursor_name=cursor_name)
    integration.last_sync_at = datetime.now(UTC)
    return {
        "provider_accepted": True,
        "collection_count": len(collection_ids),
        "page_count": pages_fetched,
        "scanned_count": scanned,
        "imported_count": imported,
        "duplicate_count": duplicate,
        "rejected_count": rejected,
    }


async def _execute_local_candidate_import(
    db: AsyncSession,
    *,
    job: IntegrationJob,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
) -> dict[str, Any]:
    """Ingest one encrypted local export in bounded, idempotent chunks."""

    if job.operation != "candidate.import" or connection.provider not in {
        "csv_xlsx",
        "somon",
        "generic_webhook",
        "hh_ru",
    }:
        raise IntegrationJobError("source_job_contract_invalid")
    references = job.reference_payload or {}
    integration_id = _reference_uuid(references, "source_integration_id")
    integration = (
        await db.execute(
            select(SourceIntegration).where(
                SourceIntegration.id == integration_id,
                SourceIntegration.organization_id == job.organization_id,
                SourceIntegration.connection_id == connection.id,
                SourceIntegration.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        raise IntegrationJobError("source_job_resource_not_found")
    try:
        validate_source_connection(
            integration.provider,
            connection,
            organization_id=job.organization_id,
        )
    except SourceImportError as exc:
        raise IntegrationJobError(exc.code) from exc

    payload = load_job_payload(job, cipher=cipher)
    if references.get("source_kind") == "local_candidate_file_import":
        format_name = payload.get("format")
        encoded = payload.get("content_b64")
        mapping = payload.get("mapping")
        max_encoded_bytes = ((MAX_DURABLE_LOCAL_IMPORT_FILE_BYTES + 2) // 3) * 4
        if (
            format_name not in {"csv", "xlsx"}
            or not isinstance(encoded, str)
            or len(encoded) > max_encoded_bytes
            or not isinstance(mapping, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in mapping.items()
            )
        ):
            raise IntegrationJobError("job_payload_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise IntegrationJobError("job_payload_invalid") from exc
        if (
            not content
            or len(content) > MAX_DURABLE_LOCAL_IMPORT_FILE_BYTES
            or references.get("content_bytes") != len(content)
            or references.get("format") != format_name
            or references.get("content_sha256") != hashlib.sha256(content).hexdigest()
        ):
            raise IntegrationJobError("source_job_contract_invalid")
        try:
            items = await parse_source_file(
                content,
                format_name=format_name,
                mapping=mapping,
                max_rows=MAX_DURABLE_LOCAL_IMPORT_ROWS,
            )
        except SourceFileImportError as exc:
            code = (
                exc.detail.get("code")
                if isinstance(exc.detail, dict)
                else "source_file_invalid"
            )
            if not isinstance(code, str) or not code:
                code = "source_file_invalid"
            raise IntegrationJobError(code[:64]) from exc
    else:
        raw_items = payload.get("candidates")
        if (
            not isinstance(raw_items, list)
            or not raw_items
            or len(raw_items) > MAX_DURABLE_LOCAL_IMPORT_ROWS
        ):
            raise IntegrationJobError("job_payload_invalid")
        requested_count = references.get("requested_count")
        if requested_count != len(raw_items):
            raise IntegrationJobError("source_job_contract_invalid")
        try:
            items = [SourceCandidateInput.model_validate(item) for item in raw_items]
        except ValidationError as exc:
            raise IntegrationJobError("job_payload_invalid") from exc

    created = duplicate = failed = 0
    for start in range(0, len(items), _LOCAL_IMPORT_CHUNK_ROWS):
        chunk = items[start : start + _LOCAL_IMPORT_CHUNK_ROWS]
        try:
            run = await ingest_source_candidates(
                db,
                integration=integration,
                items=chunk,
                actor_user_id=job.created_by_user_id,
                idempotency_key=(f"local:{job.id}:{start // _LOCAL_IMPORT_CHUNK_ROWS}"),
                commit=False,
            )
        except SourceImportError as exc:
            raise IntegrationJobError(exc.code) from exc
        created += run.created_count
        duplicate += run.duplicate_count
        failed += run.failed_count
    integration.last_sync_at = datetime.now(UTC)
    return {
        "provider_accepted": True,
        "requested_count": len(items),
        "chunk_count": (len(items) + _LOCAL_IMPORT_CHUNK_ROWS - 1)
        // _LOCAL_IMPORT_CHUNK_ROWS,
        "imported_count": created,
        "duplicate_count": duplicate,
        "rejected_count": failed,
    }


async def reconcile_sourcing_job(
    db: AsyncSession,
    *,
    job: IntegrationJob,
) -> None:
    """Project durable job state onto the user-facing publication record."""

    references = job.reference_payload or {}
    if references.get("source_kind") != "source_publication":
        return
    try:
        publication_id = _reference_uuid(references, "publication_id")
    except IntegrationJobError:
        return
    publication = (
        await db.execute(
            select(SourcePublication).where(
                SourcePublication.id == publication_id,
                SourcePublication.organization_id == job.organization_id,
            )
        )
    ).scalar_one_or_none()
    if publication is None:
        return
    now = datetime.now(UTC)
    if job.status == "succeeded":
        publication.status = "published"
        external = (job.result_summary or {}).get("external_ref")
        publication.external_id = (
            external[:255] if isinstance(external, str) and external else None
        )
        publication.failure_code = None
        publication.published_at = job.completed_at or now
    elif job.status in {"dead_letter", "cancelled"}:
        publication.status = "failed"
        publication.failure_code = (
            job.failure_code
            or ("job_cancelled" if job.status == "cancelled" else "job_failed")
        )[:64]
        publication.published_at = None
    else:
        publication.status = "queued"
        publication.failure_code = job.failure_code[:64] if job.failure_code else None
    integration = (
        await db.execute(
            select(SourceIntegration).where(
                SourceIntegration.id == publication.integration_id,
                SourceIntegration.organization_id == job.organization_id,
            )
        )
    ).scalar_one_or_none()
    if integration is not None and job.status == "succeeded":
        integration.last_sync_at = publication.published_at or now
    await db.flush()


async def _normalize_hh_negotiation(
    adapter: Any,
    *,
    credentials: dict[str, Any],
    raw_item: dict[str, Any],
    collection_id: str,
    position_id: UUID,
) -> SourceCandidateInput:
    negotiation_id = _bounded_identifier(raw_item.get("id"), 255)
    negotiation = await adapter.get_negotiation(
        credentials,
        collection_id=collection_id,
        negotiation_id=negotiation_id,
    )
    resume_stub = negotiation.get("resume") or raw_item.get("resume")
    if not isinstance(resume_stub, dict):
        raise IntegrationJobError("hh_resume_missing")
    resume_id = _bounded_identifier(resume_stub.get("id"), 255)
    resume = await adapter.get_resume(credentials, resume_id=resume_id)
    name = _resume_name(resume)
    cv_text = _resume_text(resume)
    phone, email_address = _resume_contacts(resume)
    return SourceCandidateInput(
        external_id=negotiation_id,
        position_id=position_id,
        name=name,
        phone=phone,
        email=email_address,
        cv_text=cv_text,
        source_url=f"https://hh.ru/resume/{resume_id}",
    )


def _resume_name(resume: dict[str, Any]) -> str:
    values = [
        resume.get("first_name"),
        resume.get("middle_name"),
        resume.get("last_name"),
    ]
    name = " ".join(str(value).strip() for value in values if str(value or "").strip())
    if not name:
        raise IntegrationJobError("hh_resume_name_missing")
    return name[:200]


def _resume_contacts(resume: dict[str, Any]) -> tuple[str | None, str | None]:
    phone: str | None = None
    email_address: str | None = None
    contacts = resume.get("contact")
    if not isinstance(contacts, list):
        return phone, email_address
    for item in contacts[:20]:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        kind_id = kind.get("id") if isinstance(kind, dict) else kind
        value = item.get("value")
        if kind_id == "email" and isinstance(value, str):
            email_address = value.strip()[:320] or None
        elif kind_id in {"cell", "phone"}:
            if isinstance(value, str):
                phone = value.strip()[:64] or None
            elif isinstance(value, dict):
                parts = [value.get("country"), value.get("city"), value.get("number")]
                assembled = "".join(str(part or "").strip() for part in parts)
                phone = assembled[:64] or None
    return phone, email_address


def _resume_text(resume: dict[str, Any]) -> str:
    fragments: list[str] = []

    def add(label: str, value: Any) -> None:
        if isinstance(value, str):
            cleaned = _plain(value)
            if cleaned:
                fragments.append(f"{label}: {cleaned}")

    add("Title", resume.get("title"))
    add("About", resume.get("skills"))
    skill_set = resume.get("skill_set")
    if isinstance(skill_set, list):
        skills = ", ".join(_plain(str(item)) for item in skill_set[:100] if item)
        add("Skills", skills)
    experience = resume.get("experience")
    if isinstance(experience, list):
        for item in experience[:50]:
            if not isinstance(item, dict):
                continue
            values = [
                item.get("company"),
                item.get("position"),
                item.get("start"),
                item.get("end"),
                item.get("description"),
            ]
            add("Experience", " · ".join(str(value) for value in values if value))
    education = resume.get("education")
    primary = education.get("primary") if isinstance(education, dict) else None
    if isinstance(primary, list):
        for item in primary[:25]:
            if isinstance(item, dict):
                add(
                    "Education",
                    " · ".join(
                        str(item.get(key))
                        for key in ("name", "organization", "result", "year")
                        if item.get(key)
                    ),
                )
    text = "\n".join(fragments).strip()
    if not text:
        raise IntegrationJobError("hh_resume_content_missing")
    return text[:_MAX_RESUME_TEXT]


def _plain(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", value))).strip()


def _reference_uuid(references: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(references[key]))
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrationJobError("source_job_contract_invalid") from exc


def _bounded_identifier(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise IntegrationJobError("provider_response_invalid")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > maximum
        or not re.fullmatch(r"[A-Za-z0-9_-]+", cleaned)
    ):
        raise IntegrationJobError("provider_response_invalid")
    return cleaned


def _collection_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_COLLECTIONS:
        raise IntegrationJobError("job_payload_invalid")
    result: list[str] = []
    for item in value:
        identifier = _bounded_identifier(item, 96)
        if identifier not in result:
            result.append(identifier)
    return result


def _bounded_integer(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise IntegrationJobError("job_payload_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise IntegrationJobError("job_payload_invalid") from exc
    if not minimum <= parsed <= maximum:
        raise IntegrationJobError("job_payload_invalid")
    return parsed
