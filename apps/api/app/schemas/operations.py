"""API contracts for contract limits, usage, and operational health."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

EntitlementMetric = Literal[
    "seats",
    "active_jobs",
    "candidates",
    "candidate_import_rows",
    "assessment_starts",
    "ai_cost_microusd",
    "ai_tokens",
    "storage_bytes",
    "integration_runs",
]


class EntitlementUsageOut(BaseModel):
    metric: EntitlementMetric
    limit: int | None
    used: int
    remaining: int | None
    period: Literal["contract", "month", "day"]
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    basis: Literal["current_state", "periodic_cumulative"]


class EntitlementsOut(BaseModel):
    entitlements: list[EntitlementUsageOut]
    technical_limits: dict[str, int]


class UsageEventOut(BaseModel):
    id: str
    metric: EntitlementMetric
    amount: int
    kind: Literal["reserve", "usage", "settle", "refund", "adjustment"]
    resource_type: str | None
    resource_id: str | None
    occurred_at: datetime


class UsagePage(BaseModel):
    items: list[UsageEventOut]
    next_cursor: str | None


class ComponentHealthOut(BaseModel):
    status: Literal["ok", "degraded", "failed"]
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ReadinessOut(BaseModel):
    status: Literal["ready", "not_ready"]
    checked_at: datetime
    components: dict[str, ComponentHealthOut]


class PublicReadinessOut(BaseModel):
    status: Literal["ready", "not_ready"]
    checked_at: datetime


class UsageAdjustmentIn(BaseModel):
    metric: EntitlementMetric
    amount: int
    idempotency_key: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("amount")
    @classmethod
    def amount_must_be_nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("amount must not be zero")
        return value
