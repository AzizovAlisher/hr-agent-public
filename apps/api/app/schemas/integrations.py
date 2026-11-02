"""Public, secret-safe contracts for provider connections and durable work."""

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

IntegrationProvider = Literal[
    "google_workspace",
    "microsoft_365",
    "telegram",
    "hh_ru",
    "somon",
    "csv_xlsx",
    "generic_webhook",
    "assessment_generic",
]
IntegrationState = Literal[
    "unavailable",
    "disconnected",
    "connecting",
    "connected",
    "degraded",
    "expired",
    "revoked",
]
IntegrationCapability = Literal[
    "email.send",
    "calendar.read",
    "calendar.write",
    "message.telegram.send",
    "vacancy.publish",
    "candidate.import",
    "file.import",
    "tracked_link.create",
    "webhook.receive",
    "webhook.deliver",
    "assessment.invite",
    "assessment.status",
    "assessment.result",
    "assessment.delete",
]

_SECRET_PARTS = {
    "access",
    "authorization",
    "bearer",
    "credential",
    "password",
    "private",
    "refresh",
    "secret",
    "token",
}
_SAFE_CONFIG_KEYS: dict[str, set[str]] = {
    "google_workspace": {"calendar_id", "sender_email"},
    "microsoft_365": {"tenant", "sender_email"},
    "telegram": {"chat_id", "channel_username", "parse_mode"},
    "hh_ru": {"employer_id", "locale"},
    "somon": {"campaign", "source_label"},
    "csv_xlsx": {"default_position_id", "source_label"},
    "generic_webhook": {"target_url", "event_types"},
    "assessment_generic": {"base_url", "provider_label"},
}
_CREDENTIAL_KEYS: dict[str, set[str]] = {
    "google_workspace": {
        "client_id",
        "client_secret",
        "access_token",
        "refresh_token",
    },
    "microsoft_365": {
        "client_id",
        "client_secret",
        "access_token",
        "refresh_token",
    },
    "telegram": {"bot_token", "webhook_secret"},
    "hh_ru": {
        "client_id",
        "client_secret",
        "access_token",
        "refresh_token",
    },
    "generic_webhook": {"signing_secret"},
    "assessment_generic": {"api_key", "callback_signing_secret"},
    "somon": set(),
    "csv_xlsx": set(),
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def _json_size(value: Any, *, maximum: int, label: str) -> Any:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON values") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} is too large")
    return value


def validate_non_secret_configuration(
    provider: str, configuration: dict[str, Any]
) -> dict[str, Any]:
    _json_size(configuration, maximum=8_000, label="configuration")
    unknown = set(configuration) - _SAFE_CONFIG_KEYS.get(provider, set())
    if unknown:
        raise ValueError(
            "Unsupported configuration keys: " + ", ".join(sorted(unknown))
        )
    for key in configuration:
        parts = {part for part in re.split(r"[^a-z0-9]+", key.strip().lower()) if part}
        if parts & _SECRET_PARTS:
            raise ValueError(f"Secret-like configuration key is not allowed: {key}")
    if configuration.get("parse_mode") not in {None, "HTML", "MarkdownV2"}:
        raise ValueError("parse_mode must be HTML or MarkdownV2")
    tenant = configuration.get("tenant")
    if tenant is not None and not re.fullmatch(
        r"(?:common|organizations|consumers|[0-9a-fA-F-]{36})", str(tenant)
    ):
        raise ValueError("tenant must be a Microsoft tenant ID or supported alias")
    event_types = configuration.get("event_types")
    if event_types is not None:
        if not isinstance(event_types, list) or not 1 <= len(event_types) <= 100:
            raise ValueError("event_types must contain 1 to 100 values")
        if any(
            not isinstance(item, str) or not _ID_RE.fullmatch(item)
            for item in event_types
        ):
            raise ValueError("event_types contains an invalid event type")
    return configuration


class IntegrationProviderOut(BaseModel):
    provider: IntegrationProvider
    capabilities: list[IntegrationCapability]
    oauth: bool
    credential_keys: list[str]
    locally_available: bool
    unavailable_reason: str | None = None


class IntegrationConnectionCreate(BaseModel):
    provider: IntegrationProvider
    name: str = Field(min_length=1, max_length=160)
    configuration: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def safe_configuration(self) -> "IntegrationConnectionCreate":
        self.configuration = validate_non_secret_configuration(
            self.provider, self.configuration
        )
        return self


class IntegrationConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    configuration: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def require_change(self) -> "IntegrationConnectionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be supplied")
        return self


class IntegrationConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: IntegrationProvider
    name: str
    status: IntegrationState
    capabilities: list[IntegrationCapability]
    configuration: dict[str, Any]
    provider_account_id: str | None = None
    granted_scopes: list[str]
    credentials_configured: bool
    credential_expires_at: datetime | None = None
    last_health_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_code: str | None = None
    consecutive_failures: int
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class IntegrationConnectionCreatedOut(BaseModel):
    connection: IntegrationConnectionOut
    webhook_signing_secret: str | None = None


