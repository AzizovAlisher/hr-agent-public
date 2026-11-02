"""Candidate communication preparation, persistence, and delivery queue."""

import hashlib
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from string import Formatter
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import FieldClass
from app.models.candidate import Candidate
from app.models.application import Application
from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationBatch,
    CommunicationMessage,
    CommunicationTemplate,
    CommunicationThread,
)
from app.models.integration import IntegrationConnection
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.schemas.communications import (
    CandidateCommunicationCompose,
    CandidateCommunicationPreviewOut,
    CommunicationTargetOut,
)
from app.services.activities import append_candidate_activity
from app.services.communication_providers import (
    ProviderResult,
    deliver_connected_message,
    deliver_message,
)
from app.services.integrations.crypto import IntegrationCipher
from app.services.enterprise import can_access_field, has_permission
from app.services.structured_hiring import resolve_existing_active_application

ALLOWED_TEMPLATE_VARIABLES = frozenset(
    {
        "candidate.first_name",
        "candidate.full_name",
        "candidate.source",
        "position.title",
        "recruiter.email",
        "organization.name",
    }
)
MESSAGE_CHANNELS = frozenset({"email", "telegram", "whatsapp"})
CREATABLE_MESSAGE_CHANNELS = frozenset({"email", "telegram"})
_SAFE_FAILURE_CODES = frozenset(
    {
        "connection_not_ready",
        "credential_encryption_unavailable",
        "credentials_not_configured",
        "credentials_unavailable",
        "delivery_outcome_unknown",
        "provider_auth_rejected",
        "provider_channel_mismatch",
        "provider_configuration_missing",
        "provider_credentials_missing",
        "provider_rate_limited",
        "provider_request_rejected",
        "provider_response_invalid",
        "provider_token_expired",
        "provider_unavailable",
        "provider_unconfigured",
        "provider_unsupported",
        "contact_unavailable",
        "authorization_revoked",
    }
)


def _advisory_key(namespace: str, *parts: object) -> int:
    value = ":".join((namespace, *(str(part) for part in parts)))
    return int.from_bytes(
        hashlib.sha256(value.encode()).digest()[:8], byteorder="big", signed=True
    )


class CommunicationPreparationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class CommunicationWebhookConflict(RuntimeError):
    """A signed webhook event or provider message was reused with new data."""

    code = "webhook_event_conflict"


@dataclass(frozen=True)
class PreparedCommunication:
    candidate: Candidate
    contact: CandidateContactPoint
    account: CommunicationAccount
    template: CommunicationTemplate | None
    position: Position | None
    application: Application | None
    subject: str | None
    body: str

    def preview(self) -> CandidateCommunicationPreviewOut:
        return CandidateCommunicationPreviewOut(
            candidate_id=self.candidate.id,
            position_id=self.position.id if self.position else None,
            application_id=self.application.id if self.application else None,
            template_id=self.template.id if self.template else None,
            target=CommunicationTargetOut(
                contact_point_id=self.contact.id,
                channel=self.account.channel,
                masked_value=mask_contact(self.contact.channel, self.contact.value),
                account_id=self.account.id,
                account_name=self.account.name,
                provider=self.account.provider,
            ),
            subject=self.subject,
            body=self.body,
        )


def hash_webhook_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_webhook_secret() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_webhook_secret(raw)


def normalize_contact(channel: str, value: str) -> str:
    value = value.strip()
    if channel == "email":
        normalized = value.casefold()
        if (
            len(normalized) > 320
            or normalized.count("@") != 1
            or any(character.isspace() for character in normalized)
        ):
            raise CommunicationPreparationError(
                "invalid_contact", "Invalid email address"
            )
        local, domain = normalized.split("@", 1)
        if (
            not local
            or "." not in domain
            or domain.startswith(".")
            or domain.endswith(".")
        ):
            raise CommunicationPreparationError(
                "invalid_contact", "Invalid email address"
            )
        return normalized
    if channel in {"phone", "whatsapp"}:
        digits = re.sub(r"\D", "", value)
        if not 7 <= len(digits) <= 15:
            raise CommunicationPreparationError(
                "invalid_contact", "Invalid phone number"
            )
        return f"+{digits}"
    if channel == "telegram":
        normalized = (
            re.sub(
                r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/",
                "",
                value,
                flags=re.IGNORECASE,
            )
            .lstrip("@")
            .casefold()
        )
        if not re.fullmatch(r"[a-z0-9_]{5,32}", normalized):
            raise CommunicationPreparationError(
                "invalid_contact", "Invalid Telegram username"
            )
        return normalized
    raise CommunicationPreparationError(
        "invalid_channel", "Unsupported contact channel"
    )


