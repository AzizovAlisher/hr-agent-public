"""Provider boundary for outbound candidate messages.

The legacy account method remains fail-closed. Connected providers cross the
new encrypted IntegrationConnection boundary through
``deliver_connected_message``; raw credentials and responses never enter
message, activity, notification, or job-summary records.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationMessage,
)
from app.models.integration import IntegrationConnection
from app.observability.metrics import record_provider_connection_state
from app.observability.telemetry import provider_observation
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.lifecycle import (
    IntegrationLifecycleError,
    ensure_fresh_credentials,
)
from app.services.integrations.providers import EmailRequest


@dataclass(frozen=True)
class ProviderResult:
    delivered: bool
    provider_message_id: str | None = None
    failure_code: str | None = None
    retryable: bool = False


async def deliver_message(
    account: CommunicationAccount,
    message: CommunicationMessage,
    _contact: CandidateContactPoint,
) -> ProviderResult:
    """Deliver one message through its configured provider.

    Provider implementations receive the target only in memory. Errors crossing
    this boundary must be mapped to allowlisted codes, never raw exception text.
    """

    if account.provider == "sandbox":
        return ProviderResult(
            delivered=True,
            provider_message_id=f"sandbox-{message.id}",
        )
    if account.provider in {"smtp", "telegram", "whatsapp"}:
        return ProviderResult(
            delivered=False,
            failure_code="provider_unconfigured",
        )
    return ProviderResult(delivered=False, failure_code="provider_unsupported")


async def deliver_connected_message(
    connection: IntegrationConnection,
    *,
    db: AsyncSession,
    cipher: IntegrationCipher,
    adapter,
    message: CommunicationMessage,
    contact: CandidateContactPoint,
) -> ProviderResult:
    """Measure the canonical provider boundary without exposing message data."""

    operation = "telegram_send" if connection.provider == "telegram" else "email_send"
    with provider_observation(
        provider=connection.provider,
        operation=operation,
    ) as observation:
        try:
            result = await _deliver_connected_message_unobserved(
                connection,
                db=db,
                cipher=cipher,
                adapter=adapter,
                message=message,
                contact=contact,
            )
        finally:
            record_provider_connection_state(
                connection_id=connection.id,
                provider=connection.provider,
                state=connection.status,
            )
        if result.delivered:
            observation.outcome = "succeeded"
        elif result.failure_code == "provider_rate_limited":
            observation.outcome = "rate_limited"
        elif connection.status in {"degraded", "expired"} or result.retryable:
            observation.outcome = "degraded"
        else:
            observation.outcome = "failed"
        return result


async def _deliver_connected_message_unobserved(
    connection: IntegrationConnection,
    *,
    db: AsyncSession,
    cipher: IntegrationCipher,
    adapter,
    message: CommunicationMessage,
    contact: CandidateContactPoint,
) -> ProviderResult:
    """Deliver through a verified unified connection.

    Callers enqueue the underlying message/job first and pass only a claimed
    record here. This function intentionally never changes connection or
    message state; the durable worker owns commit/retry semantics.
    """

    if connection.status not in {"connected", "degraded"}:
        return ProviderResult(delivered=False, failure_code="connection_not_ready")
    try:
        credentials = await ensure_fresh_credentials(
            db,
            connection=connection,
            cipher=cipher,
            adapter=adapter,
        )
        if connection.provider in {"google_workspace", "microsoft_365"}:
            if message.channel != "email" or contact.channel != "email":
                return ProviderResult(
                    delivered=False, failure_code="provider_channel_mismatch"
                )
            external_id = await adapter.send_email(
                credentials,
                EmailRequest(
                    to=contact.value,
                    subject=message.subject or "",
                    body_text=message.body,
                    from_email=(connection.configuration or {}).get("sender_email"),
                ),
            )
        elif connection.provider == "telegram":
            if message.channel != "telegram" or contact.channel != "telegram":
                return ProviderResult(
                    delivered=False, failure_code="provider_channel_mismatch"
                )
            chat_id = contact.normalized_value
            if not chat_id.isdigit():
                chat_id = f"@{chat_id}"
            external_id = await adapter.send_message(
                credentials,
                chat_id=chat_id,
                text=message.body,
                parse_mode=(connection.configuration or {}).get("parse_mode"),
            )
        else:
            return ProviderResult(delivered=False, failure_code="provider_unsupported")
    except IntegrationLifecycleError as exc:
        return ProviderResult(delivered=False, failure_code=exc.code)
    except ProviderRequestError as exc:
        # A lost response to a non-idempotent send is terminal and explicitly
        # ambiguous; replaying it could contact the candidate twice. Explicit
        # 429/5xx rejections are safe to retry because the provider did not
        # accept the request.
        if exc.code == "provider_network_error":
            return ProviderResult(
                delivered=False,
                failure_code="delivery_outcome_unknown",
            )
        return ProviderResult(
            delivered=False,
            failure_code=exc.code,
            retryable=exc.code in {"provider_rate_limited", "provider_unavailable"},
        )
    return ProviderResult(
        delivered=True,
        provider_message_id=str(external_id) if external_id is not None else None,
    )