class IntegrationConnectionPage(BaseModel):
    items: list[IntegrationConnectionOut]
    next_cursor: str | None = None


class IntegrationCredentialSet(BaseModel):
    credentials: dict[str, SecretStr] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded_values(self) -> "IntegrationCredentialSet":
        for key, value in self.credentials.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise ValueError("Invalid credential key")
            secret = value.get_secret_value()
            if not secret or len(secret.encode()) > 16_384:
                raise ValueError("Credential values must be 1 to 16384 bytes")
        return self


class IntegrationCredentialsOut(BaseModel):
    connection_id: UUID
    configured_keys: list[str]
    status: IntegrationState
    credentials_updated_at: datetime


class IntegrationOAuthStart(BaseModel):
    redirect_uri: HttpUrl


class IntegrationOAuthStartOut(BaseModel):
    authorization_url: str
    expires_at: datetime


class IntegrationOAuthCallback(BaseModel):
    state: str = Field(min_length=32, max_length=512)
    code: SecretStr = Field(min_length=1, max_length=8_192)


class IntegrationHealthOut(BaseModel):
    connection_id: UUID
    status: IntegrationState
    checked_at: datetime
    error_code: str | None = None
    capabilities: list[IntegrationCapability]


class IntegrationJobCreate(BaseModel):
    operation: str = Field(min_length=1, max_length=96)
    idempotency_key: str = Field(min_length=1, max_length=160)
    references: dict[str, str | int | bool | None] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("operation", "idempotency_key")
    @classmethod
    def clean_identifier(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

    @field_validator("references")
    @classmethod
    def safe_references(
        cls, value: dict[str, str | int | bool | None]
    ) -> dict[str, str | int | bool | None]:
        _json_size(value, maximum=4_000, label="references")
        for key, child in value.items():
            parts = set(re.split(r"[^a-z0-9]+", key.lower()))
            if parts & _SECRET_PARTS or parts & {
                "body",
                "contact",
                "cv",
                "email",
                "name",
                "phone",
                "prompt",
            }:
                raise ValueError(f"Sensitive job reference is not allowed: {key}")
            if isinstance(child, str) and len(child) > 1_024:
                raise ValueError("Job reference values are too long")
        return value


class ProviderEmailJobCreate(BaseModel):
    to: EmailStr
    subject: str = Field(min_length=1, max_length=500)
    body_text: str = Field(min_length=1, max_length=20_000)
    from_email: EmailStr | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)


class ProviderCalendarEventJobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    starts_at: datetime
    ends_at: datetime
    timezone: str = Field(min_length=1, max_length=64)
    attendees: list[EmailStr] = Field(default_factory=list, max_length=100)
    description: str | None = Field(default=None, max_length=20_000)
    location: str | None = Field(default=None, max_length=2_000)
    video_conference: bool = False
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def valid_times(self) -> "ProviderCalendarEventJobCreate":
        if (
            self.starts_at.tzinfo is None
            or self.ends_at.tzinfo is None
            or self.starts_at >= self.ends_at
        ):
            raise ValueError(
                "Calendar event requires ordered timezone-aware timestamps"
            )
        if len(set(self.attendees)) != len(self.attendees):
            raise ValueError("attendees must be unique")
        return self


class ProviderCalendarEventUpdateJobCreate(ProviderCalendarEventJobCreate):
    event_id: str = Field(min_length=1, max_length=255)


