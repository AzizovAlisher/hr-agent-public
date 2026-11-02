"""Permission and audit helpers shared by enterprise-aware endpoints."""

import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    CANONICAL_PERMISSIONS,
    COMPATIBILITY_EXPANSIONS,
    FIELD_DEFAULT_ROLES,
    PERMISSION_ALIASES,
    PERMISSIONS,
    ROLE_DEFAULTS,
    FieldClass,
)
from app.config import get_settings
from app.db import (
    bootstrap_api_key,
    forced_tenant_rls,
    set_tenant_context,
)
from app.models.enterprise import (
    EnterprisePolicy,
    OrganizationApiKey,
    OrganizationModelConfiguration,
    PermissionOverride,
)
from app.models.user import User

if TYPE_CHECKING:
    from app.llm.vllm import PinnedModelEndpoint

MANAGED_PROCESSING_NOTICE_VERSION = "2026-08-22-v1"
ApprovalSeparationCategory = Literal[
    "requisitions",
    "offers",
    "hiring_decisions",
]
APPROVAL_SEPARATION_DEFAULTS: dict[ApprovalSeparationCategory, bool] = {
    "requisitions": True,
    "offers": True,
    "hiring_decisions": True,
}


class UnsafeModelEndpoint(ValueError):
    """Raised when a tenant model endpoint violates the egress policy."""


class TransientModelEndpointResolutionError(RuntimeError):
    """Raised when an approved private endpoint cannot be resolved temporarily."""


def _canonical_hostname(value: str) -> str:
    return value.strip().rstrip(".").encode("idna").decode("ascii").lower()


def _normalized_model_endpoint(value: str) -> tuple[str, str, int, str, str]:
    """Return canonical exact endpoint parts or reject ambiguous URL syntax."""

    parsed = urlsplit(value.strip().rstrip("/"))
    try:
        port = parsed.port
        hostname = _canonical_hostname(parsed.hostname or "")
    except (UnicodeError, ValueError) as exc:
        raise UnsafeModelEndpoint("Model endpoint has an invalid host or port") from exc
    path = parsed.path.rstrip("/")
    path_parts = path.split("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not path.startswith("/")
        or not path
        or "//" in path
        or "\\" in path
        or "%" in path
        or any(part in {"", ".", ".."} for part in path_parts[1:])
        or any(
            re.fullmatch(r"[A-Za-z0-9._~-]+", part) is None for part in path_parts[1:]
        )
    ):
        raise UnsafeModelEndpoint(
            "Model endpoint must include an exact HTTP(S) origin with an "
            "explicit port and safe base path"
        )
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    normalized = f"{parsed.scheme.lower()}://{rendered_host}:{port}{path}"
    return parsed.scheme.lower(), hostname, port, path, normalized


def _allowed_model_endpoint(value: str) -> bool:
    """Match the complete scheme, hostname, explicit port, and base path."""

    candidate = _normalized_model_endpoint(value)[:4]
    for raw_entry in get_settings().llm_private_base_url_allowlist.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            configured = _normalized_model_endpoint(entry)[:4]
        except UnsafeModelEndpoint:
            # Settings validation normally prevents this. Runtime matching also
            # fails closed if configuration was constructed without validation.
            continue
        if configured == candidate:
            return True
    return False


_METADATA_ADDRESSES = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


def _forbidden_model_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if any(address in network for network in _METADATA_ADDRESSES):
        return True
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        return True
    if address.is_reserved:
        return True
    # Exact operator allowlisting permits ordinary RFC1918/ULA/loopback
    # addresses for an on-prem deployment. Other non-global special-purpose
    # ranges stay blocked.
    return not (address.is_global or address.is_private or address.is_loopback)


