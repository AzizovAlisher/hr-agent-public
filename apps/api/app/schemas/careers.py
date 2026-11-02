"""Stable public contract for company careers-site integrations."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class PublicCareersOrganization(BaseModel):
    slug: str
    name: str
    logo_url: str | None = None
    accent_color: str | None = None


class PublicCareerPosition(BaseModel):
    id: UUID
    title: str
    description: str | None = None
    full_description: str | None = None
    requirements: dict[str, Any] | None = None

    # Common fields are lifted out of requirements for simple website cards.
    company: str
    location: str | None = None
    work_format: str | None = None
    employment_type: str | None = None
    salary_range: str | None = None
    seniority: str | None = None

    apply_url: str
    api_url: str
    created_at: datetime
    updated_at: datetime


class PublicCareerPositionPage(BaseModel):
    organization: PublicCareersOrganization
    items: list[PublicCareerPosition]
    total: int
    next_cursor: str | None = None


class PublicCareerApplicationAccepted(BaseModel):
    accepted: Literal[True] = True
    message: str
