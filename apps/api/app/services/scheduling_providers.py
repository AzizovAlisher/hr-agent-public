"""Local and encrypted OAuth calendar-provider boundary."""

from dataclasses import dataclass
from uuid import UUID

from app.models.integration import IntegrationConnection
from app.services.integrations.crypto import IntegrationCipher
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.lifecycle import load_credentials
from app.services.integrations.providers import CalendarEventRequest


class CalendarProviderUnavailable(RuntimeError):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"{provider} calendar integration is not configured")


@dataclass(frozen=True)
class CalendarProviderStatus:
    provider: str
    connection_id: UUID | None
    name: str
    status: str | None
    capability: str
    available: bool
    reason: str | None = None


def provider_statuses(
    connections: list[IntegrationConnection] | None = None,
) -> list[CalendarProviderStatus]:
    result = [
        CalendarProviderStatus(
            provider="local",
            connection_id=None,
            name="Local calendar",
            status=None,
            capability="local",
            available=True,
        )
    ]
    for connection in connections or []:
        provider = {
            "google_workspace": "google",
            "microsoft_365": "outlook",
        }.get(connection.provider)
        if provider is None:
            continue
        has_capability = "calendar.write" in (connection.capabilities or [])
        available = connection.status == "connected" and has_capability
        result.append(
            CalendarProviderStatus(
                provider=provider,
                connection_id=connection.id,
                name=connection.name,
                status=connection.status,
                capability="calendar.write",
                available=available,
                reason=(
                    None
                    if available
                    else connection.status
                    if has_capability
                    else "capability_missing"
                ),
            )
        )
    return result


async def create_external_event(provider: str, *_args, **_kwargs) -> str | None:
    if provider == "local":
        return None
    raise CalendarProviderUnavailable(provider)


async def update_external_event(provider: str, *_args, **_kwargs) -> None:
    if provider != "local":
        raise CalendarProviderUnavailable(provider)


async def delete_external_event(provider: str, *_args, **_kwargs) -> None:
    if provider != "local":
        raise CalendarProviderUnavailable(provider)


async def create_connected_event(
    connection: IntegrationConnection,
    *,
    cipher: IntegrationCipher,
    adapter,
    request: CalendarEventRequest,
):
    if connection.status != "connected" or connection.provider not in {
        "google_workspace",
        "microsoft_365",
    }:
        raise CalendarProviderUnavailable(connection.provider)
    try:
        credentials = load_credentials(connection, cipher=cipher)
        return await adapter.create_event(
            credentials, connection.configuration or {}, request
        )
    except ProviderRequestError as exc:
        raise CalendarProviderUnavailable(connection.provider) from exc


async def update_connected_event(
    connection: IntegrationConnection,
    *,
    cipher: IntegrationCipher,
    adapter,
    event_id: str,
    request: CalendarEventRequest,
):
    if connection.status != "connected" or connection.provider not in {
        "google_workspace",
        "microsoft_365",
    }:
        raise CalendarProviderUnavailable(connection.provider)
    try:
        credentials = load_credentials(connection, cipher=cipher)
        return await adapter.update_event(
            credentials, connection.configuration or {}, event_id, request
        )
    except ProviderRequestError as exc:
        raise CalendarProviderUnavailable(connection.provider) from exc


async def delete_connected_event(
    connection: IntegrationConnection,
    *,
    cipher: IntegrationCipher,
    adapter,
    event_id: str,
) -> None:
    if connection.status != "connected" or connection.provider not in {
        "google_workspace",
        "microsoft_365",
    }:
        raise CalendarProviderUnavailable(connection.provider)
    try:
        credentials = load_credentials(connection, cipher=cipher)
        await adapter.delete_event(
            credentials, connection.configuration or {}, event_id
        )
    except ProviderRequestError as exc:
        raise CalendarProviderUnavailable(connection.provider) from exc
