"""Connection lifecycle, encrypted credentials, OAuth PKCE, and health state."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import forced_tenant_rls, resolve_oauth_state, set_tenant_context
from app.models.integration import IntegrationConnection, IntegrationOAuthState
from app.observability.metrics import record_provider_connection_state
from app.observability.telemetry import provider_observation
from app.schemas.integrations import (
    IntegrationConnectionOut,
    IntegrationHealthOut,
)
from app.services.integrations.catalog import (
    PROVIDERS,
    initial_provider_status,
    provider_definition,
    validate_credentials,
)
from app.services.integrations.crypto import (
    IntegrationCipher,
    IntegrationCryptoError,
    connection_aad,
)
from app.services.integrations.http import ProviderRequestError
from app.services.integrations.providers import OAuthTokenSet, pkce_challenge


class IntegrationLifecycleError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def connection_out(connection: IntegrationConnection) -> IntegrationConnectionOut:
    return IntegrationConnectionOut(
        id=connection.id,
        provider=connection.provider,
        name=connection.name,
        status=connection.status,
        capabilities=list(connection.capabilities or []),
        configuration=connection.configuration or {},
        provider_account_id=connection.provider_account_id,
        granted_scopes=list(connection.granted_scopes or []),
        credentials_configured=connection.credential_ciphertext is not None,
        credential_expires_at=connection.credential_expires_at,
        last_health_at=connection.last_health_at,
        last_success_at=connection.last_success_at,
        last_error_code=connection.last_error_code,
        consecutive_failures=connection.consecutive_failures,
        revoked_at=connection.revoked_at,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


async def get_connection(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    organization_id: uuid.UUID,
    for_update: bool = False,
) -> IntegrationConnection:
    statement = select(IntegrationConnection).where(
        IntegrationConnection.id == connection_id,
        IntegrationConnection.organization_id == organization_id,
    )
    if for_update:
        statement = statement.with_for_update()
    connection = (await db.execute(statement)).scalar_one_or_none()
    if connection is None:
        raise IntegrationLifecycleError("connection_not_found")
    return connection


def _store_credentials(
    connection: IntegrationConnection,
    credentials: dict[str, Any],
    *,
    cipher: IntegrationCipher,
) -> None:
    encrypted = cipher.encrypt_json(
        credentials,
        aad=connection_aad(connection.organization_id, connection.id, "credentials"),
    )
    connection.credential_ciphertext = encrypted.ciphertext
    connection.credential_nonce = encrypted.nonce
    connection.credential_key_id = encrypted.key_id
    connection.credential_version = (connection.credential_version or 0) + 1
    connection.credentials_updated_at = datetime.now(UTC)


def load_credentials(
    connection: IntegrationConnection,
    *,
    cipher: IntegrationCipher,
) -> dict[str, Any]:
    if (
        connection.credential_ciphertext is None
        or connection.credential_nonce is None
        or connection.credential_key_id is None
    ):
        raise IntegrationLifecycleError("credentials_not_configured")
    try:
        value = cipher.decrypt_json(
            connection.credential_ciphertext,
            connection.credential_nonce,
            aad=connection_aad(
                connection.organization_id, connection.id, "credentials"
            ),
            key_id=connection.credential_key_id,
        )
    except IntegrationCryptoError as exc:
        raise IntegrationLifecycleError("credentials_unavailable") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise IntegrationLifecycleError("credentials_unavailable")
    return value


async def create_connection(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    provider: str,
    name: str,
    configuration: dict,
    cipher: IntegrationCipher | None,
) -> tuple[IntegrationConnection, str | None]:
    definition = provider_definition(provider)
    connection = IntegrationConnection(
        id=uuid.uuid4(),
        organization_id=organization_id,
        created_by_user_id=actor_user_id,
        provider=provider,
        name=name,
        status=initial_provider_status(provider),
        capabilities=list(definition.capabilities),
        configuration=configuration,
    )
    one_time_secret: str | None = None
    if provider == "generic_webhook":
        if cipher is None:
            raise IntegrationLifecycleError("credential_encryption_unavailable")
        one_time_secret = secrets.token_urlsafe(48)
        _store_credentials(
            connection, {"signing_secret": one_time_secret}, cipher=cipher
        )
        connection.status = "connected"
    db.add(connection)
    await db.flush()
    return connection, one_time_secret


async def set_connection_credentials(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    credentials: dict[str, str],
    cipher: IntegrationCipher,
) -> list[str]:
    validate_credentials(connection.provider, credentials)
    _store_credentials(connection, credentials, cipher=cipher)
    connection.credential_expires_at = None
    connection.granted_scopes = []
    connection.provider_account_id = None
    connection.revoked_at = None
    connection.last_error_code = None
    connection.consecutive_failures = 0
    connection.status = (
        "connecting"
        if provider_definition(connection.provider).oauth
        else "disconnected"
    )
    await db.flush()
    return sorted(credentials)


def _valid_redirect_uri(redirect_uri: str, allowed_origins: set[str]) -> str:
    parsed = urlsplit(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.username is not None
        or parsed.password is not None
        or origin not in {item.rstrip("/") for item in allowed_origins}
    ):
        raise IntegrationLifecycleError("oauth_redirect_not_allowed")
    return redirect_uri


async def start_oauth(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    redirect_uri: str,
    allowed_redirect_origins: set[str],
    cipher: IntegrationCipher,
    adapter: Any,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    if not provider_definition(connection.provider).oauth:
        raise IntegrationLifecycleError("oauth_not_supported")
    if connection.status == "revoked":
        raise IntegrationLifecycleError("connection_revoked")
    credentials = load_credentials(connection, cipher=cipher)
    redirect_uri = _valid_redirect_uri(redirect_uri, allowed_redirect_origins)
    raw_state = secrets.token_urlsafe(48)
    verifier = secrets.token_urlsafe(64)
    now = now or datetime.now(UTC)
    expires_at = now + timedelta(minutes=10)
    encrypted = cipher.encrypt_bytes(
        verifier.encode(),
        aad=connection_aad(connection.organization_id, connection.id, "oauth-verifier"),
    )
    oauth_state = IntegrationOAuthState(
        organization_id=connection.organization_id,
        connection_id=connection.id,
        state_digest=hashlib.sha256(raw_state.encode()).hexdigest(),
        verifier_ciphertext=encrypted.ciphertext,
        verifier_nonce=encrypted.nonce,
        verifier_key_id=encrypted.key_id,
        redirect_uri=redirect_uri,
        expires_at=expires_at,
    )
    db.add(oauth_state)
    connection.status = "connecting"
    if connection.provider == "microsoft_365":
        url = adapter.authorization_url(
            credentials,
            connection.configuration or {},
            redirect_uri=redirect_uri,
            state=raw_state,
            code_challenge=pkce_challenge(verifier),
        )
    else:
        url = adapter.authorization_url(
            credentials,
            redirect_uri=redirect_uri,
            state=raw_state,
            code_challenge=pkce_challenge(verifier),
        )
    await db.flush()
    return url, expires_at


async def complete_oauth(
    db: AsyncSession,
    *,
    raw_state: str,
    code: str,
    cipher: IntegrationCipher,
    adapters: dict[str, Any],
    now: datetime | None = None,
) -> IntegrationConnection:
    now = now or datetime.now(UTC)
    digest = hashlib.sha256(raw_state.encode()).hexdigest()
    bootstrap = None
    if forced_tenant_rls():
        bootstrap = await resolve_oauth_state(db, digest=digest)
        if bootstrap is None:
            raise IntegrationLifecycleError("oauth_state_invalid")
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=None,
            identity_kind="public",
        )
    oauth_stmt = (
        select(IntegrationOAuthState)
        .where(IntegrationOAuthState.state_digest == digest)
        .with_for_update()
    )
    if bootstrap is not None:
        oauth_stmt = oauth_stmt.where(
            IntegrationOAuthState.id == bootstrap.capability_id,
            IntegrationOAuthState.organization_id == bootstrap.organization_id,
        )
    oauth_state = (await db.execute(oauth_stmt)).scalar_one_or_none()
    if oauth_state is None:
        raise IntegrationLifecycleError("oauth_state_invalid")
    if oauth_state.consumed_at is not None:
        raise IntegrationLifecycleError("oauth_state_consumed")
    if oauth_state.expires_at <= now:
        raise IntegrationLifecycleError("oauth_state_expired")
    oauth_state.consumed_at = now
    connection = (
        await db.execute(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.id == oauth_state.connection_id,
                IntegrationConnection.organization_id == oauth_state.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one()
    adapter = adapters.get(connection.provider)
    if adapter is None:
        raise IntegrationLifecycleError("provider_unavailable")
    credentials = load_credentials(connection, cipher=cipher)
    try:
        verifier = cipher.decrypt_bytes(
            oauth_state.verifier_ciphertext,
            oauth_state.verifier_nonce,
            aad=connection_aad(
                connection.organization_id, connection.id, "oauth-verifier"
            ),
            key_id=oauth_state.verifier_key_id,
        ).decode()
        with provider_observation(
            provider=connection.provider,
            operation="oauth_exchange",
        ):
            if connection.provider == "microsoft_365":
                token_set = await adapter.exchange_code(
                    credentials,
                    connection.configuration or {},
                    code=code,
                    code_verifier=verifier,
                    redirect_uri=oauth_state.redirect_uri,
                )
            else:
                token_set = await adapter.exchange_code(
                    credentials,
                    code=code,
                    code_verifier=verifier,
                    redirect_uri=oauth_state.redirect_uri,
                )
    except (ProviderRequestError, IntegrationCryptoError, UnicodeDecodeError) as exc:
        connection.status = "degraded"
        connection.last_error_code = getattr(exc, "code", "oauth_exchange_failed")
        connection.consecutive_failures += 1
        record_provider_connection_state(
            connection_id=connection.id,
            provider=connection.provider,
            state=connection.status,
        )
        raise IntegrationLifecycleError("oauth_exchange_failed") from exc
    _merge_tokens(connection, credentials, token_set, cipher=cipher)
    connection.status = "connected"
    connection.last_success_at = now
    connection.last_error_code = None
    connection.consecutive_failures = 0
    record_provider_connection_state(
        connection_id=connection.id,
        provider=connection.provider,
        state=connection.status,
    )
    await db.flush()
    return connection


def _merge_tokens(
    connection: IntegrationConnection,
    credentials: dict[str, Any],
    token_set: OAuthTokenSet,
    *,
    cipher: IntegrationCipher,
) -> None:
    updated = dict(credentials)
    updated["access_token"] = token_set.access_token
    if token_set.refresh_token:
        updated["refresh_token"] = token_set.refresh_token
    _store_credentials(connection, updated, cipher=cipher)
    connection.credential_expires_at = token_set.expires_at
    connection.granted_scopes = list(token_set.scopes)
    if token_set.provider_account_id:
        connection.provider_account_id = token_set.provider_account_id


async def ensure_fresh_credentials(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cipher: IntegrationCipher,
    adapter: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    credentials = load_credentials(connection, cipher=cipher)
    expires = connection.credential_expires_at
    if expires is None or expires > now + timedelta(minutes=2):
        return credentials
    if not credentials.get("refresh_token") or not hasattr(adapter, "refresh"):
        connection.status = "expired"
        connection.last_error_code = "provider_token_expired"
        record_provider_connection_state(
            connection_id=connection.id,
            provider=connection.provider,
            state=connection.status,
        )
        await db.flush()
        raise IntegrationLifecycleError("provider_token_expired")
    try:
        with provider_observation(
            provider=connection.provider,
            operation="oauth_refresh",
        ):
            if connection.provider == "microsoft_365":
                token_set = await adapter.refresh(
                    credentials, connection.configuration or {}
                )
            else:
                token_set = await adapter.refresh(credentials)
    except ProviderRequestError as exc:
        connection.status = (
            "expired" if exc.code == "provider_auth_rejected" else "degraded"
        )
        connection.last_error_code = exc.code
        connection.consecutive_failures += 1
        record_provider_connection_state(
            connection_id=connection.id,
            provider=connection.provider,
            state=connection.status,
        )
        await db.flush()
        raise IntegrationLifecycleError(exc.code) from exc
    _merge_tokens(connection, credentials, token_set, cipher=cipher)
    connection.status = "connected"
    connection.last_success_at = now
    connection.last_error_code = None
    connection.consecutive_failures = 0
    record_provider_connection_state(
        connection_id=connection.id,
        provider=connection.provider,
        state=connection.status,
    )
    await db.flush()
    return load_credentials(connection, cipher=cipher)


async def check_health(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cipher: IntegrationCipher | None,
    adapters: dict[str, Any],
    now: datetime | None = None,
) -> IntegrationHealthOut:
    now = now or datetime.now(UTC)
    if connection.status == "revoked":
        raise IntegrationLifecycleError("connection_revoked")
    definition = provider_definition(connection.provider)
    if not definition.locally_available:
        connection.status = "unavailable"
        connection.last_health_at = now
        connection.last_error_code = (
            definition.unavailable_reason or "provider_unavailable"
        )
    elif not definition.credential_keys:
        connection.status = "connected"
        connection.last_health_at = now
        connection.last_success_at = now
        connection.last_error_code = None
        connection.consecutive_failures = 0
    else:
        if cipher is None:
            raise IntegrationLifecycleError("credential_encryption_unavailable")
        adapter = adapters.get(connection.provider)
        if connection.provider == "generic_webhook":
            try:
                load_credentials(connection, cipher=cipher)
            except IntegrationLifecycleError as exc:
                connection.status = "disconnected"
                connection.last_error_code = exc.code
                connection.consecutive_failures += 1
            else:
                connection.status = "connected"
                connection.last_success_at = now
                connection.last_error_code = None
                connection.consecutive_failures = 0
            connection.last_health_at = now
            record_provider_connection_state(
                connection_id=connection.id,
                provider=connection.provider,
                state=connection.status,
            )
            await db.flush()
            return IntegrationHealthOut(
                connection_id=connection.id,
                status=connection.status,
                checked_at=now,
                error_code=connection.last_error_code,
                capabilities=list(connection.capabilities or []),
            )
        if adapter is None:
            connection.status = "unavailable"
            connection.last_error_code = "provider_adapter_unavailable"
            connection.last_health_at = now
        else:
            try:
                with provider_observation(
                    provider=connection.provider,
                    operation="health",
                ):
                    credentials = await ensure_fresh_credentials(
                        db,
                        connection=connection,
                        cipher=cipher,
                        adapter=adapter,
                        now=now,
                    )
                    details = await adapter.health(
                        credentials, connection.configuration or {}
                    )
            except (ProviderRequestError, IntegrationLifecycleError) as exc:
                code = getattr(exc, "code", "provider_health_failed")
                if code in {
                    "credentials_not_configured",
                    "provider_credentials_missing",
                }:
                    connection.status = "disconnected"
                elif code in {"provider_auth_rejected", "provider_token_expired"}:
                    connection.status = "expired"
                else:
                    connection.status = "degraded"
                connection.last_error_code = code
                connection.consecutive_failures += 1
            else:
                account_id = details.get("provider_account_id")
                if isinstance(account_id, str) and account_id:
                    connection.provider_account_id = account_id
                connection.status = "connected"
                connection.last_success_at = now
                connection.last_error_code = None
                connection.consecutive_failures = 0
            connection.last_health_at = now
    record_provider_connection_state(
        connection_id=connection.id,
        provider=connection.provider,
        state=connection.status,
    )
    await db.flush()
    return IntegrationHealthOut(
        connection_id=connection.id,
        status=connection.status,
        checked_at=now,
        error_code=connection.last_error_code,
        capabilities=list(connection.capabilities or []),
    )


async def revoke_connection(
    db: AsyncSession,
    *,
    connection: IntegrationConnection,
    cipher: IntegrationCipher | None,
    adapters: dict[str, Any],
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    remote_error: str | None = None
    if cipher is not None and connection.credential_ciphertext is not None:
        try:
            credentials = load_credentials(connection, cipher=cipher)
            adapter = adapters.get(connection.provider)
            if adapter is not None and hasattr(adapter, "revoke"):
                with provider_observation(
                    provider=connection.provider,
                    operation="oauth_revoke",
                ):
                    await adapter.revoke(credentials)
        except (IntegrationLifecycleError, ProviderRequestError) as exc:
            remote_error = getattr(exc, "code", "provider_revoke_failed")
    # Local revoke is authoritative and immediate even if the provider is down.
    connection.credential_ciphertext = None
    connection.credential_nonce = None
    connection.credential_key_id = None
    connection.credential_expires_at = None
    connection.granted_scopes = []
    connection.provider_account_id = None
    connection.status = "revoked"
    connection.revoked_at = now
    connection.last_error_code = remote_error
    record_provider_connection_state(
        connection_id=connection.id,
        provider=connection.provider,
        state=connection.status,
    )
    await db.flush()


def provider_catalog() -> list:
    return list(PROVIDERS.values())
