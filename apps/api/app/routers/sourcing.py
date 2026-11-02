"""Regional sourcing connectors, imports, referrals, and source reporting."""

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import (
    bootstrap_resource,
    forced_tenant_rls,
    get_session,
    resolve_capability,
    set_tenant_context,
)
from app.models.application import Application
from app.models.candidate import Candidate
from app.models.integration import IntegrationConnection, IntegrationJob
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
from app.routers.integrations import (
    ProviderRuntime,
    _bounded_request_body,
    provider_runtime,
)
from app.schemas.integrations import WebhookEnvelope
from app.schemas.pagination import CursorPage
from app.schemas.sourcing import (
    PublicReferralOut,
    ReferralLinkCreate,
    ReferralLinkCreatedOut,
    ReferralLinkOut,
    ReferralLinkRotate,
    SourceAnalyticsItem,
    SourceAnalyticsOut,
    SourceCandidateInput,
    SourceHeadHunterImportRequest,
    SourceImportFilePreviewOut,
    SourceImportPreviewItem,
    SourceImportRequest,
    SourceImportRunOut,
    SourceImportRunPage,
    SourceIntegrationCreate,
    SourceIntegrationCreatedOut,
    SourceIntegrationOut,
    SourceIntegrationPage,
    SourceIntegrationUpdate,
    SourceJobOut,
    SourcePublicationOut,
    SourcePublicationPreviewRequest,
    SourceWebhookOut,
    SourceWebhookSecretOut,
)
from app.services.enterprise import require_permission
from app.services.entitlements import TECHNICAL_LIMITS
from app.services.integrations.jobs import IntegrationJobError
from app.services.integrations.sourcing import (
    MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES,
    MAX_DURABLE_LOCAL_IMPORT_ROWS,
    enqueue_source_provider_job,
)
from app.services.integrations.webhooks import (
    WEBHOOK_VERSION,
    WebhookError,
    verify_webhook_signature,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)
from app.services.public_positions import redact_position
from app.services.source_import_files import (
    IMPORT_FIELDS,
    SourceFileImportError,
    rows_from_csv,
    rows_from_values,
    rows_from_xlsx,
)
from app.services.sourcing import (
    SourceImportError,
    hash_source_webhook_secret,
    ingest_source_candidates,
    new_source_webhook_secret,
    source_capabilities,
    source_integration_status,
    validate_source_connection,
)

router = APIRouter(tags=["sourcing"])
settings = get_settings()
MAX_IMPORT_FILE_BYTES = TECHNICAL_LIMITS["sourcing_document_bytes"]
MAX_SYNCHRONOUS_IMPORT_ROWS = TECHNICAL_LIMITS["synchronous_import_rows"]
MAX_DURABLE_IMPORT_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_REFERRAL_LINK_DAYS = 30
MAX_REFERRAL_LINK_DAYS = 90
_PUBLIC_REFERRAL_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_ONE_TIME_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


def _protect_one_time_response(response: Response | None) -> None:
    if response is not None:
        response.headers.update(_ONE_TIME_RESPONSE_HEADERS)


def _integration_out(
    integration: SourceIntegration,
    connection: IntegrationConnection | None = None,
) -> SourceIntegrationOut:
    derived_status = source_integration_status(
        integration.provider,
        integration.configuration,
        is_active=integration.is_active,
        connection=connection,
    )
    return SourceIntegrationOut(
        id=integration.id,
        connection_id=integration.connection_id,
        connection_provider=connection.provider if connection is not None else None,
        connection_status=connection.status if connection is not None else None,
        provider=integration.provider,
        name=integration.name,
        configuration=integration.configuration or {},
        is_active=integration.is_active,
        status=derived_status,
        webhook_configured=bool(integration.webhook_secret_hash),
        capabilities=source_capabilities(
            integration.provider, integration.configuration, connection
        ),
        last_sync_at=integration.last_sync_at,
        created_at=integration.created_at,
        updated_at=integration.updated_at,
    )


def _run_out(run: SourceImportRun) -> SourceImportRunOut:
    return SourceImportRunOut(
        id=run.id,
        integration_id=run.integration_id,
        provider=run.provider_snapshot,
        idempotency_key=run.idempotency_key,
        status=run.status,
        requested_count=run.requested_count,
        created_count=run.created_count,
        duplicate_count=run.duplicate_count,
        failed_count=run.failed_count,
        error_code=run.error_code,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
    )


def _job_out(job: IntegrationJob) -> SourceJobOut:
    return SourceJobOut(
        id=job.id,
        connection_id=job.connection_id,
        operation=job.operation,
        status=job.status,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        failure_code=job.failure_code,
        result_summary=job.result_summary or {},
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _local_import_capability(provider: str) -> str:
    return {
        "csv": "file.import",
        "somon": "file.import",
        "generic_webhook": "webhook.receive",
        "hh": "candidate.import",
    }.get(provider, "candidate.import")


async def _enqueue_local_candidate_import(
    db: AsyncSession,
    *,
    integration: SourceIntegration,
    connection: IntegrationConnection | None,
    rows: list[SourceCandidateInput],
    actor_user_id: UUID,
    idempotency_key: str | None,
    runtime: ProviderRuntime,
) -> IntegrationJob:
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 160:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "idempotency_key_invalid"},
            )
    if connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "connection_not_ready"},
        )
    if runtime.cipher is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "credential_encryption_unavailable"},
        )
    if len(rows) > MAX_DURABLE_LOCAL_IMPORT_ROWS:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {
                "code": "limit_exceeded",
                "limit": MAX_DURABLE_LOCAL_IMPORT_ROWS,
                "requested": len(rows),
            },
        )
    payload_rows = [row.model_dump(mode="json") for row in rows]
    payload_bytes = json.dumps(
        payload_rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    if len(payload_bytes) > MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {
                "code": "limit_exceeded",
                "limit": MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES,
                "requested": len(payload_bytes),
                "unit": "bytes",
            },
        )
    stable_key = idempotency_key or (
        f"local-import:{hashlib.sha256(payload_bytes).hexdigest()}"
    )
    try:
        job, _created = await enqueue_source_provider_job(
            db,
            integration=integration,
            connection=connection,
            actor_user_id=actor_user_id,
            operation="candidate.import",
            idempotency_key=stable_key,
            references={
                "source_kind": "local_candidate_import",
                "source_integration_id": str(integration.id),
                "requested_count": len(rows),
            },
            sensitive_payload={"candidates": payload_rows},
            cipher=runtime.cipher,
            required_capability=_local_import_capability(integration.provider),
            max_attempts=5,
            max_payload_bytes=MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES,
        )
        await db.commit()
    except (IntegrationJobError, SourceImportError) as exc:
        await db.rollback()
        code = getattr(exc, "code", "source_import_enqueue_failed")
        if code == "job_payload_too_large":
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                {"code": "limit_exceeded"},
            ) from exc
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": code}) from exc
    await db.refresh(job)
    return job


