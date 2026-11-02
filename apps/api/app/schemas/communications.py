"""Public contracts for candidate communications."""

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ContactChannel = Literal["email", "phone", "telegram", "whatsapp"]
MessageChannel = Literal["email", "telegram", "whatsapp"]
OutboundMessageChannel = Literal["email", "telegram"]
ProviderName = Literal[
    "sandbox",
    "google_workspace",
    "microsoft_365",
    "telegram",
    "smtp",
    "whatsapp",
]
CreatableProviderName = Literal[
    "sandbox", "google_workspace", "microsoft_365", "telegram"
]
ConnectionState = Literal[
    "unavailable",
    "disconnected",
    "connecting",
    "connected",
    "degraded",
    "expired",
    "revoked",
]


def _clean_text(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("value cannot be blank")
    return cleaned


class CandidateContactPointCreate(BaseModel):
    channel: ContactChannel
    value: str = Field(min_length=1, max_length=512)
    is_primary: bool = False

    _clean_value = field_validator("value")(_clean_text)


class CandidateContactPointUpdate(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=512)
    is_primary: bool | None = None
    is_opted_out: bool | None = None

    _clean_value = field_validator("value")(_clean_text)

    @model_validator(mode="after")
    def require_change(self) -> "CandidateContactPointUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class CandidateContactPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    channel: ContactChannel
    value: str
    source: str
    is_primary: bool
    is_opted_out: bool
    opted_out_at: datetime | None = None
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


_SECRET_PARTS = {"secret", "token", "password", "credential", "api", "key"}


def _reject_secret_configuration(value: Any, path: str = "configuration") -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            parts = {
                part
                for part in re.split(r"[^a-z0-9]+", str(key).strip().lower())
                if part
            }
            if parts & _SECRET_PARTS:
                raise ValueError(
                    f"Secret-like account setting is not allowed: {path}.{key}"
                )
            _reject_secret_configuration(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_configuration(child, f"{path}[{index}]")
    return value


class CommunicationAccountCreate(BaseModel):
    channel: OutboundMessageChannel
    provider: CreatableProviderName = "sandbox"
    integration_connection_id: UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    sender_label: str | None = Field(default=None, max_length=255)
    configuration: dict[str, Any] = Field(default_factory=dict)

    _clean_name = field_validator("name")(_clean_text)

    @field_validator("sender_label")
    @classmethod
    def clean_sender_label(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("configuration")
    @classmethod
    def reject_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > 4000:
            raise ValueError("configuration is too large")
        return _reject_secret_configuration(value)

    @model_validator(mode="after")
    def provider_matches_channel(self) -> "CommunicationAccountCreate":
        expected = {
            "google_workspace": "email",
            "microsoft_365": "email",
            "telegram": "telegram",
        }
        if self.provider == "sandbox" and self.integration_connection_id is not None:
            raise ValueError("Sandbox accounts cannot bind a provider connection")
        if self.provider != "sandbox" and self.integration_connection_id is None:
            raise ValueError("Connected accounts require integration_connection_id")
        if self.provider != "sandbox" and expected[self.provider] != self.channel:
            raise ValueError("Provider does not match account channel")
        return self


class CommunicationAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    integration_connection_id: UUID | None = None
    channel: MessageChannel
    provider: ProviderName
    name: str
    sender_label: str | None = None
    configuration: dict[str, Any]
    is_active: bool
    connection_status: ConnectionState | None = None
    required_capability: (
        Literal["sandbox.local", "email.send", "message.telegram.send"] | None
    )
    delivery_available: bool
    webhook_configured: bool = False
    created_at: datetime
    updated_at: datetime


class CommunicationAccountCreateOut(BaseModel):
    account: CommunicationAccountOut
    webhook_secret: str | None = None


MAX_DURABLE_BULK_RECIPIENTS = 1_000


class CommunicationCapabilitiesOut(BaseModel):
    synchronous_recipient_limit: int = Field(ge=1, le=100)
    durable_recipient_limit: int = Field(
        default=MAX_DURABLE_BULK_RECIPIENTS,
        ge=100,
        le=MAX_DURABLE_BULK_RECIPIENTS,
    )


class WebhookSecretOut(BaseModel):
    account_id: UUID
    webhook_secret: str


class CommunicationTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel: OutboundMessageChannel
    language: str = Field(default="ru", min_length=2, max_length=8)
    subject_template: str | None = Field(default=None, max_length=500)
    body_template: str = Field(min_length=1, max_length=20_000)
    is_active: bool = True

    _clean_name = field_validator("name")(_clean_text)
    _clean_body = field_validator("body_template")(_clean_text)


class CommunicationTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    language: str | None = Field(default=None, min_length=2, max_length=8)
    subject_template: str | None = Field(default=None, max_length=500)
    body_template: str | None = Field(default=None, min_length=1, max_length=20_000)
    is_active: bool | None = None

    _clean_name = field_validator("name")(_clean_text)
    _clean_body = field_validator("body_template")(_clean_text)

    @model_validator(mode="after")
    def require_change(self) -> "CommunicationTemplateUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        return self


class CommunicationTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    channel: MessageChannel
    language: str
    subject_template: str | None = None
    body_template: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CandidateCommunicationCompose(BaseModel):
    # WhatsApp is accepted at the transport boundary only so the service can
    # return the stable pilot capability error instead of a generic DTO error.
    channel: MessageChannel
    contact_point_id: UUID | None = None
    account_id: UUID | None = None
    template_id: UUID | None = None
    position_id: UUID | None = None
    application_id: UUID | None = None
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def require_one_content_source(self) -> "CandidateCommunicationCompose":
        if (self.template_id is None) == (self.body is None):
            raise ValueError("Provide exactly one of template_id or body")
        if self.template_id is not None and self.subject is not None:
            raise ValueError("subject is supplied by the selected template")
        if self.application_id is not None and self.position_id is None:
            raise ValueError("application_id requires position_id")
        return self


class CandidateCommunicationSend(CandidateCommunicationCompose):
    idempotency_key: str = Field(min_length=1, max_length=128)
    scheduled_for: datetime | None = None

    @field_validator("idempotency_key")
    @classmethod
    def clean_idempotency_key(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator("scheduled_for")
    @classmethod
    def require_aware_schedule(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("scheduled_for must include a timezone")
        return value


class CommunicationTargetOut(BaseModel):
    contact_point_id: UUID
    channel: OutboundMessageChannel
    masked_value: str
    account_id: UUID
    account_name: str
    provider: ProviderName


class CandidateCommunicationPreviewOut(BaseModel):
    candidate_id: UUID
    position_id: UUID | None = None
    application_id: UUID | None = None
    template_id: UUID | None = None
    target: CommunicationTargetOut
    subject: str | None = None
    body: str


class CommunicationMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    thread_id: UUID
    candidate_id: UUID
    contact_point_id: UUID | None = None
    account_id: UUID | None = None
    batch_id: UUID | None = None
    template_id: UUID | None = None
    actor_user_id: UUID | None = None
    direction: Literal["inbound", "outbound"]
    channel: MessageChannel
    status: Literal[
        "queued", "processing", "sent", "delivered", "received", "failed", "cancelled"
    ]
    subject: str | None = None
    body: str
    scheduled_for: datetime | None = None
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    received_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    failure_code: str | None = None
    created_at: datetime


class CommunicationThreadOut(BaseModel):
    id: UUID
    candidate_id: UUID
    application_id: UUID | None = None
    contact_point_id: UUID | None = None
    account_id: UUID | None = None
    position_id: UUID | None = None
    channel: MessageChannel
    status: Literal["open", "closed"]
    last_message_at: datetime | None = None
    unread_count: int = 0
    created_at: datetime
    latest_message: CommunicationMessageOut | None = None


class CommunicationInboxThreadOut(CommunicationThreadOut):
    candidate_source: str
    candidate_source_ref: str | None = None


class CommunicationThreadPage(BaseModel):
    items: list[CommunicationInboxThreadOut]
    next_cursor: str | None = None


class CommunicationMessagePage(BaseModel):
    items: list[CommunicationMessageOut]
    next_cursor: str | None = None


class BulkCommunicationPreviewRequest(CandidateCommunicationCompose):
    candidate_ids: list[UUID] = Field(
        min_length=1, max_length=MAX_DURABLE_BULK_RECIPIENTS
    )

    @model_validator(mode="after")
    def unique_candidates(self) -> "BulkCommunicationPreviewRequest":
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate_ids must be unique")
        if self.contact_point_id is not None:
            raise ValueError("contact_point_id cannot be shared across a bulk request")
        return self


class BulkCommunicationCreateRequest(BulkCommunicationPreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=128)
    scheduled_for: datetime | None = None

    _clean_idempotency = field_validator("idempotency_key")(_clean_text)

    @field_validator("scheduled_for")
    @classmethod
    def require_aware_schedule(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("scheduled_for must include a timezone")
        return value


class BulkCommunicationPreviewItem(BaseModel):
    candidate_id: UUID
    status: Literal["ready", "missing_contact", "opted_out"]
    preview: CandidateCommunicationPreviewOut | None = None


class BulkCommunicationPreviewOut(BaseModel):
    items: list[BulkCommunicationPreviewItem]
    ready_count: int
    skipped_count: int
    requested_count: int
    complete: bool
    delivery_mode: Literal["durable_queue"] = "durable_queue"
    synchronous_recipient_limit: int = Field(ge=1, le=100)


class CommunicationBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID | None = None
    template_id: UUID | None = None
    position_id: UUID | None = None
    channel: MessageChannel
    status: Literal[
        "queued", "processing", "completed", "partial", "failed", "cancelled"
    ]
    idempotency_key: str
    scheduled_for: datetime | None = None
    total_count: int
    requested_count: int
    skipped_count: int
    sent_count: int
    failed_count: int
    cancelled_count: int
    completed_at: datetime | None = None
    created_at: datetime


class BulkCommunicationCreateOut(BaseModel):
    batch: CommunicationBatchOut
    messages: list[CommunicationMessageOut]
    skipped: list[BulkCommunicationPreviewItem]
    created: bool
    queued_count: int
    messages_truncated: bool = False
    delivery_mode: Literal["durable_queue"] = "durable_queue"


class SandboxInboundRequest(BaseModel):
    candidate_id: UUID | None = None
    from_value: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=20_000)
    subject: str | None = Field(default=None, max_length=500)
    provider_message_id: str = Field(min_length=1, max_length=255)
    received_at: datetime | None = None

    _clean_from = field_validator("from_value")(_clean_text)
    _clean_body = field_validator("body")(_clean_text)

    @field_validator("received_at")
    @classmethod
    def require_aware_received_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("received_at must include a timezone")
        return value


class SandboxInboundWebhookOut(CommunicationMessageOut):
    """Backward-compatible message fields plus signed-envelope replay state."""

    webhook_event_id: str = Field(min_length=1, max_length=160)
    replay: bool


class ProcessDueOut(BaseModel):
    claimed: int
    sent: int
    failed: int
