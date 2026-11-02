"""Headcount requisition and candidate-offer contracts."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

Vote = Literal["approve", "reject"]
ChecklistItem = Literal["documents", "equipment", "accounts", "first_day"]


def _required_text(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} cannot be blank")
    return cleaned


class ApprovalVoteRequest(BaseModel):
    vote: Vote
    comment: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def rejection_reason(self) -> "ApprovalVoteRequest":
        if self.vote == "reject" and not (self.comment or "").strip():
            raise ValueError("A rejection reason is required")
        return self


class ApprovalOut(BaseModel):
    id: UUID
    user_id: UUID | None
    user_email: str
    subject_version: int
    approval_cycle: int
    vote: Vote
    comment: str | None
    created_at: datetime
    updated_at: datetime


class RequisitionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    department: str | None = Field(default=None, max_length=160)
    business_justification: str = Field(min_length=1, max_length=20_000)
    headcount: int = Field(default=1, ge=1, le=10_000)
    hiring_manager_user_id: UUID | None = None
    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_max: Decimal | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="TJS", pattern=r"^[A-Z]{3}$")
    salary_confidential: bool = True
    required_approvals: int = Field(default=1, ge=1, le=20)

    @field_validator("title", "business_justification")
    @classmethod
    def clean_required(cls, value: str) -> str:
        return _required_text(value, field="Value")

    @field_validator("department")
    @classmethod
    def clean_department(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def valid_salary(self) -> "RequisitionCreate":
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            raise ValueError("salary_min cannot exceed salary_max")
        return self


class RequisitionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    department: str | None = Field(default=None, max_length=160)
    business_justification: str | None = Field(default=None, min_length=1, max_length=20_000)
    headcount: int | None = Field(default=None, ge=1, le=10_000)
    hiring_manager_user_id: UUID | None = None
    salary_min: Decimal | None = Field(default=None, ge=0)
    salary_max: Decimal | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    salary_confidential: bool | None = None
    required_approvals: int | None = Field(default=None, ge=1, le=20)

    @field_validator("title", "business_justification")
    @classmethod
    def clean_required(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _required_text(value, field="Value")

    @field_validator("department")
    @classmethod
    def clean_department(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def require_change(self) -> "RequisitionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        nullable_fields = {
            "department",
            "hiring_manager_user_id",
            "salary_min",
            "salary_max",
        }
        invalid_nulls = {
            field
            for field in self.model_fields_set
            if field not in nullable_fields and getattr(self, field) is None
        }
        if invalid_nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(sorted(invalid_nulls))}")
        return self


class RequisitionOut(BaseModel):
    id: UUID
    title: str
    department: str | None
    business_justification: str
    headcount: int
    hiring_manager_user_id: UUID | None
    position_id: UUID | None
    salary_min: Decimal | None
    salary_max: Decimal | None
    salary_currency: str | None
    salary_confidential: bool
    compensation_visible: bool
    status: Literal["draft", "pending", "approved", "rejected", "opened", "cancelled"]
    required_approvals: int
    version: int
    approval_cycle: int
    approvals: list[ApprovalOut]
    submitted_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RequisitionOpenRequest(BaseModel):
    description: str | None = Field(default=None, max_length=20_000)
    requirements: dict | None = None
    public_apply_enabled: bool = True


class OfferTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    subject_template: str = Field(min_length=1, max_length=500)
    body_template: str = Field(min_length=1, max_length=30_000)

    @field_validator("name", "subject_template", "body_template")
    @classmethod
    def clean_required(cls, value: str) -> str:
        return _required_text(value, field="Value")


class OfferTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    subject_template: str | None = Field(default=None, min_length=1, max_length=500)
    body_template: str | None = Field(default=None, min_length=1, max_length=30_000)
    is_active: bool | None = None

    @field_validator("name", "subject_template", "body_template")
    @classmethod
    def clean_required(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _required_text(value, field="Value")

    @model_validator(mode="after")
    def require_change(self) -> "OfferTemplateUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Offer template fields cannot be null")
        return self


class OfferTemplateOut(BaseModel):
    id: UUID
    name: str
    subject_template: str
    body_template: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class CandidateOfferCreate(BaseModel):
    position_id: UUID
    application_id: UUID | None = None
    template_id: UUID | None = None
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=30_000)
    salary_amount: Decimal | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="TJS", pattern=r"^[A-Z]{3}$")
    start_date: date | None = None
    expires_at: datetime | None = None
    required_approvals: int = Field(default=1, ge=1, le=20)

    @field_validator("subject", "body")
    @classmethod
    def clean_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _required_text(value, field="Offer content")

    @model_validator(mode="after")
    def content_source(self) -> "CandidateOfferCreate":
        if self.template_id is None and (not self.subject or not self.body):
            raise ValueError("Provide a template or both subject and body")
        if self.template_id is not None and (self.subject is not None or self.body is not None):
            raise ValueError("Use either a template or manual subject and body, not both")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return self


class CandidateOfferUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=30_000)
    salary_amount: Decimal | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    start_date: date | None = None
    expires_at: datetime | None = None
    required_approvals: int | None = Field(default=None, ge=1, le=20)

    @field_validator("subject", "body")
    @classmethod
    def clean_content(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _required_text(value, field="Offer content")

    @model_validator(mode="after")
    def valid_update(self) -> "CandidateOfferUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided")
        nonnullable = {"subject", "body", "salary_currency", "required_approvals"}
        invalid_nulls = {
            field
            for field in self.model_fields_set
            if field in nonnullable and getattr(self, field) is None
        }
        if invalid_nulls:
            raise ValueError(f"Fields cannot be null: {', '.join(sorted(invalid_nulls))}")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return self


class CandidateOfferOut(BaseModel):
    id: UUID
    application_id: UUID | None
    candidate_id: UUID
    position_id: UUID | None
    position_title: str
    template_id: UUID | None
    template_name: str | None
    subject_source: str | None
    body_source: str | None
    subject: str | None
    body: str | None
    salary_amount: Decimal | None
    salary_currency: str | None
    compensation_visible: bool
    start_date: date | None
    expires_at: datetime
    required_approvals: int
    version: int
    approval_cycle: int
    status: Literal["draft", "pending", "approved", "sent", "accepted", "declined", "withdrawn", "expired"]
    approvals: list[ApprovalOut]
    sent_at: datetime | None
    responded_at: datetime | None
    signature_name: str | None
    created_at: datetime
    updated_at: datetime


class CandidateOfferSendOut(BaseModel):
    offer: CandidateOfferOut
    token: str
    url: str


class CandidateOfferRotateRequest(BaseModel):
    expires_in_days: int = Field(default=14, ge=1, le=30)


class PublicOfferOut(BaseModel):
    organization_name: str
    position_title: str
    subject: str
    body: str
    salary_amount: Decimal | None
    salary_currency: str
    start_date: date | None
    expires_at: datetime
    status: Literal["sent", "accepted", "declined", "expired"]


class PublicOfferRespondRequest(BaseModel):
    response: Literal["accept", "decline"]
    signature_name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def signature_for_accept(self) -> "PublicOfferRespondRequest":
        if self.signature_name is not None:
            self.signature_name = self.signature_name.strip() or None
        if self.response == "accept" and self.signature_name is None:
            raise ValueError("A typed signature is required to accept")
        return self


class CandidateHandoffOut(BaseModel):
    id: UUID
    application_id: UUID | None
    offer_id: UUID | None
    candidate_id: UUID
    position_id: UUID | None
    position_title: str
    status: Literal["pending", "in_progress", "completed"]
    checklist: dict
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CandidateHandoffUpdate(BaseModel):
    checklist: dict[ChecklistItem, bool] = Field(min_length=1)
