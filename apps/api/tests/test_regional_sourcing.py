import asyncio
import hashlib
import io
import json
import random
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException, Request, Response, UploadFile
from openpyxl import Workbook
from pydantic import ValidationError
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.db import SessionLocal
from app.main import app
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.enterprise import PermissionOverride
from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.sourcing import (
    ReferralLink,
    SourceCandidateReceipt,
    SourceImportRun,
    SourceIntegration,
    SourcePublication,
)
from app.models.user import User
from app.routers import sourcing as sourcing_router_module
from app.routers.public import public_apply
from app.routers.sourcing import (
    create_referral_link,
    create_source_integration,
    import_source_candidates,
    import_source_file,
    list_source_integrations,
    preview_source_file,
    preview_source_publication,
    publish_source_publication,
    receive_source_webhook,
    resolve_public_referral,
    rotate_source_webhook_secret,
    source_analytics,
    update_source_integration,
)
from app.schemas.integrations import WebhookEnvelope
from app.schemas.sourcing import (
    ReferralLinkCreate,
    SourceCandidateInput,
    SourceImportRequest,
    SourceIntegrationCreate,
    SourceIntegrationUpdate,
    SourcePublicationPreviewRequest,
)
from app.services.applications import ensure_application_link
from app.services.candidate_merge import merge_candidates
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.lifecycle import create_connection
from app.services.integrations.webhooks import (
    canonical_envelope_bytes,
    sign_webhook,
)
from app.services.integrations.worker import process_integration_jobs_once
from app.services.sourcing import SOURCE_CONNECTION_PROVIDERS

pytestmark = pytest.mark.asyncio
TEST_CIPHER = IntegrationCipher(b"s" * 32, key_id="sourcing-test-key")


async def seed_tenant(db, suffix: str):
    organization = Organization(
        name=f"Sourcing org {suffix}",
        slug=f"sourcing-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"sourcing-{suffix}-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    first = Position(
        organization_id=organization.id,
        title=f"Backend Engineer {suffix}",
        description="Build reliable services for regional customers.",
        status="open",
    )
    second = Position(
        organization_id=organization.id,
        title=f"Platform Engineer {suffix}",
        description="Own developer infrastructure and observability.",
        status="open",
    )
    db.add_all([admin, first, second])
    await db.flush()
    first.created_by = admin.id
    second.created_by = admin.id
    await db.commit()
    return organization, admin, first, second


async def create_integration(db, user, provider: str, name: str, configuration=None):
    configuration = configuration or {}
    connection = None
    if configuration.get("mode") != "sandbox":
        connection, _secret = await create_connection(
            db,
            organization_id=user.organization_id,
            actor_user_id=user.id,
            provider=SOURCE_CONNECTION_PROVIDERS[provider],
            name=f"{name} canonical",
            configuration={},
            cipher=TEST_CIPHER,
        )
    response = await create_source_integration(
        SourceIntegrationCreate(
            provider=provider,
            name=name,
            connection_id=connection.id if connection is not None else None,
            configuration=configuration,
        ),
        db=db,
        user=user,
    )
    integration = await db.get(SourceIntegration, response.integration.id)
    assert integration is not None
    return response, integration


def source_webhook_envelope(
    event_id: str,
    candidate: SourceCandidateInput,
    *,
    created_at: datetime | None = None,
) -> WebhookEnvelope:
    return WebhookEnvelope(
        id=event_id,
        type="source.candidate.created",
        created_at=created_at or datetime.now(UTC),
        data=candidate.model_dump(mode="json"),
    )


def signed_source_webhook_request(
    envelope: WebhookEnvelope,
    secret: str,
    *,
    signed_at: datetime | None = None,
    signature_body: bytes | None = None,
) -> tuple[Request, str, str]:
    raw_body = canonical_envelope_bytes(envelope)
    request = raw_source_webhook_request(raw_body)
    timestamp = int((signed_at or datetime.now(UTC)).timestamp())
    signature = sign_webhook(
        secret,
        timestamp,
        signature_body if signature_body is not None else raw_body,
    )
    return request, str(timestamp), signature


def raw_source_webhook_request(raw_body: bytes) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/source-webhooks/test",
            "raw_path": b"/source-webhooks/test",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        },
        receive,
    )


async def test_source_webhook_rejects_oversized_body_before_resource_lookup():
    request = raw_source_webhook_request(b"x" * 256_001)
    with pytest.raises(HTTPException) as oversized:
        await receive_source_webhook(
            uuid4(),
            request=request,
            webhook_secret=None,
            webhook_timestamp=None,
            webhook_signature=None,
            db=object(),
        )
    assert oversized.value.status_code == 413
    assert oversized.value.detail["code"] == "webhook_payload_too_large"


def csv_upload(position_id) -> UploadFile:
    content = (
        "external_id,position_id,name,phone,email,cv_text,source_url,utm_campaign\n"
        f'csv-1,{position_id},"Ada CSV","+992 900 111 222",,'
        '"Python engineer with PostgreSQL and distributed systems experience.",'
        'https://source.example/csv-1,"summer hiring"\n'
    ).encode()
    return UploadFile(filename="candidates.csv", file=io.BytesIO(content))


def xlsx_upload(position_id) -> UploadFile:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "external_id",
            "position_id",
            "name",
            "phone",
            "email",
            "cv_text",
            "source_url",
            "utm_campaign",
        ]
    )
    sheet.append(
        [
            "xlsx-1",
            str(position_id),
            "Ada Spreadsheet",
            "+992 900 111 222",
            None,
            "The same engineer now applying to a second vacancy through XLSX.",
            "https://source.example/xlsx-1",
            None,
        ]
    )
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return UploadFile(filename="candidates.xlsx", file=output)


