"""Operator telemetry security, correlation, and readiness regression tests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.main import app, settings
from app.models.operations import WorkerHeartbeat
from app.observability.context import current_correlation
from app.observability.metrics import (
    install_worker_telemetry_snapshots,
    observe_audit_database,
    record_provider_connection_state,
    render_metrics,
    update_provider_connection_counts,
    update_readiness_metrics,
    worker_telemetry_snapshot,
)
from app.observability.middleware import ObservabilityMiddleware
from app.observability.telemetry import (
    build_telemetry_runtime,
    job_observation,
    provider_observation,
)

pytestmark = pytest.mark.asyncio


async def test_metrics_access_fails_closed_and_never_exposes_operator_secret(
    db,
    monkeypatch,
):
    operator_token = "operator-metrics-token-0123456789abcdef"
    monkeypatch.setattr(settings, "observability_metrics_enabled", True)
    monkeypatch.setattr(settings, "observability_metrics_allowed_cidrs", "")
    monkeypatch.setattr(
        settings,
        "observability_metrics_bearer_token",
        SecretStr(operator_token),
    )
    now = datetime.now(UTC)
    db.add(
        WorkerHeartbeat(
            worker_name="durable-work",
            worker_id="observability-test-worker",
            process_started_at=now,
            last_seen_at=now,
            metadata_json={
                "queues": ["integrations"],
                "telemetry": {
                    "version": 1,
                    "job_operations": [
                        {
                            "queue": "integration_jobs",
                            "outcome": "succeeded",
                            "count": 7,
                        }
                    ],
                    "job_durations": [],
                    "provider_operations": [
                        {
                            "provider": "telegram",
                            "operation": "telegram_send",
                            "outcome": "succeeded",
                            "count": 5,
                        }
                    ],
                    "provider_durations": [],
                    "provider_connections": [
                        {"provider": "telegram", "state": "connected", "count": 1}
                    ],
                },
            },
        )
    )
    await db.commit()
    transport = httpx.ASGITransport(
        app=app,
        client=("203.0.113.44", 48151),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/internal/metrics")
        wrong = await client.get(
            "/internal/metrics",
            headers={"Authorization": "Bearer wrong-token"},
        )
        allowed = await client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {operator_token}"},
        )

    assert missing.status_code == 403
    assert missing.json()["code"] == "metrics_access_denied"
    assert wrong.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"
    assert allowed.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert operator_token not in allowed.text
    assert "hr_readiness_overall_ok" in allowed.text
    assert "hr_observability_snapshot_success" in allowed.text
    assert (
        'hr_worker_job_operations_total{outcome="succeeded",queue="integration_jobs"} 7.0'
        in allowed.text
    )
    assert (
        'hr_worker_provider_operations_total{operation="telegram_send",outcome="succeeded",provider="telegram"} 5.0'
        in allowed.text
    )


async def test_disabled_metrics_endpoint_is_not_discoverable(monkeypatch):
    monkeypatch.setattr(settings, "observability_metrics_enabled", False)
    transport = httpx.ASGITransport(
        app=app,
        client=("127.0.0.1", 38151),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/internal/metrics")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


async def test_request_metrics_use_route_templates_and_server_owned_correlation(
    monkeypatch,
):
    attacker_value = "candidate.person@example.test"
    query_secret = "candidate-token-super-secret"
    operator_token = "route-metrics-token-0123456789abcdef0"
    monkeypatch.setattr(settings, "observability_metrics_enabled", True)
    monkeypatch.setattr(settings, "observability_metrics_allowed_cidrs", "")
    monkeypatch.setattr(
        settings,
        "observability_metrics_bearer_token",
        SecretStr(operator_token),
    )
    transport = httpx.ASGITransport(
        app=app,
        client=("198.51.100.8", 38151),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get(
            f"/missing/{attacker_value}",
            params={"token": query_secret},
            headers={"X-Request-ID": attacker_value},
        )
        metrics = await client.get(
            "/internal/metrics",
            headers={"Authorization": f"Bearer {operator_token}"},
        )

    server_request_id = UUID(missing.headers["x-request-id"])
    assert str(server_request_id) != attacker_value
    assert missing.status_code == 404
    assert metrics.status_code == 200
    assert 'route="_unmatched",status_class="4xx"' in metrics.text
    assert attacker_value not in metrics.text
    assert query_secret not in metrics.text
    assert str(server_request_id) not in metrics.text
    assert operator_token not in metrics.text


async def test_trace_and_worker_provider_context_are_correlated_but_sanitized():
    exporter = InMemorySpanExporter()
    trace_settings = Settings(
        _env_file=None,
        observability_trace_sample_ratio=1.0,
        observability_otlp_traces_endpoint=None,
    )
    runtime = build_telemetry_runtime(trace_settings, exporter=exporter)
    mini = FastAPI()
    mini.add_middleware(ObservabilityMiddleware, runtime=runtime)

    @mini.get("/candidates/{candidate_id}")
    async def candidate_probe(candidate_id: UUID) -> dict[str, str]:
        return {"id": str(candidate_id)}

    candidate_id = uuid4()
    incoming_trace_id = "1234567890abcdef1234567890abcdef"
    transport = httpx.ASGITransport(app=mini)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/candidates/{candidate_id}",
            headers={"traceparent": f"00-{incoming_trace_id}-0123456789abcdef-01"},
        )

    assert response.status_code == 200
    assert response.headers["x-trace-id"] == incoming_trace_id
    request_span = next(
        span for span in exporter.get_finished_spans() if span.name.startswith("GET ")
    )
    assert request_span.name == "GET /candidates/{candidate_id}"
    assert request_span.attributes["http.route"] == "/candidates/{candidate_id}"
    assert str(candidate_id) not in repr(request_span.attributes)

    job_id = uuid4()
    with (
        job_observation(queue="integrations", job_id=job_id),
        provider_observation(
            provider="candidate.person@example.test",
            operation="token=provider-secret",
        ) as observation,
    ):
        observation.outcome = "degraded"
        record = logging.getLogger("observability-test").makeRecord(
            "observability-test",
            logging.INFO,
            __file__,
            1,
            "provider observation",
            (),
            None,
        )
        correlation = current_correlation()

    assert correlation.job_id == str(job_id)
    assert correlation.job_queue == "integrations"
    assert correlation.provider == "other"
    assert correlation.provider_operation == "other"
    assert record.correlation_job_id == str(job_id)
    assert record.correlation_provider == "other"

    private_error = "candidate.person@example.test provider-secret-value"
    with (
        pytest.raises(RuntimeError),
        provider_observation(provider="telegram", operation="telegram_send"),
    ):
        raise RuntimeError(private_error)

    record_provider_connection_state(
        connection_id=uuid4(),
        provider="telegram",
        state="degraded",
    )
    heartbeat_snapshot = worker_telemetry_snapshot()
    install_worker_telemetry_snapshots([heartbeat_snapshot])

    exposition = render_metrics().decode()
    spans = repr(
        [
            (span.name, dict(span.attributes), tuple(span.events))
            for span in exporter.get_finished_spans()
        ]
    )
    assert 'queue="integrations"' in exposition
    assert (
        'hr_provider_operations_total{operation="other",outcome="degraded",provider="other"}'
        in exposition
    )
    assert str(job_id) not in exposition
    assert "candidate.person@example.test" not in exposition
    assert "provider-secret" not in exposition
    assert private_error not in spans
    serialized_snapshot = repr(heartbeat_snapshot)
    assert str(job_id) not in serialized_snapshot
    assert "candidate.person@example.test" not in serialized_snapshot
    assert "provider-secret" not in serialized_snapshot
    assert "hr_worker_provider_operations_total" in exposition
    runtime.shutdown()


async def test_readiness_metrics_preserve_dependency_and_queue_failure_state():
    private_detail = "postgres://candidate.person@example.test:password@db"
    update_readiness_metrics(
        {
            "database": {"status": "failed", "detail": private_detail, "data": {}},
            "malware_scanner": {
                "status": "failed",
                "detail": private_detail,
                "data": {
                    "backend": "clamav",
                    "signature_age_seconds": 200_000,
                },
            },
            "workers": {
                "status": "failed",
                "detail": private_detail,
                "data": {
                    "fresh_replicas": 0,
                    "missing_queues": ["integrations"],
                },
            },
            "queues": {
                "status": "failed",
                "detail": private_detail,
                "data": {
                    "queues": [
                        {
                            "queue": "integration_jobs",
                            "due_count": 3,
                            "oldest_due_age_seconds": 901,
                            "dlq_count": 1,
                        }
                    ]
                },
            },
        }
    )
    update_provider_connection_counts(
        {
            ("telegram", "degraded"): 2,
            ("candidate.person@example.test", "provider-secret"): 1,
        }
    )
    exposition = render_metrics().decode()

    assert "hr_readiness_overall_ok 0.0" in exposition
    assert (
        'hr_readiness_component_status{component="database",state="failed"} 1.0'
        in exposition
    )
    assert (
        'hr_readiness_component_status{component="malware_scanner",state="failed"} 1.0'
        in exposition
    )
    assert "hr_malware_signature_age_seconds 200000.0" in exposition
    assert (
        'hr_queue_oldest_due_age_seconds{queue="integration_jobs"} 901.0' in exposition
    )
    assert "hr_worker_missing_queues 1.0" in exposition
    assert (
        'hr_provider_connections{provider="telegram",state="degraded"} 2.0'
        in exposition
    )
    assert private_detail not in exposition
    assert "candidate.person@example.test" not in exposition
    assert "provider-secret" not in exposition

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ready = await client.get("/health/ready")
    assert ready.status_code in {200, 503}
    assert set(ready.json()) == {"status", "checked_at"}
    assert "components" not in ready.json()
    assert ready.headers["cache-control"] == "no-store"


async def test_audit_database_metrics_are_bounded_and_payload_free():
    private_value = "candidate.person@example.test-provider-secret"
    observe_audit_database(
        phase="sensitive_read",
        outcome="succeeded",
        duration_seconds=0.125,
    )
    observe_audit_database(
        phase=private_value,
        outcome=private_value,
        duration_seconds=0.25,
    )
    exposition = render_metrics().decode()

    assert (
        'hr_audit_database_operations_total{outcome="succeeded",phase="sensitive_read"}'
        in exposition
    )
    assert (
        'hr_audit_database_operations_total{outcome="other",phase="other"}'
        in exposition
    )
    assert (
        'hr_audit_database_operation_duration_seconds_count{phase="sensitive_read"}'
        in exposition
    )
    assert private_value not in exposition


async def test_observability_configuration_requires_a_fail_closed_boundary():
    with pytest.raises(ValidationError, match="operator token or an allowed network"):
        Settings(
            _env_file=None,
            observability_metrics_enabled=True,
            observability_metrics_allowed_cidrs="",
            observability_metrics_bearer_token=None,
        )
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(
            _env_file=None,
            observability_metrics_allowed_cidrs="",
            observability_metrics_bearer_token="short",
        )
    with pytest.raises(ValidationError, match="private monitoring networks"):
        Settings(
            _env_file=None,
            observability_metrics_allowed_cidrs="0.0.0.0/0",
        )
    with pytest.raises(ValidationError, match="without credentials"):
        Settings(
            _env_file=None,
            observability_otlp_traces_endpoint="https://user:pass@collector/v1/traces",
        )

    secret = "independent-observability-token-1234567890"
    configured = Settings(
        _env_file=None,
        observability_metrics_allowed_cidrs="",
        observability_metrics_bearer_token=secret,
    )
    assert secret not in repr(configured)

    with pytest.raises(ValidationError, match="Input should be 48"):
        Settings(_env_file=None, clamav_max_signature_age_hours=72)