def mask_contact(channel: str, value: str) -> str:
    if channel == "email" and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1]}***@{domain}"
    if channel in {"phone", "whatsapp"}:
        normalized = normalize_contact(channel, value)
        return f"***{normalized[-4:]}"
    if channel == "telegram":
        normalized = normalize_contact(channel, value)
        return f"@{normalized[:1]}***"
    return "***"


def validate_template_text(value: str | None) -> None:
    if value is None:
        return
    try:
        parsed = list(Formatter().parse(value))
    except ValueError as exc:
        raise CommunicationPreparationError(
            "invalid_template", "Invalid template braces"
        ) from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in ALLOWED_TEMPLATE_VARIABLES:
            raise CommunicationPreparationError(
                "invalid_template_variable",
                f"Unsupported template variable: {field_name}",
            )
        if format_spec or conversion:
            raise CommunicationPreparationError(
                "invalid_template",
                "Template conversions and format specs are not allowed",
            )


def render_template(value: str | None, variables: dict[str, str]) -> str | None:
    if value is None:
        return None
    validate_template_text(value)
    rendered = value
    for key in ALLOWED_TEMPLATE_VARIABLES:
        rendered = rendered.replace("{" + key + "}", variables.get(key, ""))
    return rendered.replace("{{", "{").replace("}}", "}")


def _candidate_name(candidate: Candidate) -> str:
    parsed = candidate.parsed_cv or {}
    values: list[str] = []
    if isinstance(parsed, dict):
        for key in ("full_name", "name"):
            if isinstance(parsed.get(key), str):
                values.append(parsed[key])
        first = parsed.get("first_name")
        last = parsed.get("last_name")
        if isinstance(first, str) or isinstance(last, str):
            values.append(
                " ".join(part for part in (first, last) if isinstance(part, str))
            )
        for container_key in ("personal_information", "personal", "profile"):
            nested = parsed.get(container_key)
            if isinstance(nested, dict):
                for key in ("full_name", "name"):
                    if isinstance(nested.get(key), str):
                        values.append(nested[key])
    for value in values:
        cleaned = " ".join(value.split())
        if cleaned:
            return cleaned[:200]
    return "Candidate"


