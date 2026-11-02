"""Request, durable-job, and provider correlation without tenant data.

Correlation identifiers belong in logs and traces, where they are useful for a
single investigation. They never become metric labels. Provider, operation,
and queue names are reduced to bounded vocabularies before they enter either
context or telemetry.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from uuid import UUID

KNOWN_JOB_QUEUES = frozenset(
    {
        "ai-hiring",
        "candidate-processing",
        "candidate_processing_jobs",
        "communication_messages",
        "communications",
        "integration_jobs",
        "integration_webhook_deliveries",
        "integrations",
        "interview_reminders",
        "legacy-private-object-deletions",
        "legacy_private_object_deletions",
        "maintenance",
        "mission_steps",
        "private-object-deletions",
        "private_object_deletions",
        "reminders",
        "workflow_runs",
        "workflows",
    }
)
KNOWN_PROVIDERS = frozenset(
    {
        "assessment_generic",
        "csv_xlsx",
        "generic_webhook",
        "google_workspace",
        "hh_ru",
        "microsoft_365",
        "openrouter",
        "private_vllm",
        "sandbox",
        "somon",
        "telegram",
    }
)
KNOWN_PROVIDER_OPERATIONS = frozenset(
    {
        "assessment_invitation",
        "assessment_result",
        "calendar_create",
        "calendar_delete",
        "calendar_update",
        "email_send",
        "health",
        "oauth_exchange",
        "oauth_refresh",
        "oauth_revoke",
        "source_import",
        "source_publish",
        "telegram_send",
        "webhook_delivery",
    }
)

_PROVIDER_JOB_OPERATION_GROUPS = {
    "assessment.callback": "assessment_result",
    "assessment.invite": "assessment_invitation",
    "assessment.result": "assessment_result",
    "assessment.status": "assessment_result",
    "calendar.create": "calendar_create",
    "calendar.delete": "calendar_delete",
    "calendar.sync": "calendar_update",
    "calendar.update": "calendar_update",
    "candidate.import": "source_import",
    "email.send": "email_send",
    "telegram.send": "telegram_send",
    "vacancy.publish": "source_publish",
}


def normalized_job_queue(value: str | None) -> str:
    return value if value in KNOWN_JOB_QUEUES else "other"


def normalized_provider(value: str | None) -> str:
    return value if value in KNOWN_PROVIDERS else "other"


def normalized_provider_operation(value: str | None) -> str:
    return value if value in KNOWN_PROVIDER_OPERATIONS else "other"


def provider_operation_group(value: str | None) -> str:
    """Map durable integration operation IDs onto a bounded metric contract."""

    return normalized_provider_operation(
        _PROVIDER_JOB_OPERATION_GROUPS.get(value, value)
    )


def normalized_uuid(value: UUID | str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(value if isinstance(value, UUID) else UUID(value))
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    request_id: str | None = None
    job_id: str | None = None
    job_queue: str | None = None
    provider: str | None = None
    provider_operation: str | None = None

    def as_log_fields(self) -> dict[str, str | None]:
        return {
            "correlation_request_id": self.request_id,
            "correlation_job_id": self.job_id,
            "correlation_job_queue": self.job_queue,
            "correlation_provider": self.provider,
            "correlation_provider_operation": self.provider_operation,
        }


_EMPTY_CONTEXT = CorrelationContext()
_correlation: ContextVar[CorrelationContext] = ContextVar(
    "hr_observability_correlation",
    default=_EMPTY_CONTEXT,
)


def current_correlation() -> CorrelationContext:
    return _correlation.get()


@contextmanager
def request_correlation(request_id: UUID | str) -> Iterator[CorrelationContext]:
    value = normalized_uuid(request_id)
    token = _correlation.set(replace(_correlation.get(), request_id=value))
    try:
        yield _correlation.get()
    finally:
        _correlation.reset(token)


@contextmanager
def job_correlation(
    *,
    queue: str,
    job_id: UUID | str,
) -> Iterator[CorrelationContext]:
    token: Token[CorrelationContext] = _correlation.set(
        replace(
            _correlation.get(),
            job_id=normalized_uuid(job_id),
            job_queue=normalized_job_queue(queue),
        )
    )
    try:
        yield _correlation.get()
    finally:
        _correlation.reset(token)


@contextmanager
def provider_correlation(
    *,
    provider: str,
    operation: str,
) -> Iterator[CorrelationContext]:
    token: Token[CorrelationContext] = _correlation.set(
        replace(
            _correlation.get(),
            provider=normalized_provider(provider),
            provider_operation=normalized_provider_operation(operation),
        )
    )
    try:
        yield _correlation.get()
    finally:
        _correlation.reset(token)