async def _enqueue_local_candidate_file_import(
    db: AsyncSession,
    *,
    integration: SourceIntegration,
    connection: IntegrationConnection | None,
    content: bytes,
    format_name: str,
    mapping: dict[str, str],
    actor_user_id: UUID,
    idempotency_key: str | None,
    runtime: ProviderRuntime,
) -> IntegrationJob:
    """Queue encrypted raw bytes so large files are parsed only by a worker."""

    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 160:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "idempotency_key_invalid"},
            )
    if connection is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "connection_not_ready"},
        )
    if runtime.cipher is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "credential_encryption_unavailable"},
        )
    if format_name not in {"csv", "xlsx"}:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "This endpoint accepts UTF-8 CSV or XLSX",
        )
    sensitive_payload = {
        "format": format_name,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "mapping": mapping,
    }
    payload_bytes = json.dumps(
        sensitive_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    if len(payload_bytes) > MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {
                "code": "limit_exceeded",
                "limit": MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES,
                "requested": len(payload_bytes),
                "unit": "bytes",
            },
        )
    content_sha256 = hashlib.sha256(content).hexdigest()
    stable_key = idempotency_key or (
        "local-file-import:"
        + hashlib.sha256(
            content
            + format_name.encode()
            + json.dumps(mapping, sort_keys=True).encode()
        ).hexdigest()
    )
    try:
        job, _created = await enqueue_source_provider_job(
            db,
            integration=integration,
            connection=connection,
            actor_user_id=actor_user_id,
            operation="candidate.import",
            idempotency_key=stable_key,
            references={
                "source_kind": "local_candidate_file_import",
                "source_integration_id": str(integration.id),
                "format": format_name,
                "content_bytes": len(content),
                "content_sha256": content_sha256,
            },
            sensitive_payload=sensitive_payload,
            cipher=runtime.cipher,
            required_capability=_local_import_capability(integration.provider),
            max_attempts=5,
            max_payload_bytes=MAX_DURABLE_LOCAL_IMPORT_PAYLOAD_BYTES,
        )
        await db.commit()
    except (IntegrationJobError, SourceImportError) as exc:
        await db.rollback()
        code = getattr(exc, "code", "source_import_enqueue_failed")
        if code == "job_payload_too_large":
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                {"code": "limit_exceeded"},
            ) from exc
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": code}) from exc
    await db.refresh(job)
    return job


def _encode_cursor(
    created_at: datetime,
    row_id: UUID,
    *,
    namespace: str,
    scope: str,
) -> str:
    return encode_collection_cursor(
        namespace,
        scope=scope,
        boundary={"created_at": created_at.isoformat(), "id": str(row_id)},
    )


def _decode_cursor(
    value: str | None,
    *,
    namespace: str,
    scope: str,
) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    payload = decode_collection_cursor(
        value,
        namespace=namespace,
        scope=scope,
        boundary_keys={"created_at", "id"},
        detail="Invalid sourcing cursor",
    )
    return (
        cursor_datetime(payload, "created_at", detail="Invalid sourcing cursor"),
        cursor_uuid(payload, detail="Invalid sourcing cursor"),
    )


async def _connection_for_integration(
    db: AsyncSession,
    integration: SourceIntegration,
) -> IntegrationConnection | None:
    if integration.connection_id is None:
        return None
    return (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.id == integration.connection_id,
                IntegrationConnection.organization_id == integration.organization_id,
            )
        )
    ).scalar_one_or_none()


async def _source_connection(
    db: AsyncSession,
    *,
    connection_id: UUID,
    organization_id: UUID,
    provider: str,
) -> IntegrationConnection:
    connection = (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.id == connection_id,
                IntegrationConnection.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "connection_not_found"})
    try:
        validate_source_connection(
            provider, connection, organization_id=organization_id
        )
    except SourceImportError as exc:
        status_code = (
            status.HTTP_409_CONFLICT
            if exc.code == "connection_not_ready"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code, {"code": exc.code}) from exc
    return connection


async def _integration(
    db: AsyncSession,
    integration_id: UUID,
    organization_id: UUID,
    *,
    active: bool = False,
) -> SourceIntegration:
    stmt = select(SourceIntegration).where(
        SourceIntegration.id == integration_id,
        SourceIntegration.organization_id == organization_id,
    )
    if active:
        stmt = stmt.where(SourceIntegration.is_active.is_(True))
    integration = (await db.execute(stmt)).scalar_one_or_none()
    if integration is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source integration not found")
    return integration


