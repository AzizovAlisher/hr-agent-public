"""Canonical capability and operation catalog for pilot connectors."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDefinition:
    provider: str
    capabilities: tuple[str, ...]
    oauth: bool
    credential_keys: tuple[str, ...]
    locally_available: bool = True
    unavailable_reason: str | None = None


PROVIDERS: dict[str, ProviderDefinition] = {
    "google_workspace": ProviderDefinition(
        provider="google_workspace",
        capabilities=("email.send", "calendar.write"),
        oauth=True,
        credential_keys=(
            "client_id",
            "client_secret",
            "access_token",
            "refresh_token",
        ),
    ),
    "microsoft_365": ProviderDefinition(
        provider="microsoft_365",
        capabilities=("email.send", "calendar.write"),
        oauth=True,
        credential_keys=(
            "client_id",
            "client_secret",
            "access_token",
            "refresh_token",
        ),
    ),
    "telegram": ProviderDefinition(
        provider="telegram",
        capabilities=(
            "message.telegram.send",
            "vacancy.publish",
        ),
        oauth=False,
        credential_keys=("bot_token", "webhook_secret"),
    ),
    "hh_ru": ProviderDefinition(
        provider="hh_ru",
        capabilities=("vacancy.publish", "candidate.import"),
        oauth=True,
        credential_keys=(
            "client_id",
            "client_secret",
            "access_token",
            "refresh_token",
        ),
    ),
    "somon": ProviderDefinition(
        provider="somon",
        capabilities=("tracked_link.create", "file.import"),
        oauth=False,
        credential_keys=(),
    ),
    "csv_xlsx": ProviderDefinition(
        provider="csv_xlsx",
        capabilities=("file.import",),
        oauth=False,
        credential_keys=(),
    ),
    "generic_webhook": ProviderDefinition(
        provider="generic_webhook",
        capabilities=("webhook.receive", "webhook.deliver"),
        oauth=False,
        credential_keys=("signing_secret",),
    ),
    "assessment_generic": ProviderDefinition(
        provider="assessment_generic",
        capabilities=(
            "assessment.invite",
            "assessment.status",
            "assessment.result",
            "assessment.delete",
            "webhook.receive",
        ),
        oauth=False,
        credential_keys=("api_key", "callback_signing_secret"),
    ),
}


PROVIDER_OPERATIONS: dict[str, frozenset[str]] = {
    "google_workspace": frozenset(
        {
            "email.send",
            "calendar.create",
            "calendar.update",
            "calendar.delete",
            "calendar.sync",
            "health",
        }
    ),
    "microsoft_365": frozenset(
        {
            "email.send",
            "calendar.create",
            "calendar.update",
            "calendar.delete",
            "calendar.sync",
            "health",
        }
    ),
    "telegram": frozenset({"telegram.send", "vacancy.publish", "health"}),
    "hh_ru": frozenset({"vacancy.publish", "candidate.import", "health"}),
    "somon": frozenset({"candidate.import", "tracked_link.create", "health"}),
    "csv_xlsx": frozenset({"candidate.import", "health"}),
    "generic_webhook": frozenset({"webhook.deliver", "health"}),
    "assessment_generic": frozenset(
        {
            "assessment.invite",
            "assessment.status",
            "assessment.result",
            "assessment.delete",
            "health",
        }
    ),
}


def provider_definition(provider: str) -> ProviderDefinition:
    try:
        return PROVIDERS[provider]
    except KeyError as exc:
        raise ValueError("Unsupported integration provider") from exc


def initial_provider_status(provider: str) -> str:
    definition = provider_definition(provider)
    if not definition.locally_available:
        return "unavailable"
    if not definition.credential_keys:
        return "connected"
    return "disconnected"


def validate_credentials(provider: str, credentials: dict[str, str]) -> None:
    definition = provider_definition(provider)
    supplied = set(credentials)
    allowed = set(definition.credential_keys)
    unknown = supplied - allowed
    if unknown:
        raise ValueError("Unsupported credential keys: " + ", ".join(sorted(unknown)))
    required: dict[str, set[str]] = {
        "google_workspace": {"client_id", "client_secret"},
        "microsoft_365": {"client_id", "client_secret"},
        "telegram": {"bot_token"},
        "hh_ru": {"client_id", "client_secret"},
        "generic_webhook": {"signing_secret"},
        "assessment_generic": {"api_key", "callback_signing_secret"},
    }
    missing = required.get(provider, set()) - supplied
    if missing:
        raise ValueError("Missing credential keys: " + ", ".join(sorted(missing)))