class ProviderCalendarEventDeleteJobCreate(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ProviderTelegramMessageJobCreate(BaseModel):
    chat_id: str | None = Field(default=None, min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ProviderHeadHunterVacancyJobCreate(BaseModel):
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("payload")
    @classmethod
    def valid_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _json_size(value, maximum=128_000, label="payload")
        required = {
            "name",
            "description",
            "area",
            "employment",
            "experience",
            "schedule",
            "type",
        }
        if not required.issubset(value):
            raise ValueError(
                "hh.ru vacancy payload is missing required official fields"
            )
        return value


class ProviderHeadHunterImportJobCreate(BaseModel):
    vacancy_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=160)


class IntegrationJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    connection_id: UUID
    operation: str
    status: Literal[
        "queued", "running", "retrying", "succeeded", "dead_letter", "cancelled"
    ]
    idempotency_key: str
    reference_payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    leased_at: datetime | None = None
    lease_expires_at: datetime | None = None
    failure_code: str | None = None
    result_summary: dict[str, Any]
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class IntegrationJobPage(BaseModel):
    items: list[IntegrationJobOut]
    next_cursor: str | None = None


class WebhookEnvelope(BaseModel):
    version: Literal["2026-08-01"] = "2026-08-01"
    id: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=160)
    created_at: datetime
    data: dict[str, Any]

    @field_validator("id", "type")
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("Invalid webhook identifier")
        return value

    @field_validator("created_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_size(value, maximum=256_000, label="data")


class WebhookAcceptedOut(BaseModel):
    event_id: str
    accepted: bool
    replay: bool


class WebhookDeliveryCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    type: str = Field(min_length=1, max_length=160)
    data: dict[str, Any]

    @field_validator("type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("Invalid webhook identifier")
        return value

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _json_size(value, maximum=256_000, label="data")


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    connection_id: UUID
    event_id: str
    envelope_version: str
    event_type: str
    target_url: str
    payload_sha256: str
    status: Literal[
        "queued", "delivering", "retrying", "delivered", "dead_letter", "cancelled"
    ]
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    last_response_status: int | None = None
    failure_code: str | None = None
    delivered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryPage(BaseModel):
    items: list[WebhookDeliveryOut]
    next_cursor: str | None = None


class WebhookDeliveryAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    delivery_id: UUID
    attempt_number: int
    status: Literal["delivering", "delivered", "retrying", "failed"]
    response_status: int | None = None
    failure_code: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime


class WebhookDeliveryDetailOut(BaseModel):
    delivery: WebhookDeliveryOut
    attempts: list[WebhookDeliveryAttemptOut]


class AssessmentProviderInvitationCreate(BaseModel):
    application_id: UUID
    template_version_id: UUID | None = None
    external_template_ref: str = Field(min_length=1, max_length=255)
    expires_at: datetime
    locale: Literal["ru", "en"] = "ru"
    accommodations: dict[str, bool | int | str] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("external_template_ref", "idempotency_key")
    @classmethod
    def clean_reference(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value cannot be blank")
        return cleaned

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @field_validator("accommodations")
    @classmethod
    def bounded_accommodations(
        cls, value: dict[str, bool | int | str]
    ) -> dict[str, bool | int | str]:
        return _json_size(value, maximum=4_000, label="accommodations")


class AssessmentProviderInvitationOut(BaseModel):
    provider_invitation_id: str
    status: Literal["invited", "started", "submitted", "graded", "expired", "cancelled"]
    launch_url: str
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value


class AssessmentProviderPollRequest(BaseModel):
    provider_assessment_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=160)


class AssessmentProviderErasureRequest(BaseModel):
    """Encrypted, worker-only contract for a provider privacy deletion."""

    model_config = ConfigDict(extra="forbid")

    subject_ref: UUID
    provider_invitation_id: str | None = Field(
        default=None, min_length=1, max_length=255
    )
    provider_attempt_id: str | None = Field(default=None, min_length=1, max_length=255)


class AssessmentProviderAttemptOut(BaseModel):
    provider_attempt_id: str
    status: Literal["invited", "started", "submitted", "graded", "expired", "cancelled"]
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    updated_at: datetime

    @field_validator("started_at", "submitted_at", "updated_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("provider timestamps must include a timezone")
        return value


class AssessmentProviderResultOut(BaseModel):
    provider_attempt_id: str
    status: Literal["submitted", "graded"]
    score: float | None = Field(default=None, ge=0, le=100)
    recommendation: Literal["advance", "review", "do_not_advance"] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    completed_at: datetime

    @field_validator("evidence")
    @classmethod
    def bounded_evidence(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _json_size(value, maximum=128_000, label="evidence")

    @field_validator("completed_at")
    @classmethod
    def aware_completion(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at must include a timezone")
        return value


class AssessmentProviderAttemptCallback(AssessmentProviderAttemptOut):
    provider_invitation_id: str = Field(min_length=1, max_length=255)


class AssessmentProviderResultCallback(AssessmentProviderResultOut):
    provider_invitation_id: str = Field(min_length=1, max_length=255)


class ProviderAssessmentResultEvidenceOut(BaseModel):
    id: UUID
    provider_attempt_id: str
    status: Literal["submitted", "graded"]
    score: float | None = Field(default=None, ge=0, le=100)
    recommendation: Literal["advance", "review", "do_not_advance"] | None = None
    evidence: list[dict[str, Any]]
    provenance: dict[str, Any]
    completed_at: datetime
    provider_occurred_at: datetime
    version: int = Field(ge=1)


class ProviderAssessmentEvidenceOut(BaseModel):
    id: UUID
    connection_id: UUID
    application_id: UUID
    candidate_id: UUID
    template_version_id: UUID | None
    external_template_ref: str
    provider_invitation_id: str | None
    provider_attempt_id: str | None
    status: Literal[
        "queued",
        "invited",
        "started",
        "submitted",
        "graded",
        "expired",
        "cancelled",
        "failed",
    ]
    candidate_launch_url: str | None = None
    expires_at: datetime
    provider_updated_at: datetime | None = None
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    last_event_at: datetime | None = None
    last_job_status: (
        Literal[
            "queued", "running", "retrying", "succeeded", "dead_letter", "cancelled"
        ]
        | None
    ) = None
    failure_code: str | None = None
    result: ProviderAssessmentResultEvidenceOut | None = None
    created_at: datetime
    updated_at: datetime


class ProviderAssessmentApplicationEvidenceOut(BaseModel):
    application_id: UUID
    items: list[ProviderAssessmentEvidenceOut]