@router.get("/source-integrations", response_model=SourceIntegrationPage)
async def list_source_integrations(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SourceIntegrationPage:
    await require_permission(db, user, "integrations.read")
    statement = (
        select(SourceIntegration, IntegrationConnection)
        .outerjoin(
            IntegrationConnection,
            and_(
                IntegrationConnection.id == SourceIntegration.connection_id,
                IntegrationConnection.organization_id
                == SourceIntegration.organization_id,
            ),
        )
        .where(SourceIntegration.organization_id == user.organization_id)
    )
    namespace = "sourcing.integrations"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        statement = statement.where(
            or_(
                SourceIntegration.created_at < created_at,
                and_(
                    SourceIntegration.created_at == created_at,
                    SourceIntegration.id < row_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    SourceIntegration.created_at.desc(),
                    SourceIntegration.id.desc(),
                ).limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    items = rows[:limit]
    return SourceIntegrationPage(
        items=[
            _integration_out(integration, connection)
            for integration, connection in items
        ],
        next_cursor=(
            _encode_cursor(
                items[-1][0].created_at,
                items[-1][0].id,
                namespace=namespace,
                scope=scope,
            )
            if more and items
            else None
        ),
    )


@router.post(
    "/source-integrations",
    response_model=SourceIntegrationCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_source_integration(
    body: SourceIntegrationCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    response: Response = None,
) -> SourceIntegrationCreatedOut:
    await require_permission(db, user, "integrations.manage")
    connection: IntegrationConnection | None = None
    if body.connection_id is not None:
        connection = await _source_connection(
            db,
            connection_id=body.connection_id,
            organization_id=user.organization_id,
            provider=body.provider,
        )
    secret: str | None = None
    digest: str | None = None
    if body.provider == "generic_webhook":
        secret, digest = new_source_webhook_secret()
    integration = SourceIntegration(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        connection_id=connection.id if connection is not None else None,
        provider=body.provider,
        name=body.name,
        configuration=body.configuration,
        webhook_secret_hash=digest,
        status=source_integration_status(
            body.provider, body.configuration, connection=connection
        ),
    )
    db.add(integration)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A source integration with this name already exists",
        ) from exc
    await db.refresh(integration)
    _protect_one_time_response(response)
    return SourceIntegrationCreatedOut(
        integration=_integration_out(integration, connection), webhook_secret=secret
    )


@router.patch(
    "/source-integrations/{integration_id}", response_model=SourceIntegrationOut
)
async def update_source_integration(
    integration_id: UUID,
    body: SourceIntegrationUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SourceIntegrationOut:
    await require_permission(db, user, "integrations.manage")
    integration = await _integration(db, integration_id, user.organization_id)
    changes = body.model_dump(exclude_unset=True)
    proposed_configuration = changes.get(
        "configuration", integration.configuration or {}
    )
    proposed_connection_id = (
        changes["connection_id"]
        if "connection_id" in changes
        else integration.connection_id
    )
    sandbox = proposed_configuration.get("mode") == "sandbox"
    if sandbox and integration.provider not in {"hh", "telegram"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sandbox_not_supported"},
        )
    if sandbox and proposed_connection_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sandbox_connection_conflict"},
        )
    if not sandbox and proposed_connection_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "connection_required"},
        )
    connection: IntegrationConnection | None = None
    if proposed_connection_id is not None:
        connection = await _source_connection(
            db,
            connection_id=proposed_connection_id,
            organization_id=user.organization_id,
            provider=integration.provider,
        )
    for field, value in changes.items():
        setattr(integration, field, value)
    integration.status = source_integration_status(
        integration.provider,
        integration.configuration,
        is_active=integration.is_active,
        connection=connection,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A source integration with this name already exists",
        ) from exc
    await db.refresh(integration)
    return _integration_out(integration, connection)


@router.post(
    "/source-integrations/{integration_id}/rotate-webhook-secret",
    response_model=SourceWebhookSecretOut,
)
async def rotate_source_webhook_secret(
    integration_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    response: Response = None,
) -> SourceWebhookSecretOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "integrations.credentials")
    integration = await _integration(db, integration_id, user.organization_id)
    if integration.provider != "generic_webhook":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Webhook secrets are only available for generic webhook integrations",
        )
    secret, digest = new_source_webhook_secret()
    integration.webhook_secret_hash = digest
    await db.commit()
    _protect_one_time_response(response)
    return SourceWebhookSecretOut(integration_id=integration.id, webhook_secret=secret)


@router.get("/source-integrations/runs", response_model=SourceImportRunPage)
async def list_source_import_runs(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SourceImportRunPage:
    await require_permission(db, user, "integrations.read")
    statement = select(SourceImportRun).where(
        SourceImportRun.organization_id == user.organization_id
    )
    namespace = "sourcing.import-runs"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        statement = statement.where(
            or_(
                SourceImportRun.created_at < created_at,
                and_(
                    SourceImportRun.created_at == created_at,
                    SourceImportRun.id < row_id,
                ),
            )
        )
    runs = list(
        (
            await db.execute(
                statement.order_by(
                    SourceImportRun.created_at.desc(), SourceImportRun.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(runs) > limit
    items = runs[:limit]
    return SourceImportRunPage(
        items=[_run_out(run) for run in items],
        next_cursor=(
            _encode_cursor(
                items[-1].created_at,
                items[-1].id,
                namespace=namespace,
                scope=scope,
            )
            if more and items
            else None
        ),
    )


@router.post(
    "/source-integrations/{integration_id}/import",
    response_model=SourceImportRunOut | SourceJobOut,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": SourceJobOut,
            "description": "Oversized import accepted by the durable worker queue.",
        }
    },
)
async def import_source_candidates(
    integration_id: UUID,
    body: SourceImportRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    runtime: ProviderRuntime = Depends(provider_runtime),
    response: Response = None,
) -> SourceImportRunOut | SourceJobOut:
    await require_permission(db, user, "sourcing.manage")
    integration = await _integration(
        db, integration_id, user.organization_id, active=True
    )
    connection = await _connection_for_integration(db, integration)
    capability = source_capabilities(
        integration.provider, integration.configuration, connection
    ).candidate_import
    if capability not in {"available", "sandbox"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Candidate import is {capability} for this integration",
        )
    if len(body.candidates) > MAX_SYNCHRONOUS_IMPORT_ROWS:
        job = await _enqueue_local_candidate_import(
            db,
            integration=integration,
            connection=connection,
            rows=body.candidates,
            actor_user_id=user.id,
            idempotency_key=idempotency_key,
            runtime=runtime,
        )
        if response is not None:
            response.status_code = status.HTTP_202_ACCEPTED
        return _job_out(job)
    try:
        run = await ingest_source_candidates(
            db,
            integration=integration,
            items=body.candidates,
            actor_user_id=user.id,
            idempotency_key=idempotency_key,
        )
    except SourceImportError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": exc.code}) from exc
    return _run_out(run)


_IMPORT_FIELDS = set(IMPORT_FIELDS)


def _mapping_from_json(value: str | None) -> dict[str, str]:
    if value is None:
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "mapping must be valid JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "mapping must be an object"
        )
    mapping = {
        str(key).strip().lower(): str(source).strip().lower()
        for key, source in raw.items()
    }
    if set(mapping) - _IMPORT_FIELDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "mapping contains unsupported target fields",
        )
    if any(not source for source in mapping.values()) or len(
        set(mapping.values())
    ) != len(mapping):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "mapping source headers must be non-empty and unique",
        )
    return mapping