def mapped_somon_upload(position_id) -> UploadFile:
    content = (
        "somon_ref,vacancy_ref,applicant,telephone,mail,resume,listing_url\n"
        f'somon-1,{position_id},"Somon Candidate","+992 900 777 888",'
        'somon@example.com,"Python engineer from a validated partner export.",'
        "https://somon.tj/adv/validated-export-1\n"
    ).encode()
    return UploadFile(filename="somon-export.csv", file=io.BytesIO(content))


async def test_provider_capabilities_secret_handling_and_tenant_isolation(db):
    organization, admin, _position, _second = await seed_tenant(db, "capabilities")
    other_org, other_admin, _other_position, _ = await seed_tenant(db, "other")

    generic, generic_model = await create_integration(
        db, admin, "generic_webhook", "Inbound ATS webhook"
    )
    assert generic.webhook_secret
    assert (
        generic_model.webhook_secret_hash
        == hashlib.sha256(generic.webhook_secret.encode()).hexdigest()
    )
    assert generic_model.webhook_secret_hash != generic.webhook_secret
    assert generic.integration.webhook_configured is True
    assert generic.integration.capabilities.webhook_intake == "available"
    assert generic.integration.capabilities.vacancy_publish == "unsupported"

    hh, hh_model = await create_integration(
        db, admin, "hh", "hh.ru sandbox", {"mode": "sandbox"}
    )
    assert hh.webhook_secret is None
    assert hh_model.webhook_secret_hash is None
    assert hh.integration.status == "ready"
    assert hh.integration.capabilities.candidate_import == "sandbox"

    somon, _ = await create_integration(db, admin, "somon", "Somon validated")
    assert somon.integration.status == "ready"
    assert somon.integration.capabilities.candidate_import == "available"

    telegram, _ = await create_integration(
        db,
        admin,
        "telegram",
        "Telegram sandbox",
        {"mode": "sandbox", "channel_username": "jobs_tj"},
    )
    assert telegram.integration.capabilities.vacancy_publish == "sandbox"
    assert telegram.integration.capabilities.candidate_import == "unsupported"

    with pytest.raises(HTTPException) as rotate_error:
        await rotate_source_webhook_secret(hh_model.id, db=db, user=admin)
    assert rotate_error.value.status_code == 422

    with pytest.raises(ValidationError):
        SourceIntegrationCreate(
            provider="hh",
            name="Unsafe",
            configuration={"nested": {"api_key": "must-not-be-stored"}},
        )
    with pytest.raises(ValidationError):
        SourceIntegrationCreate(provider="csv", name="   ")
    with pytest.raises(ValidationError):
        SourceIntegrationUpdate(configuration={"mode": "unknown"})

    other, _ = await create_integration(db, other_admin, "csv", "Other tenant CSV")
    visible = await list_source_integrations(limit=50, cursor=None, db=db, user=admin)
    assert {item.id for item in visible.items} == {
        generic.integration.id,
        hh.integration.id,
        somon.integration.id,
        telegram.integration.id,
    }
    assert other.integration.id not in {item.id for item in visible.items}
    with pytest.raises(HTTPException) as tenant_error:
        await update_source_integration(
            other.integration.id,
            SourceIntegrationUpdate(name="Cross-tenant mutation"),
            db=db,
            user=admin,
        )
    assert tenant_error.value.status_code == 404
    assert other_org.id != organization.id


async def test_source_integration_mutations_follow_permission_overrides(db):
    organization, admin, _position, _second = await seed_tenant(db, "permissions")
    restricted = User(
        organization_id=organization.id,
        email=f"sourcing-restricted-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="restricted",
        locale="en",
    )
    db.add_all(
        [
            restricted,
            PermissionOverride(
                organization_id=organization.id,
                role="restricted",
                permission="integrations.manage",
                allowed=True,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="admin",
                permission="integrations.manage",
                allowed=False,
            ),
        ]
    )
    await db.commit()

    delegated_connection, _ = await create_connection(
        db,
        organization_id=organization.id,
        actor_user_id=restricted.id,
        provider="generic_webhook",
        name="Delegated canonical intake",
        configuration={},
        cipher=TEST_CIPHER,
    )
    created = await create_source_integration(
        SourceIntegrationCreate(
            provider="generic_webhook",
            name="Delegated intake",
            connection_id=delegated_connection.id,
        ),
        db=db,
        user=restricted,
    )
    assert created.webhook_secret

    with pytest.raises(HTTPException) as credential_denied:
        await rotate_source_webhook_secret(
            created.integration.id,
            db=db,
            user=restricted,
        )
    assert credential_denied.value.status_code == 403

    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="restricted",
            permission="integrations.credentials",
            allowed=True,
        )
    )
    await db.commit()
    rotated = await rotate_source_webhook_secret(
        created.integration.id,
        db=db,
        user=restricted,
    )
    assert rotated.webhook_secret

    with pytest.raises(HTTPException) as admin_denied:
        await create_source_integration(
            SourceIntegrationCreate(
                provider="csv",
                name="Denied admin mutation",
                connection_id=delegated_connection.id,
            ),
            db=db,
            user=admin,
        )
    assert admin_denied.value.status_code == 403


