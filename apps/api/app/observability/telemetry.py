"""OpenTelemetry runtime and worker/provider observation scopes."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer

from app.observability.context import (
    current_correlation,
    job_correlation,
    normalized_job_queue,
    normalized_provider,
    normalized_provider_operation,
    provider_correlation,
)
from app.observability.metrics import observe_job, observe_provider


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for part in raw.split(","):
        key, separator, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value or not key.replace("-", "").isalnum():
            raise ValueError("OTLP headers must use comma-separated name=value pairs")
        headers[key] = value
    return headers


@dataclass(slots=True)
class TelemetryRuntime:
    tracer: Tracer
    provider: TracerProvider | None = None

    def shutdown(self) -> None:
        global _runtime
        if self.provider is not None:
            self.provider.shutdown()
            self.provider = None
        if _runtime is self:
            _runtime = TelemetryRuntime(trace.get_tracer("hr-agent.observability"))


_runtime = TelemetryRuntime(trace.get_tracer("hr-agent.observability"))


def current_runtime() -> TelemetryRuntime:
    return _runtime


def build_telemetry_runtime(
    settings, *, exporter: SpanExporter | None = None
) -> TelemetryRuntime:
    """Create an isolated SDK provider, or a no-op tracer when not configured."""

    global _runtime
    endpoint = settings.observability_otlp_traces_endpoint
    ratio = float(settings.observability_trace_sample_ratio)
    if ratio <= 0 or (endpoint is None and exporter is None):
        _runtime = TelemetryRuntime(trace.get_tracer("hr-agent.observability"))
        return _runtime

    provider = TracerProvider(
        resource=Resource.create(
            {
                SERVICE_NAME: settings.observability_service_name,
                DEPLOYMENT_ENVIRONMENT: settings.app_environment,
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    if exporter is None:
        raw_headers = (
            settings.observability_otlp_headers.get_secret_value()
            if settings.observability_otlp_headers is not None
            else None
        )
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=_parse_otlp_headers(raw_headers),
            timeout=settings.observability_export_timeout_seconds,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    _runtime = TelemetryRuntime(
        provider.get_tracer("hr-agent.observability"),
        provider,
    )
    return _runtime


_factory_installed = False
_original_record_factory = logging.getLogRecordFactory()


def install_correlation_log_record_factory() -> None:
    """Add safe structured context fields to all application log records."""

    global _factory_installed
    if _factory_installed:
        return

    def record_factory(*args, **kwargs):
        record = _original_record_factory(*args, **kwargs)
        for key, value in current_correlation().as_log_fields().items():
            setattr(record, key, value)
        span_context = trace.get_current_span().get_span_context()
        record.correlation_trace_id = (
            f"{span_context.trace_id:032x}" if span_context.is_valid else None
        )
        record.correlation_span_id = (
            f"{span_context.span_id:016x}" if span_context.is_valid else None
        )
        return record

    logging.setLogRecordFactory(record_factory)
    _factory_installed = True


@dataclass(slots=True)
class Observation:
    outcome: str = "succeeded"


def job_outcome_for_result(result: object) -> str:
    """Normalize worker return conventions without changing worker behavior."""

    if not isinstance(result, str):
        return "succeeded"
    normalized = result.lower()
    if normalized in {"cancelled", "missing", "not_due", "stale"}:
        return "cancelled"
    if normalized in {"dead_letter", "failed"}:
        return "failed"
    if normalized in {"retry_scheduled", "retrying"}:
        return "retrying"
    if normalized in {
        "already_deleted",
        "completed",
        "delivered",
        "sent",
        "succeeded",
    }:
        return "succeeded"
    return "other"


@contextmanager
def job_observation(
    *,
    queue: str,
    job_id: UUID | str,
) -> Iterator[Observation]:
    """Correlate and measure one durable job without metric ID labels."""

    started = monotonic()
    observation = Observation()
    safe_queue = normalized_job_queue(queue)
    with job_correlation(queue=safe_queue, job_id=job_id):
        correlation = current_correlation()
        with current_runtime().tracer.start_as_current_span(
            "durable job",
            kind=SpanKind.CONSUMER,
            attributes={
                "hr.job.queue": safe_queue,
                "hr.job.id": correlation.job_id or "invalid",
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield observation
            except BaseException as exc:
                observation.outcome = "failed"
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                outcome = (
                    observation.outcome
                    if observation.outcome
                    in {"succeeded", "failed", "retrying", "cancelled"}
                    else "other"
                )
                span.set_attribute("hr.job.outcome", outcome)
                observe_job(
                    queue=safe_queue,
                    outcome=outcome,
                    duration_seconds=monotonic() - started,
                )


@contextmanager
def provider_observation(
    *,
    provider: str,
    operation: str,
) -> Iterator[Observation]:
    """Correlate and measure one outbound provider call using bounded labels."""

    started = monotonic()
    observation = Observation()
    safe_provider = normalized_provider(provider)
    safe_operation = normalized_provider_operation(operation)
    with (
        provider_correlation(provider=safe_provider, operation=safe_operation),
        current_runtime().tracer.start_as_current_span(
            "provider operation",
            kind=SpanKind.CLIENT,
            attributes={
                "hr.provider": safe_provider,
                "hr.provider.operation": safe_operation,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span,
    ):
        try:
            yield observation
        except BaseException as exc:
            observation.outcome = "failed"
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            outcome = (
                observation.outcome
                if observation.outcome
                in {
                    "succeeded",
                    "failed",
                    "degraded",
                    "rate_limited",
                    "cancelled",
                }
                else "other"
            )
            span.set_attribute("hr.provider.outcome", outcome)
            observe_provider(
                provider=safe_provider,
                operation=safe_operation,
                outcome=outcome,
                duration_seconds=monotonic() - started,
            )