def _rows_from_values(
    headers: list[object],
    rows: list[tuple[object, ...]] | object,
    mapping: dict[str, str] | None = None,
    *,
    max_rows: int = MAX_DURABLE_LOCAL_IMPORT_ROWS,
) -> list[SourceCandidateInput]:
    try:
        return rows_from_values(
            headers,
            rows,
            mapping,
            max_rows=max_rows,
        )
    except SourceFileImportError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _rows_from_csv(
    content: bytes, mapping: dict[str, str] | None = None
) -> list[SourceCandidateInput]:
    try:
        return rows_from_csv(
            content,
            mapping,
            max_rows=MAX_DURABLE_LOCAL_IMPORT_ROWS,
        )
    except SourceFileImportError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


async def _rows_from_xlsx(
    content: bytes, mapping: dict[str, str] | None = None
) -> list[SourceCandidateInput]:
    try:
        return await rows_from_xlsx(
            content,
            mapping,
            max_rows=MAX_DURABLE_LOCAL_IMPORT_ROWS,
        )
    except SourceFileImportError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post(
    "/source-integrations/{integration_id}/import-file",
    response_model=SourceImportRunOut | SourceJobOut,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": SourceJobOut,
            "description": "Oversized file import accepted by the durable worker queue.",
        }
    },
)
async def import_source_file(
    integration_id: UUID,
    file: Annotated[UploadFile, File(...)],
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    mapping: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    runtime: ProviderRuntime = Depends(provider_runtime),
    response: Response = None,
) -> SourceImportRunOut | SourceJobOut:
    await require_permission(db, user, "sourcing.manage")
    integration = await _integration(
        db, integration_id, user.organization_id, active=True
    )
    connection = await _connection_for_integration(db, integration)
    capability = source_capabilities(
        integration.provider, integration.configuration, connection
    ).file_import
    if capability != "available":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "File import is unsupported for this integration",
        )
    content = await file.read(MAX_DURABLE_IMPORT_FILE_BYTES + 1)
    if len(content) > MAX_DURABLE_IMPORT_FILE_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {
                "code": "limit_exceeded",
                "limit": MAX_DURABLE_IMPORT_FILE_BYTES,
                "requested": len(content),
                "unit": "bytes",
            },
        )
    filename = (file.filename or "").lower()
    parsed_mapping = _mapping_from_json(mapping)
    if filename.endswith(".csv"):
        format_name = "csv"
    elif filename.endswith(".xlsx"):
        format_name = "xlsx"
    else:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "This endpoint accepts UTF-8 CSV or XLSX",
        )
    if len(content) > MAX_IMPORT_FILE_BYTES:
        job = await _enqueue_local_candidate_file_import(
            db,
            integration=integration,
            connection=connection,
            content=content,
            format_name=format_name,
            mapping=parsed_mapping,
            actor_user_id=user.id,
            idempotency_key=idempotency_key,
            runtime=runtime,
        )
        if response is not None:
            response.status_code = status.HTTP_202_ACCEPTED
        return _job_out(job)
    rows = (
        _rows_from_csv(content, parsed_mapping)
        if format_name == "csv"
        else await _rows_from_xlsx(content, parsed_mapping)
    )
    if len(rows) > MAX_SYNCHRONOUS_IMPORT_ROWS:
        job = await _enqueue_local_candidate_import(
            db,
            integration=integration,
            connection=connection,
            rows=rows,
            actor_user_id=user.id,
            idempotency_key=idempotency_key,
            runtime=runtime,
        )
        if response is not None:
            response.status_code = status.HTTP_202_ACCEPTED
        return _job_out(job)
    try:
        run = await ingest_source_candidates(
            db,
            integration=integration,
            items=rows,
            actor_user_id=user.id,
            idempotency_key=idempotency_key,
        )
    except SourceImportError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": exc.code}) from exc
    return _run_out(run)


def _masked_phone(value: str | None) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    return f"***{digits[-4:]}" if digits else "***"


def _masked_email(value: str | None) -> str | None:
    if value is None:
        return None
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