async def test_csv_and_xlsx_import_reuse_identity_and_keep_activity_safe(db):
    organization, admin, first, second = await seed_tenant(db, "files")
    _created, integration = await create_integration(
        db, admin, "csv", "Spreadsheet imports"
    )

    csv_run = await import_source_file(
        integration.id,
        csv_upload(first.id),
        db=db,
        user=admin,
    )
    assert (csv_run.status, csv_run.created_count, csv_run.failed_count) == (
        "completed",
        1,
        0,
    )

    xlsx_run = await import_source_file(
        integration.id,
        xlsx_upload(second.id),
        db=db,
        user=admin,
    )
    assert (xlsx_run.status, xlsx_run.created_count, xlsx_run.duplicate_count) == (
        "completed",
        0,
        1,
    )

    candidates = list(
        (
            await db.execute(
                select(Candidate).where(Candidate.organization_id == organization.id)
            )
        ).scalars()
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.phone_normalized == "+992900111222"
    assert candidate.email_normalized is None
    assert candidate.parsed_cv == {
        "applicant_name": "Ada CSV",
        "applied_via": "csv",
        "utm_campaign": "summer hiring",
    }
    assert (
        await db.scalar(
            select(func.count(Score.id)).where(Score.candidate_id == candidate.id)
        )
        == 2
    )
    receipts = list(
        (
            await db.execute(
                select(SourceCandidateReceipt)
                .where(SourceCandidateReceipt.candidate_id == candidate.id)
                .order_by(SourceCandidateReceipt.external_id)
            )
        ).scalars()
    )
    assert [receipt.external_id for receipt in receipts] == ["csv-1", "xlsx-1"]
    assert {receipt.provider_snapshot for receipt in receipts} == {"csv"}
    activities = list(
        (
            await db.execute(
                select(CandidateActivity)
                .where(CandidateActivity.candidate_id == candidate.id)
                .order_by(CandidateActivity.occurred_at)
            )
        ).scalars()
    )
    assert [activity.event_type for activity in activities] == [
        "candidate_added",
        "candidate_sourced",
    ]
    assert all(activity.actor_user_id == admin.id for activity in activities)
    serialized_payloads = repr([activity.payload for activity in activities]).lower()
    assert "postgresql" not in serialized_payloads
    assert "phone" not in serialized_payloads
    assert "email" not in serialized_payloads
    assert "cv_text" not in serialized_payloads

    _generic_created, generic = await create_integration(
        db, admin, "generic_webhook", "Webhook cannot import files"
    )
    with pytest.raises(HTTPException) as unsupported:
        await import_source_file(
            generic.id,
            csv_upload(first.id),
            db=db,
            user=admin,
        )
    assert unsupported.value.status_code == 422


async def test_file_import_exact_row_and_byte_boundaries(db, monkeypatch):
    _organization, admin, position, _second = await seed_tenant(db, "exact-file-limits")
    _created, integration = await create_integration(
        db, admin, "csv", "Exact import limits"
    )

    def row_csv(count: int) -> UploadFile:
        header = "external_id,position_id,name,cv_text\n"
        rows = "".join(
            f"row-{index},{position.id},Candidate {index},Qualified engineer\n"
            for index in range(count)
        )
        return UploadFile(
            filename="candidates.csv",
            file=io.BytesIO((header + rows).encode()),
        )

    at_row_limit = await preview_source_file(
        integration.id,
        row_csv(sourcing_router_module.MAX_SYNCHRONOUS_IMPORT_ROWS),
        db=db,
        user=admin,
    )
    above_row_limit = await preview_source_file(
        integration.id,
        row_csv(sourcing_router_module.MAX_SYNCHRONOUS_IMPORT_ROWS + 1),
        db=db,
        user=admin,
    )
    assert at_row_limit.row_count == 500
    assert at_row_limit.import_mode == "synchronous"
    assert above_row_limit.row_count == 501
    assert above_row_limit.import_mode == "durable_job"

    parsed_row = SourceCandidateInput(
        external_id="threshold-row",
        position_id=position.id,
        name="Threshold Candidate",
        cv_text="Qualified engineer",
    )
    monkeypatch.setattr(
        sourcing_router_module,
        "_rows_from_csv",
        lambda _content, _mapping=None: [parsed_row],
    )

    def sized_csv(size: int) -> UploadFile:
        return UploadFile(filename="sized.csv", file=io.BytesIO(b"x" * size))

    sync_bytes = sourcing_router_module.MAX_IMPORT_FILE_BYTES
    absolute_bytes = sourcing_router_module.MAX_DURABLE_IMPORT_FILE_BYTES
    at_sync_bytes = await preview_source_file(
        integration.id,
        sized_csv(sync_bytes),
        db=db,
        user=admin,
    )
    above_sync_bytes = await preview_source_file(
        integration.id,
        sized_csv(sync_bytes + 1),
        db=db,
        user=admin,
    )
    at_absolute_bytes = await preview_source_file(
        integration.id,
        sized_csv(absolute_bytes),
        db=db,
        user=admin,
    )
    assert at_sync_bytes.import_mode == "synchronous"
    assert above_sync_bytes.import_mode == "durable_job"
    assert at_absolute_bytes.import_mode == "durable_job"
    with pytest.raises(HTTPException) as above_absolute:
        await preview_source_file(
            integration.id,
            sized_csv(absolute_bytes + 1),
            db=db,
            user=admin,
        )
    assert above_absolute.value.status_code == 413
    assert above_absolute.value.detail == {
        "code": "limit_exceeded",
        "limit": absolute_bytes,
        "requested": absolute_bytes + 1,
        "unit": "bytes",
    }


async def test_csv_xlsx_malformed_mapping_and_expansion_boundaries(db):
    _organization, _admin, position, _second = await seed_tenant(
        db, "malformed-file-limits"
    )
    with pytest.raises(HTTPException) as invalid_utf8:
        sourcing_router_module._rows_from_csv(b"\xff\xfe")
    assert invalid_utf8.value.status_code == 422
    with pytest.raises(HTTPException) as malformed_csv:
        sourcing_router_module._rows_from_csv(
            b'external_id,position_id,name,cv_text\n"unterminated'
        )
    assert malformed_csv.value.status_code == 422
    with pytest.raises(HTTPException) as missing_headers:
        sourcing_router_module._rows_from_csv(b"external_id,name\n1,Ada\n")
    assert missing_headers.value.status_code == 422
    with pytest.raises(HTTPException) as duplicate_mapping:
        sourcing_router_module._mapping_from_json(
            json.dumps(
                {
                    "external_id": "candidate_ref",
                    "position_id": "candidate_ref",
                }
            )
        )
    assert duplicate_mapping.value.status_code == 422
    with pytest.raises(HTTPException) as malformed_xlsx:
        await sourcing_router_module._rows_from_xlsx(b"not-a-zip")
    assert malformed_xlsx.value.status_code == 422

    base_upload = xlsx_upload(position.id)
    base_content = base_upload.file.read()

    def padded_xlsx(expanded_size: int) -> bytes:
        source = zipfile.ZipFile(io.BytesIO(base_content))
        source_size = sum(item.file_size for item in source.infolist())
        assert source_size < expanded_size
        output = io.BytesIO()
        with (
            source,
            zipfile.ZipFile(
                output, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as target,
        ):
            for item in source.infolist():
                target.writestr(item, source.read(item.filename))
            remaining = expanded_size - source_size
            index = 0
            pseudo_random = random.Random(20260824)
            while remaining:
                chunk_size = min(20 * 1024 * 1024, remaining)
                noisy_size = max(16 * 1024, chunk_size // 50)
                payload = pseudo_random.randbytes(noisy_size) + b"\0" * (
                    chunk_size - noisy_size
                )
                target.writestr(f"padding-{index}.bin", payload)
                remaining -= chunk_size
                index += 1
        return output.getvalue()

    expanded_limit = sourcing_router_module.MAX_XLSX_UNCOMPRESSED_BYTES
    # The expansion limit is an outer resource ceiling, not permission to add
    # opaque OOXML parts.  A package at the exact byte ceiling still fails the
    # strict rectangular-workbook allowlist, while crossing the ceiling is a
    # size error before the parser is invoked.
    with pytest.raises(HTTPException) as unsupported_at_expanded_limit:
        await sourcing_router_module._rows_from_xlsx(padded_xlsx(expanded_limit))
    assert unsupported_at_expanded_limit.value.status_code == 422
    assert unsupported_at_expanded_limit.value.detail == {
        "code": "spreadsheet_package_unsupported"
    }
    with pytest.raises(HTTPException) as expanded_too_large:
        await sourcing_router_module._rows_from_xlsx(padded_xlsx(expanded_limit + 1))
    assert expanded_too_large.value.status_code == 413


async def test_large_local_import_is_encrypted_durable_idempotent_and_chunked(
    db, monkeypatch
):
    organization, admin, position, _second = await seed_tenant(db, "durable-local")
    _created, integration = await create_integration(
        db, admin, "csv", "Durable spreadsheet intake"
    )
    monkeypatch.setattr(
        sourcing_router_module,
        "MAX_SYNCHRONOUS_IMPORT_ROWS",
        2,
    )
    body = SourceImportRequest(
        candidates=[
            SourceCandidateInput(
                external_id=f"durable-{index}",
                position_id=position.id,
                name=f"Durable Candidate {index}",
                email=f"durable-{index}@example.com",
                cv_text="Candidate data encrypted until the worker consumes it.",
            )
            for index in range(3)
        ]
    )
    submission_response = Response()
    submitted = await import_source_candidates(
        integration.id,
        body,
        db=db,
        user=admin,
        idempotency_key="durable-local-import-1",
        runtime=SimpleNamespace(cipher=TEST_CIPHER),
        response=submission_response,
    )
    assert submission_response.status_code == 202
    job = await db.get(IntegrationJob, submitted.id)
    assert job is not None
    assert job.status == "queued"
    assert job.reference_payload == {
        "source_kind": "local_candidate_import",
        "source_integration_id": str(integration.id),
        "requested_count": 3,
    }
    assert job.payload_ciphertext is not None
    assert b"Durable Candidate" not in job.payload_ciphertext
    assert "example.com" not in repr(job.reference_payload)

    connection = await db.get(IntegrationConnection, integration.connection_id)
    assert connection is not None
    await db.commit()
    worker_counts = await process_integration_jobs_once(
        SessionLocal,
        cipher=TEST_CIPHER,
        adapters={},
        worker_id="durable-local-import-test-worker",
        limit=1,
        mode="off",
    )
    await db.refresh(job)
    assert worker_counts == {"claimed": 1, "succeeded": 1, "failed": 0}, (
        job.failure_code,
        job.result_summary,
    )
    assert job.status == "succeeded"
    assert job.result_summary == {
        "provider_accepted": True,
        "requested_count": 3,
        "chunk_count": 1,
        "imported_count": 3,
        "duplicate_count": 0,
        "rejected_count": 0,
    }
    assert job.payload_ciphertext is None
    assert job.payload_nonce is None
    assert job.payload_key_id is None
    assert (
        await db.scalar(
            select(func.count(Candidate.id)).where(
                Candidate.organization_id == organization.id
            )
        )
        == 3
    )

    replay = await import_source_candidates(
        integration.id,
        body,
        db=db,
        user=admin,
        idempotency_key="durable-local-import-1",
        runtime=SimpleNamespace(cipher=TEST_CIPHER),
    )
    assert replay.id == job.id
    assert (
        await db.scalar(
            select(func.count(Candidate.id)).where(
                Candidate.organization_id == organization.id
            )
        )
        == 3
    )


async def test_file_preview_marks_payloads_over_sync_bytes_for_durable_queue(
    db, monkeypatch
):
    organization, admin, position, _second = await seed_tenant(
        db, "durable-file-preview"
    )
    _created, integration = await create_integration(
        db, admin, "csv", "Durable file preview"
    )
    monkeypatch.setattr(sourcing_router_module, "MAX_IMPORT_FILE_BYTES", 64)
    preview = await preview_source_file(
        integration.id,
        csv_upload(position.id),
        db=db,
        user=admin,
    )
    assert preview.import_mode == "durable_job"
    assert preview.synchronous_file_bytes == 64
    assert preview.absolute_file_bytes > preview.synchronous_file_bytes
    assert preview.absolute_row_limit > preview.synchronous_row_limit

    submission_response = Response()
    submitted = await import_source_file(
        integration.id,
        csv_upload(position.id),
        db=db,
        user=admin,
        idempotency_key="durable-file-import-1",
        runtime=SimpleNamespace(cipher=TEST_CIPHER),
        response=submission_response,
    )
    assert submission_response.status_code == 202
    job = await db.get(IntegrationJob, submitted.id)
    assert job is not None
    assert job.status == "queued"
    assert job.reference_payload["source_kind"] == "local_candidate_file_import"
    assert job.reference_payload["format"] == "csv"
    assert job.reference_payload["content_bytes"] > 64
    assert len(job.reference_payload["content_sha256"]) == 64
    assert "Ada CSV" not in repr(job.reference_payload)
    assert job.payload_ciphertext is not None
    assert b"Ada CSV" not in job.payload_ciphertext

    await db.commit()
    worker_counts = await process_integration_jobs_once(
        SessionLocal,
        cipher=TEST_CIPHER,
        adapters={},
        worker_id="durable-file-import-test-worker",
        limit=1,
        mode="off",
    )
    await db.refresh(job)
    assert worker_counts == {"claimed": 1, "succeeded": 1, "failed": 0}, (
        job.failure_code,
        job.result_summary,
    )
    assert job.status == "succeeded"
    assert job.result_summary["requested_count"] == 1
    assert job.payload_ciphertext is None
    assert (
        await db.scalar(
            select(func.count(Candidate.id)).where(
                Candidate.organization_id == organization.id
            )
        )
        == 1
    )


async def test_somon_tracked_links_mapped_preview_and_file_replay_are_real_not_scraped(
    db,
):
    _organization, admin, position, _second = await seed_tenant(db, "somon-live")
    created, integration = await create_integration(
        db, admin, "somon", "Somon validated exports"
    )
    assert created.integration.status == "ready"
    assert created.integration.capabilities.file_import == "available"
    mapping = json.dumps(
        {
            "external_id": "somon_ref",
            "position_id": "vacancy_ref",
            "name": "applicant",
            "phone": "telephone",
            "email": "mail",
            "cv_text": "resume",
            "source_url": "listing_url",
        }
    )
    preview = await preview_source_file(
        integration.id,
        mapped_somon_upload(position.id),
        db=db,
        user=admin,
        mapping=mapping,
    )
    assert preview.row_count == 1
    assert preview.items[0].masked_phone == "***7888"
    assert preview.items[0].masked_email == "s***@example.com"
    assert preview.items[0].cv_characters > 20

    first = await import_source_file(
        integration.id,
        mapped_somon_upload(position.id),
        db=db,
        user=admin,
        mapping=mapping,
        idempotency_key="somon-export-1",
    )
    replay = await import_source_file(
        integration.id,
        mapped_somon_upload(position.id),
        db=db,
        user=admin,
        mapping=mapping,
        idempotency_key="somon-export-1",
    )
    assert first.id == replay.id
    assert first.created_count == 1

    publication_request = SourcePublicationPreviewRequest(
        position_id=position.id,
        locale="ru",
        idempotency_key="somon-tracked-position-1",
    )
    tracked = await publish_source_publication(
        integration.id, publication_request, db=db, user=admin
    )
    tracked_replay = await publish_source_publication(
        integration.id, publication_request, db=db, user=admin
    )
    assert tracked.status == "tracked"
    assert tracked.id == tracked_replay.id
    assert "utm_source=somon" in tracked.tracking_url


async def test_secret_webhook_is_idempotent_and_concurrency_safe(db):
    organization, admin, position, _second = await seed_tenant(db, "webhook")
    other_org, _other_admin, other_position, _ = await seed_tenant(db, "webhook-other")
    created, integration = await create_integration(
        db, admin, "generic_webhook", "Partner webhook"
    )
    assert created.webhook_secret is not None
    body = SourceCandidateInput(
        external_id="partner-candidate-42",
        position_id=position.id,
        name="Webhook Candidate",
        phone="+992 900 333 444",
        email="candidate@example.com",
        cv_text="Backend candidate delivered by an external sourcing partner.",
        source_url="https://partner.example/candidates/42",
    )
    envelope = source_webhook_envelope("evt_partner_candidate_42", body)

    bad_request, bad_timestamp, bad_signature = signed_source_webhook_request(
        envelope, "incorrect-secret"
    )
    with pytest.raises(HTTPException) as bad_secret:
        await receive_source_webhook(
            integration.id,
            request=bad_request,
            webhook_secret="incorrect-secret",
            webhook_timestamp=bad_timestamp,
            webhook_signature=bad_signature,
            db=db,
        )
    assert bad_secret.value.status_code == 404

    missing_request, missing_timestamp, _missing_signature = (
        signed_source_webhook_request(envelope, created.webhook_secret)
    )
    with pytest.raises(HTTPException) as missing_signature:
        await receive_source_webhook(
            integration.id,
            request=missing_request,
            webhook_secret=created.webhook_secret,
            webhook_timestamp=missing_timestamp,
            webhook_signature=None,
            db=db,
        )
    assert missing_signature.value.status_code == 404

    stale_envelope = source_webhook_envelope("evt_stale_candidate", body)
    stale_request, stale_timestamp, stale_signature = signed_source_webhook_request(
        stale_envelope,
        created.webhook_secret,
        signed_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    with pytest.raises(HTTPException) as stale:
        await receive_source_webhook(
            integration.id,
            request=stale_request,
            webhook_secret=created.webhook_secret,
            webhook_timestamp=stale_timestamp,
            webhook_signature=stale_signature,
            db=db,
        )
    assert stale.value.status_code == 404

    original_tamper_envelope = source_webhook_envelope("evt_tampered_candidate", body)
    original_tamper_body = canonical_envelope_bytes(original_tamper_envelope)
    tampered_envelope = source_webhook_envelope(
        "evt_tampered_candidate",
        body.model_copy(update={"name": "Tampered candidate"}),
        created_at=original_tamper_envelope.created_at,
    )
    tampered_request, tampered_timestamp, tampered_signature = (
        signed_source_webhook_request(
            tampered_envelope,
            created.webhook_secret,
            signature_body=original_tamper_body,
        )
    )
    with pytest.raises(HTTPException) as tampered:
        await receive_source_webhook(
            integration.id,
            request=tampered_request,
            webhook_secret=created.webhook_secret,
            webhook_timestamp=tampered_timestamp,
            webhook_signature=tampered_signature,
            db=db,
        )
    assert tampered.value.status_code == 404

    async def deliver():
        async with SessionLocal() as session:
            request, timestamp, signature = signed_source_webhook_request(
                envelope, created.webhook_secret
            )
            return await receive_source_webhook(
                integration.id,
                request=request,
                webhook_secret=created.webhook_secret,
                webhook_timestamp=timestamp,
                webhook_signature=signature,
                db=session,
            )

    first_result, second_result = await asyncio.gather(deliver(), deliver())
    assert first_result.candidate_id == second_result.candidate_id
    assert sorted([first_result.created, second_result.created]) == [False, True]
    assert sorted([first_result.replay, second_result.replay]) == [False, True]
    assert first_result.event_id == second_result.event_id == envelope.id
    assert (
        await db.scalar(
            select(func.count(SourceCandidateReceipt.id)).where(
                SourceCandidateReceipt.integration_id == integration.id,
                SourceCandidateReceipt.external_id == body.external_id,
            )
        )
        == 1
    )

    conflicting_envelope = source_webhook_envelope(
        envelope.id,
        body.model_copy(update={"name": "Conflicting signed payload"}),
        created_at=envelope.created_at,
    )
    conflict_request, conflict_timestamp, conflict_signature = (
        signed_source_webhook_request(
            conflicting_envelope, created.webhook_secret
        )
    )
    with pytest.raises(HTTPException) as conflict:
        await receive_source_webhook(
            integration.id,
            request=conflict_request,
            webhook_secret=created.webhook_secret,
            webhook_timestamp=conflict_timestamp,
            webhook_signature=conflict_signature,
            db=db,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "webhook_event_conflict"
    # Provider replay returns the original durable run instead of creating a
    # second operational record for the same external effect.
    assert first_result.run.id == second_result.run.id
    assert (
        await db.scalar(
            select(func.count(SourceImportRun.id)).where(
                SourceImportRun.integration_id == integration.id
            )
        )
        == 1
    )
    activity = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == first_result.candidate_id,
                CandidateActivity.event_type == "candidate_added",
            )
        )
    ).scalar_one()
    assert activity.actor_user_id is None
    assert activity.payload == {
        "source": "generic_webhook",
        "position_title": position.title,
    }

    invalid_tenant_body = body.model_copy(
        update={
            "external_id": "wrong-tenant-position",
            "position_id": other_position.id,
        }
    )
    invalid_envelope = source_webhook_envelope(
        "evt_wrong_tenant_position", invalid_tenant_body
    )
    invalid_request, invalid_timestamp, invalid_signature = (
        signed_source_webhook_request(invalid_envelope, created.webhook_secret)
    )
    with pytest.raises(HTTPException) as invalid_position:
        await receive_source_webhook(
            integration.id,
            request=invalid_request,
            webhook_secret=created.webhook_secret,
            webhook_timestamp=invalid_timestamp,
            webhook_signature=invalid_signature,
            db=db,
        )
    assert invalid_position.value.status_code == 422
    assert (
        await db.scalar(
            select(func.count(SourceCandidateReceipt.id)).where(
                SourceCandidateReceipt.external_id == "wrong-tenant-position"
            )
        )
        == 0
    )
    assert other_org.id != organization.id


async def test_telegram_sandbox_publication_and_live_capability_state(db):
    _organization, admin, position, _second = await seed_tenant(db, "telegram")
    sandbox_response, sandbox = await create_integration(
        db,
        admin,
        "telegram",
        "Vacancies sandbox",
        {"mode": "sandbox", "channel_username": "vacancies_tj"},
    )
    assert sandbox_response.webhook_secret is None
    request = SourcePublicationPreviewRequest(position_id=position.id, locale="ru")
    preview = await preview_source_publication(sandbox.id, request, db=db, user=admin)
    assert preview.status == "preview"
    assert position.title in preview.post_text
    assert "utm_source=telegram" in preview.tracking_url

    published = await publish_source_publication(sandbox.id, request, db=db, user=admin)
    assert published.status == "published_sandbox"
    assert published.failure_code is None
    assert published.published_at is not None
    stored = await db.get(SourcePublication, published.id)
    assert stored is not None
    assert stored.external_id is not None
    assert stored.external_id.startswith("sandbox-")

    live_connection, _secret = await create_connection(
        db,
        organization_id=admin.organization_id,
        actor_user_id=admin.id,
        provider="telegram",
        name="Vacancies live canonical",
        configuration={"chat_id": "@vacancies"},
        cipher=TEST_CIPHER,
    )
    with pytest.raises(HTTPException) as unavailable:
        await create_source_integration(
            SourceIntegrationCreate(
                provider="telegram",
                name="Vacancies live",
                connection_id=live_connection.id,
                configuration={"mode": "live"},
            ),
            db=db,
            user=admin,
        )
    assert unavailable.value.status_code == 409
    assert unavailable.value.detail["code"] == "connection_not_ready"


async def test_referral_attribution_analytics_limits_and_receipt_merge(db):
    organization, admin, position, second = await seed_tenant(db, "referral")
    created = await create_referral_link(
        position.id,
        ReferralLinkCreate(
            label="Employee ambassadors",
            campaign="q3-referrals",
            source="employee_referral",
            expires_at=datetime.now(UTC) + timedelta(days=7),
            max_applications=1,
        ),
        db=db,
        user=admin,
    )
    stored_link = await db.get(ReferralLink, created.link.id)
    assert stored_link is not None
    assert created.token not in stored_link.token_digest
    resolved = await resolve_public_referral(created.token, db=db)
    assert resolved.position_id == position.id
    assert "utm_source=employee_referral" in resolved.apply_url

    accepted = await public_apply(
        position.id,
        name="Referral Candidate",
        phone="+992 900 555 666",
        email="referral@example.test",
        source="spoofed-source",
        referral_token=created.token,
        file=None,
        raw_text="Candidate referred by an employee with Python platform experience.",
        db=db,
    )
    assert accepted.accepted is True
    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.organization_id == organization.id,
                Candidate.email_normalized == "referral@example.test",
            )
        )
    ).scalar_one()
    assert candidate.source == "public"
    assert candidate.parsed_cv["applied_via"] == "employee_referral"
    assert candidate.parsed_cv["utm_campaign"] == "q3-referrals"
    assert candidate.parsed_cv["referral_link_id"] == str(created.link.id)
    link = await db.get(ReferralLink, created.link.id)
    assert link is not None
    assert (link.click_count, link.application_count) == (1, 1)
    activity = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == candidate.id,
                CandidateActivity.event_type == "candidate_added",
            )
        )
    ).scalar_one()
    assert activity.payload == {
        "source": "public",
        "applied_via": "employee_referral",
        "position_title": position.title,
    }
    assert created.token not in repr(activity.payload)

    score = (
        await db.execute(select(Score).where(Score.candidate_id == candidate.id))
    ).scalar_one()
    score.stage = "hired"
    await db.commit()
    analytics = await source_analytics(db=db, user=admin)
    referral_item = next(
        item for item in analytics.items if item.source == "employee_referral"
    )
    assert (
        referral_item.applications,
        referral_item.hired,
        referral_item.conversion_rate,
    ) == (1, 1, 100.0)

    with pytest.raises(HTTPException) as exhausted:
        await public_apply(
            position.id,
            name="Second Referral Candidate",
            phone="+992 900 555 667",
            email=None,
            source=None,
            referral_token=created.token,
            file=None,
            raw_text="A second candidate after the referral limit is exhausted.",
            db=db,
        )
    assert exhausted.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_position:
        await public_apply(
            second.id,
            name="Wrong Position",
            phone="+992 900 555 668",
            email=None,
            source=None,
            referral_token=created.token,
            file=None,
            raw_text="A candidate attempting to use a link for another vacancy.",
            db=db,
        )
    assert wrong_position.value.status_code == 404

    _integration_response, integration = await create_integration(
        db, admin, "generic_webhook", "Merge receipt source"
    )
    duplicate = Candidate(
        organization_id=organization.id,
        source="generic_webhook",
        source_ref="duplicate-before-merge",
        raw_cv="Duplicate candidate evidence retained as alias history.",
    )
    db.add(duplicate)
    await db.flush()
    duplicate_link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=second.id,
        source=duplicate.source or "generic_webhook",
        owner_user_id=None,
        actor_user_id=admin.id,
        actor_kind="human",
        reason="regional_sourcing_receipt_merge_fixture",
    )
    receipt = SourceCandidateReceipt(
        organization_id=organization.id,
        integration_id=integration.id,
        candidate_id=duplicate.id,
        position_id=second.id,
        application_id=duplicate_link.application.id,
        external_id="merge-source-receipt",
        provider_snapshot="generic_webhook",
        received_at=datetime.now(UTC),
    )
    db.add(receipt)
    await db.commit()
    receipt_id = receipt.id
    await merge_candidates(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        primary_candidate_id=candidate.id,
        duplicate_candidate_ids=[duplicate.id],
        score_winners={},
    )
    await db.commit()
    merged_receipt = await db.get(SourceCandidateReceipt, receipt_id)
    assert merged_receipt is not None
    assert merged_receipt.candidate_id == candidate.id


