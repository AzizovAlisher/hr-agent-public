"""Versioned HMAC webhooks with replay protection and durable inspection."""

import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import (
    IntegrationConnection,
    IntegrationWebhookDelivery,
    IntegrationWebhookDeliveryAttempt,
    IntegrationWebhookEvent,
)
from app.schemas.integrations import WebhookEnvelope
from app.services.integrations.crypto import (
    IntegrationCipher,
    IntegrationCryptoError,
    connection_aad,
)
from app.services.integrations.http import ProviderHttpClient, ProviderRequestError
from app.services.integrations.lifecycle import (
    IntegrationLifecycleError,
    load_credentials,
)

WEBHOOK_VERSION = "2026-08-01"
SIGNATURE_VERSION = "v1"


class WebhookError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_envelope_bytes(envelope: WebhookEnvelope | dict[str, Any]) -> bytes:
    value = (
        envelope.model_dump(mode="json")
        if isinstance(envelope, WebhookEnvelope)
        else envelope
    )
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def sign_webhook(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={mac}"


def verify_webhook_signature(
    secret: str,
    *,
    timestamp: str,
    signature: str,
    body: bytes,
    now: datetime | None = None,
    tolerance_seconds: int = 300,
) -> datetime:
    now = now or datetime.now(UTC)
    try:
        unix_timestamp = int(timestamp)
        signed_at = datetime.fromtimestamp(unix_timestamp, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise WebhookError("webhook_timestamp_invalid") from exc
    if abs((now - signed_at).total_seconds()) > tolerance_seconds:
        raise WebhookError("webhook_timestamp_expired")
    expected = sign_webhook(secret, unix_timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise WebhookError("webhook_signature_invalid")
    return signed_at


def _event_aad(
    connection: IntegrationConnection, event_id: str, direction: str
) -> bytes:
    return connection_aad(
        connection.organization_id,
        connection.id,
        f"webhook-{direction}:{event_id}",
    )


def load_webhook_envelope(
    event: IntegrationWebhookEvent,
    *,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
) -> WebhookEnvelope:
    """Decrypt one accepted event only in its original tenant/connection context."""

    if (
        event.organization_id != connection.organization_id
        or event.connection_id != connection.id
    ):
        raise ProviderRequestError("webhook_event_not_found")
    try:
        raw = cipher.decrypt_bytes(
            event.body_ciphertext,
            event.body_nonce,
            aad=_event_aad(connection, event.event_id, "inbound"),
            key_id=event.body_key_id,
        )
        envelope = WebhookEnvelope.model_validate_json(raw)
    except (IntegrationCryptoError, ValueError) as exc:
        raise ProviderRequestError("webhook_payload_unavailable") from exc
    if (
        envelope.id != event.event_id
        or envelope.type != event.event_type
        or envelope.version != event.envelope_version
        or hashlib.sha256(raw).hexdigest() != event.payload_sha256
    ):
        raise ProviderRequestError("webhook_payload_unavailable")
    return envelope


async def accept_webhook_event(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
    raw_body: bytes,
    timestamp: str,
    signature: str,
    now: datetime | None = None,
    credential_key: str = "signing_secret",
) -> tuple[IntegrationWebhookEvent, bool]:
    if connection.status not in {"connected", "degraded"}:
        raise WebhookError("connection_not_ready")
    if "webhook.receive" not in set(connection.capabilities or []):
        raise WebhookError("webhook_receive_not_supported")
    if len(raw_body) > 256_000:
        raise WebhookError("webhook_payload_too_large")
    credentials = load_credentials(connection, cipher=cipher)
    secret = credentials.get(credential_key)
    if not isinstance(secret, str) or not secret:
        raise WebhookError("webhook_secret_not_configured")
    now = now or datetime.now(UTC)
    verify_webhook_signature(
        secret,
        timestamp=timestamp,
        signature=signature,
        body=raw_body,
        now=now,
    )
    try:
        raw = json.loads(raw_body)
        envelope = WebhookEnvelope.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WebhookError("webhook_envelope_invalid") from exc
    if envelope.version != WEBHOOK_VERSION:
        raise WebhookError("webhook_version_unsupported")
    # Event creation time has its own generous bound; the signature timestamp
    # is the strict anti-replay window.
    if envelope.created_at > now + timedelta(
        minutes=5
    ) or envelope.created_at < now - timedelta(days=30):
        raise WebhookError("webhook_event_time_invalid")
    payload_sha = hashlib.sha256(raw_body).hexdigest()
    lock_key = f"integration-webhook:{connection.id}:{envelope.id}"
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    existing = (
        await db.execute(
            select(IntegrationWebhookEvent).where(
                IntegrationWebhookEvent.connection_id == connection.id,
                IntegrationWebhookEvent.event_id == envelope.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_sha256 != payload_sha:
            raise WebhookError("webhook_event_conflict")
        return existing, True
    encrypted = cipher.encrypt_bytes(
        raw_body, aad=_event_aad(connection, envelope.id, "inbound")
    )
    event = IntegrationWebhookEvent(
        organization_id=connection.organization_id,
        connection_id=connection.id,
        event_id=envelope.id,
        envelope_version=envelope.version,
        event_type=envelope.type,
        occurred_at=envelope.created_at,
        payload_sha256=payload_sha,
        body_ciphertext=encrypted.ciphertext,
        body_nonce=encrypted.nonce,
        body_key_id=encrypted.key_id,
        status="accepted",
        received_at=now,
    )
    db.add(event)
    await db.flush()
    return event, False


async def queue_webhook_delivery(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
    event_type: str,
    data: dict[str, Any],
    idempotency_key: str,
    now: datetime | None = None,
) -> tuple[IntegrationWebhookDelivery, bool]:
    if connection.status != "connected":
        raise WebhookError("connection_not_ready")
    if "webhook.deliver" not in set(connection.capabilities or []):
        raise WebhookError("webhook_delivery_not_supported")
    target_url = (connection.configuration or {}).get("target_url")
    if not isinstance(target_url, str) or not target_url:
        raise WebhookError("webhook_target_not_configured")
    now = now or datetime.now(UTC)
    event_id = (
        "evt_"
        + hashlib.sha256(f"{connection.id}:{idempotency_key}".encode()).hexdigest()[:40]
    )
    envelope = WebhookEnvelope(
        version=WEBHOOK_VERSION,
        id=event_id,
        type=event_type,
        created_at=now,
        data=data,
    )
    body = canonical_envelope_bytes(envelope)
    payload_sha = hashlib.sha256(body).hexdigest()
    lock_key = f"integration-delivery:{connection.id}:{event_id}"
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    existing = (
        await db.execute(
            select(IntegrationWebhookDelivery).where(
                IntegrationWebhookDelivery.connection_id == connection.id,
                IntegrationWebhookDelivery.event_id == event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        try:
            existing_raw = cipher.decrypt_bytes(
                existing.body_ciphertext,
                existing.body_nonce,
                aad=_event_aad(connection, existing.event_id, "outbound"),
                key_id=existing.body_key_id,
            )
            existing_envelope = WebhookEnvelope.model_validate_json(existing_raw)
        except (IntegrationCryptoError, ValueError) as exc:
            raise WebhookError("delivery_payload_unavailable") from exc
        if existing_envelope.type != event_type or existing_envelope.data != data:
            raise WebhookError("idempotency_conflict")
        return existing, False
    encrypted = cipher.encrypt_bytes(
        body, aad=_event_aad(connection, event_id, "outbound")
    )
    delivery = IntegrationWebhookDelivery(
        organization_id=connection.organization_id,
        connection_id=connection.id,
        event_id=event_id,
        envelope_version=WEBHOOK_VERSION,
        event_type=event_type,
        target_url=target_url,
        payload_sha256=payload_sha,
        body_ciphertext=encrypted.ciphertext,
        body_nonce=encrypted.nonce,
        body_key_id=encrypted.key_id,
        status="queued",
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now,
    )
    db.add(delivery)
    await db.flush()
    return delivery, True


async def request_webhook_delivery_attempt(
    db: AsyncSession,
    *,
    delivery: IntegrationWebhookDelivery,
    now: datetime | None = None,
) -> None:
    """Queue a delivery attempt; only the dedicated worker performs HTTP I/O."""

    if delivery.status in {"delivered", "cancelled", "delivering"}:
        raise WebhookError("delivery_not_runnable")
    now = now or datetime.now(UTC)
    if delivery.status == "dead_letter":
        delivery.status = "retrying"
        delivery.attempt_count = 0
        delivery.failure_code = None
        delivery.last_response_status = None
        delivery.delivered_at = None
    elif delivery.status not in {"queued", "retrying"}:
        raise WebhookError("delivery_not_runnable")
    delivery.next_attempt_at = now
    await db.flush()


EndpointValidator = Callable[[str], Awaitable[str]]


async def deliver_webhook(
    db: AsyncSession,
    *,
    delivery: IntegrationWebhookDelivery,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
    http: ProviderHttpClient,
    validate_endpoint: EndpointValidator,
    now: datetime | None = None,
) -> None:
    if delivery.status not in {"queued", "retrying"}:
        raise WebhookError("delivery_not_due")
    now = now or datetime.now(UTC)
    if delivery.next_attempt_at > now:
        raise WebhookError("delivery_not_due")
    delivery.status = "delivering"
    delivery.attempt_count += 1
    # ``attempt_count`` is the bounded count for the current delivery cycle
    # and is reset by an explicit dead-letter replay.  The immutable attempt
    # history remains monotonic across cycles so replay cannot collide with a
    # prior `(delivery_id, attempt_number)` record or erase audit evidence.
    history_attempt_number = int(
        await db.scalar(
            select(
                func.coalesce(
                    func.max(IntegrationWebhookDeliveryAttempt.attempt_number), 0
                )
            ).where(
                IntegrationWebhookDeliveryAttempt.delivery_id == delivery.id,
                IntegrationWebhookDeliveryAttempt.organization_id
                == delivery.organization_id,
            )
        )
        or 0
    ) + 1
    attempt = IntegrationWebhookDeliveryAttempt(
        organization_id=delivery.organization_id,
        delivery_id=delivery.id,
        attempt_number=history_attempt_number,
        status="delivering",
        started_at=now,
    )
    db.add(attempt)
    # Do not flush the delivering transition before network I/O. The broker
    # lease is already durable; flushing here would hold delivery/attempt row
    # locks across the receiver request and make cancellation/recovery block.
    if connection.status not in {"connected", "degraded"}:
        await _record_delivery_failure(
            db,
            delivery=delivery,
            attempt=attempt,
            connection=connection,
            code="connection_not_ready",
            retryable=connection.status not in {"revoked", "unavailable"},
            now=now,
        )
        return
    try:
        credentials = load_credentials(connection, cipher=cipher)
        secret = credentials.get("signing_secret")
        if not isinstance(secret, str) or not secret:
            raise WebhookError("webhook_secret_not_configured")
        target = await validate_endpoint(delivery.target_url)
        body = cipher.decrypt_bytes(
            delivery.body_ciphertext,
            delivery.body_nonce,
            aad=_event_aad(connection, delivery.event_id, "outbound"),
            key_id=delivery.body_key_id,
        )
    except (
        IntegrationLifecycleError,
        IntegrationCryptoError,
        ProviderRequestError,
        WebhookError,
    ) as exc:
        # Crypto and endpoint failures are permanent configuration failures.
        if isinstance(exc, ProviderRequestError) and exc.retryable:
            retryable = True
            code = exc.code
        else:
            retryable = False
            code = getattr(exc, "code", "delivery_configuration_invalid")
        await _record_delivery_failure(
            db,
            delivery=delivery,
            attempt=attempt,
            connection=connection,
            code=code,
            retryable=retryable,
            now=now,
        )
        return
    timestamp = int(now.timestamp())
    try:
        response = await http.request(
            "POST",
            target,
            headers={
                "Content-Type": "application/json",
                "X-HR-Webhook-Id": delivery.event_id,
                "X-HR-Webhook-Timestamp": str(timestamp),
                "X-HR-Webhook-Signature": sign_webhook(secret, timestamp, body),
                "X-HR-Webhook-Version": WEBHOOK_VERSION,
            },
            content=body,
            expected=set(range(200, 300)),
            # Receivers are contractually required to deduplicate event IDs.
            retry_safe=True,
            automatic_retries=False,
        )
    except ProviderRequestError as exc:
        delivery.last_response_status = exc.response_status
        await _record_delivery_failure(
            db,
            delivery=delivery,
            attempt=attempt,
            connection=connection,
            code=exc.code,
            retryable=exc.retryable,
            now=now,
        )
        return
    delivery.status = "delivered"
    delivery.last_response_status = response.status_code
    delivery.failure_code = None
    delivery.delivered_at = now
    attempt.status = "delivered"
    attempt.response_status = response.status_code
    attempt.completed_at = now
    connection.status = "connected"
    connection.last_success_at = now
    connection.last_error_code = None
    connection.consecutive_failures = 0
    await db.flush()


async def _record_delivery_failure(
    db: AsyncSession,
    *,
    delivery: IntegrationWebhookDelivery,
    attempt: IntegrationWebhookDeliveryAttempt,
    connection: IntegrationConnection,
    code: str,
    retryable: bool,
    now: datetime,
) -> None:
    delivery.failure_code = code[:64]
    attempt.failure_code = code[:64]
    attempt.response_status = delivery.last_response_status
    attempt.completed_at = now
    if retryable and delivery.attempt_count < delivery.max_attempts:
        delivery.status = "retrying"
        attempt.status = "retrying"
        delivery.next_attempt_at = now + timedelta(
            seconds=min(10 * (2 ** max(delivery.attempt_count - 1, 0)), 900)
        )
    else:
        delivery.status = "dead_letter"
        attempt.status = "failed"
    if connection.status in {"connected", "degraded"}:
        connection.status = "degraded"
        connection.last_error_code = code[:64]
        connection.consecutive_failures += 1
    await db.flush()


def new_webhook_secret() -> str:
    return secrets.token_urlsafe(48)