@router.post(
    "/source-integrations/{integration_id}/import-file/preview",
    response_model=SourceImportFilePreviewOut,
)
async def preview_source_file(
    integration_id: UUID,
    file: Annotated[UploadFile, File(...)],
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    mapping: Annotated[str | None, Form()] = None,
) -> SourceImportFilePreviewOut:
    await require_permission(db, user, "sourcing.manage")
    integration = await _integration(
        db, integration_id, user.organization_id, active=True
    )
    connection = await _connection_for_integration(db, integration)
    if (
        source_capabilities(
            integration.provider, integration.configuration, connection
        ).file_import
        != "available"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "File import is unsupported for this integration",
        )
    content = await file.read(MAX_DURABLE_IMPORT_FILE_BYTES + 1)
    if len(content) > MAX_DURABLE_IMPORT_FILE_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            {
                "code": "limit_exceeded",
                "limit": MAX_DURABLE_IMPORT_FILE_BYTES,
                "requested": len(content),
                "unit": "bytes",
            },
        )
    parsed_mapping = _mapping_from_json(mapping)
    filename = (file.filename or "").lower()
    if filename.endswith(".csv"):
        format_name = "csv"
        rows = _rows_from_csv(content, parsed_mapping)
    elif filename.endswith(".xlsx"):
        format_name = "xlsx"
        rows = await _rows_from_xlsx(content, parsed_mapping)
    else:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "This endpoint accepts UTF-8 CSV or XLSX",
        )
    return SourceImportFilePreviewOut(
        format=format_name,
        row_count=len(rows),
        mapping={
            field: parsed_mapping.get(field, field) for field in sorted(_IMPORT_FIELDS)
        },
        items=[
            SourceImportPreviewItem(
                row=index,
                external_id=item.external_id,
                position_id=item.position_id,
                name=item.name,
                masked_phone=_masked_phone(item.phone),
                masked_email=_masked_email(str(item.email) if item.email else None),
                cv_characters=len(item.cv_text),
                source_url=item.source_url,
            )
            for index, item in enumerate(rows[:20], start=2)
        ],
        truncated=len(rows) > 20,
        import_mode=(
            "durable_job"
            if len(content) > MAX_IMPORT_FILE_BYTES
            or len(rows) > MAX_SYNCHRONOUS_IMPORT_ROWS
            else "synchronous"
        ),
        synchronous_row_limit=MAX_SYNCHRONOUS_IMPORT_ROWS,
        synchronous_file_bytes=MAX_IMPORT_FILE_BYTES,
        absolute_row_limit=MAX_DURABLE_LOCAL_IMPORT_ROWS,
        absolute_file_bytes=MAX_DURABLE_IMPORT_FILE_BYTES,
    )


@router.post(
    "/source-integrations/{integration_id}/hh/imports",
    response_model=SourceJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_hh_candidate_import(
    integration_id: UUID,
    body: SourceHeadHunterImportRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> SourceJobOut:
    await require_permission(db, user, "sourcing.manage")
    integration = await _integration(
        db, integration_id, user.organization_id, active=True
    )
    if (
        integration.provider != "hh"
        or (integration.configuration or {}).get("mode") == "sandbox"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "hh_live_connection_required"},
        )
    position = (
        await db.execute(
            select(Position.id).where(
                Position.id == body.position_id,
                Position.organization_id == user.organization_id,
                Position.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "position_not_found"})
    connection = await _connection_for_integration(db, integration)
    if connection is None or runtime.cipher is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {
                "code": (
                    "connection_not_ready"
                    if connection is None
                    else "credential_encryption_unavailable"
                )
            },
        )
    idempotency_key = body.idempotency_key or (
        f"hh-sync:{integration.id}:{body.position_id}:{body.vacancy_id}:"
        f"{int(datetime.now(UTC).timestamp()) // 300}"
    )
    try:
        job, _created = await enqueue_source_provider_job(
            db,
            integration=integration,
            connection=connection,
            actor_user_id=user.id,
            operation="candidate.import",
            idempotency_key=idempotency_key,
            references={
                "source_kind": "hh_candidate_import",
                "source_integration_id": str(integration.id),
                "position_id": str(body.position_id),
            },
            sensitive_payload={
                "vacancy_id": body.vacancy_id,
                "collection_ids": body.collection_ids,
                "max_pages": body.max_pages,
                "max_records": body.max_records,
                "resume_from_cursor": body.resume_from_cursor,
            },
            cipher=runtime.cipher,
            required_capability="candidate.import",
            max_attempts=5,
        )
        await db.commit()
    except (IntegrationJobError, SourceImportError) as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": getattr(exc, "code", "hh_import_enqueue_failed")},
        ) from exc
    await db.refresh(job)
    return _job_out(job)


@router.post(
    "/source-webhooks/{integration_id}",
    response_model=SourceWebhookOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": WebhookEnvelope.model_json_schema(),
                }
            },
        }
    },
)
async def receive_source_webhook(
    integration_id: UUID,
    request: Request,
    webhook_secret: Annotated[
        str | None, Header(alias="X-Webhook-Secret")
    ] = None,
    webhook_timestamp: Annotated[
        str | None, Header(alias="X-HR-Webhook-Timestamp")
    ] = None,
    webhook_signature: Annotated[
        str | None, Header(alias="X-HR-Webhook-Signature")
    ] = None,
    db: AsyncSession = Depends(get_session),
) -> SourceWebhookOut:
    # Bound the raw stream before JSON parsing or tenant/database work. This
    # keeps unauthenticated malformed and chunked floods below the documented
    # 256 KiB webhook contract.
    raw_body = await _bounded_request_body(request, 256_000)
    if forced_tenant_rls():
        bootstrap = await bootstrap_resource(
            db, kind="source_integration", resource_id=integration_id
        )
        if bootstrap is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    integration = (
        await db.execute(
            select(SourceIntegration).where(
                SourceIntegration.id == integration_id,
                SourceIntegration.is_active.is_(True),
                SourceIntegration.provider == "generic_webhook",
            )
        )
    ).scalar_one_or_none()
    connection = (
        await _connection_for_integration(db, integration)
        if integration is not None
        else None
    )
    expected_digest = integration.webhook_secret_hash if integration is not None else ""
    if (
        integration is None
        or source_capabilities(
            integration.provider, integration.configuration, connection
        ).webhook_intake
        != "available"
        or not expected_digest
        or not webhook_secret
        or not webhook_timestamp
        or not webhook_signature
        or not hmac.compare_digest(
            expected_digest,
            hash_source_webhook_secret(webhook_secret),
        )
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    try:
        verify_webhook_signature(
            webhook_secret,
            timestamp=webhook_timestamp,
            signature=webhook_signature,
            body=raw_body,
        )
    except WebhookError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found") from exc
    try:
        body = WebhookEnvelope.model_validate_json(raw_body)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_envelope_invalid"},
        ) from exc
    now = datetime.now(UTC)
    if body.version != WEBHOOK_VERSION:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_version_unsupported"},
        )
    if body.type != "source.candidate.created":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_event_type_unsupported"},
        )
    if body.created_at > now + timedelta(minutes=5) or body.created_at < now - timedelta(
        days=30
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_event_time_invalid"},
        )
    try:
        candidate = SourceCandidateInput.model_validate(body.data)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_candidate_invalid"},
        ) from exc
    payload_fingerprint = hashlib.sha256(raw_body).hexdigest()
    try:
        run = await ingest_source_candidates(
            db,
            integration=integration,
            items=[candidate],
            actor_user_id=None,
            idempotency_key=f"webhook:{body.id}",
            request_fingerprint_override=payload_fingerprint,
        )
    except SourceImportError as exc:
        code = (
            "webhook_event_conflict"
            if exc.code == "idempotency_conflict"
            else exc.code
        )
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": code}) from exc
    receipt_candidate_id = (
        await db.execute(
            select(SourceCandidateReceipt.candidate_id).where(
                SourceCandidateReceipt.integration_id == integration.id,
                SourceCandidateReceipt.external_id == candidate.external_id,
                SourceCandidateReceipt.organization_id == integration.organization_id,
            )
        )
    ).scalar_one_or_none()
    if receipt_candidate_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Candidate could not be imported",
        )
    return SourceWebhookOut(
        event_id=body.id,
        replay=bool(getattr(run, "_idempotent_replay", False)),
        candidate_id=receipt_candidate_id,
        created=run.created_count == 1
        and not bool(getattr(run, "_idempotent_replay", False)),
        run=_run_out(run),
    )


