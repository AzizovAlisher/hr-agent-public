"""Tenant-safe API for provider lifecycle, jobs, webhooks, and assessment adapters."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import (
    bootstrap_resource,
    forced_tenant_rls,
    get_session,
    set_tenant_context,
)
from app.models.integration import (
    IntegrationConnection,
    IntegrationJob,
    IntegrationWebhookDelivery,
    IntegrationWebhookDeliveryAttempt,
)
from app.models.user import User
from app.schemas.integrations import (
    AssessmentProviderInvitationCreate,
    AssessmentProviderPollRequest,
    IntegrationConnectionCreate,
    IntegrationConnectionCreatedOut,
    IntegrationConnectionOut,
    IntegrationConnectionPage,
    IntegrationConnectionUpdate,
    IntegrationCredentialSet,
    IntegrationCredentialsOut,
    IntegrationHealthOut,
    IntegrationJobCreate,
    IntegrationJobOut,
    IntegrationJobPage,
    IntegrationOAuthCallback,
    IntegrationOAuthStart,
    IntegrationOAuthStartOut,
    IntegrationProviderOut,
    ProviderAssessmentApplicationEvidenceOut,
    ProviderCalendarEventDeleteJobCreate,
    ProviderCalendarEventJobCreate,
    ProviderCalendarEventUpdateJobCreate,
    ProviderEmailJobCreate,
    ProviderHeadHunterImportJobCreate,
    ProviderHeadHunterVacancyJobCreate,
    ProviderTelegramMessageJobCreate,
    WebhookAcceptedOut,
    WebhookDeliveryCreate,
    WebhookDeliveryDetailOut,
    WebhookDeliveryOut,
    WebhookDeliveryPage,
)
from app.schemas.pagination import CursorPage
from app.services.enterprise import require_field_access, require_permission
from app.services.integrations.assessments import (
    application_provider_evidence,
    bind_provider_invitation_job,
    reconcile_provider_assessment_job,
    require_provider_assessment,
)
from app.services.integrations.catalog import PROVIDERS
from app.services.integrations.crypto import IntegrationCipher, IntegrationCryptoError
from app.services.integrations.http import (
    ProviderHttpClient,
    create_integration_http_client,
)
from app.services.integrations.jobs import (
    IntegrationJobError,
    cancel_job,
    enqueue_job,
    replay_dead_letter,
    request_job_execution,
)
from app.services.integrations.lifecycle import (
    IntegrationLifecycleError,
    check_health,
    complete_oauth,
    connection_out,
    create_connection,
    get_connection,
    revoke_connection,
    set_connection_credentials,
    start_oauth,
)
from app.services.integrations.providers import default_adapters
from app.services.integrations.sourcing import reconcile_sourcing_job
from app.services.integrations.webhooks import (
    WebhookError,
    accept_webhook_event,
    queue_webhook_delivery,
    request_webhook_delivery_attempt,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

router = APIRouter(prefix="/integrations", tags=["integrations"])
settings = get_settings()
_SECRET_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


@dataclass
class ProviderRuntime:
    cipher: IntegrationCipher | None
    http: ProviderHttpClient
    adapters: dict
    redirect_origins: set[str]
    outbound_hosts: set[str]


async def provider_runtime() -> AsyncIterator[ProviderRuntime]:
    key = getattr(settings, "integration_credential_encryption_key", None)
    cipher: IntegrationCipher | None = None
    if key:
        try:
            cipher = IntegrationCipher.from_base64(
                key,
                key_id=getattr(settings, "integration_credential_key_id", None),
            )
        except IntegrationCryptoError:
            # Configuration details remain server-side; mutating endpoints emit
            # one stable unavailable code through _require_cipher.
            cipher = None
    timeout = float(getattr(settings, "integration_http_timeout_seconds", 15.0))
    outbound_hosts = _csv_setting(
        getattr(settings, "integration_outbound_host_allowlist", "")
    )
    client = create_integration_http_client(
        allowed_hosts=outbound_hosts,
        timeout=timeout,
        max_connections=50,
        max_keepalive_connections=20,
    )
    http = ProviderHttpClient(client)
    user_agent = getattr(
        settings,
        "hh_user_agent",
        "HR-Agent-Pilot/1.0 (integration-contact-not-configured)",
    )
    redirect_origins = _csv_setting(
        getattr(settings, "integration_oauth_redirect_origins", "")
        or settings.web_origin
    )
    try:
        yield ProviderRuntime(
            cipher=cipher,
            http=http,
            adapters=default_adapters(
                http,
                hh_user_agent=user_agent,
                outbound_hosts=outbound_hosts,
            ),
            redirect_origins=redirect_origins,
            outbound_hosts=outbound_hosts,
        )
    finally:
        await client.aclose()


def _csv_setting(value: str) -> set[str]:
    return {item.strip().rstrip("/") for item in value.split(",") if item.strip()}


def _require_cipher(runtime: ProviderRuntime) -> IntegrationCipher:
    if runtime.cipher is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "credential_encryption_unavailable"},
        )
    return runtime.cipher


def _assessment_callback_url(
    request: Request, runtime: ProviderRuntime, connection_id: UUID
) -> str:
    """Build callbacks only from an operator-approved ingress origin."""

    parsed = urlsplit(str(request.base_url))
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if origin not in runtime.redirect_origins:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "integration_callback_origin_unavailable"},
        )
    return str(
        request.url_for("receive_integration_webhook", connection_id=str(connection_id))
    )


def _lifecycle_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "integration_operation_failed")
    if code in {
        "assessment_mapping_not_found",
        "assessment_target_not_found",
        "connection_not_found",
    }:
        status_code = status.HTTP_404_NOT_FOUND
    elif code in {
        "idempotency_conflict",
        "oauth_state_consumed",
        "connection_revoked",
        "job_terminal",
        "job_not_runnable",
        "delivery_not_runnable",
    }:
        status_code = status.HTTP_409_CONFLICT
    elif code in {
        "credential_encryption_unavailable",
        "provider_unavailable",
        "provider_adapter_unavailable",
    }:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(status_code, {"code": code})


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
        detail="Invalid integration cursor",
    )
    return (
        cursor_datetime(payload, "created_at", detail="Invalid integration cursor"),
        cursor_uuid(payload, detail="Invalid integration cursor"),
    )


def _validate_dynamic_configuration(
    provider: str, configuration: dict, runtime: ProviderRuntime
) -> None:
    key = "target_url" if provider == "generic_webhook" else "base_url"
    if provider not in {"generic_webhook", "assessment_generic"}:
        return
    value = configuration.get(key)
    if value is None and provider == "generic_webhook":
        return
    parsed = urlsplit(str(value or ""))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.hostname.rstrip(".").lower()
        not in {item.rstrip(".").lower() for item in runtime.outbound_hosts}
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "endpoint_not_allowed"},
        )


async def _bounded_request_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum:
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    {"code": "webhook_payload_too_large"},
                )
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"code": "content_length_invalid"},
            ) from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                {"code": "webhook_payload_too_large"},
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/providers", response_model=CursorPage[IntegrationProviderOut])
async def list_integration_providers(
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[IntegrationProviderOut]:
    await require_permission(db, user, "integrations.read")
    return CursorPage(
        items=[
            IntegrationProviderOut(
                provider=item.provider,
                capabilities=list(item.capabilities),
                oauth=item.oauth,
                credential_keys=list(item.credential_keys),
                locally_available=item.locally_available,
                unavailable_reason=item.unavailable_reason,
            )
            for item in PROVIDERS.values()
        ],
        next_cursor=None,
    )


@router.get("", response_model=IntegrationConnectionPage)
async def list_integrations(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationConnectionPage:
    await require_permission(db, user, "integrations.read")
    statement = select(IntegrationConnection).where(
        IntegrationConnection.organization_id == user.organization_id
    )
    namespace = "integrations.connections"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        statement = statement.where(
            or_(
                IntegrationConnection.created_at < created_at,
                and_(
                    IntegrationConnection.created_at == created_at,
                    IntegrationConnection.id < row_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    IntegrationConnection.created_at.desc(),
                    IntegrationConnection.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    items = rows[:limit]
    return IntegrationConnectionPage(
        items=[connection_out(item) for item in items],
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
    "",
    response_model=IntegrationConnectionCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_integration(
    body: IntegrationConnectionCreate,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationConnectionCreatedOut:
    await require_permission(db, user, "integrations.manage")
    _validate_dynamic_configuration(body.provider, body.configuration, runtime)
    try:
        connection, one_time_secret = await create_connection(
            db,
            organization_id=user.organization_id,
            actor_user_id=user.id,
            provider=body.provider,
            name=body.name,
            configuration=body.configuration,
            cipher=runtime.cipher,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "connection_name_exists"}
        ) from exc
    except IntegrationLifecycleError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(connection)
    response.headers.update(_SECRET_RESPONSE_HEADERS)
    return IntegrationConnectionCreatedOut(
        connection=connection_out(connection),
        webhook_signing_secret=one_time_secret,
    )


@router.patch("/{connection_id}", response_model=IntegrationConnectionOut)
async def update_integration(
    connection_id: UUID,
    body: IntegrationConnectionUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationConnectionOut:
    await require_permission(db, user, "integrations.manage")
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
            for_update=True,
        )
        if body.name is not None:
            connection.name = body.name
        if body.configuration is not None:
            # Update validation needs the persisted provider discriminator.
            from app.schemas.integrations import validate_non_secret_configuration

            _validate_dynamic_configuration(
                connection.provider, body.configuration, runtime
            )
            connection.configuration = validate_non_secret_configuration(
                connection.provider, body.configuration
            )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "connection_name_exists"}
        ) from exc
    except IntegrationLifecycleError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "configuration_invalid"},
        ) from exc
    await db.refresh(connection)
    return connection_out(connection)


@router.put("/{connection_id}/credentials", response_model=IntegrationCredentialsOut)
async def put_integration_credentials(
    connection_id: UUID,
    body: IntegrationCredentialSet,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationCredentialsOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "integrations.credentials")
    cipher = _require_cipher(runtime)
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
            for_update=True,
        )
        configured = await set_connection_credentials(
            db,
            connection=connection,
            credentials={
                key: value.get_secret_value() for key, value in body.credentials.items()
            },
            cipher=cipher,
        )
        await db.commit()
    except (IntegrationLifecycleError, ValueError) as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    response.headers.update(_SECRET_RESPONSE_HEADERS)
    return IntegrationCredentialsOut(
        connection_id=connection.id,
        configured_keys=configured,
        status=connection.status,
        credentials_updated_at=connection.credentials_updated_at,
    )


@router.post("/{connection_id}/oauth/start", response_model=IntegrationOAuthStartOut)
async def begin_integration_oauth(
    connection_id: UUID,
    body: IntegrationOAuthStart,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationOAuthStartOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "integrations.credentials")
    cipher = _require_cipher(runtime)
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
            for_update=True,
        )
        adapter = runtime.adapters.get(connection.provider)
        if adapter is None:
            raise IntegrationLifecycleError("provider_adapter_unavailable")
        authorization_url, expires_at = await start_oauth(
            db,
            connection=connection,
            redirect_uri=str(body.redirect_uri),
            allowed_redirect_origins=runtime.redirect_origins,
            cipher=cipher,
            adapter=adapter,
        )
        await db.commit()
    except IntegrationLifecycleError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    response.headers.update(_SECRET_RESPONSE_HEADERS)
    return IntegrationOAuthStartOut(
        authorization_url=authorization_url, expires_at=expires_at
    )


@router.post("/oauth/callback", response_model=IntegrationConnectionOut)
async def finish_integration_oauth(
    body: IntegrationOAuthCallback,
    response: Response,
    db: AsyncSession = Depends(get_session),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationConnectionOut:
    cipher = _require_cipher(runtime)
    try:
        connection = await complete_oauth(
            db,
            raw_state=body.state,
            code=body.code.get_secret_value(),
            cipher=cipher,
            adapters=runtime.adapters,
        )
        await db.commit()
    except IntegrationLifecycleError as exc:
        # A provider exchange failure still consumes the one-time state and
        # records degraded health; committing those security facts prevents an
        # authorization-code replay. Invalid/expired state has no mutation.
        if exc.code == "oauth_exchange_failed":
            await db.commit()
        else:
            await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(connection)
    response.headers.update(_SECRET_RESPONSE_HEADERS)
    return connection_out(connection)


@router.post("/{connection_id}/health", response_model=IntegrationHealthOut)
async def check_integration_health(
    connection_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationHealthOut:
    await require_permission(db, user, "integrations.manage")
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
            for_update=True,
        )
        result = await check_health(
            db,
            connection=connection,
            cipher=runtime.cipher,
            adapters=runtime.adapters,
        )
        await db.commit()
        return result
    except IntegrationLifecycleError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc


@router.post("/{connection_id}/revoke", response_model=IntegrationConnectionOut)
async def revoke_integration(
    connection_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationConnectionOut:
    await require_permission(db, user, "integrations.manage")
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
            for_update=True,
        )
        await revoke_connection(
            db,
            connection=connection,
            cipher=runtime.cipher,
            adapters=runtime.adapters,
        )
        await db.commit()
    except IntegrationLifecycleError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(connection)
    return connection_out(connection)


@router.post("/{connection_id}/jobs", response_model=IntegrationJobOut)
async def create_integration_job(
    connection_id: UUID,
    body: IntegrationJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
        )
        # Payload-bearing effects use the typed endpoints below, where their
        # encrypted payload is validated before enqueue. The generic endpoint
        # remains useful for a durable provider health probe only.
        if body.operation != "health" or connection.provider not in runtime.adapters:
            raise IntegrationJobError("operation_requires_typed_endpoint")
        job, _created = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=user.id,
            operation=body.operation,
            idempotency_key=body.idempotency_key,
            references=body.references,
            max_attempts=body.max_attempts,
        )
        await db.commit()
    except (IntegrationLifecycleError, IntegrationJobError) as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


async def _enqueue_provider_payload(
    connection_id: UUID,
    *,
    operation: str,
    idempotency_key: str,
    payload: dict,
    references: dict,
    db: AsyncSession,
    user: User,
    runtime: ProviderRuntime,
) -> IntegrationJobOut:
    cipher = _require_cipher(runtime)
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
        )
        job, _created = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=user.id,
            operation=operation,
            idempotency_key=idempotency_key,
            references=references,
            sensitive_payload=payload,
            cipher=cipher,
        )
        await db.commit()
    except (IntegrationLifecycleError, IntegrationJobError) as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


@router.post(
    "/{connection_id}/email/messages",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_provider_email(
    connection_id: UUID,
    body: ProviderEmailJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "communications.send")
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    return await _enqueue_provider_payload(
        connection_id,
        operation="email.send",
        idempotency_key=body.idempotency_key,
        payload=payload,
        references={},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.post(
    "/{connection_id}/calendar/events",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_provider_calendar_event(
    connection_id: UUID,
    body: ProviderCalendarEventJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "scheduling.manage")
    return await _enqueue_provider_payload(
        connection_id,
        operation="calendar.create",
        idempotency_key=body.idempotency_key,
        payload=body.model_dump(mode="json", exclude={"idempotency_key"}),
        references={},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.put(
    "/{connection_id}/calendar/events",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_provider_calendar_event_update(
    connection_id: UUID,
    body: ProviderCalendarEventUpdateJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "scheduling.manage")
    return await _enqueue_provider_payload(
        connection_id,
        operation="calendar.update",
        idempotency_key=body.idempotency_key,
        payload=body.model_dump(mode="json", exclude={"idempotency_key"}),
        references={"event_ref": body.event_id},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.delete(
    "/{connection_id}/calendar/events",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_provider_calendar_event_delete(
    connection_id: UUID,
    body: ProviderCalendarEventDeleteJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "scheduling.manage")
    return await _enqueue_provider_payload(
        connection_id,
        operation="calendar.delete",
        idempotency_key=body.idempotency_key,
        payload={"event_id": body.event_id},
        references={"event_ref": body.event_id},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.post(
    "/{connection_id}/telegram/messages",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_provider_telegram_message(
    connection_id: UUID,
    body: ProviderTelegramMessageJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "communications.send")
    return await _enqueue_provider_payload(
        connection_id,
        operation="telegram.send",
        idempotency_key=body.idempotency_key,
        payload=body.model_dump(mode="json", exclude={"idempotency_key"}),
        references={},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.post(
    "/{connection_id}/hh/vacancies",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_hh_vacancy(
    connection_id: UUID,
    body: ProviderHeadHunterVacancyJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "sourcing.publish")
    return await _enqueue_provider_payload(
        connection_id,
        operation="vacancy.publish",
        idempotency_key=body.idempotency_key,
        payload=body.payload,
        references={},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.post(
    "/{connection_id}/hh/imports",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enqueue_hh_import(
    connection_id: UUID,
    body: ProviderHeadHunterImportJobCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    await require_permission(db, user, "sourcing.manage")
    return await _enqueue_provider_payload(
        connection_id,
        operation="candidate.import",
        idempotency_key=body.idempotency_key,
        payload={"vacancy_id": body.vacancy_id},
        references={"vacancy_ref": body.vacancy_id},
        db=db,
        user=user,
        runtime=runtime,
    )


@router.get("/jobs", response_model=IntegrationJobPage)
async def list_integration_jobs(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobPage:
    await require_permission(db, user, "integrations.read")
    statement = select(IntegrationJob).where(
        IntegrationJob.organization_id == user.organization_id
    )
    namespace = "integrations.jobs"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        statement = statement.where(
            or_(
                IntegrationJob.created_at < created_at,
                and_(
                    IntegrationJob.created_at == created_at, IntegrationJob.id < row_id
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    IntegrationJob.created_at.desc(), IntegrationJob.id.desc()
                ).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    items = rows[:limit]
    return IntegrationJobPage(
        items=[IntegrationJobOut.model_validate(item) for item in items],
        next_cursor=_encode_cursor(
            items[-1].created_at,
            items[-1].id,
            namespace=namespace,
            scope=scope,
        )
        if more and items
        else None,
    )


async def _tenant_job(
    db: AsyncSession, job_id: UUID, organization_id: UUID, *, lock: bool = False
) -> IntegrationJob:
    statement = select(IntegrationJob).where(
        IntegrationJob.id == job_id,
        IntegrationJob.organization_id == organization_id,
    )
    if lock:
        statement = statement.with_for_update()
    job = (await db.execute(statement)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "job_not_found"})
    return job


@router.get("/jobs/{job_id}", response_model=IntegrationJobOut)
async def get_integration_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.read")
    return IntegrationJobOut.model_validate(
        await _tenant_job(db, job_id, user.organization_id)
    )


@router.post("/jobs/{job_id}/cancel", response_model=IntegrationJobOut)
async def cancel_integration_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    job = await _tenant_job(db, job_id, user.organization_id, lock=True)
    try:
        await cancel_job(db, job=job)
        await reconcile_sourcing_job(db, job=job)
        await reconcile_provider_assessment_job(db, job=job)
        await db.commit()
    except IntegrationJobError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


@router.post("/jobs/{job_id}/replay", response_model=IntegrationJobOut)
async def replay_integration_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    job = await _tenant_job(db, job_id, user.organization_id, lock=True)
    try:
        await replay_dead_letter(db, job=job)
        await reconcile_sourcing_job(db, job=job)
        await reconcile_provider_assessment_job(db, job=job)
        await db.commit()
    except IntegrationJobError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


@router.post(
    "/jobs/{job_id}/run",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_integration_job_now(
    job_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobOut:
    await require_permission(db, user, "integrations.manage")
    job = await _tenant_job(db, job_id, user.organization_id, lock=True)
    try:
        await request_job_execution(db, job=job)
        await reconcile_sourcing_job(db, job=job)
        await reconcile_provider_assessment_job(db, job=job)
        await db.commit()
    except IntegrationJobError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


@router.get(
    "/assessment/connections",
    response_model=IntegrationConnectionPage,
)
async def list_assessment_provider_connections(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationConnectionPage:
    """Expose assessment-provider capability state without credential access."""

    await require_permission(db, user, "assessments.read")
    namespace = "integrations.assessment.connections"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    statement = select(IntegrationConnection).where(
        IntegrationConnection.organization_id == user.organization_id,
        IntegrationConnection.provider == "assessment_generic",
    )
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        statement = statement.where(
            or_(
                IntegrationConnection.created_at < created_at,
                and_(
                    IntegrationConnection.created_at == created_at,
                    IntegrationConnection.id < row_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    IntegrationConnection.created_at.desc(),
                    IntegrationConnection.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    items = rows[:limit]
    return IntegrationConnectionPage(
        items=[connection_out(item) for item in items],
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
    "/{connection_id}/assessment/invitations",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_assessment_provider_invitation(
    connection_id: UUID,
    body: AssessmentProviderInvitationCreate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> IntegrationJobOut:
    await require_permission(db, user, "assessments.manage")
    cipher = _require_cipher(runtime)
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
        )
        if connection.provider != "assessment_generic":
            raise IntegrationJobError("operation_not_supported")
        job, _created = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=user.id,
            operation="assessment.invite",
            idempotency_key=body.idempotency_key,
            references={
                "application_id": str(body.application_id),
                "template_version_id": (
                    str(body.template_version_id)
                    if body.template_version_id is not None
                    else None
                ),
                "external_template_ref": body.external_template_ref,
            },
            sensitive_payload={
                **body.model_dump(mode="json"),
                "callback_url": _assessment_callback_url(
                    request, runtime, connection.id
                ),
            },
            cipher=cipher,
        )
        await bind_provider_invitation_job(
            db,
            connection=connection,
            job=job,
            actor_user_id=user.id,
            request=body,
        )
        await db.commit()
    except (IntegrationLifecycleError, IntegrationJobError) as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


@router.post(
    "/{connection_id}/assessment/attempts/status",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def poll_assessment_provider_attempt(
    connection_id: UUID,
    body: AssessmentProviderPollRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobOut:
    await require_permission(db, user, "assessments.manage")
    return await _enqueue_assessment_poll(
        connection_id,
        body,
        operation="assessment.status",
        db=db,
        user=user,
    )


@router.post(
    "/{connection_id}/assessment/attempts/result",
    response_model=IntegrationJobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def poll_assessment_provider_result(
    connection_id: UUID,
    body: AssessmentProviderPollRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationJobOut:
    await require_permission(db, user, "assessments.results")
    return await _enqueue_assessment_poll(
        connection_id,
        body,
        operation="assessment.result",
        db=db,
        user=user,
    )


async def _enqueue_assessment_poll(
    connection_id: UUID,
    body: AssessmentProviderPollRequest,
    *,
    operation: str,
    db: AsyncSession,
    user: User,
) -> IntegrationJobOut:
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
        )
        if connection.provider != "assessment_generic":
            raise IntegrationJobError("operation_not_supported")
        await require_provider_assessment(
            db,
            organization_id=user.organization_id,
            connection_id=connection.id,
            provider_assessment_id=body.provider_assessment_id,
        )
        job, _created = await enqueue_job(
            db,
            connection=connection,
            actor_user_id=user.id,
            operation=operation,
            idempotency_key=body.idempotency_key,
            references={"provider_assessment_id": str(body.provider_assessment_id)},
        )
        await db.commit()
    except (IntegrationLifecycleError, IntegrationJobError) as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(job)
    return IntegrationJobOut.model_validate(job)


@router.get(
    "/assessment/applications/{application_id}/evidence",
    response_model=ProviderAssessmentApplicationEvidenceOut,
)
async def get_application_provider_assessment_evidence(
    application_id: UUID,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> ProviderAssessmentApplicationEvidenceOut:
    await require_permission(db, user, "assessments.results")
    await require_field_access(db, user, FieldClass.ASSESSMENT_RESULT)
    cipher = _require_cipher(runtime)
    try:
        output = await application_provider_evidence(
            db,
            organization_id=user.organization_id,
            application_id=application_id,
            cipher=cipher,
        )
    except IntegrationJobError as exc:
        raise _lifecycle_error(exc) from exc
    response.headers.update(_SECRET_RESPONSE_HEADERS)
    return output


@router.post(
    "/webhooks/{connection_id}",
    response_model=WebhookAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_integration_webhook(
    connection_id: UUID,
    request: Request,
    webhook_timestamp: str = Header(alias="X-HR-Webhook-Timestamp"),
    webhook_signature: str = Header(alias="X-HR-Webhook-Signature"),
    db: AsyncSession = Depends(get_session),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> WebhookAcceptedOut:
    cipher = _require_cipher(runtime)
    if forced_tenant_rls():
        bootstrap = await bootstrap_resource(
            db, kind="integration_connection", resource_id=connection_id
        )
        if bootstrap is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    connection = await db.get(IntegrationConnection, connection_id)
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    body = await _bounded_request_body(request, 256_000)
    try:
        event, replay = await accept_webhook_event(
            db,
            connection=connection,
            cipher=cipher,
            raw_body=body,
            timestamp=webhook_timestamp,
            signature=webhook_signature,
            credential_key=(
                "callback_signing_secret"
                if connection.provider == "assessment_generic"
                else "signing_secret"
            ),
        )
        if connection.provider == "assessment_generic":
            await enqueue_job(
                db,
                connection=connection,
                actor_user_id=None,
                operation="assessment.callback",
                idempotency_key=event.event_id,
                references={"webhook_event_id": str(event.id)},
                max_attempts=5,
                allow_unready=True,
            )
        await db.commit()
    except (IntegrationJobError, WebhookError) as exc:
        await db.rollback()
        if exc.code in {
            "webhook_signature_invalid",
            "webhook_timestamp_invalid",
            "webhook_timestamp_expired",
            "webhook_secret_not_configured",
            "connection_not_ready",
        }:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found") from exc
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": exc.code}
        ) from exc
    return WebhookAcceptedOut(event_id=event.event_id, accepted=True, replay=replay)


@router.post(
    "/{connection_id}/deliveries",
    response_model=WebhookDeliveryOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_webhook_delivery(
    connection_id: UUID,
    body: WebhookDeliveryCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    runtime: ProviderRuntime = Depends(provider_runtime),
) -> WebhookDeliveryOut:
    await require_permission(db, user, "integrations.manage")
    cipher = _require_cipher(runtime)
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
        )
        delivery, _created = await queue_webhook_delivery(
            db,
            connection=connection,
            cipher=cipher,
            event_type=body.type,
            data=body.data,
            idempotency_key=body.idempotency_key,
        )
        await db.commit()
    except (IntegrationLifecycleError, WebhookError) as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(delivery)
    return WebhookDeliveryOut.model_validate(delivery)


@router.get("/deliveries", response_model=WebhookDeliveryPage)
async def list_webhook_deliveries(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> WebhookDeliveryPage:
    await require_permission(db, user, "integrations.read")
    statement = select(IntegrationWebhookDelivery).where(
        IntegrationWebhookDelivery.organization_id == user.organization_id
    )
    namespace = "integrations.deliveries"
    scope = collection_cursor_scope(namespace, organization_id=user.organization_id)
    boundary = _decode_cursor(cursor, namespace=namespace, scope=scope)
    if boundary:
        created_at, row_id = boundary
        statement = statement.where(
            or_(
                IntegrationWebhookDelivery.created_at < created_at,
                and_(
                    IntegrationWebhookDelivery.created_at == created_at,
                    IntegrationWebhookDelivery.id < row_id,
                ),
            )
        )
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    IntegrationWebhookDelivery.created_at.desc(),
                    IntegrationWebhookDelivery.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    more = len(rows) > limit
    items = rows[:limit]
    return WebhookDeliveryPage(
        items=[WebhookDeliveryOut.model_validate(item) for item in items],
        next_cursor=_encode_cursor(
            items[-1].created_at,
            items[-1].id,
            namespace=namespace,
            scope=scope,
        )
        if more and items
        else None,
    )


@router.post(
    "/deliveries/{delivery_id}/attempt",
    response_model=WebhookDeliveryOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def attempt_webhook_delivery(
    delivery_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> WebhookDeliveryOut:
    await require_permission(db, user, "integrations.manage")
    delivery = (
        await db.execute(
            select(IntegrationWebhookDelivery)
            .where(
                IntegrationWebhookDelivery.id == delivery_id,
                IntegrationWebhookDelivery.organization_id == user.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "delivery_not_found"})
    try:
        await request_webhook_delivery_attempt(db, delivery=delivery)
        await db.commit()
    except WebhookError as exc:
        await db.rollback()
        raise _lifecycle_error(exc) from exc
    await db.refresh(delivery)
    return WebhookDeliveryOut.model_validate(delivery)


@router.get("/deliveries/{delivery_id}", response_model=WebhookDeliveryDetailOut)
async def inspect_webhook_delivery(
    delivery_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> WebhookDeliveryDetailOut:
    await require_permission(db, user, "integrations.read")
    delivery = (
        await db.execute(
            select(IntegrationWebhookDelivery).where(
                IntegrationWebhookDelivery.id == delivery_id,
                IntegrationWebhookDelivery.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if delivery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "delivery_not_found"})
    attempts = list(
        (
            await db.execute(
                select(IntegrationWebhookDeliveryAttempt)
                .where(
                    IntegrationWebhookDeliveryAttempt.organization_id
                    == user.organization_id,
                    IntegrationWebhookDeliveryAttempt.delivery_id == delivery.id,
                )
                .order_by(IntegrationWebhookDeliveryAttempt.attempt_number)
            )
        ).scalars()
    )
    return WebhookDeliveryDetailOut(
        delivery=WebhookDeliveryOut.model_validate(delivery),
        attempts=attempts,
    )


# Keep the single-segment dynamic GET last so it cannot shadow the static
# ``/providers``, ``/jobs``, or ``/deliveries`` administration routes.
@router.get("/{connection_id}", response_model=IntegrationConnectionOut)
async def get_integration(
    connection_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> IntegrationConnectionOut:
    await require_permission(db, user, "integrations.read")
    try:
        connection = await get_connection(
            db,
            connection_id=connection_id,
            organization_id=user.organization_id,
        )
    except IntegrationLifecycleError as exc:
        raise _lifecycle_error(exc) from exc
    return connection_out(connection)