async def test_source_analytics_uses_application_cycle_provenance(db):
    organization, admin, first, second = await seed_tenant(
        db, "application-provenance"
    )
    custom = await create_referral_link(
        second.id,
        ReferralLinkCreate(
            label="Campus partnership",
            source="campus_partner",
        ),
        db=db,
        user=admin,
    )
    assert custom.link.source == "campus_partner"

    for position, source, referral_token in (
        (first, "company_website", None),
        (second, "untrusted-spoof", custom.token),
    ):
        accepted = await public_apply(
            position.id,
            name="Same multi-position candidate",
            phone="+992 900 777 001",
            email="multi-position@example.test",
            source=source,
            referral_token=referral_token,
            file=None,
            raw_text="Experienced platform engineer with Python and PostgreSQL.",
            db=db,
        )
        assert accepted.accepted is True

    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.organization_id == organization.id,
                Candidate.email_normalized == "multi-position@example.test",
            )
        )
    ).scalar_one()
    applications = list(
        (
            await db.execute(
                select(Application)
                .where(
                    Application.organization_id == organization.id,
                    Application.candidate_id == candidate.id,
                )
                .order_by(Application.position_id, Application.cycle_number)
            )
        ).scalars()
    )
    assert {application.source for application in applications} == {
        "company_website",
        "campus_partner",
    }

    first_application = next(
        application for application in applications if application.position_id == first.id
    )
    first_application.stage = "rejected"
    first_application.closed_at = datetime.now(UTC)
    await db.commit()
    reapplied = await public_apply(
        first.id,
        name="Same multi-position candidate",
        phone="+992 900 777 001",
        email="multi-position@example.test",
        source="direct",
        referral_token=None,
        file=None,
        raw_text="Updated platform experience for a new hiring cycle.",
        db=db,
    )
    assert reapplied.accepted is True

    third = Position(
        organization_id=organization.id,
        title="Data Engineer application-provenance",
        description="Build data products.",
        status="open",
        created_by=admin.id,
    )
    duplicate = Candidate(
        organization_id=organization.id,
        source="agency",
        source_ref="opaque-agency-receipt",
        raw_cv="Duplicate identity with a distinct third-position application.",
    )
    db.add_all([third, duplicate])
    await db.flush()
    duplicate_score = Score(
        organization_id=organization.id,
        candidate_id=duplicate.id,
        position_id=third.id,
        stage="hired",
    )
    db.add(duplicate_score)
    await db.flush()
    await merge_candidates(
        db,
        organization_id=organization.id,
        actor_user_id=admin.id,
        primary_candidate_id=candidate.id,
        duplicate_candidate_ids=[duplicate.id],
        score_winners={},
    )
    await db.commit()

    analytics = await source_analytics(db=db, user=admin)
    by_source = {item.source: item for item in analytics.items}
    assert {
        source: (item.applications, item.hired)
        for source, item in by_source.items()
    } == {
        "agency": (1, 1),
        "campus_partner": (1, 0),
        "company_website": (1, 0),
        "direct": (1, 0),
    }
    assert analytics.total_applications == 4