async def ensure_default_sandbox_account(
    db: AsyncSession,
    *,
    organization_id: UUID,
    channel: str,
    created_by_user_id: UUID | None = None,
) -> CommunicationAccount:
    if channel not in CREATABLE_MESSAGE_CHANNELS:
        raise CommunicationPreparationError(
            "capability_unavailable", "Outbound channel is unavailable"
        )
    account = (
        await db.execute(
            select(CommunicationAccount)
            .where(
                CommunicationAccount.organization_id == organization_id,
                CommunicationAccount.channel == channel,
                CommunicationAccount.provider == "sandbox",
                CommunicationAccount.is_active.is_(True),
            )
            .order_by(CommunicationAccount.created_at, CommunicationAccount.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if account is not None:
        return account

    # Serialize lazy creation per tenant/channel. Advisory transaction locks
    # avoid an exception-driven rollback and work with the functional
    # case-insensitive name index.
    lock_key = _advisory_key("sandbox-account", organization_id, channel)
    await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    account = (
        await db.execute(
            select(CommunicationAccount)
            .where(
                CommunicationAccount.organization_id == organization_id,
                CommunicationAccount.channel == channel,
                CommunicationAccount.provider == "sandbox",
                CommunicationAccount.is_active.is_(True),
            )
            .order_by(CommunicationAccount.created_at, CommunicationAccount.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if account is not None:
        return account
    account = CommunicationAccount(
        organization_id=organization_id,
        created_by_user_id=created_by_user_id,
        channel=channel,
        provider="sandbox",
        name="Local sandbox",
        configuration={"mode": "local"},
        is_active=True,
    )
    db.add(account)
    await db.flush()
    return account


async def communication_account_available(
    db: AsyncSession, account: CommunicationAccount
) -> bool:
    """Return the authoritative delivery capability for one tenant account."""

    if not account.is_active:
        return False
    if account.provider == "sandbox":
        return account.channel in {"email", "telegram"}
    expected = {
        "google_workspace": ("email", "email.send"),
        "microsoft_365": ("email", "email.send"),
        "telegram": ("telegram", "message.telegram.send"),
    }.get(account.provider)
    if expected is None or account.integration_connection_id is None:
        return False
    channel, capability = expected
    if account.channel != channel:
        return False
    connection = (
        await db.execute(
            select(IntegrationConnection).where(
                IntegrationConnection.id == account.integration_connection_id,
                IntegrationConnection.organization_id == account.organization_id,
                IntegrationConnection.provider == account.provider,
            )
        )
    ).scalar_one_or_none()
    return bool(
        connection is not None
        and connection.status == "connected"
        and capability in (connection.capabilities or [])
    )


async def prepare_candidate_communication(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor: User | None,
    candidate: Candidate,
    request: CandidateCommunicationCompose,
) -> PreparedCommunication:
    if request.channel == "whatsapp":
        raise CommunicationPreparationError(
            "capability_unavailable",
            "WhatsApp delivery is not available in this pilot",
        )
    if request.contact_point_id is not None:
        contact = (
            await db.execute(
                select(CandidateContactPoint).where(
                    CandidateContactPoint.id == request.contact_point_id,
                    CandidateContactPoint.organization_id == organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                    CandidateContactPoint.channel == request.channel,
                )
            )
        ).scalar_one_or_none()
    else:
        contact = (
            await db.execute(
                select(CandidateContactPoint)
                .where(
                    CandidateContactPoint.organization_id == organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                    CandidateContactPoint.channel == request.channel,
                )
                .order_by(
                    CandidateContactPoint.is_opted_out.asc(),
                    CandidateContactPoint.is_primary.desc(),
                    CandidateContactPoint.created_at,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if contact is None:
        raise CommunicationPreparationError(
            "missing_contact", "Candidate has no contact for this channel"
        )
    if contact.is_opted_out:
        raise CommunicationPreparationError(
            "opted_out", "Candidate has opted out on this contact"
        )

    if request.account_id is not None:
        account = (
            await db.execute(
                select(CommunicationAccount).where(
                    CommunicationAccount.id == request.account_id,
                    CommunicationAccount.organization_id == organization_id,
                    CommunicationAccount.channel == request.channel,
                    CommunicationAccount.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise CommunicationPreparationError(
                "account_unavailable", "Communication account not found or inactive"
            )
        if not await communication_account_available(db, account):
            raise CommunicationPreparationError(
                "account_unavailable", "Communication account is not connected"
            )
    else:
        # Prefer a configured real account; the sandbox remains the guaranteed
        # zero-credential fallback for the local mini application.
        candidates = list(
            (
                await db.execute(
                    select(CommunicationAccount)
                    .where(
                        CommunicationAccount.organization_id == organization_id,
                        CommunicationAccount.channel == request.channel,
                        CommunicationAccount.is_active.is_(True),
                    )
                    .order_by(
                        case((CommunicationAccount.provider == "sandbox", 1), else_=0),
                        CommunicationAccount.created_at,
                    )
                )
            ).scalars()
        )
        account = None
        for candidate_account in candidates:
            if await communication_account_available(db, candidate_account):
                account = candidate_account
                break
        if account is None:
            account = await ensure_default_sandbox_account(
                db,
                organization_id=organization_id,
                channel=request.channel,
                created_by_user_id=actor.id if actor is not None else None,
            )

    template: CommunicationTemplate | None = None
    if request.template_id is not None:
        template = (
            await db.execute(
                select(CommunicationTemplate).where(
                    CommunicationTemplate.id == request.template_id,
                    CommunicationTemplate.organization_id == organization_id,
                    CommunicationTemplate.channel == request.channel,
                    CommunicationTemplate.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if template is None:
            raise CommunicationPreparationError(
                "template_unavailable", "Template not found or inactive"
            )

    position: Position | None = None
    application: Application | None = None
    if request.position_id is not None:
        position = (
            await db.execute(
                select(Position).where(
                    Position.id == request.position_id,
                    Position.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if position is None:
            raise CommunicationPreparationError(
                "position_missing", "Position not found"
            )
        try:
            application = await resolve_existing_active_application(
                db,
                organization_id=organization_id,
                candidate_id=candidate.id,
                position_id=position.id,
                application_id=request.application_id,
            )
        except HTTPException as exc:
            raise CommunicationPreparationError(
                "application_unavailable",
                "Active application not found for this candidate and position",
            ) from exc
    elif request.application_id is not None:
        raise CommunicationPreparationError(
            "application_unavailable", "application_id requires position_id"
        )

    organization = await db.get(Organization, organization_id)
    full_name = _candidate_name(candidate)
    variables = {
        "candidate.full_name": full_name,
        "candidate.first_name": full_name.split(maxsplit=1)[0],
        "candidate.source": candidate.source,
        "position.title": position.title if position else "",
        "recruiter.email": actor.email if actor is not None else "",
        "organization.name": organization.name if organization else "",
    }
    subject_source = template.subject_template if template else request.subject
    body_source = template.body_template if template else (request.body or "")
    subject = render_template(subject_source, variables)
    body = render_template(body_source, variables) or ""
    return PreparedCommunication(
        candidate=candidate,
        contact=contact,
        account=account,
        template=template,
        position=position,
        application=application,
        subject=subject,
        body=body,
    )


async def get_or_create_thread(
    db: AsyncSession,
    prepared: PreparedCommunication,
) -> CommunicationThread:
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                _advisory_key(
                    "communication-thread",
                    prepared.candidate.organization_id,
                    prepared.candidate.id,
                    prepared.account.channel,
                    prepared.contact.id,
                    prepared.account.id,
                    prepared.position.id if prepared.position else "general",
                    prepared.application.id if prepared.application else "general",
                )
            )
        )
    )
    thread = (
        await db.execute(
            select(CommunicationThread)
            .where(
                CommunicationThread.organization_id
                == prepared.candidate.organization_id,
                CommunicationThread.candidate_id == prepared.candidate.id,
                CommunicationThread.channel == prepared.account.channel,
                CommunicationThread.contact_point_id == prepared.contact.id,
                CommunicationThread.account_id == prepared.account.id,
                CommunicationThread.position_id
                == (prepared.position.id if prepared.position else None),
                CommunicationThread.application_id
                == (prepared.application.id if prepared.application else None),
                CommunicationThread.status == "open",
            )
            .order_by(CommunicationThread.created_at, CommunicationThread.id)
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if thread is None:
        thread = CommunicationThread(
            organization_id=prepared.candidate.organization_id,
            candidate_id=prepared.candidate.id,
            contact_point_id=prepared.contact.id,
            account_id=prepared.account.id,
            position_id=prepared.position.id if prepared.position else None,
            application_id=(
                prepared.application.id if prepared.application is not None else None
            ),
            channel=prepared.account.channel,
            status="open",
        )
        db.add(thread)
        await db.flush()
    return thread


async def create_outbound_message(
    db: AsyncSession,
    *,
    prepared: PreparedCommunication,
    actor_user_id: UUID | None,
    idempotency_key: str,
    scheduled_for: datetime | None,
    batch_id: UUID | None = None,
) -> tuple[CommunicationMessage, bool]:
    organization_id = prepared.candidate.organization_id
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                _advisory_key("communication-message", organization_id, idempotency_key)
            )
        )
    )
    existing_row = (
        await db.execute(
            select(CommunicationMessage, CommunicationThread.application_id)
            .join(CommunicationThread, CommunicationThread.id == CommunicationMessage.thread_id)
            .where(
                CommunicationMessage.organization_id == organization_id,
                CommunicationMessage.idempotency_key == idempotency_key,
            )
        )
    ).first()
    if existing_row is not None:
        existing, existing_application_id = existing_row
        if existing.candidate_id != prepared.candidate.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency key is already used for another candidate.",
            )
        if existing_application_id != (
            prepared.application.id if prepared.application is not None else None
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency key is already used for another application.",
            )
        return existing, False

    now = datetime.now(UTC)
    due_at = scheduled_for or now
    thread = await get_or_create_thread(db, prepared)
    message = CommunicationMessage(
        organization_id=organization_id,
        thread_id=thread.id,
        candidate_id=prepared.candidate.id,
        contact_point_id=prepared.contact.id,
        account_id=prepared.account.id,
        batch_id=batch_id,
        template_id=prepared.template.id if prepared.template else None,
        actor_user_id=actor_user_id,
        direction="outbound",
        channel=prepared.account.channel,
        status="queued",
        subject=prepared.subject,
        body=prepared.body,
        idempotency_key=idempotency_key,
        scheduled_for=due_at,
    )
    db.add(message)
    await db.flush()
    thread.last_message_at = now
    if due_at > now:
        append_candidate_activity(
            db,
            organization_id=organization_id,
            candidate_id=prepared.candidate.id,
            actor_user_id=actor_user_id,
            application_id=(
                prepared.application.id if prepared.application is not None else None
            ),
            position_id=prepared.position.id if prepared.position else None,
            event_type="message_scheduled",
            subject_type="communication_message",
            subject_id=message.id,
            payload={"channel": message.channel, "status": "queued"},
            occurred_at=now,
        )
    return message, True


async def _notification_user_ids(
    db: AsyncSession, *, organization_id: UUID, candidate_id: UUID
) -> list[UUID]:
    owner_ids = list(
        (
            await db.execute(
                select(Score.owner_user_id)
                .where(
                    Score.organization_id == organization_id,
                    Score.candidate_id == candidate_id,
                    Score.owner_user_id.is_not(None),
                )
                .distinct()
            )
        ).scalars()
    )
    if owner_ids:
        return owner_ids
    return list(
        (
            await db.execute(
                select(User.id).where(User.organization_id == organization_id)
            )
        ).scalars()
    )


async def _add_failure_notification(
    db: AsyncSession, message: CommunicationMessage
) -> None:
    recipients = (
        [message.actor_user_id]
        if message.actor_user_id is not None
        else await _notification_user_ids(
            db,
            organization_id=message.organization_id,
            candidate_id=message.candidate_id,
        )
    )
    for user_id in set(recipients):
        db.add(
            Notification(
                organization_id=message.organization_id,
                user_id=user_id,
                type="communication_failed",
                payload={
                    "candidate_id": str(message.candidate_id),
                    "message_id": str(message.id),
                    "channel": message.channel,
                    "failure_code": message.failure_code,
                },
            )
        )


async def _refresh_batch(db: AsyncSession, batch_id: UUID | None) -> None:
    if batch_id is None:
        return
    batch = (
        await db.execute(
            select(CommunicationBatch)
            .where(CommunicationBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        return
    counts = dict(
        (
            await db.execute(
                select(CommunicationMessage.status, func.count(CommunicationMessage.id))
                .where(CommunicationMessage.batch_id == batch_id)
                .group_by(CommunicationMessage.status)
            )
        ).all()
    )
    batch.sent_count = int(counts.get("sent", 0) + counts.get("delivered", 0))
    batch.failed_count = int(counts.get("failed", 0))
    batch.cancelled_count = int(counts.get("cancelled", 0))
    terminal = batch.sent_count + batch.failed_count + batch.cancelled_count
    if terminal >= batch.total_count:
        batch.completed_at = datetime.now(UTC)
        if batch.sent_count == batch.total_count:
            batch.status = "completed"
        elif batch.failed_count == batch.total_count:
            batch.status = "failed"
        elif batch.cancelled_count == batch.total_count:
            batch.status = "cancelled"
        else:
            batch.status = "partial"
    elif terminal:
        batch.status = "processing"


async def process_claimed_message(
    db: AsyncSession,
    *,
    organization_id: UUID,
    message_id: UUID,
    now: datetime | None = None,
    already_claimed: bool = False,
    expected_claimed_at: datetime | None = None,
    recovered_claim: bool = False,
    cipher: IntegrationCipher | None = None,
    adapters: dict[str, Any] | None = None,
) -> str | None:
    """Process exactly one outbound message without committing.

    Durable workers call this after acquiring the broker lease inside a fresh
    tenant transaction.  The legacy local claim path passes
    ``already_claimed=True`` and the precise claim timestamp so an abandoned
    worker cannot deliver after another replica has refreshed the claim.
    """

    current = now or datetime.now(UTC)
    statement = (
        select(CommunicationMessage)
        .where(
            CommunicationMessage.id == message_id,
            CommunicationMessage.organization_id == organization_id,
            CommunicationMessage.direction == "outbound",
        )
        .with_for_update()
    )
    if already_claimed and expected_claimed_at is not None:
        statement = statement.where(
            CommunicationMessage.claimed_at == expected_claimed_at
        )
    message = (await db.execute(statement)).scalar_one_or_none()
    if message is None:
        return None
    if already_claimed:
        if message.status != "processing":
            return None
    else:
        stale_before = current - timedelta(minutes=5)
        is_due = (
            message.status == "queued"
            and message.scheduled_for is not None
            and message.scheduled_for <= current
        ) or (
            message.status == "processing"
            and message.claimed_at is not None
            and message.claimed_at < stale_before
        )
        if not is_due:
            return None
        message.status = "processing"
        message.claimed_at = current
        message.attempt_count += 1
        await db.flush()

    connection: IntegrationConnection | None = None
    authorization_revoked = False
    if (message.idempotency_key or "").startswith("nurture:"):
        actor = None
        if message.actor_user_id is not None:
            actor = (
                await db.execute(
                    select(User).where(
                        User.id == message.actor_user_id,
                        User.organization_id == message.organization_id,
                        User.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
        authorization_revoked = (
            actor is None
            or not await has_permission(db, actor, "communications.send")
            or not await has_permission(db, actor, "candidates.contact")
            or not await can_access_field(db, actor, FieldClass.CONTACT)
        )
    if recovered_claim:
        # A previous worker may have contacted the provider before crashing.
        # Fail closed instead of replaying a non-idempotent candidate message.
        provider_result = ProviderResult(
            delivered=False,
            failure_code="delivery_outcome_unknown",
        )
        account = None
        contact = None
    elif authorization_revoked:
        provider_result = ProviderResult(
            delivered=False,
            failure_code="authorization_revoked",
        )
        account = None
        contact = None
    else:
        # The durable claim function preserves a safe failure marker only for
        # provider-authorized retries. Clear it inside this delivery
        # transaction; a crash rolls the change back and the next stale claim
        # clears the marker atomically before refusing an ambiguous replay.
        message.failure_code = None
        account = (
            await db.execute(
                select(CommunicationAccount).where(
                    CommunicationAccount.id == message.account_id,
                    CommunicationAccount.organization_id == message.organization_id,
                    CommunicationAccount.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        contact = (
            await db.execute(
                select(CandidateContactPoint).where(
                    CandidateContactPoint.id == message.contact_point_id,
                    CandidateContactPoint.organization_id == message.organization_id,
                    CandidateContactPoint.candidate_id == message.candidate_id,
                    CandidateContactPoint.is_opted_out.is_(False),
                )
            )
        ).scalar_one_or_none()
        provider_result = None
    if recovered_claim:
        failure_code = provider_result.failure_code
    elif authorization_revoked:
        failure_code = "authorization_revoked"
    elif account is None or contact is None:
        failure_code = "contact_unavailable"
    elif account.provider == "sandbox":
        provider_result = await deliver_message(account, message, contact)
        failure_code = provider_result.failure_code
    else:
        if account.integration_connection_id is not None:
            connection = (
                await db.execute(
                    select(IntegrationConnection).where(
                        IntegrationConnection.id == account.integration_connection_id,
                        IntegrationConnection.organization_id
                        == message.organization_id,
                        IntegrationConnection.provider == account.provider,
                    )
                )
            ).scalar_one_or_none()
        adapter = adapters.get(account.provider) if adapters is not None else None
        if connection is None:
            provider_result = ProviderResult(
                delivered=False, failure_code="connection_not_ready"
            )
        elif cipher is None:
            provider_result = ProviderResult(
                delivered=False,
                failure_code="credential_encryption_unavailable",
            )
        elif adapter is None:
            provider_result = ProviderResult(
                delivered=False, failure_code="provider_unsupported"
            )
        else:
            provider_result = await deliver_connected_message(
                connection,
                db=db,
                cipher=cipher,
                adapter=adapter,
                message=message,
                contact=contact,
            )
        failure_code = provider_result.failure_code

    happened_at = datetime.now(UTC)
    thread = await db.get(CommunicationThread, message.thread_id)
    if connection is not None and provider_result is not None:
        if provider_result.delivered:
            connection.status = "connected"
            connection.last_success_at = happened_at
            connection.last_error_code = None
            connection.consecutive_failures = 0
        elif failure_code in {"provider_auth_rejected", "provider_token_expired"}:
            connection.status = "expired"
            connection.last_error_code = failure_code
            connection.consecutive_failures += 1
        elif failure_code in {
            "credentials_not_configured",
            "provider_credentials_missing",
        }:
            connection.status = "disconnected"
            connection.last_error_code = failure_code
            connection.consecutive_failures += 1
        elif failure_code in {
            "delivery_outcome_unknown",
            "provider_rate_limited",
            "provider_request_rejected",
            "provider_response_invalid",
            "provider_unavailable",
        }:
            connection.status = "degraded"
            connection.last_error_code = failure_code
            connection.consecutive_failures += 1
    if provider_result is not None and provider_result.delivered:
        message.status = "sent"
        message.sent_at = happened_at
        message.provider_message_id = provider_result.provider_message_id
        message.failure_code = None
        append_candidate_activity(
            db,
            organization_id=message.organization_id,
            candidate_id=message.candidate_id,
            actor_user_id=message.actor_user_id,
            application_id=thread.application_id if thread is not None else None,
            position_id=thread.position_id if thread is not None else None,
            event_type="message_sent",
            subject_type="communication_message",
            subject_id=message.id,
            payload={"channel": message.channel, "status": "sent"},
            occurred_at=happened_at,
        )
    elif (
        provider_result is not None
        and provider_result.retryable
        and message.attempt_count < 3
    ):
        message.status = "queued"
        message.claimed_at = None
        message.scheduled_for = happened_at + timedelta(
            seconds=min(5 * (2 ** max(message.attempt_count - 1, 0)), 300)
        )
        message.failure_code = (
            failure_code
            if failure_code in _SAFE_FAILURE_CODES
            else "provider_unavailable"
        )
        await _refresh_batch(db, message.batch_id)
        await db.flush()
        return "retrying"
    else:
        message.status = "failed"
        message.failed_at = happened_at
        message.failure_code = (
            failure_code
            if failure_code in _SAFE_FAILURE_CODES
            else "provider_unsupported"
        )
        append_candidate_activity(
            db,
            organization_id=message.organization_id,
            candidate_id=message.candidate_id,
            actor_user_id=message.actor_user_id,
            application_id=thread.application_id if thread is not None else None,
            position_id=thread.position_id if thread is not None else None,
            event_type="message_failed",
            subject_type="communication_message",
            subject_id=message.id,
            payload={
                "channel": message.channel,
                "status": "failed",
                "failure_code": message.failure_code,
            },
            occurred_at=happened_at,
        )
        await _add_failure_notification(db, message)
    if thread is not None:
        thread.last_message_at = happened_at
    await _refresh_batch(db, message.batch_id)
    await db.flush()
    return message.status


async def process_due_messages(
    db: AsyncSession,
    *,
    limit: int = 50,
    organization_id: UUID | None = None,
    message_ids: Iterable[UUID] | None = None,
) -> dict[str, int]:
    """Claim and process due messages with PostgreSQL SKIP LOCKED.

    Claiming is committed before provider work so concurrent workers never hold
    locks during delivery. A stale processing claim becomes eligible after five
    minutes, allowing recovery after a worker crash.
    """

    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    stmt = (
        select(CommunicationMessage)
        .where(
            CommunicationMessage.direction == "outbound",
            or_(
                and_(
                    CommunicationMessage.status == "queued",
                    CommunicationMessage.attempt_count < 3,
                    CommunicationMessage.scheduled_for <= now,
                ),
                and_(
                    CommunicationMessage.status == "processing",
                    CommunicationMessage.attempt_count <= 3,
                    CommunicationMessage.claimed_at < stale_before,
                ),
            ),
        )
        .order_by(
            CommunicationMessage.scheduled_for,
            CommunicationMessage.created_at,
            CommunicationMessage.id,
        )
        .with_for_update(skip_locked=True)
        .limit(max(1, min(limit, 100)))
    )
    if organization_id is not None:
        stmt = stmt.where(CommunicationMessage.organization_id == organization_id)
    requested_ids = set(message_ids or [])
    if requested_ids:
        stmt = stmt.where(CommunicationMessage.id.in_(requested_ids))
    claimed = list((await db.execute(stmt)).scalars())
    recovered_ids = {
        message.id for message in claimed if message.status == "processing"
    }
    for message in claimed:
        message.status = "processing"
        message.claimed_at = now
        message.attempt_count += 1
    await db.commit()

    result_counts = {
        "claimed": len(claimed),
        "sent": 0,
        "retrying": 0,
        "failed": 0,
    }
    for claimed_message in claimed:
        status_value = await process_claimed_message(
            db,
            organization_id=claimed_message.organization_id,
            message_id=claimed_message.id,
            already_claimed=True,
            expected_claimed_at=now,
            recovered_claim=claimed_message.id in recovered_ids,
        )
        if status_value == "sent":
            result_counts["sent"] += 1
        elif status_value == "retrying":
            result_counts["retrying"] += 1
        elif status_value == "failed":
            result_counts["failed"] += 1
        await db.commit()
    return result_counts


async def cancel_outbound_message(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    message_id: UUID,
) -> CommunicationMessage:
    message = (
        await db.execute(
            select(CommunicationMessage)
            .where(
                CommunicationMessage.id == message_id,
                CommunicationMessage.organization_id == organization_id,
                CommunicationMessage.direction == "outbound",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found")
    if message.status == "cancelled":
        return message
    if message.status != "queued":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a queued message can be cancelled."
        )
    happened_at = datetime.now(UTC)
    thread = await db.get(CommunicationThread, message.thread_id)
    message.status = "cancelled"
    message.cancelled_at = happened_at
    append_candidate_activity(
        db,
        organization_id=organization_id,
        candidate_id=message.candidate_id,
        actor_user_id=actor_user_id,
        application_id=thread.application_id if thread is not None else None,
        position_id=thread.position_id if thread is not None else None,
        event_type="message_cancelled",
        subject_type="communication_message",
        subject_id=message.id,
        payload={"channel": message.channel, "status": "cancelled"},
        occurred_at=happened_at,
    )
    await _refresh_batch(db, message.batch_id)
    await db.commit()
    await db.refresh(message)
    return message


async def ingest_sandbox_inbound(
    db: AsyncSession,
    *,
    account: CommunicationAccount,
    candidate: Candidate,
    contact: CandidateContactPoint,
    webhook_event_id: str,
    webhook_payload_sha256: str,
    provider_message_id: str,
    subject: str | None,
    body: str,
    received_at: datetime | None,
) -> tuple[CommunicationMessage, bool]:
    """Persist one signed sandbox webhook exactly once.

    Both the envelope event ID and the upstream provider message ID are
    idempotency boundaries. An exact retry returns the existing message;
    either identifier paired with a different raw payload fails closed.
    """

    lock_keys = {
        _advisory_key("sandbox-inbound-event", account.id, webhook_event_id),
        _advisory_key("sandbox-inbound-provider", account.id, provider_message_id),
    }
    for lock_key in sorted(lock_keys):
        await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    existing_event = (
        await db.execute(
            select(CommunicationMessage).where(
                CommunicationMessage.account_id == account.id,
                CommunicationMessage.webhook_event_id == webhook_event_id,
            )
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        if existing_event.webhook_payload_sha256 != webhook_payload_sha256:
            raise CommunicationWebhookConflict
        return existing_event, True

    existing_provider = (
        await db.execute(
            select(CommunicationMessage).where(
                CommunicationMessage.account_id == account.id,
                CommunicationMessage.provider_message_id == provider_message_id,
            )
        )
    ).scalar_one_or_none()
    if existing_provider is not None:
        if existing_provider.webhook_payload_sha256 != webhook_payload_sha256:
            raise CommunicationWebhookConflict
        return existing_provider, True

    thread = (
        await db.execute(
            select(CommunicationThread)
            .where(
                CommunicationThread.organization_id == account.organization_id,
                CommunicationThread.candidate_id == candidate.id,
                CommunicationThread.channel == account.channel,
                CommunicationThread.contact_point_id == contact.id,
                CommunicationThread.account_id == account.id,
                CommunicationThread.position_id.is_(None),
                CommunicationThread.status == "open",
            )
            .order_by(CommunicationThread.created_at, CommunicationThread.id)
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if thread is None:
        thread = CommunicationThread(
            organization_id=account.organization_id,
            candidate_id=candidate.id,
            contact_point_id=contact.id,
            account_id=account.id,
            channel=account.channel,
            status="open",
        )
        db.add(thread)
        await db.flush()

    happened_at = received_at or datetime.now(UTC)
    message = CommunicationMessage(
        organization_id=account.organization_id,
        thread_id=thread.id,
        candidate_id=candidate.id,
        contact_point_id=contact.id,
        account_id=account.id,
        direction="inbound",
        channel=account.channel,
        status="received",
        subject=subject,
        body=body,
        provider_message_id=provider_message_id,
        webhook_event_id=webhook_event_id,
        webhook_payload_sha256=webhook_payload_sha256,
        idempotency_key=(
            f"inbound:{account.id}:"
            f"{hashlib.sha256(provider_message_id.encode()).hexdigest()}"
        ),
        received_at=happened_at,
    )
    db.add(message)
    await db.flush()
    thread.last_message_at = happened_at
    thread.unread_count += 1
    append_candidate_activity(
        db,
        organization_id=account.organization_id,
        candidate_id=candidate.id,
        event_type="message_received",
        subject_type="communication_message",
        subject_id=message.id,
        payload={"channel": message.channel, "status": "received"},
        occurred_at=happened_at,
    )
    for user_id in set(
        await _notification_user_ids(
            db,
            organization_id=account.organization_id,
            candidate_id=candidate.id,
        )
    ):
        db.add(
            Notification(
                organization_id=account.organization_id,
                user_id=user_id,
                type="communication_received",
                payload={
                    "candidate_id": str(candidate.id),
                    "message_id": str(message.id),
                    "thread_id": str(thread.id),
                    "channel": message.channel,
                },
            )
        )
    await db.commit()
    await db.refresh(message)
    return message, False