def _referral_out(link: ReferralLink, *, url: str | None = None) -> ReferralLinkOut:
    return ReferralLinkOut(
        id=link.id,
        position_id=link.position_id,
        label=link.label,
        campaign=link.campaign,
        source=link.source,
        is_active=link.is_active,
        expires_at=link.expires_at,
        max_applications=link.max_applications,
        click_count=link.click_count,
        application_count=link.application_count,
        url=url,
        created_at=link.created_at,
        updated_at=link.updated_at,
    )


def _bounded_referral_expiry(requested: datetime | None, *, now: datetime) -> datetime:
    expires_at = requested or now + timedelta(days=DEFAULT_REFERRAL_LINK_DAYS)
    if expires_at <= now:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Referral link expiration must be in the future",
        )
    if expires_at > now + timedelta(days=MAX_REFERRAL_LINK_DAYS):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Referral link expiration cannot exceed {MAX_REFERRAL_LINK_DAYS} days",
        )
    return expires_at


def _public_referral_error(
    status_code: int, detail: str, *, code: str = "referral_not_found"
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "detail": detail},
        headers=_PUBLIC_REFERRAL_HEADERS,
    )


@router.get(
    "/positions/{position_id}/referral-links",
    response_model=CursorPage[ReferralLinkOut],
)
async def list_referral_links(
    position_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[ReferralLinkOut]:
    await require_permission(db, user, "sourcing.read")
    position = (
        await db.execute(
            select(Position.id).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    namespace = "sourcing.referral-links"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"position_id": str(position_id)},
    )
    stmt = select(ReferralLink).where(
        ReferralLink.organization_id == user.organization_id,
        ReferralLink.position_id == position_id,
    )
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        stmt = stmt.where(
            or_(
                ReferralLink.created_at < created_at,
                and_(ReferralLink.created_at == created_at, ReferralLink.id < row_id),
            )
        )
    links = list(
        (
            await db.execute(
                stmt.order_by(
                    ReferralLink.created_at.desc(), ReferralLink.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(links) > limit
    links = links[:limit]
    return CursorPage(
        items=[_referral_out(link) for link in links],
        next_cursor=(
            _encode_cursor(
                links[-1].created_at,
                links[-1].id,
                namespace=namespace,
                scope=scope,
            )
            if has_more and links
            else None
        ),
    )


@router.post(
    "/positions/{position_id}/referral-links",
    response_model=ReferralLinkCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_referral_link(
    position_id: UUID,
    body: ReferralLinkCreate,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReferralLinkCreatedOut:
    await require_permission(db, user, "sourcing.manage")
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    now = datetime.now(UTC)
    expires_at = _bounded_referral_expiry(body.expires_at, now=now)
    token = secrets.token_urlsafe(32)
    values = body.model_dump(exclude={"expires_at"})
    link = ReferralLink(
        organization_id=user.organization_id,
        position_id=position.id,
        created_by_user_id=user.id,
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=expires_at,
        **values,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    if response is not None:
        response.headers.update(_PUBLIC_REFERRAL_HEADERS)
    url = f"{settings.web_origin.rstrip('/')}/r/{token}"
    return ReferralLinkCreatedOut(link=_referral_out(link, url=url), token=token)


@router.post("/referral-links/{link_id}/revoke", response_model=ReferralLinkOut)
async def revoke_referral_link(
    link_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReferralLinkOut:
    await require_permission(db, user, "sourcing.manage")
    link = (
        await db.execute(
            select(ReferralLink).where(
                ReferralLink.id == link_id,
                ReferralLink.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral link not found")
    if link.is_active:
        link.is_active = False
        await db.commit()
        await db.refresh(link)
    return _referral_out(link)


@router.post(
    "/referral-links/{link_id}/rotate",
    response_model=ReferralLinkCreatedOut,
)
async def rotate_referral_link(
    link_id: UUID,
    body: ReferralLinkRotate,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> ReferralLinkCreatedOut:
    await require_permission(db, user, "sourcing.manage")
    link = (
        await db.execute(
            select(ReferralLink)
            .where(
                ReferralLink.id == link_id,
                ReferralLink.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Referral link not found")
    if not link.is_active:
        raise HTTPException(status.HTTP_409_CONFLICT, "Referral link is revoked")
    token = secrets.token_urlsafe(32)
    link.token_digest = hashlib.sha256(token.encode()).hexdigest()
    link.expires_at = datetime.now(UTC) + timedelta(days=body.expires_in_days)
    await db.commit()
    await db.refresh(link)
    response.headers.update(_PUBLIC_REFERRAL_HEADERS)
    url = f"{settings.web_origin.rstrip('/')}/r/{token}"
    return ReferralLinkCreatedOut(link=_referral_out(link, url=url), token=token)


@router.get("/public/referrals/{token}", response_model=PublicReferralOut)
async def resolve_public_referral(
    token: str,
    response: Response = None,
    db: AsyncSession = Depends(get_session),
) -> PublicReferralOut:
    if response is not None:
        response.headers.update(_PUBLIC_REFERRAL_HEADERS)
    if len(token) < 32 or len(token) > 128:
        raise _public_referral_error(
            status.HTTP_404_NOT_FOUND, "Referral link not found"
        )
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(UTC)
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_capability(db, kind="referral", digest=digest)
        if bootstrap is None:
            raise _public_referral_error(
                status.HTTP_404_NOT_FOUND, "Referral link not found"
            )
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    link_stmt = (
        select(ReferralLink)
        .where(ReferralLink.token_digest == digest)
        .with_for_update()
    )
    if bootstrap is not None:
        link_stmt = link_stmt.where(
            ReferralLink.id == bootstrap.capability_id,
            ReferralLink.organization_id == bootstrap.organization_id,
        )
    link = (await db.execute(link_stmt)).scalar_one_or_none()
    if link is None:
        raise _public_referral_error(
            status.HTTP_404_NOT_FOUND, "Referral link not found"
        )
    if not link.is_active or link.position_id is None:
        raise _public_referral_error(
            status.HTTP_410_GONE,
            "Referral link has been revoked",
            code="referral_revoked",
        )
    if link.expires_at <= now:
        raise _public_referral_error(
            status.HTTP_410_GONE,
            "Referral link has expired",
            code="referral_expired",
        )
    if (
        link.max_applications is not None
        and link.application_count >= link.max_applications
    ):
        raise _public_referral_error(
            status.HTTP_410_GONE,
            "Referral link has reached its usage limit",
            code="referral_consumed",
        )
    link.click_count += 1
    await db.commit()
    query = urlencode(
        {
            "ref": token,
            "utm_source": link.source,
            "utm_campaign": link.campaign or link.label,
        }
    )
    return PublicReferralOut(
        position_id=link.position_id,
        apply_url=f"{settings.web_origin.rstrip('/')}/apply/{link.position_id}?{query}",
        source=link.source,
        campaign=link.campaign,
    )


async def _publication_context(
    db: AsyncSession,
    integration_id: UUID,
    position_id: UUID,
    organization_id: UUID,
) -> tuple[SourceIntegration, IntegrationConnection | None, Position, str, str]:
    integration = await _integration(db, integration_id, organization_id, active=True)
    if integration.provider not in {"telegram", "somon", "hh"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This integration cannot create a vacancy publication or tracked link",
        )
    position = (
        await db.execute(
            select(Position).where(
                Position.id == position_id,
                Position.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    if position.status != "open" or not position.public_apply_enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "position_not_publishable"}
        )
    query = urlencode(
        {
            "utm_source": integration.provider,
            "utm_campaign": str(integration.id),
        }
    )
    tracking_url = f"{settings.web_origin.rstrip('/')}/apply/{position.id}?{query}"
    # A Telegram post is a broadcast to an audience the recruiter does not
    # control, so it gets the same projection as any other public surface —
    # never the raw internal description. On a confidential role that means
    # the vetted public_description, or no body text at all if none exists.
    description = (redact_position(position).description or "").strip()
    prefix = f"{position.title}\n\n"
    suffix = f"\n\n{tracking_url}"
    available = max(0, 4096 - len(prefix) - len(suffix))
    if len(description) > available:
        description = description[: max(0, available - 1)].rstrip() + "…"
    post_text = f"{prefix}{description}{suffix}".strip()
    connection = await _connection_for_integration(db, integration)
    return integration, connection, position, tracking_url, post_text


def _hh_publication_payload(
    integration: SourceIntegration,
    position: Position,
) -> dict[str, object]:
    configuration = integration.configuration or {}
    area_id = str(configuration.get("area_id") or "").strip()
    if not area_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "hh_area_required"},
        )
    public_description = (redact_position(position).description or "").strip()
    if not public_description:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "public_description_required"},
        )
    paragraphs = [
        f"<p>{html.escape(part.strip())}</p>"
        for part in public_description.split("\n")
        if part.strip()
    ]
    return {
        "name": position.title[:255],
        "description": "".join(paragraphs),
        "area": {"id": area_id},
        "employment": {"id": str(configuration.get("employment_id") or "full")},
        "experience": {"id": str(configuration.get("experience_id") or "noExperience")},
        "schedule": {"id": str(configuration.get("schedule_id") or "fullDay")},
        "type": {"id": str(configuration.get("type_id") or "open")},
        "response_letter_required": False,
    }


async def _publication_job(
    db: AsyncSession,
    publication: SourcePublication,
) -> IntegrationJob | None:
    return (
        await db.execute(
            select(IntegrationJob)
            .where(
                IntegrationJob.organization_id == publication.organization_id,
                IntegrationJob.operation == "vacancy.publish",
                IntegrationJob.reference_payload["publication_id"].astext
                == str(publication.id),
            )
            .order_by(IntegrationJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _publication_out(
    publication: SourcePublication,
    job: IntegrationJob | None = None,
) -> SourcePublicationOut:
    return SourcePublicationOut(
        id=publication.id,
        integration_id=publication.integration_id,
        position_id=publication.position_id,
        provider=publication.provider_snapshot,
        status=publication.status,
        tracking_url=publication.tracking_url,
        post_text=publication.post_text,
        failure_code=publication.failure_code,
        external_id=publication.external_id,
        job_id=job.id if job is not None else None,
        job_status=job.status if job is not None else None,
        job_attempt_count=job.attempt_count if job is not None else None,
        job_max_attempts=job.max_attempts if job is not None else None,
        published_at=publication.published_at,
        created_at=publication.created_at,
    )


@router.post(
    "/source-integrations/{integration_id}/publications/preview",
    response_model=SourcePublicationOut,
)
async def preview_source_publication(
    integration_id: UUID,
    body: SourcePublicationPreviewRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SourcePublicationOut:
    await require_permission(db, user, "sourcing.publish")
    (
        integration,
        _connection,
        position,
        tracking_url,
        post_text,
    ) = await _publication_context(
        db, integration_id, body.position_id, user.organization_id
    )
    return SourcePublicationOut(
        integration_id=integration.id,
        position_id=position.id,
        provider=integration.provider,
        status="preview",
        tracking_url=tracking_url,
        post_text=post_text,
    )


@router.post(
    "/source-integrations/{integration_id}/publications",
    response_model=SourcePublicationOut,
)
async def publish_source_publication(
    integration_id: UUID,
    body: SourcePublicationPreviewRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> SourcePublicationOut:
    await require_permission(db, user, "sourcing.publish")
    (
        integration,
        connection,
        position,
        tracking_url,
        post_text,
    ) = await _publication_context(
        db, integration_id, body.position_id, user.organization_id
    )
    sandbox = (integration.configuration or {}).get("mode") == "sandbox"
    now = datetime.now(UTC)
    idempotency_key = body.idempotency_key or (
        f"publication:{integration.provider}:{position.id}:{body.locale}"
    )
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"source-publication:{integration.id}:{idempotency_key}", 0
                )
            )
        )
    )
    existing = (
        await db.execute(
            select(SourcePublication).where(
                SourcePublication.organization_id == user.organization_id,
                SourcePublication.integration_id == integration.id,
                SourcePublication.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.position_id != position.id or existing.post_text != post_text:
            raise HTTPException(
                status.HTTP_409_CONFLICT, {"code": "idempotency_conflict"}
            )
        return _publication_out(existing, await _publication_job(db, existing))
    if integration.provider == "somon":
        publication_status = "tracked"
        external_id = (
            f"tracked-{hashlib.sha256(tracking_url.encode()).hexdigest()[:16]}"
        )
        failure_code = None
        published_at = now
    elif sandbox:
        publication_status = "published_sandbox"
        external_id = f"sandbox-{secrets.token_hex(8)}"
        failure_code = None
        published_at = now
    else:
        capabilities = source_capabilities(
            integration.provider, integration.configuration, connection
        )
        if capabilities.vacancy_publish != "available" or connection is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "connection_not_ready",
                    "connection_status": connection.status if connection else None,
                },
            )
        if runtime.cipher is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {"code": "credential_encryption_unavailable"},
            )
        publication_status = "queued"
        external_id = None
        failure_code = None
        published_at = None
    publication = SourcePublication(
        id=uuid4(),
        organization_id=user.organization_id,
        integration_id=integration.id,
        position_id=position.id,
        created_by_user_id=user.id,
        provider_snapshot=integration.provider,
        idempotency_key=idempotency_key,
        status=publication_status,
        external_id=external_id,
        tracking_url=tracking_url,
        post_text=post_text,
        failure_code=failure_code,
        published_at=published_at,
    )
    db.add(publication)
    job: IntegrationJob | None = None
    if publication_status == "queued":
        assert connection is not None and runtime.cipher is not None
        payload = (
            {"text": post_text}
            if integration.provider == "telegram"
            else _hh_publication_payload(integration, position)
        )
        try:
            job, _created = await enqueue_source_provider_job(
                db,
                integration=integration,
                connection=connection,
                actor_user_id=user.id,
                operation="vacancy.publish",
                idempotency_key=idempotency_key,
                references={
                    "source_kind": "source_publication",
                    "source_integration_id": str(integration.id),
                    "publication_id": str(publication.id),
                    "position_id": str(position.id),
                },
                sensitive_payload=payload,
                cipher=runtime.cipher,
                required_capability="vacancy.publish",
            )
        except (IntegrationJobError, SourceImportError) as exc:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": getattr(exc, "code", "source_publish_failed")},
            ) from exc
    await db.commit()
    await db.refresh(publication)
    if job is not None:
        await db.refresh(job)
    return _publication_out(publication, job)


@router.get("/sourcing/analytics", response_model=SourceAnalyticsOut)
async def source_analytics(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> SourceAnalyticsOut:
    await require_permission(db, user, "analytics.read")
    # Source is an application-cycle attribute. Candidate.parsed_cv is merely
    # the person's latest/general document projection and cannot distinguish a
    # company-site application to X from a later referral application to Y.
    # Grouping canonical Application rows also counts reapplications as their
    # own cycles and keeps drill-down totals reconcilable.
    attributed_source = func.coalesce(
        func.nullif(Application.source, ""),
        "unknown",
    )
    rows = (
        await db.execute(
            select(
                attributed_source.label("attributed_source"),
                func.count(func.distinct(Application.id)),
                func.count(func.distinct(Application.id)).filter(
                    Application.stage == "hired"
                ),
            )
            .join(
                Candidate,
                Candidate.id == Application.candidate_id,
            )
            .where(
                Application.organization_id == user.organization_id,
                Candidate.merged_at.is_(None),
            )
            .group_by(attributed_source)
            .order_by(
                func.count(func.distinct(Application.id)).desc(),
                attributed_source,
            )
        )
    ).all()
    items = [
        SourceAnalyticsItem(
            source=source,
            applications=applications,
            hired=hired,
            conversion_rate=round(
                (hired / applications * 100) if applications else 0, 2
            ),
        )
        for source, applications, hired in rows
    ]
    return SourceAnalyticsOut(
        items=items,
        total_applications=sum(item.applications for item in items),
    )