async def test_referral_capability_defaults_rotation_revocation_and_semantics(db):
    _organization, admin, position, _second = await seed_tenant(
        db, "referral-capability"
    )
    headers = {
        "Authorization": "Bearer "
        + encode_access_token(
            admin.id,
            admin.role,
            admin.locale,
            token_version=admin.token_version,
        )
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/positions/{position.id}/referral-links",
            headers=headers,
            json={"label": "Bounded campaign", "source": "referral"},
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "private, no-store, max-age=0"
        created_payload = created.json()
        original_token = created_payload["token"]
        assert datetime.fromisoformat(
            created_payload["link"]["expires_at"]
        ) <= datetime.now(UTC) + timedelta(days=31)

        listed = await client.get(
            f"/positions/{position.id}/referral-links", headers=headers
        )
        assert listed.status_code == 200
        assert listed.json()["next_cursor"] is None
        assert "token" not in listed.json()["items"][0]

        public = await client.get(f"/public/referrals/{original_token}")
        assert public.status_code == 200
        assert public.headers["referrer-policy"] == "no-referrer"
        assert "noindex" in public.headers["x-robots-tag"]

        rotated = await client.post(
            f"/referral-links/{created_payload['link']['id']}/rotate",
            headers=headers,
            json={"expires_in_days": 7},
        )
        assert rotated.status_code == 200
        rotated_token = rotated.json()["token"]
        assert rotated_token != original_token
        assert (
            await client.get(f"/public/referrals/{original_token}")
        ).status_code == 404
        assert (
            await client.get(f"/public/referrals/{rotated_token}")
        ).status_code == 200

        revoked = await client.post(
            f"/referral-links/{created_payload['link']['id']}/revoke",
            headers=headers,
        )
        assert revoked.status_code == 200
        gone = await client.get(f"/public/referrals/{rotated_token}")
        assert gone.status_code == 410
        assert gone.headers["cache-control"] == "private, no-store, max-age=0"

        unbounded = await client.post(
            f"/positions/{position.id}/referral-links",
            headers=headers,
            json={
                "label": "Too long",
                "source": "referral",
                "expires_at": (datetime.now(UTC) + timedelta(days=91)).isoformat(),
            },
        )
        assert unbounded.status_code == 422
