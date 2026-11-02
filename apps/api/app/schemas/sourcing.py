"""Sourcing integration, import, publication, and referral contracts."""

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.services.source_provenance import configured_source_slug

SourceProvider = Literal["hh", "somon", "telegram", "csv", "generic_webhook"]
SourceCapabilityState = Literal[
    "available", "sandbox", "credentials_not_configured", "unsupported"
]
IntegrationProviderState = Literal[
    "unavailable",
    "disconnected",
    "connecting",
    "connected",
    "degraded",
    "expired",
    "revoked",
]
SourceJobState = Literal[
    "queued", "running", "retrying", "succeeded", "dead_letter", "cancelled"
]


_SECRET_PARTS = {"secret", "token", "password", "credential", "api", "key"}


def _reject_secrets(value: Any, path: str = "configuration") -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            parts = {part for part in re.split(r"[^a-z0-9]+", str(key).lower()) if part}
            if parts & _SECRET_PARTS:
                raise ValueError(f"Secret-like setting is not allowed: {path}.{key}")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")
    return value


def _safe_configuration(value: dict[str, Any]) -> dict[str, Any]:
    """Require a small, JSON-safe, non-secret connector configuration."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("configuration must contain JSON values") from exc
    if len(encoded) > 8_000:
        raise ValueError("configuration is too large")
    mode = value.get("mode")
    if mode is not None and mode not in {"live", "sandbox"}:
        raise ValueError("configuration.mode must be live or sandbox")
    return _reject_secrets(value)


class SourceCapabilitiesOut(BaseModel):
    candidate_import: SourceCapabilityState
    file_import: SourceCapabilityState
    webhook_intake: SourceCapabilityState
    vacancy_publish: SourceCapabilityState


class SourceIntegrationCreate(BaseModel):
    provider: SourceProvider
    name: str = Field(min_length=1, max_length=160)
    connection_id: UUID | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @field_validator("configuration")
    @classmethod
    def safe_configuration(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_configuration(value)

    @model_validator(mode="after")
    def valid_connection_mode(self) -> "SourceIntegrationCreate":
        sandbox = self.configuration.get("mode") == "sandbox"
        if sandbox and self.provider not in {"hh", "telegram"}:
            raise ValueError("sandbox mode is available only for hh.ru and Telegram")
        if sandbox and self.connection_id is not None:
            raise ValueError(
                "sandbox mode cannot be bound to a live provider connection"
            )
        if not sandbox and self.connection_id is None:
            raise ValueError("connection_id is required outside explicit sandbox mode")
        return self


class SourceIntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    connection_id: UUID | None = None
    configuration: dict[str, Any] | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @field_validator("configuration")
    @classmethod
    def safe_configuration(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _safe_configuration(value) if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "SourceIntegrationUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class SourceIntegrationOut(BaseModel):
    id: UUID
    connection_id: UUID | None = None
    connection_provider: str | None = None
    connection_status: IntegrationProviderState | None = None
    provider: SourceProvider
    name: str
    configuration: dict[str, Any]
    is_active: bool
    status: Literal["ready", "credentials_not_configured", "error", "disabled"]
    webhook_configured: bool
    capabilities: SourceCapabilitiesOut
    last_sync_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceIntegrationPage(BaseModel):
    items: list[SourceIntegrationOut]
    next_cursor: str | None = None


class SourceIntegrationCreatedOut(BaseModel):
    integration: SourceIntegrationOut
    webhook_secret: str | None


class SourceWebhookSecretOut(BaseModel):
    integration_id: UUID
    webhook_secret: str


class SourceCandidateInput(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    position_id: UUID
    name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    cv_text: str = Field(min_length=1, max_length=200_000)
    source_url: str | None = Field(default=None, max_length=1_024)
    utm_campaign: str | None = Field(default=None, max_length=160)

    @model_validator(mode="before")
    @classmethod
    def blank_optional_values_are_null(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        for field in ("phone", "email", "source_url", "utm_campaign"):
            if isinstance(cleaned.get(field), str) and not cleaned[field].strip():
                cleaned[field] = None
        return cleaned

    @field_validator("external_id", "name", "cv_text")
    @classmethod
    def clean_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

    @field_validator("source_url", "utm_campaign")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        digits = re.sub(r"\D", "", cleaned)
        if not 7 <= len(digits) <= 15:
            raise ValueError("phone must contain 7 to 15 digits")
        return cleaned


class SourceImportRequest(BaseModel):
    # The route enforces the centralized technical limit so an oversized
    # request returns the stable ``limit_exceeded`` problem code rather than a
    # generic validation error.
    candidates: list[SourceCandidateInput] = Field(min_length=1)


class SourceImportRunOut(BaseModel):
    id: UUID
    integration_id: UUID | None
    provider: str
    idempotency_key: str | None = None
    status: Literal["processing", "completed", "partial", "failed"]
    requested_count: int
    created_count: int
    duplicate_count: int
    failed_count: int
    error_code: str | None
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class SourceImportRunPage(BaseModel):
    items: list[SourceImportRunOut]
    next_cursor: str | None = None


class SourceWebhookOut(BaseModel):
    event_id: str
    replay: bool
    candidate_id: UUID
    created: bool
    run: SourceImportRunOut


class ReferralLinkCreate(BaseModel):
    label: str = Field(min_length=1, max_length=160)
    campaign: str | None = Field(default=None, max_length=160)
    source: str = Field(default="referral", min_length=1, max_length=32)
    expires_at: datetime | None = None
    max_applications: int | None = Field(default=None, ge=1, le=100_000)

    @field_validator("label")
    @classmethod
    def clean_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

    @field_validator("source")
    @classmethod
    def clean_source(cls, value: str) -> str:
        return configured_source_slug(value)

    @field_validator("campaign")
    @classmethod
    def clean_campaign(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at must include a timezone")
        return value


class ReferralLinkRotate(BaseModel):
    expires_in_days: int = Field(default=30, ge=1, le=90)


class ReferralLinkOut(BaseModel):
    id: UUID
    position_id: UUID | None
    label: str
    campaign: str | None
    source: str
    is_active: bool
    expires_at: datetime
    max_applications: int | None
    click_count: int
    application_count: int
    url: str | None = None
    created_at: datetime
    updated_at: datetime


class ReferralLinkCreatedOut(BaseModel):
    link: ReferralLinkOut
    token: str


class PublicReferralOut(BaseModel):
    position_id: UUID
    apply_url: str
    source: str
    campaign: str | None


class SourcePublicationPreviewRequest(BaseModel):
    position_id: UUID
    locale: Literal["ru", "en"] = "ru"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("idempotency_key cannot be blank")
        return cleaned


class SourcePublicationOut(BaseModel):
    id: UUID | None = None
    integration_id: UUID
    position_id: UUID
    provider: str
    status: str
    tracking_url: str
    post_text: str
    failure_code: str | None = None
    external_id: str | None = None
    job_id: UUID | None = None
    job_status: SourceJobState | None = None
    job_attempt_count: int | None = None
    job_max_attempts: int | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None


class SourcePublicationPage(BaseModel):
    items: list[SourcePublicationOut]
    next_cursor: str | None = None


class SourceHeadHunterImportRequest(BaseModel):
    """Bounded hh.ru negotiation synchronization into one local position."""

    position_id: UUID
    vacancy_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,255}$")
    collection_ids: list[str] = Field(default_factory=list, max_length=20)
    max_pages: int = Field(default=10, ge=1, le=20)
    max_records: int = Field(default=500, ge=1, le=500)
    resume_from_cursor: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("collection_ids")
    @classmethod
    def valid_collection_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = item.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", normalized):
                raise ValueError("collection_ids contains an invalid identifier")
            if normalized not in seen:
                cleaned.append(normalized)
                seen.add(normalized)
        return cleaned

    @field_validator("idempotency_key")
    @classmethod
    def clean_hh_idempotency_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("idempotency_key cannot be blank")
        return cleaned


class SourceJobOut(BaseModel):
    id: UUID
    connection_id: UUID
    operation: str
    status: SourceJobState
    attempt_count: int
    max_attempts: int
    failure_code: str | None = None
    result_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SourceAnalyticsItem(BaseModel):
    source: str
    applications: int
    hired: int
    conversion_rate: float


class SourceAnalyticsOut(BaseModel):
    items: list[SourceAnalyticsItem]
    total_applications: int


class SourceImportPreviewItem(BaseModel):
    row: int
    external_id: str
    position_id: UUID
    name: str
    masked_phone: str | None = None
    masked_email: str | None = None
    cv_characters: int
    source_url: str | None = None


class SourceImportFilePreviewOut(BaseModel):
    format: Literal["csv", "xlsx"]
    row_count: int
    mapping: dict[str, str]
    items: list[SourceImportPreviewItem]
    truncated: bool
    import_mode: Literal["synchronous", "durable_job"]
    synchronous_row_limit: int = Field(ge=1)
    synchronous_file_bytes: int = Field(ge=1)
    absolute_row_limit: int = Field(ge=1)
    absolute_file_bytes: int = Field(ge=1)