async def validate_model_base_url(value: str) -> str:
    """Validate an OpenAI-compatible endpoint before any network request.

    Every private/OpenAI-compatible endpoint requires an exact operator
    allowlist entry including scheme, explicit port, and base path. Tenant
    administrators cannot widen that approved egress origin or downgrade HTTPS.
    DNS is pinned separately when a runtime client is created.
    """

    _scheme, hostname, _port, _path, normalized = _normalized_model_endpoint(value)
    if not _allowed_model_endpoint(normalized):
        raise UnsafeModelEndpoint("Model endpoint is not exactly operator-allowlisted")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        return normalized
    if _forbidden_model_address(literal):
        raise UnsafeModelEndpoint(
            "Model endpoint targets a link-local, metadata, or reserved address"
        )
    return normalized


async def resolve_model_endpoint(value: str) -> "PinnedModelEndpoint":
    """Resolve and pin one allowlisted endpoint for the private-model transport."""

    from app.llm.vllm import PinnedModelEndpoint, resolve_host_addresses

    normalized = await validate_model_base_url(value)
    parsed = urlsplit(normalized)
    if parsed.hostname is None:  # defensive: validation has already established this
        raise UnsafeModelEndpoint("Model endpoint hostname is missing")
    hostname = _canonical_hostname(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await resolve_host_addresses(hostname, port)
    except OSError as exc:
        # Resolution availability is not an egress-policy verdict. Keeping this
        # distinct lets durable workers retry a transient resolver outage while
        # unsafe origins and unsafe resolved addresses continue to fail closed.
        raise TransientModelEndpointResolutionError(
            "Approved model endpoint hostname is temporarily unavailable"
        ) from exc
    parsed_addresses = tuple(ipaddress.ip_address(item) for item in addresses)
    if not parsed_addresses or any(
        _forbidden_model_address(address) for address in parsed_addresses
    ):
        raise UnsafeModelEndpoint(
            "Model endpoint resolves to a link-local, metadata, or reserved address"
        )
    return PinnedModelEndpoint(
        base_url=normalized,
        scheme=parsed.scheme.lower(),
        hostname=hostname,
        port=port,
        addresses=frozenset(str(address) for address in parsed_addresses),
    )


async def get_or_create_policy(
    db: AsyncSession, organization_id: UUID
) -> EnterprisePolicy:
    policy = (
        await db.execute(
            select(EnterprisePolicy).where(
                EnterprisePolicy.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if policy is None:
        policy = EnterprisePolicy(organization_id=organization_id)
        db.add(policy)
        await db.flush()
    return policy


def normalize_approval_separation_policy(value: object) -> dict[str, bool]:
    """Return a complete policy, failing closed on any malformed value."""

    if not isinstance(value, dict) or set(value) != set(APPROVAL_SEPARATION_DEFAULTS):
        return dict(APPROVAL_SEPARATION_DEFAULTS)
    if any(type(value[key]) is not bool for key in APPROVAL_SEPARATION_DEFAULTS):
        return dict(APPROVAL_SEPARATION_DEFAULTS)
    return {key: value[key] for key in APPROVAL_SEPARATION_DEFAULTS}


async def approval_separation_required(
    db: AsyncSession,
    *,
    organization_id: UUID,
    category: ApprovalSeparationCategory,
) -> bool:
    """Resolve self-approval policy without ever weakening an invalid config."""

    configured = (
        await db.execute(
            select(EnterprisePolicy.approval_separation_of_duties).where(
                EnterprisePolicy.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    normalized = normalize_approval_separation_policy(configured)
    return normalized.get(category, True)


async def effective_permissions(
    db: AsyncSession, *, organization_id: UUID, role: str
) -> dict[str, bool]:
    canonical_values = {
        name: name in ROLE_DEFAULTS.get(role, frozenset())
        for name in CANONICAL_PERMISSIONS
    }
    overrides = list(
        (
            await db.execute(
                select(PermissionOverride).where(
                    PermissionOverride.organization_id == organization_id,
                    PermissionOverride.role == role,
                )
            )
        ).scalars()
    )
    # Compatibility aliases/expansions are applied first.  Canonical rows are
    # the final authority if both representations exist in historical data.
    for override in overrides:
        for target in PERMISSION_ALIASES.get(override.permission, ()):
            canonical_values[target] = override.allowed
        for target in COMPATIBILITY_EXPANSIONS.get(override.permission, ()):
            canonical_values[target] = override.allowed
    for override in overrides:
        if override.permission in CANONICAL_PERMISSIONS:
            canonical_values[override.permission] = override.allowed
    values = dict(canonical_values)
    for alias, targets in PERMISSION_ALIASES.items():
        values[alias] = all(canonical_values[target] for target in targets)
    return dict(sorted(values.items()))


async def has_permission(db: AsyncSession, user: User, permission: str) -> bool:
    if permission not in PERMISSIONS:
        raise RuntimeError(f"Unknown permission: {permission}")
    permissions = await effective_permissions(
        db, organization_id=user.organization_id, role=user.role
    )
    if permission in PERMISSION_ALIASES:
        return all(permissions[target] for target in PERMISSION_ALIASES[permission])
    return permissions[permission]


async def require_permission(db: AsyncSession, user: User, permission: str) -> None:
    if not await has_permission(db, user, permission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Permission denied")


_FIELD_PERMISSIONS: dict[FieldClass, tuple[str, ...]] = {
    FieldClass.INTERNAL: (),
    FieldClass.CONTACT: ("candidates.contact",),
    FieldClass.CANDIDATE_DOCUMENT: ("candidates.documents",),
    FieldClass.ASSESSMENT_RESULT: ("assessments.results",),
    FieldClass.COMPENSATION: (
        "requisitions.compensation",
        "offers.compensation",
    ),
    FieldClass.OFFER: ("offers.read",),
    FieldClass.EXPORT: ("candidates.export", "analytics.export"),
    FieldClass.BACKUP: ("enterprise.backup_export",),
    FieldClass.AI_CONTEXT: ("ai.sensitive_context",),
    FieldClass.SECRET: ("integrations.credentials", "ai.managed_provider"),
    FieldClass.AUDIT: ("enterprise.audit_export",),
}


async def can_access_field(
    db: AsyncSession,
    user: User,
    field_class: FieldClass | str,
    *,
    required_permission: str | None = None,
) -> bool:
    """Evaluate field visibility before response-model serialization.

    Both the action override and the organization's field-role policy must
    permit access.  Missing/malformed policy keys use conservative reviewed
    defaults; an unknown field class fails closed.
    """

    try:
        normalized = (
            field_class
            if isinstance(field_class, FieldClass)
            else FieldClass(field_class)
        )
    except ValueError:
        return False
    permissions = (
        (required_permission,)
        if required_permission is not None
        else _FIELD_PERMISSIONS[normalized]
    )
    if permissions:
        permitted = False
        for permission in permissions:
            if await has_permission(db, user, permission):
                permitted = True
                break
        if not permitted:
            return False
    policy = (
        await db.execute(
            select(EnterprisePolicy).where(
                EnterprisePolicy.organization_id == user.organization_id
            )
        )
    ).scalar_one_or_none()
    configured = (
        (policy.field_visibility or {}).get(normalized.value) if policy else None
    )
    allowed_roles = (
        {str(role) for role in configured}
        if isinstance(configured, list)
        else set(FIELD_DEFAULT_ROLES[normalized])
    )
    return user.role in allowed_roles


async def require_field_access(
    db: AsyncSession,
    user: User,
    field_class: FieldClass | str,
    *,
    required_permission: str | None = None,
) -> None:
    if not await can_access_field(
        db,
        user,
        field_class,
        required_permission=required_permission,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sensitive field access denied")


def secret_digest(secret: str) -> str:
    settings = get_settings()
    return hmac.new(
        settings.jwt_secret.encode(), secret.encode(), hashlib.sha256
    ).hexdigest()


def new_api_secret() -> tuple[str, str, str]:
    secret = "hr_live_" + secrets.token_urlsafe(32)
    return secret, secret[:16], secret_digest(secret)


async def authenticate_api_key(
    db: AsyncSession, secret: str, required_scope: str
) -> OrganizationApiKey:
    now = datetime.now(UTC)
    digest = secret_digest(secret)
    if forced_tenant_rls():
        bootstrap = await bootstrap_api_key(db, digest=digest, now=now)
        if bootstrap is None:
            record = None
        else:
            await set_tenant_context(
                db,
                organization_id=bootstrap.organization_id,
                actor_id=bootstrap.created_by_user_id,
                identity_kind="api_key",
            )
            record = (
                await db.execute(
                    select(OrganizationApiKey).where(
                        OrganizationApiKey.id == bootstrap.api_key_id,
                        OrganizationApiKey.organization_id == bootstrap.organization_id,
                        OrganizationApiKey.key_digest == digest,
                        OrganizationApiKey.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
    else:
        record = (
            await db.execute(
                select(OrganizationApiKey).where(
                    OrganizationApiKey.key_digest == digest,
                    OrganizationApiKey.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if (
        record is None
        or (record.expires_at is not None and record.expires_at <= now)
        or required_scope not in record.scopes
    ):
        # Identical response for missing, expired, revoked, and under-scoped keys.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    await set_tenant_context(
        db,
        organization_id=record.organization_id,
        actor_id=record.created_by_user_id,
        identity_kind="api_key",
    )
    record.last_used_at = now
    await db.commit()
    return record


async def organization_llm_capability(
    db: AsyncSession,
    organization_id: UUID,
) -> dict[str, bool | str | None]:
    """Return sanitized AI readiness without contacting a model provider.

    This is deliberately a static control-plane check: it evaluates tenant
    routing, managed-processing consent, configured credentials, operator
    allowlisting, and hard contract caps, but never performs DNS or provider
    I/O. Runtime failures remain retryable through the durable gateway.
    """

    from app.services.entitlements import entitlement_snapshot

    config = (
        await db.execute(
            select(OrganizationModelConfiguration).where(
                OrganizationModelConfiguration.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    settings = get_settings()

    managed_acceptance_is_active = bool(
        config is not None
        and config.managed_processing_accepted_at is not None
        and config.managed_processing_revoked_at is None
        and config.managed_processing_notice_version
        == MANAGED_PROCESSING_NOTICE_VERSION
    )

    route: Literal["managed", "private"]
    reason: str | None = None
    if config is not None and config.provider in {
        "openai_compatible",
        "vllm",
        "ollama",
    }:
        route = "private"
        if config.status != "ready" or not config.base_url or not config.model:
            reason = "ai_private_provider_unavailable"
        else:
            try:
                await validate_model_base_url(config.base_url)
            except UnsafeModelEndpoint:
                reason = "ai_private_provider_unavailable"
    elif config is not None and config.provider == "openrouter":
        route = "managed"
        if not managed_acceptance_is_active:
            reason = "ai_managed_processing_opt_in_required"
        elif config.status != "ready" or not settings.openrouter_api_key:
            reason = "ai_managed_provider_unavailable"
    else:
        inherited_provider = settings.llm_provider.lower()
        route = "managed" if inherited_provider == "openrouter" else "private"
        if inherited_provider == "openrouter":
            if not managed_acceptance_is_active:
                reason = "ai_managed_processing_opt_in_required"
            elif not settings.openrouter_api_key:
                reason = "ai_managed_provider_unavailable"
        elif inherited_provider in {"vllm", "ollama", "openai_compatible"}:
            base_url = getattr(settings, "vllm_base_url", "http://localhost:8001/v1")
            try:
                await validate_model_base_url(base_url)
            except UnsafeModelEndpoint:
                reason = "ai_private_provider_unavailable"
        else:
            reason = "ai_provider_unavailable"
        if config is not None and config.status not in {"ready", "disabled"}:
            reason = "ai_provider_unavailable"

    if reason is None:
        for metric in ("ai_tokens", "ai_cost_microusd"):
            snapshot = await entitlement_snapshot(db, organization_id, metric)
            if snapshot.limit is not None and snapshot.used >= snapshot.limit:
                reason = "ai_budget_exceeded"
                break

    return {
        "available": reason is None,
        "route": route if reason is None else None,
        "reason": reason,
    }


async def organization_llm_client(
    db: AsyncSession,
    organization_id: UUID,
    *,
    fallback_factory=None,
    budget_resource_id: UUID | None = None,
    model_budget_cents: int | None = None,
):
    """Return the tenant-routed, residency-safe, budgeted model gateway."""

    from app.llm import BudgetedLLMClient, LLMConfigurationError, get_llm_client
    from app.llm.openrouter import OpenRouterClient
    from app.llm.vllm import VLLMClient

    config = (
        await db.execute(
            select(OrganizationModelConfiguration).where(
                OrganizationModelConfiguration.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    settings = get_settings()

    def managed_acceptance_is_active() -> bool:
        return bool(
            config is not None
            and config.managed_processing_accepted_at is not None
            and config.managed_processing_revoked_at is None
            and config.managed_processing_notice_version
            == MANAGED_PROCESSING_NOTICE_VERSION
        )

    if config is not None and config.provider in {
        "openai_compatible",
        "vllm",
        "ollama",
    }:
        if config.status != "ready" or not config.base_url or not config.model:
            raise LLMConfigurationError(
                "ai_private_provider_unavailable",
                "The tenant private model provider is not ready",
            )
        try:
            endpoint = await resolve_model_endpoint(config.base_url)
        except UnsafeModelEndpoint as exc:
            raise LLMConfigurationError(
                "ai_private_provider_unavailable",
                "The tenant private model endpoint failed its egress policy",
            ) from exc
        client = VLLMClient(endpoint=endpoint, default_model=config.model)
        return BudgetedLLMClient(
            client,
            db=db,
            organization_id=organization_id,
            provider=config.provider,
            managed=False,
            budget_resource_id=budget_resource_id,
            model_budget_cents=model_budget_cents,
        )
    if config is not None and config.provider == "openrouter":
        if not managed_acceptance_is_active():
            raise LLMConfigurationError(
                "ai_managed_processing_opt_in_required",
                "Tenant acceptance is required before managed AI processing",
            )
        if config.status != "ready" or not settings.openrouter_api_key:
            raise LLMConfigurationError(
                "ai_managed_provider_unavailable",
                "The managed AI provider is not ready",
            )
        client = OpenRouterClient(default_model=config.model)
        return BudgetedLLMClient(
            client,
            db=db,
            organization_id=organization_id,
            provider="openrouter",
            managed=True,
            budget_resource_id=budget_resource_id,
            model_budget_cents=model_budget_cents,
        )

    inherited_provider = settings.llm_provider.lower()
    if inherited_provider == "openrouter" and not managed_acceptance_is_active():
        raise LLMConfigurationError(
            "ai_managed_processing_opt_in_required",
            "Tenant acceptance is required before managed AI processing",
        )
    if config is not None and config.status not in {"ready", "disabled"}:
        raise LLMConfigurationError(
            "ai_provider_unavailable",
            "The tenant AI provider is not ready",
        )
    if inherited_provider in {"vllm", "ollama", "openai_compatible"}:
        # Operator-provided global private endpoints are subject to the same
        # exact allowlist and DNS/SSRF validation as tenant configuration.
        base_url = getattr(settings, "vllm_base_url", "http://localhost:8001/v1")
        try:
            endpoint = await resolve_model_endpoint(base_url)
        except UnsafeModelEndpoint as exc:
            raise LLMConfigurationError(
                "ai_private_provider_unavailable",
                "The inherited private model endpoint failed its egress policy",
            ) from exc
        client = VLLMClient(endpoint=endpoint)
    else:
        client = (fallback_factory or get_llm_client)()
    return BudgetedLLMClient(
        client,
        db=db,
        organization_id=organization_id,
        provider=inherited_provider,
        managed=inherited_provider == "openrouter",
        budget_resource_id=budget_resource_id,
        model_budget_cents=model_budget_cents,
    )
