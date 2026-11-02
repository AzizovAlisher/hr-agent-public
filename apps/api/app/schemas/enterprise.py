"""Public contracts for organization-level enterprise controls."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from app.auth.authorization import PERMISSIONS, FieldClass

Role = Literal[
    "admin",
    "recruiter",
    "hiring_manager",
    "interviewer",
    "restricted",
]


class ApprovalSeparationOfDuties(BaseModel):
    """Complete, strict policy object for creator/approver separation."""

    model_config = ConfigDict(extra="forbid")

    requisitions: StrictBool
    offers: StrictBool
    hiring_decisions: StrictBool


class EnterprisePolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_retention_days: int
    deleted_retention_days: int
    audit_retention_days: int
    candidate_export_enabled: bool
    backup_export_enabled: bool
    approval_separation_of_duties: ApprovalSeparationOfDuties
    field_visibility: dict[str, list[str]]
    updated_at: datetime


class EnterprisePolicyUpdate(BaseModel):
    candidate_retention_days: int | None = Field(default=None, ge=30, le=730)
    deleted_retention_days: int | None = Field(default=None, ge=1, le=3650)
    audit_retention_days: int | None = Field(default=None, ge=90, le=36500)
    candidate_export_enabled: bool | None = None
    backup_export_enabled: bool | None = None
    approval_separation_of_duties: ApprovalSeparationOfDuties | None = None
    field_visibility: dict[str, list[Role]] | None = None

    @field_validator("field_visibility")
    @classmethod
    def validate_visibility(
        cls, value: dict[str, list[Role]] | None
    ) -> dict[str, list[Role]] | None:
        if value is None:
            return value
        allowed_fields = {
            field.value
            for field in FieldClass
            if field is not FieldClass.INTERNAL
        }
        if not set(value).issubset(allowed_fields):
            raise ValueError("Unknown field visibility key")
        return {key: list(dict.fromkeys(roles)) for key, roles in value.items()}


class PermissionOverrideIn(BaseModel):
    role: Role
    permission: str = Field(min_length=1, max_length=64)
    allowed: bool

    @field_validator("permission")
    @classmethod
    def known_permission(cls, value: str) -> str:
        if value not in PERMISSIONS:
            raise ValueError("Unknown permission")
        return value


class PermissionOverrideOut(PermissionOverrideIn):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    updated_at: datetime


class EffectivePermissionsOut(BaseModel):
    role: Role
    permissions: dict[str, bool]


class AuditActorOut(BaseModel):
    id: UUID | None
    email: str | None


class AuditLogOut(BaseModel):
    id: UUID
    actor: AuditActorOut
    action: str
    resource_type: str | None
    resource_id: UUID | None
    path_template: str
    method: str
    status_code: int
    request_id: UUID
    metadata: dict
    occurred_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    next_cursor: str | None


class TrashItemOut(BaseModel):
    id: UUID
    resource_type: Literal["candidate", "position"]
    label: str
    deleted_at: datetime
    deleted_by_user_id: UUID | None


class TrashPage(BaseModel):
    items: list[TrashItemOut]
    next_cursor: str | None = None


class RetentionPreviewOut(BaseModel):
    deleted_candidates: int
    deleted_positions: int
    old_active_candidates: int
    expired_audit_logs: int


class RetentionRunRequest(BaseModel):
    confirmation: Literal["PURGE"]
    include_active_candidates: bool = False


class RetentionRunOut(RetentionPreviewOut):
    purged_at: datetime


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    scopes: list[
        Literal["candidates:read", "candidates:contact", "positions:read"]
    ] = Field(
        default_factory=lambda: ["candidates:read"]
    )
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("At least one scope is required")
        unique = list(dict.fromkeys(value))
        if "candidates:contact" in unique and "candidates:read" not in unique:
            raise ValueError("candidates:contact requires candidates:read")
        return unique


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    key_prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreatedOut(ApiKeyOut):
    secret: str


class SsoConfigurationIn(BaseModel):
    provider: Literal["oidc", "saml"] = "oidc"
    issuer_url: str = Field(min_length=8, max_length=1024)
    client_id: str = Field(min_length=1, max_length=512)
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    is_enabled: bool = False
    enforce_for_members: bool = False

    @field_validator("issuer_url")
    @classmethod
    def validate_issuer_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("https://"):
            raise ValueError("issuer_url must use https")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def clean_domains(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for domain in value:
            item = domain.strip().lower().lstrip("@")
            if not item or "." not in item or any(char.isspace() for char in item):
                raise ValueError("Invalid email domain")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class SsoConfigurationOut(SsoConfigurationIn):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    updated_at: datetime


class ModelConfigurationIn(BaseModel):
    provider: Literal[
        "inherit", "openrouter", "openai_compatible", "vllm", "ollama"
    ] = "inherit"
    base_url: str | None = Field(default=None, max_length=1024)
    model: str | None = Field(default=None, max_length=255)
    data_residency: Literal["provider", "private_cloud", "on_prem"] = "provider"
    managed_processing_accepted: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        return value

    @model_validator(mode="after")
    def protect_managed_provider_credentials(self) -> "ModelConfigurationIn":
        if self.provider == "openrouter" and self.base_url is not None:
            raise ValueError("openrouter uses the application-managed endpoint")
        if self.provider == "openrouter" and not self.managed_processing_accepted:
            raise ValueError(
                "managed OpenRouter processing requires explicit tenant acceptance"
            )
        if self.provider == "openrouter" and self.data_residency != "provider":
            raise ValueError("openrouter data residency is the managed provider")
        private_providers = {"openai_compatible", "vllm", "ollama"}
        if self.provider in {"inherit", "openrouter"} and self.base_url is not None:
            raise ValueError("inherited and managed providers do not accept base_url")
        if self.provider in private_providers and not (self.base_url and self.model):
            raise ValueError("private providers require base_url and model")
        if self.provider in private_providers and self.data_residency == "provider":
            raise ValueError("private providers require private_cloud or on_prem residency")
        if self.provider in private_providers and self.managed_processing_accepted:
            raise ValueError(
                "private providers do not use managed-processing acceptance"
            )
        return self


class ModelConfigurationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    base_url: str | None
    model: str | None
    data_residency: str
    status: str
    managed_processing_accepted_at: datetime | None
    managed_processing_accepted_by_user_id: UUID | None
    managed_processing_notice_version: str | None
    managed_processing_revoked_at: datetime | None
    updated_at: datetime


class EnterpriseApiCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    # Candidate contact data is projected only for API keys explicitly granted
    # both ``candidates:read`` and ``candidates:contact``.  The enterprise
    # endpoint also excludes ``None`` values, so read-only keys cannot infer a
    # contact field's presence from its serialized value.
    source_ref: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    created_at: datetime


class EnterpriseApiPositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: str
    created_at: datetime
