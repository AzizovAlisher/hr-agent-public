"""Tenant-scoped candidate communication API."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import current_user
from app.config import get_settings
from app.db import (
    bootstrap_resource,
    forced_tenant_rls,
    get_session,
    set_tenant_context,
)
from app.models.candidate import Candidate
from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationBatch,
    CommunicationMessage,
    CommunicationTemplate,
    CommunicationThread,
)
from app.models.integration import IntegrationConnection
from app.models.user import User
from app.routers.integrations import _bounded_request_body
from app.schemas.communications import (
    MAX_DURABLE_BULK_RECIPIENTS,
    BulkCommunicationCreateOut,
    BulkCommunicationCreateRequest,
    BulkCommunicationPreviewItem,
    BulkCommunicationPreviewOut,
    BulkCommunicationPreviewRequest,
    CandidateCommunicationCompose,
    CandidateCommunicationPreviewOut,
    CandidateCommunicationSend,
    CandidateContactPointCreate,
    CandidateContactPointOut,
    CandidateContactPointUpdate,
    CommunicationAccountCreate,
    CommunicationAccountCreateOut,
    CommunicationAccountOut,
    CommunicationBatchOut,
    CommunicationCapabilitiesOut,
    CommunicationInboxThreadOut,
    CommunicationMessageOut,
    CommunicationMessagePage,
    CommunicationTemplateCreate,
    CommunicationTemplateOut,
    CommunicationTemplateUpdate,
    CommunicationThreadOut,
    CommunicationThreadPage,
    SandboxInboundRequest,
    SandboxInboundWebhookOut,
    WebhookSecretOut,
)
from app.schemas.integrations import WebhookEnvelope
from app.schemas.pagination import CursorPage
from app.services.activities import append_candidate_activity
from app.services.candidate_merge import resolve_canonical_candidate
from app.services.communications import (
    CommunicationPreparationError,
    CommunicationWebhookConflict,
    PreparedCommunication,
    cancel_outbound_message,
    create_outbound_message,
    ensure_default_sandbox_account,
    hash_webhook_secret,
    ingest_sandbox_inbound,
    new_webhook_secret,
    normalize_contact,
    prepare_candidate_communication,
    validate_template_text,
)
from app.services.entitlements import effective_synchronous_recipient_limit
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
from app.services.structured_hiring import resolve_existing_active_application

router = APIRouter(tags=["communications"])
settings = get_settings()


def _synchronous_recipient_limit() -> int:
    return effective_synchronous_recipient_limit(
        settings.communications_max_bulk_candidates
    )


def _preparation_http_error(exc: CommunicationPreparationError) -> HTTPException:
    response_status = (
        status.HTTP_409_CONFLICT
        if exc.code == "application_unavailable"
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(
        response_status,
        {"code": exc.code, "message": str(exc)},
    )


async def _canonical_candidate(
    db: AsyncSession, candidate_id: UUID, user: User
) -> Candidate:
    candidate = await resolve_canonical_candidate(
        db, candidate_id, user.organization_id
    )
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
    return candidate


def _account_capability(account: CommunicationAccount) -> str | None:
    if account.provider == "sandbox":
        return "sandbox.local"
    if account.provider in {"google_workspace", "microsoft_365"}:
        return "email.send"
    if account.provider == "telegram":
        return "message.telegram.send"
    return None


def _account_out(
    account: CommunicationAccount,
    connection: IntegrationConnection | None = None,
    *,
    include_configuration: bool = True,
) -> CommunicationAccountOut:
    capability = _account_capability(account)
    connection_status = connection.status if connection is not None else None
    delivery_available = bool(
        account.is_active
        and (
            account.provider == "sandbox"
            or (
                connection is not None
                and connection.status == "connected"
                and capability is not None
                and capability in (connection.capabilities or [])
                and connection.provider == account.provider
            )
        )
    )
    return CommunicationAccountOut(
        id=account.id,
        integration_connection_id=account.integration_connection_id,
        channel=account.channel,
        provider=account.provider,
        name=account.name,
        sender_label=account.sender_label,
        configuration=(account.configuration or {}) if include_configuration else {},
        is_active=account.is_active,
        connection_status=connection_status,
        required_capability=capability,
        delivery_available=delivery_available,
        webhook_configured=account.webhook_secret_hash is not None,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


async def _latest_messages(
    db: AsyncSession, thread_ids: list[UUID], organization_id: UUID
) -> dict[UUID, CommunicationMessage]:
    if not thread_ids:
        return {}
    ranked = (
        select(
            CommunicationMessage.id.label("message_id"),
            func.row_number()
            .over(
                partition_by=CommunicationMessage.thread_id,
                order_by=(
                    CommunicationMessage.created_at.desc(),
                    CommunicationMessage.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(
            CommunicationMessage.organization_id == organization_id,
            CommunicationMessage.thread_id.in_(thread_ids),
        )
        .subquery()
    )
    messages = list(
        (
            await db.execute(
                select(CommunicationMessage)
                .join(ranked, ranked.c.message_id == CommunicationMessage.id)
                .where(ranked.c.rank == 1)
            )
        ).scalars()
    )
    return {message.thread_id: message for message in messages}


@router.get(
    "/candidates/{candidate_id}/contact-points",
    response_model=CursorPage[CandidateContactPointOut],
    dependencies=[Depends(current_user)],
)
async def list_candidate_contact_points(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CandidateContactPointOut]:
    candidate = await _canonical_candidate(db, candidate_id, user)
    namespace = "communications.contact-points"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(CandidateContactPoint).where(
        CandidateContactPoint.organization_id == user.organization_id,
        CandidateContactPoint.candidate_id == candidate.id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid contact points cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid contact points cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid contact points cursor")
        stmt = stmt.where(
            or_(
                CandidateContactPoint.created_at > cursor_time,
                and_(
                    CandidateContactPoint.created_at == cursor_time,
                    CandidateContactPoint.id > cursor_id,
                ),
            )
        )
    contacts = list(
        (
            await db.execute(
                stmt.order_by(
                    CandidateContactPoint.created_at.asc(),
                    CandidateContactPoint.id.asc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(contacts) > limit
    contacts = contacts[:limit]
    return CursorPage(
        items=[
            CandidateContactPointOut.model_validate(contact) for contact in contacts
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": contacts[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(contacts[-1].id),
                },
            )
            if has_more and contacts
            else None
        ),
    )


@router.post(
    "/candidates/{candidate_id}/contact-points",
    response_model=CandidateContactPointOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(current_user)],
)
async def create_candidate_contact_point(
    candidate_id: UUID,
    body: CandidateContactPointCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateContactPointOut:
    if body.channel == "whatsapp":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "capability_unavailable"},
        )
    candidate = await _canonical_candidate(db, candidate_id, user)
    try:
        normalized = normalize_contact(body.channel, body.value)
    except CommunicationPreparationError as exc:
        raise _preparation_http_error(exc) from exc
    is_primary = body.is_primary or not bool(
        (
            await db.execute(
                select(CandidateContactPoint.id).where(
                    CandidateContactPoint.organization_id == user.organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                    CandidateContactPoint.channel == body.channel,
                )
            )
        ).first()
    )
    if is_primary:
        await db.execute(
            update(CandidateContactPoint)
            .where(
                CandidateContactPoint.organization_id == user.organization_id,
                CandidateContactPoint.candidate_id == candidate.id,
                CandidateContactPoint.channel == body.channel,
            )
            .values(is_primary=False)
        )
    contact = CandidateContactPoint(
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        created_by_user_id=user.id,
        channel=body.channel,
        value=body.value,
        normalized_value=normalized,
        source="manual",
        is_primary=is_primary,
    )
    db.add(contact)
    if is_primary and body.channel == "email":
        candidate.email_normalized = normalized
    elif is_primary and body.channel == "phone":
        candidate.phone_normalized = normalized
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Contact already exists") from exc
    append_candidate_activity(
        db,
        organization_id=user.organization_id,
        candidate_id=candidate.id,
        actor_user_id=user.id,
        event_type="contact_point_added",
        subject_type="contact_point",
        subject_id=contact.id,
        payload={"channel": contact.channel},
    )
    await db.commit()
    await db.refresh(contact)
    return CandidateContactPointOut.model_validate(contact)


@router.patch(
    "/candidates/{candidate_id}/contact-points/{contact_point_id}",
    response_model=CandidateContactPointOut,
    dependencies=[Depends(current_user)],
)
async def update_candidate_contact_point(
    candidate_id: UUID,
    contact_point_id: UUID,
    body: CandidateContactPointUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateContactPointOut:
    candidate = await _canonical_candidate(db, candidate_id, user)
    contact = (
        await db.execute(
            select(CandidateContactPoint)
            .where(
                CandidateContactPoint.id == contact_point_id,
                CandidateContactPoint.organization_id == user.organization_id,
                CandidateContactPoint.candidate_id == candidate.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if contact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact point not found")
    changed = False
    opted_out_changed = False
    if "value" in body.model_fields_set and body.value is not None:
        try:
            normalized = normalize_contact(contact.channel, body.value)
        except CommunicationPreparationError as exc:
            raise _preparation_http_error(exc) from exc
        if normalized != contact.normalized_value or body.value != contact.value:
            contact.value = body.value
            contact.normalized_value = normalized
            changed = True
    if body.is_primary is not None and body.is_primary != contact.is_primary:
        if body.is_primary:
            await db.execute(
                update(CandidateContactPoint)
                .where(
                    CandidateContactPoint.organization_id == user.organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                    CandidateContactPoint.channel == contact.channel,
                    CandidateContactPoint.id != contact.id,
                )
                .values(is_primary=False)
            )
        contact.is_primary = body.is_primary
        changed = True
    if body.is_opted_out is not None and body.is_opted_out != contact.is_opted_out:
        contact.is_opted_out = body.is_opted_out
        contact.opted_out_at = datetime.now(UTC) if body.is_opted_out else None
        opted_out_changed = True
        changed = True
    if contact.is_primary and contact.channel == "email":
        candidate.email_normalized = contact.normalized_value
    elif contact.is_primary and contact.channel == "phone":
        candidate.phone_normalized = contact.normalized_value
    if changed:
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Contact already exists"
            ) from exc
        append_candidate_activity(
            db,
            organization_id=user.organization_id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            event_type=(
                "contact_point_opted_out"
                if opted_out_changed and contact.is_opted_out
                else "contact_point_updated"
            ),
            subject_type="contact_point",
            subject_id=contact.id,
            payload={"channel": contact.channel},
        )
        await db.commit()
        await db.refresh(contact)
    return CandidateContactPointOut.model_validate(contact)


@router.get(
    "/communications/capabilities",
    response_model=CommunicationCapabilitiesOut,
    dependencies=[Depends(current_user)],
)
async def communication_capabilities() -> CommunicationCapabilitiesOut:
    return CommunicationCapabilitiesOut(
        synchronous_recipient_limit=_synchronous_recipient_limit(),
        durable_recipient_limit=MAX_DURABLE_BULK_RECIPIENTS,
    )


@router.get(
    "/communications/accounts",
    response_model=CursorPage[CommunicationAccountOut],
    dependencies=[Depends(current_user)],
)
async def list_communication_accounts(
    channel: Literal["email", "telegram", "whatsapp"] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CommunicationAccountOut]:
    sandbox_channel = channel or "email"
    if sandbox_channel in {"email", "telegram"}:
        await ensure_default_sandbox_account(
            db,
            organization_id=user.organization_id,
            channel=sandbox_channel,
            created_by_user_id=user.id,
        )
        await db.commit()
    stmt = select(CommunicationAccount).where(
        CommunicationAccount.organization_id == user.organization_id
    )
    if channel is not None:
        stmt = stmt.where(CommunicationAccount.channel == channel)
    namespace = "communications.accounts"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"channel": channel},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid communication accounts cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {
                    "code": "cursor_invalid",
                    "detail": "Invalid communication accounts cursor",
                },
            )
        cursor_id = cursor_uuid(payload, detail="Invalid communication accounts cursor")
        stmt = stmt.where(
            or_(
                CommunicationAccount.name > cursor_name,
                and_(
                    CommunicationAccount.name == cursor_name,
                    CommunicationAccount.id > cursor_id,
                ),
            )
        )
    accounts = list(
        (
            await db.execute(
                stmt.order_by(
                    CommunicationAccount.name.asc(),
                    CommunicationAccount.id.asc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(accounts) > limit
    accounts = accounts[:limit]
    connection_ids = {
        account.integration_connection_id
        for account in accounts
        if account.integration_connection_id is not None
    }
    connections = (
        list(
            (
                await db.execute(
                    select(IntegrationConnection).where(
                        IntegrationConnection.organization_id == user.organization_id,
                        IntegrationConnection.id.in_(connection_ids),
                    )
                )
            ).scalars()
        )
        if connection_ids
        else []
    )
    by_id = {connection.id: connection for connection in connections}
    return CursorPage(
        items=[
            _account_out(
                account,
                by_id.get(account.integration_connection_id),
                # Collection reads are a delivery-capability projection. Old
                # rows may predate schema-level secret rejection, so never
                # serialize their free-form configuration—even for a role
                # whose action override permits credential administration.
                # Credentials and webhook secrets remain one-time create/
                # rotate values on their dedicated privileged endpoints.
                include_configuration=False,
            )
            for account in accounts
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"name": accounts[-1].name, "id": str(accounts[-1].id)},
            )
            if has_more and accounts
            else None
        ),
    )


@router.post(
    "/communications/accounts",
    response_model=CommunicationAccountCreateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(current_user)],
)
async def create_communication_account(
    body: CommunicationAccountCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationAccountCreateOut:
    connection = None
    capability = (
        "email.send"
        if body.provider in {"google_workspace", "microsoft_365"}
        else "message.telegram.send"
    )
    if body.integration_connection_id is not None:
        connection = (
            await db.execute(
                select(IntegrationConnection).where(
                    IntegrationConnection.id == body.integration_connection_id,
                    IntegrationConnection.organization_id == user.organization_id,
                )
            )
        ).scalar_one_or_none()
        if connection is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                {"code": "connection_not_found"},
            )
        if (
            connection.provider != body.provider
            or connection.status != "connected"
            or capability not in (connection.capabilities or [])
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "connection_capability_mismatch"},
            )
    webhook_secret = None
    webhook_secret_hash = None
    if body.provider == "sandbox":
        webhook_secret, webhook_secret_hash = new_webhook_secret()
    account = CommunicationAccount(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        integration_connection_id=(connection.id if connection is not None else None),
        channel=body.channel,
        provider=body.provider,
        name=body.name,
        sender_label=body.sender_label,
        configuration=(body.configuration if body.provider == "sandbox" else {}),
        webhook_secret_hash=webhook_secret_hash,
        is_active=True,
    )
    db.add(account)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with this name already exists"
        ) from exc
    await db.refresh(account)
    return CommunicationAccountCreateOut(
        account=_account_out(account, connection), webhook_secret=webhook_secret
    )


@router.post(
    "/communications/accounts/{account_id}/rotate-webhook-secret",
    response_model=WebhookSecretOut,
    dependencies=[Depends(current_user)],
)
async def rotate_communication_webhook_secret(
    account_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> WebhookSecretOut:
    account = (
        await db.execute(
            select(CommunicationAccount).where(
                CommunicationAccount.id == account_id,
                CommunicationAccount.organization_id == user.organization_id,
                CommunicationAccount.provider == "sandbox",
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sandbox account not found")
    raw, hashed = new_webhook_secret()
    account.webhook_secret_hash = hashed
    await db.commit()
    return WebhookSecretOut(account_id=account.id, webhook_secret=raw)


@router.get(
    "/communications/templates",
    response_model=CursorPage[CommunicationTemplateOut],
    dependencies=[Depends(current_user)],
)
async def list_communication_templates(
    channel: Literal["email", "telegram", "whatsapp"] | None = None,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CommunicationTemplateOut]:
    stmt = select(CommunicationTemplate).where(
        CommunicationTemplate.organization_id == user.organization_id
    )
    if channel is not None:
        stmt = stmt.where(CommunicationTemplate.channel == channel)
    if not include_inactive:
        stmt = stmt.where(CommunicationTemplate.is_active.is_(True))
    namespace = "communications.templates"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"channel": channel, "include_inactive": include_inactive},
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"name", "id"},
            detail="Invalid communication templates cursor",
        )
        cursor_name = payload["name"]
        if not isinstance(cursor_name, str):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {
                    "code": "cursor_invalid",
                    "detail": "Invalid communication templates cursor",
                },
            )
        cursor_id = cursor_uuid(
            payload, detail="Invalid communication templates cursor"
        )
        stmt = stmt.where(
            or_(
                CommunicationTemplate.name > cursor_name,
                and_(
                    CommunicationTemplate.name == cursor_name,
                    CommunicationTemplate.id > cursor_id,
                ),
            )
        )
    templates = list(
        (
            await db.execute(
                stmt.order_by(
                    CommunicationTemplate.name.asc(),
                    CommunicationTemplate.id.asc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(templates) > limit
    templates = templates[:limit]
    return CursorPage(
        items=[
            CommunicationTemplateOut.model_validate(template) for template in templates
        ],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={"name": templates[-1].name, "id": str(templates[-1].id)},
            )
            if has_more and templates
            else None
        ),
    )


@router.post(
    "/communications/templates",
    response_model=CommunicationTemplateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(current_user)],
)
async def create_communication_template(
    body: CommunicationTemplateCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationTemplateOut:
    try:
        validate_template_text(body.subject_template)
        validate_template_text(body.body_template)
    except CommunicationPreparationError as exc:
        raise _preparation_http_error(exc) from exc
    template = CommunicationTemplate(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        **body.model_dump(),
    )
    db.add(template)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A template with this name already exists"
        ) from exc
    await db.refresh(template)
    return CommunicationTemplateOut.model_validate(template)


@router.patch(
    "/communications/templates/{template_id}",
    response_model=CommunicationTemplateOut,
    dependencies=[Depends(current_user)],
)
async def update_communication_template(
    template_id: UUID,
    body: CommunicationTemplateUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationTemplateOut:
    template = (
        await db.execute(
            select(CommunicationTemplate).where(
                CommunicationTemplate.id == template_id,
                CommunicationTemplate.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    values = body.model_dump(exclude_unset=True)
    subject = values.get("subject_template", template.subject_template)
    content = values.get("body_template", template.body_template)
    try:
        validate_template_text(subject)
        validate_template_text(content)
    except CommunicationPreparationError as exc:
        raise _preparation_http_error(exc) from exc
    for field, value in values.items():
        setattr(template, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A template with this name already exists"
        ) from exc
    await db.refresh(template)
    return CommunicationTemplateOut.model_validate(template)


@router.post(
    "/candidates/{candidate_id}/communications/preview",
    response_model=CandidateCommunicationPreviewOut,
    dependencies=[Depends(current_user)],
)
async def preview_candidate_communication(
    candidate_id: UUID,
    body: CandidateCommunicationCompose,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CandidateCommunicationPreviewOut:
    if user.role == "interviewer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    candidate = await _canonical_candidate(db, candidate_id, user)
    try:
        prepared = await prepare_candidate_communication(
            db,
            organization_id=user.organization_id,
            actor=user,
            candidate=candidate,
            request=body,
        )
    except CommunicationPreparationError as exc:
        raise _preparation_http_error(exc) from exc
    return prepared.preview()


@router.post(
    "/candidates/{candidate_id}/communications/send",
    response_model=CommunicationMessageOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(current_user)],
)
async def send_candidate_communication(
    candidate_id: UUID,
    body: CandidateCommunicationSend,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationMessageOut:
    if user.role == "interviewer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    candidate = await _canonical_candidate(db, candidate_id, user)
    try:
        prepared = await prepare_candidate_communication(
            db,
            organization_id=user.organization_id,
            actor=user,
            candidate=candidate,
            request=body,
        )
    except CommunicationPreparationError as exc:
        raise _preparation_http_error(exc) from exc
    message, _created = await create_outbound_message(
        db,
        prepared=prepared,
        actor_user_id=user.id,
        idempotency_key=body.idempotency_key,
        scheduled_for=body.scheduled_for,
    )
    await db.commit()
    persisted = await db.get(CommunicationMessage, message.id)
    return CommunicationMessageOut.model_validate(persisted)


@router.post(
    "/communications/messages/{message_id}/cancel",
    response_model=CommunicationMessageOut,
    dependencies=[Depends(current_user)],
)
async def cancel_communication_message(
    message_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationMessageOut:
    message = await cancel_outbound_message(
        db,
        organization_id=user.organization_id,
        actor_user_id=user.id,
        message_id=message_id,
    )
    return CommunicationMessageOut.model_validate(message)


@router.get(
    "/candidates/{candidate_id}/communication-threads",
    response_model=CursorPage[CommunicationThreadOut],
    dependencies=[Depends(current_user)],
)
async def list_candidate_communication_threads(
    candidate_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=32, max_length=2_048)] = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CursorPage[CommunicationThreadOut]:
    candidate = await _canonical_candidate(db, candidate_id, user)
    sort_at = func.coalesce(
        CommunicationThread.last_message_at, CommunicationThread.created_at
    )
    namespace = "communications.candidate-threads"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"candidate_id": str(candidate.id)},
    )
    stmt = select(CommunicationThread).where(
        CommunicationThread.organization_id == user.organization_id,
        CommunicationThread.candidate_id == candidate.id,
    )
    if cursor is not None:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"sort_at", "id"},
            detail="Invalid candidate communication threads cursor",
        )
        cursor_time = cursor_datetime(
            payload, "sort_at", detail="Invalid candidate communication threads cursor"
        )
        cursor_id = cursor_uuid(
            payload, detail="Invalid candidate communication threads cursor"
        )
        stmt = stmt.where(
            or_(
                sort_at < cursor_time,
                and_(sort_at == cursor_time, CommunicationThread.id < cursor_id),
            )
        )
    threads = list(
        (
            await db.execute(
                stmt.order_by(sort_at.desc(), CommunicationThread.id.desc()).limit(
                    limit + 1
                )
            )
        ).scalars()
    )
    has_more = len(threads) > limit
    threads = threads[:limit]
    latest = await _latest_messages(
        db, [thread.id for thread in threads], user.organization_id
    )
    items = [
        CommunicationThreadOut(
            id=thread.id,
            candidate_id=thread.candidate_id,
            application_id=thread.application_id,
            contact_point_id=thread.contact_point_id,
            account_id=thread.account_id,
            position_id=thread.position_id,
            channel=thread.channel,
            status=thread.status,
            last_message_at=thread.last_message_at,
            unread_count=thread.unread_count,
            created_at=thread.created_at,
            latest_message=(
                CommunicationMessageOut.model_validate(latest[thread.id])
                if thread.id in latest
                else None
            ),
        )
        for thread in threads
    ]
    last = threads[-1] if threads else None
    return CursorPage(
        items=items,
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "sort_at": (last.last_message_at or last.created_at)
                    .astimezone(UTC)
                    .isoformat(),
                    "id": str(last.id),
                },
            )
            if has_more and last is not None
            else None
        ),
    )


@router.get(
    "/communications/threads",
    response_model=CommunicationThreadPage,
    dependencies=[Depends(current_user)],
)
async def list_communication_inbox(
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    channel: Literal["email", "telegram", "whatsapp"] | None = None,
    unread: bool | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationThreadPage:
    sort_at = func.coalesce(
        CommunicationThread.last_message_at, CommunicationThread.created_at
    )
    stmt = (
        select(CommunicationThread, Candidate)
        .join(Candidate, Candidate.id == CommunicationThread.candidate_id)
        .where(
            CommunicationThread.organization_id == user.organization_id,
            Candidate.organization_id == user.organization_id,
            Candidate.merged_at.is_(None),
        )
    )
    if channel is not None:
        stmt = stmt.where(CommunicationThread.channel == channel)
    if unread is True:
        stmt = stmt.where(CommunicationThread.unread_count > 0)
    elif unread is False:
        stmt = stmt.where(CommunicationThread.unread_count == 0)
    namespace = "communications.inbox"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"channel": channel, "unread": unread},
    )
    if cursor:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"sort_at", "id"},
            detail="Invalid communication inbox cursor",
        )
        cursor_time = cursor_datetime(
            payload, "sort_at", detail="Invalid communication inbox cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid communication inbox cursor")
        stmt = stmt.where(
            or_(
                sort_at < cursor_time,
                and_(sort_at == cursor_time, CommunicationThread.id < cursor_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(sort_at.desc(), CommunicationThread.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    latest = await _latest_messages(
        db, [thread.id for thread, _candidate in page_rows], user.organization_id
    )
    items = [
        CommunicationInboxThreadOut(
            id=thread.id,
            candidate_id=thread.candidate_id,
            application_id=thread.application_id,
            contact_point_id=thread.contact_point_id,
            account_id=thread.account_id,
            position_id=thread.position_id,
            channel=thread.channel,
            status=thread.status,
            last_message_at=thread.last_message_at,
            unread_count=thread.unread_count,
            created_at=thread.created_at,
            latest_message=(
                CommunicationMessageOut.model_validate(latest[thread.id])
                if thread.id in latest
                else None
            ),
            candidate_source=candidate.source,
            candidate_source_ref=candidate.source_ref,
        )
        for thread, candidate in page_rows
    ]
    next_cursor = None
    if has_more and page_rows:
        last_thread = page_rows[-1][0]
        next_cursor = encode_collection_cursor(
            namespace,
            scope=scope,
            boundary={
                "sort_at": (last_thread.last_message_at or last_thread.created_at)
                .astimezone(UTC)
                .isoformat(),
                "id": str(last_thread.id),
            },
        )
    return CommunicationThreadPage(items=items, next_cursor=next_cursor)


@router.post(
    "/communications/threads/{thread_id}/read",
    response_model=CommunicationThreadOut,
    dependencies=[Depends(current_user)],
)
async def mark_communication_thread_read(
    thread_id: UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationThreadOut:
    thread = (
        await db.execute(
            select(CommunicationThread).where(
                CommunicationThread.id == thread_id,
                CommunicationThread.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    if thread.unread_count:
        thread.unread_count = 0
        await db.commit()
        await db.refresh(thread)
    latest = await _latest_messages(db, [thread.id], user.organization_id)
    return CommunicationThreadOut(
        id=thread.id,
        candidate_id=thread.candidate_id,
        application_id=thread.application_id,
        contact_point_id=thread.contact_point_id,
        account_id=thread.account_id,
        position_id=thread.position_id,
        channel=thread.channel,
        status=thread.status,
        last_message_at=thread.last_message_at,
        unread_count=thread.unread_count,
        created_at=thread.created_at,
        latest_message=(
            CommunicationMessageOut.model_validate(latest[thread.id])
            if thread.id in latest
            else None
        ),
    )


@router.get(
    "/communications/threads/{thread_id}/messages",
    response_model=CommunicationMessagePage,
    dependencies=[Depends(current_user)],
)
async def list_communication_messages(
    thread_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> CommunicationMessagePage:
    thread = (
        await db.execute(
            select(CommunicationThread.id).where(
                CommunicationThread.id == thread_id,
                CommunicationThread.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Thread not found")
    namespace = "communications.messages"
    scope = collection_cursor_scope(
        namespace,
        organization_id=user.organization_id,
        filters={"thread_id": str(thread_id)},
    )
    stmt = select(CommunicationMessage).where(
        CommunicationMessage.thread_id == thread_id,
        CommunicationMessage.organization_id == user.organization_id,
    )
    if cursor:
        payload = decode_collection_cursor(
            cursor,
            namespace=namespace,
            scope=scope,
            boundary_keys={"created_at", "id"},
            detail="Invalid communication messages cursor",
        )
        cursor_time = cursor_datetime(
            payload, "created_at", detail="Invalid communication messages cursor"
        )
        cursor_id = cursor_uuid(payload, detail="Invalid communication messages cursor")
        stmt = stmt.where(
            or_(
                CommunicationMessage.created_at < cursor_time,
                and_(
                    CommunicationMessage.created_at == cursor_time,
                    CommunicationMessage.id < cursor_id,
                ),
            )
        )
    messages = list(
        (
            await db.execute(
                stmt.order_by(
                    CommunicationMessage.created_at.desc(),
                    CommunicationMessage.id.desc(),
                ).limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(messages) > limit
    page = messages[:limit]
    return CommunicationMessagePage(
        items=[CommunicationMessageOut.model_validate(message) for message in page],
        next_cursor=(
            encode_collection_cursor(
                namespace,
                scope=scope,
                boundary={
                    "created_at": page[-1].created_at.astimezone(UTC).isoformat(),
                    "id": str(page[-1].id),
                },
            )
            if has_more and page
            else None
        ),
    )


async def _bulk_previews(
    body: BulkCommunicationPreviewRequest,
    *,
    db: AsyncSession,
    user: User,
) -> tuple[
    list[BulkCommunicationPreviewItem],
    list[tuple[Candidate, PreparedCommunication]],
]:
    candidates: list[Candidate] = []
    seen: set[UUID] = set()
    for candidate_id in body.candidate_ids:
        candidate = await _canonical_candidate(db, candidate_id, user)
        if candidate.id not in seen:
            candidates.append(candidate)
            seen.add(candidate.id)
    compose = CandidateCommunicationCompose(
        channel=body.channel,
        account_id=body.account_id,
        template_id=body.template_id,
        position_id=body.position_id,
        application_id=body.application_id,
        subject=body.subject,
        body=body.body,
    )
    items: list[BulkCommunicationPreviewItem] = []
    ready: list[tuple[Candidate, PreparedCommunication]] = []
    for candidate in candidates:
        try:
            prepared = await prepare_candidate_communication(
                db,
                organization_id=user.organization_id,
                actor=user,
                candidate=candidate,
                request=compose,
            )
        except CommunicationPreparationError as exc:
            if exc.code not in {"missing_contact", "opted_out"}:
                raise _preparation_http_error(exc) from exc
            items.append(
                BulkCommunicationPreviewItem(
                    candidate_id=candidate.id,
                    status=exc.code,
                    preview=None,
                )
            )
            continue
        items.append(
            BulkCommunicationPreviewItem(
                candidate_id=candidate.id,
                status="ready",
                preview=prepared.preview(),
            )
        )
        ready.append((candidate, prepared))
    return items, ready


@router.post(
    "/communications/bulk/preview",
    response_model=BulkCommunicationPreviewOut,
    dependencies=[Depends(current_user)],
)
async def preview_bulk_communication(
    body: BulkCommunicationPreviewRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> BulkCommunicationPreviewOut:
    if user.role == "interviewer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    recipient_limit = _synchronous_recipient_limit()
    if len(body.candidate_ids) > recipient_limit:
        return BulkCommunicationPreviewOut(
            items=[],
            ready_count=0,
            skipped_count=0,
            requested_count=len(body.candidate_ids),
            complete=False,
            synchronous_recipient_limit=recipient_limit,
        )
    items, ready = await _bulk_previews(body, db=db, user=user)
    return BulkCommunicationPreviewOut(
        items=items,
        ready_count=len(ready),
        skipped_count=len(items) - len(ready),
        requested_count=len(body.candidate_ids),
        complete=True,
        synchronous_recipient_limit=recipient_limit,
    )


async def _existing_batch_out(
    db: AsyncSession, batch: CommunicationBatch
) -> BulkCommunicationCreateOut:
    messages = list(
        (
            await db.execute(
                select(CommunicationMessage)
                .where(CommunicationMessage.batch_id == batch.id)
                .order_by(CommunicationMessage.created_at, CommunicationMessage.id)
            )
        ).scalars()
    )
    skipped = [
        BulkCommunicationPreviewItem(
            candidate_id=UUID(entry["candidate_id"]),
            status=entry["status"],
            preview=None,
        )
        for entry in (batch.skip_summary or [])
    ]
    truncate_messages = batch.requested_count > _synchronous_recipient_limit()
    return BulkCommunicationCreateOut(
        batch=CommunicationBatchOut.model_validate(batch),
        messages=(
            []
            if truncate_messages
            else [
                CommunicationMessageOut.model_validate(message) for message in messages
            ]
        ),
        skipped=skipped,
        created=False,
        queued_count=len(messages),
        messages_truncated=truncate_messages,
    )


async def _validate_existing_batch_reuse(
    db: AsyncSession,
    *,
    batch: CommunicationBatch,
    body: BulkCommunicationCreateRequest,
    user: User,
) -> None:
    """Reject reuse after the recipient or application-cycle target changes."""

    requested_ids: set[UUID] = set()
    for candidate_id in body.candidate_ids:
        requested_ids.add((await _canonical_candidate(db, candidate_id, user)).id)

    ready_rows = list(
        (
            await db.execute(
                select(
                    CommunicationMessage.candidate_id,
                    CommunicationThread.application_id,
                )
                .join(
                    CommunicationThread,
                    CommunicationThread.id == CommunicationMessage.thread_id,
                )
                .where(CommunicationMessage.batch_id == batch.id)
            )
        ).all()
    )
    persisted_ids = {candidate_id for candidate_id, _app_id in ready_rows}
    for entry in batch.skip_summary or []:
        candidate = await resolve_canonical_candidate(
            db, UUID(entry["candidate_id"]), user.organization_id
        )
        if candidate is not None:
            persisted_ids.add(candidate.id)

    conflict = (
        requested_ids != persisted_ids
        or batch.position_id != body.position_id
        or batch.channel != body.channel
        or (
            body.account_id is not None
            and batch.account_id != body.account_id
        )
        or batch.template_id != body.template_id
        or batch.scheduled_for != body.scheduled_for
    )
    if conflict:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Idempotency key is already used for another bulk communication.",
        )

    if body.position_id is None:
        if any(application_id is not None for _candidate_id, application_id in ready_rows):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency key is already used for another application.",
            )
        return

    for candidate_id, persisted_application_id in ready_rows:
        try:
            current_application = await resolve_existing_active_application(
                db,
                organization_id=user.organization_id,
                candidate_id=candidate_id,
                position_id=body.position_id,
                application_id=body.application_id,
            )
        except HTTPException as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency key is already used for another application.",
            ) from exc
        if current_application.id != persisted_application_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency key is already used for another application.",
            )


@router.post(
    "/communications/bulk",
    response_model=BulkCommunicationCreateOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(current_user)],
)
async def create_bulk_communication(
    body: BulkCommunicationCreateRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
) -> BulkCommunicationCreateOut:
    if user.role == "interviewer":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
    existing = (
        await db.execute(
            select(CommunicationBatch).where(
                CommunicationBatch.organization_id == user.organization_id,
                CommunicationBatch.idempotency_key == body.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        await _validate_existing_batch_reuse(
            db, batch=existing, body=body, user=user
        )
        return await _existing_batch_out(db, existing)
    items, ready = await _bulk_previews(body, db=db, user=user)
    if not ready:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "no_recipients",
                "message": "No candidate has an eligible contact.",
            },
        )
    skipped = [item for item in items if item.status != "ready"]
    first_prepared = ready[0][1]
    batch = CommunicationBatch(
        organization_id=user.organization_id,
        created_by_user_id=user.id,
        account_id=first_prepared.account.id,
        template_id=first_prepared.template.id if first_prepared.template else None,
        position_id=first_prepared.position.id if first_prepared.position else None,
        channel=body.channel,
        status="queued",
        idempotency_key=body.idempotency_key,
        scheduled_for=body.scheduled_for,
        total_count=len(ready),
        requested_count=len(items),
        skipped_count=len(skipped),
        skip_summary=[
            {"candidate_id": str(item.candidate_id), "status": item.status}
            for item in skipped
        ],
    )
    db.add(batch)
    try:
        await db.flush()
        messages: list[CommunicationMessage] = []
        for candidate, prepared in ready:
            message, _created = await create_outbound_message(
                db,
                prepared=prepared,
                actor_user_id=user.id,
                idempotency_key=f"batch:{batch.id}:{candidate.id}",
                scheduled_for=body.scheduled_for,
                batch_id=batch.id,
            )
            messages.append(message)
        batch_id = batch.id
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(
                select(CommunicationBatch).where(
                    CommunicationBatch.organization_id == user.organization_id,
                    CommunicationBatch.idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        await _validate_existing_batch_reuse(
            db, batch=existing, body=body, user=user
        )
        return await _existing_batch_out(db, existing)

    persisted_batch = await db.get(CommunicationBatch, batch_id)
    persisted_messages = list(
        (
            await db.execute(
                select(CommunicationMessage)
                .where(CommunicationMessage.batch_id == batch_id)
                .order_by(CommunicationMessage.created_at, CommunicationMessage.id)
            )
        ).scalars()
    )
    truncate_messages = persisted_batch.requested_count > _synchronous_recipient_limit()
    return BulkCommunicationCreateOut(
        batch=CommunicationBatchOut.model_validate(persisted_batch),
        messages=(
            []
            if truncate_messages
            else [
                CommunicationMessageOut.model_validate(message)
                for message in persisted_messages
            ]
        ),
        skipped=skipped,
        created=True,
        queued_count=len(persisted_messages),
        messages_truncated=truncate_messages,
    )


@router.post(
    "/communications/webhooks/sandbox/{account_id}",
    response_model=SandboxInboundWebhookOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": WebhookEnvelope.model_json_schema()}
            },
        }
    },
)
async def simulate_sandbox_inbound(
    account_id: UUID,
    request: Request,
    webhook_secret: Annotated[str | None, Header(alias="X-Webhook-Secret")] = None,
    webhook_timestamp: Annotated[
        str | None, Header(alias="X-HR-Webhook-Timestamp")
    ] = None,
    webhook_signature: Annotated[
        str | None, Header(alias="X-HR-Webhook-Signature")
    ] = None,
    db: AsyncSession = Depends(get_session),
) -> SandboxInboundWebhookOut:
    # Authenticate the exact bounded bytes before JSON parsing. Missing,
    # expired, or invalid credentials deliberately share one non-enumerating
    # response.
    raw_body = await _bounded_request_body(request, 256_000)
    if forced_tenant_rls():
        bootstrap = await bootstrap_resource(
            db, kind="communication_account", resource_id=account_id
        )
        if bootstrap is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    account = (
        await db.execute(
            select(CommunicationAccount).where(
                CommunicationAccount.id == account_id,
                CommunicationAccount.provider == "sandbox",
                CommunicationAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if (
        account is None
        or account.webhook_secret_hash is None
        or not webhook_secret
        or not webhook_timestamp
        or not webhook_signature
        or not hmac.compare_digest(
            account.webhook_secret_hash, hash_webhook_secret(webhook_secret)
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
        envelope = WebhookEnvelope.model_validate_json(raw_body)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_envelope_invalid"},
        ) from exc
    now = datetime.now(UTC)
    if envelope.version != WEBHOOK_VERSION:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_version_unsupported"},
        )
    if envelope.type != "communication.message.received":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_event_type_unsupported"},
        )
    if envelope.created_at > now + timedelta(
        minutes=5
    ) or envelope.created_at < now - timedelta(days=30):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_event_time_invalid"},
        )
    try:
        body = SandboxInboundRequest.model_validate(envelope.data)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "webhook_message_invalid"},
        ) from exc
    try:
        normalized = normalize_contact(account.channel, body.from_value)
    except CommunicationPreparationError as exc:
        raise _preparation_http_error(exc) from exc
    contact_stmt = select(CandidateContactPoint).where(
        CandidateContactPoint.organization_id == account.organization_id,
        CandidateContactPoint.channel == account.channel,
        CandidateContactPoint.normalized_value == normalized,
    )
    if body.candidate_id is not None:
        candidate = await resolve_canonical_candidate(
            db, body.candidate_id, account.organization_id
        )
        if candidate is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found")
        contact_stmt = contact_stmt.where(
            CandidateContactPoint.candidate_id == candidate.id
        )
        contact = (await db.execute(contact_stmt)).scalar_one_or_none()
    else:
        contacts = list((await db.execute(contact_stmt.limit(2))).scalars())
        if len(contacts) > 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Multiple candidates match this contact; provide candidate_id.",
            )
        contact = contacts[0] if contacts else None
        candidate = (
            await resolve_canonical_candidate(
                db, contact.candidate_id, account.organization_id
            )
            if contact
            else None
        )
    if contact is None or candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate contact not found")
    try:
        message, replay = await ingest_sandbox_inbound(
            db,
            account=account,
            candidate=candidate,
            contact=contact,
            webhook_event_id=envelope.id,
            webhook_payload_sha256=hashlib.sha256(raw_body).hexdigest(),
            provider_message_id=body.provider_message_id,
            subject=body.subject,
            body=body.body,
            received_at=body.received_at,
        )
    except CommunicationWebhookConflict as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": exc.code},
        ) from exc
    return SandboxInboundWebhookOut(
        **CommunicationMessageOut.model_validate(message).model_dump(),
        webhook_event_id=envelope.id,
        replay=replay,
    )
