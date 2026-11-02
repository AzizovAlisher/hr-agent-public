"""Prometheus metrics with bounded labels and no tenant/object identifiers."""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from math import isfinite
from threading import Lock
from typing import Any
from uuid import UUID

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import CounterMetricFamily, HistogramMetricFamily
from prometheus_client.exposition import generate_latest

from app.observability.context import (
    normalized_job_queue,
    normalized_provider,
    normalized_provider_operation,
)

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "hr_http_requests_total",
    "Completed HTTP requests by bounded route and status class.",
    ("method", "route", "status_class"),
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "hr_http_request_duration_seconds",
    "HTTP request duration by bounded route and method.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
HTTP_IN_PROGRESS = Gauge(
    "hr_http_requests_in_progress",
    "Requests currently executing by method.",
    ("method",),
    registry=REGISTRY,
)
AUDIT_DATABASE_OPERATIONS = Counter(
    "hr_audit_database_operations_total",
    "Central audit database outcomes by bounded middleware phase.",
    ("phase", "outcome"),
    registry=REGISTRY,
)
AUDIT_DATABASE_DURATION = Histogram(
    "hr_audit_database_operation_duration_seconds",
    "Central audit database latency by bounded middleware phase.",
    ("phase",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

JOB_OPERATIONS = Counter(
    "hr_job_operations_total",
    "Durable job outcomes by bounded queue.",
    ("queue", "outcome"),
    registry=REGISTRY,
)
JOB_DURATION = Histogram(
    "hr_job_duration_seconds",
    "Durable job execution duration by bounded queue.",
    ("queue",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, 60, 180),
    registry=REGISTRY,
)
PROVIDER_OPERATIONS = Counter(
    "hr_provider_operations_total",
    "External provider outcomes using bounded provider and operation groups.",
    ("provider", "operation", "outcome"),
    registry=REGISTRY,
)
PROVIDER_DURATION = Histogram(
    "hr_provider_duration_seconds",
    "External provider latency using bounded provider and operation groups.",
    ("provider", "operation"),
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=REGISTRY,
)
PROVIDER_CONNECTIONS = Gauge(
    "hr_provider_connections",
    "Aggregate provider connections by normalized state; never tenant-labelled.",
    ("provider", "state"),
    registry=REGISTRY,
)

READINESS_OVERALL = Gauge(
    "hr_readiness_overall_ok",
    "One when no authoritative readiness component is failed.",
    registry=REGISTRY,
)
READINESS_COMPONENT = Gauge(
    "hr_readiness_component_status",
    "Readiness component state as a one-hot gauge.",
    ("component", "state"),
    registry=REGISTRY,
)
QUEUE_DUE = Gauge(
    "hr_queue_due_jobs",
    "Due durable work by bounded queue.",
    ("queue",),
    registry=REGISTRY,
)
QUEUE_AGE = Gauge(
    "hr_queue_oldest_due_age_seconds",
    "Age of the oldest due durable work item by bounded queue.",
    ("queue",),
    registry=REGISTRY,
)
QUEUE_DLQ = Gauge(
    "hr_queue_dead_letter_jobs",
    "Dead-letter work items by bounded queue.",
    ("queue",),
    registry=REGISTRY,
)
WORKER_FRESH_REPLICAS = Gauge(
    "hr_worker_fresh_replicas",
    "Worker replicas with a recent heartbeat.",
    registry=REGISTRY,
)
WORKER_MISSING_QUEUES = Gauge(
    "hr_worker_missing_queues",
    "Required queues without a recent worker heartbeat.",
    registry=REGISTRY,
)
MALWARE_SIGNATURE_AGE = Gauge(
    "hr_malware_signature_age_seconds",
    "Age of the ClamAV signature database reported by readiness.",
    registry=REGISTRY,
)
SNAPSHOT_SUCCESS = Gauge(
    "hr_observability_snapshot_success",
    "One when dependency telemetry was refreshed for this scrape.",
    registry=REGISTRY,
)

_READINESS_STATES = ("ok", "degraded", "failed")
_AUDIT_DATABASE_PHASES = ("finalize", "preflight", "sensitive_read")
_AUDIT_DATABASE_OUTCOMES = ("failed", "succeeded")
_PROVIDER_STATES = (
    "unavailable",
    "disconnected",
    "connecting",
    "connected",
    "degraded",
    "expired",
    "revoked",
)
_JOB_OUTCOMES = ("cancelled", "failed", "other", "retrying", "succeeded")
_PROVIDER_OUTCOMES = (
    "cancelled",
    "degraded",
    "failed",
    "other",
    "rate_limited",
    "succeeded",
)
_JOB_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 30, 60, 180)
_PROVIDER_DURATION_BUCKETS = (0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
_previous_components: set[str] = set()
_previous_queues: set[str] = set()
_previous_provider_series: set[tuple[str, str]] = set()
_state_lock = Lock()

_worker_local_job_counts: dict[tuple[str, str], int] = defaultdict(int)
_worker_local_provider_counts: dict[tuple[str, str, str], int] = defaultdict(int)
_worker_local_job_durations: dict[tuple[str], dict[str, Any]] = {}
_worker_local_provider_durations: dict[tuple[str, str], dict[str, Any]] = {}
_worker_local_connection_states: OrderedDict[str, tuple[str, str]] = OrderedDict()
_worker_remote_job_counts: dict[tuple[str, str], int] = {}
_worker_remote_provider_counts: dict[tuple[str, str, str], int] = {}
_worker_remote_job_durations: dict[tuple[str], dict[str, Any]] = {}
_worker_remote_provider_durations: dict[tuple[str, str], dict[str, Any]] = {}


def _new_histogram(bucket_count: int) -> dict[str, Any]:
    return {"count": 0, "sum": 0.0, "buckets": [0] * bucket_count}


def _observe_histogram(
    store: dict[tuple[str, ...], dict[str, Any]],
    labels: tuple[str, ...],
    value: float,
    boundaries: tuple[float, ...],
) -> None:
    sample = max(0.0, float(value))
    item = store.setdefault(labels, _new_histogram(len(boundaries)))
    item["count"] += 1
    item["sum"] += sample
    for index, boundary in enumerate(boundaries):
        if sample <= boundary:
            item["buckets"][index] += 1


class _WorkerTelemetryCollector:
    """Expose sanitized cumulative snapshots from dedicated worker replicas."""

    def collect(self):
        with _state_lock:
            job_counts = dict(_worker_remote_job_counts)
            provider_counts = dict(_worker_remote_provider_counts)
            job_durations = {
                labels: {**value, "buckets": list(value["buckets"])}
                for labels, value in _worker_remote_job_durations.items()
            }
            provider_durations = {
                labels: {**value, "buckets": list(value["buckets"])}
                for labels, value in _worker_remote_provider_durations.items()
            }

        jobs = CounterMetricFamily(
            "hr_worker_job_operations",
            "Cumulative durable job outcomes reported by fresh worker heartbeats.",
            labels=("queue", "outcome"),
        )
        for (queue, outcome), count in sorted(job_counts.items()):
            jobs.add_metric((queue, outcome), count)
        yield jobs

        providers = CounterMetricFamily(
            "hr_worker_provider_operations",
            "Cumulative provider outcomes reported by fresh worker heartbeats.",
            labels=("provider", "operation", "outcome"),
        )
        for (provider, operation, outcome), count in sorted(provider_counts.items()):
            providers.add_metric((provider, operation, outcome), count)
        yield providers

        job_latency = HistogramMetricFamily(
            "hr_worker_job_duration_seconds",
            "Durable job duration reported by fresh worker heartbeats.",
            labels=("queue",),
        )
        for (queue,), item in sorted(job_durations.items()):
            buckets = [
                (str(boundary), item["buckets"][index])
                for index, boundary in enumerate(_JOB_DURATION_BUCKETS)
            ]
            buckets.append(("+Inf", item["count"]))
            job_latency.add_metric((queue,), buckets, item["sum"])
        yield job_latency

        provider_latency = HistogramMetricFamily(
            "hr_worker_provider_duration_seconds",
            "Provider duration reported by fresh worker heartbeats.",
            labels=("provider", "operation"),
        )
        for (provider, operation), item in sorted(provider_durations.items()):
            buckets = [
                (str(boundary), item["buckets"][index])
                for index, boundary in enumerate(_PROVIDER_DURATION_BUCKETS)
            ]
            buckets.append(("+Inf", item["count"]))
            provider_latency.add_metric(
                (provider, operation),
                buckets,
                item["sum"],
            )
        yield provider_latency


REGISTRY.register(_WorkerTelemetryCollector())


def _bounded_component(value: object) -> str:
    text = str(value)
    if text in {
        "database",
        "malware_scanner",
        "migrations",
        "private_storage",
        "public_rate_limits",
        "queues",
        "tenant_isolation",
        "workers",
    }:
        return text
    return "other"


def observe_http_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    status_class = f"{max(1, min(status_code // 100, 5))}xx"
    HTTP_REQUESTS.labels(method, route, status_class).inc()
    HTTP_DURATION.labels(method, route).observe(max(0.0, duration_seconds))


def observe_audit_database(
    *,
    phase: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    safe_phase = phase if phase in _AUDIT_DATABASE_PHASES else "other"
    safe_outcome = outcome if outcome in _AUDIT_DATABASE_OUTCOMES else "other"
    AUDIT_DATABASE_OPERATIONS.labels(safe_phase, safe_outcome).inc()
    AUDIT_DATABASE_DURATION.labels(safe_phase).observe(max(0.0, duration_seconds))


def observe_job(*, queue: str, outcome: str, duration_seconds: float) -> None:
    safe_queue = normalized_job_queue(queue)
    safe_outcome = (
        outcome
        if outcome in {"succeeded", "failed", "retrying", "cancelled"}
        else "other"
    )
    JOB_OPERATIONS.labels(safe_queue, safe_outcome).inc()
    JOB_DURATION.labels(safe_queue).observe(max(0.0, duration_seconds))
    with _state_lock:
        _worker_local_job_counts[(safe_queue, safe_outcome)] += 1
        _observe_histogram(
            _worker_local_job_durations,
            (safe_queue,),
            duration_seconds,
            _JOB_DURATION_BUCKETS,
        )


def observe_provider(
    *,
    provider: str,
    operation: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    safe_provider = normalized_provider(provider)
    safe_operation = normalized_provider_operation(operation)
    safe_outcome = (
        outcome
        if outcome in {"succeeded", "failed", "degraded", "rate_limited", "cancelled"}
        else "other"
    )
    PROVIDER_OPERATIONS.labels(safe_provider, safe_operation, safe_outcome).inc()
    PROVIDER_DURATION.labels(safe_provider, safe_operation).observe(
        max(0.0, duration_seconds)
    )
    with _state_lock:
        _worker_local_provider_counts[
            (safe_provider, safe_operation, safe_outcome)
        ] += 1
        _observe_histogram(
            _worker_local_provider_durations,
            (safe_provider, safe_operation),
            duration_seconds,
            _PROVIDER_DURATION_BUCKETS,
        )


def record_provider_connection_state(
    *,
    connection_id: UUID | str,
    provider: str,
    state: str,
) -> None:
    """Remember only enough identity to deduplicate this worker's aggregate."""

    try:
        key = str(
            connection_id if isinstance(connection_id, UUID) else UUID(connection_id)
        )
    except (TypeError, ValueError, AttributeError):
        return
    safe_provider = normalized_provider(provider)
    safe_state = state if state in _PROVIDER_STATES else "unavailable"
    with _state_lock:
        _worker_local_connection_states.pop(key, None)
        _worker_local_connection_states[key] = (safe_provider, safe_state)
        while len(_worker_local_connection_states) > 10_000:
            _worker_local_connection_states.popitem(last=False)


def worker_telemetry_snapshot() -> dict[str, Any]:
    """Return a bounded aggregate safe to persist in a worker heartbeat."""

    with _state_lock:
        connection_counts: dict[tuple[str, str], int] = defaultdict(int)
        for labels in _worker_local_connection_states.values():
            connection_counts[labels] += 1
        return {
            "version": 1,
            "job_operations": [
                {"queue": queue, "outcome": outcome, "count": count}
                for (queue, outcome), count in sorted(_worker_local_job_counts.items())
            ],
            "job_durations": [
                {
                    "queue": queue,
                    "count": item["count"],
                    "sum": item["sum"],
                    "buckets": list(item["buckets"]),
                }
                for (queue,), item in sorted(_worker_local_job_durations.items())
            ],
            "provider_operations": [
                {
                    "provider": provider,
                    "operation": operation,
                    "outcome": outcome,
                    "count": count,
                }
                for (provider, operation, outcome), count in sorted(
                    _worker_local_provider_counts.items()
                )
            ],
            "provider_durations": [
                {
                    "provider": provider,
                    "operation": operation,
                    "count": item["count"],
                    "sum": item["sum"],
                    "buckets": list(item["buckets"]),
                }
                for (provider, operation), item in sorted(
                    _worker_local_provider_durations.items()
                )
            ],
            "provider_connections": [
                {"provider": provider, "state": state, "count": count}
                for (provider, state), count in sorted(connection_counts.items())
            ],
        }


def _safe_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed <= 10**15 else None


def _safe_nonnegative_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) and 0 <= parsed <= 10**15 else None


def _merge_duration_row(
    store: dict[tuple[str, ...], dict[str, Any]],
    *,
    labels: tuple[str, ...],
    row: dict[str, Any],
    bucket_count: int,
) -> None:
    count = _safe_nonnegative_int(row.get("count"))
    total = _safe_nonnegative_float(row.get("sum"))
    buckets_raw = row.get("buckets")
    if count is None or total is None or not isinstance(buckets_raw, list):
        return
    if len(buckets_raw) != bucket_count:
        return
    buckets = [_safe_nonnegative_int(value) for value in buckets_raw]
    if any(value is None for value in buckets):
        return
    item = store.setdefault(labels, _new_histogram(bucket_count))
    item["count"] += count
    item["sum"] += total
    for index, value in enumerate(buckets):
        item["buckets"][index] += value


def install_worker_telemetry_snapshots(snapshots: list[object]) -> None:
    """Merge fresh heartbeat snapshots without accepting arbitrary labels."""

    global _worker_remote_job_counts
    global _worker_remote_job_durations
    global _worker_remote_provider_counts
    global _worker_remote_provider_durations
    job_counts: dict[tuple[str, str], int] = defaultdict(int)
    provider_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    job_durations: dict[tuple[str], dict[str, Any]] = {}
    provider_durations: dict[tuple[str, str], dict[str, Any]] = {}
    connection_counts: dict[tuple[str, str], int] = defaultdict(int)

    for snapshot in snapshots[:1_000]:
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            continue
        for row in (snapshot.get("job_operations") or [])[:2_000]:
            if not isinstance(row, dict):
                continue
            queue = normalized_job_queue(str(row.get("queue", "")))
            outcome = str(row.get("outcome", ""))
            count = _safe_nonnegative_int(row.get("count"))
            if outcome in _JOB_OUTCOMES and count is not None:
                job_counts[(queue, outcome)] += count
        for row in (snapshot.get("job_durations") or [])[:2_000]:
            if isinstance(row, dict):
                _merge_duration_row(
                    job_durations,
                    labels=(normalized_job_queue(str(row.get("queue", ""))),),
                    row=row,
                    bucket_count=len(_JOB_DURATION_BUCKETS),
                )
        for row in (snapshot.get("provider_operations") or [])[:2_000]:
            if not isinstance(row, dict):
                continue
            provider = normalized_provider(str(row.get("provider", "")))
            operation = normalized_provider_operation(str(row.get("operation", "")))
            outcome = str(row.get("outcome", ""))
            count = _safe_nonnegative_int(row.get("count"))
            if outcome in _PROVIDER_OUTCOMES and count is not None:
                provider_counts[(provider, operation, outcome)] += count
        for row in (snapshot.get("provider_durations") or [])[:2_000]:
            if isinstance(row, dict):
                _merge_duration_row(
                    provider_durations,
                    labels=(
                        normalized_provider(str(row.get("provider", ""))),
                        normalized_provider_operation(str(row.get("operation", ""))),
                    ),
                    row=row,
                    bucket_count=len(_PROVIDER_DURATION_BUCKETS),
                )
        for row in (snapshot.get("provider_connections") or [])[:2_000]:
            if not isinstance(row, dict):
                continue
            provider = normalized_provider(str(row.get("provider", "")))
            state = str(row.get("state", ""))
            count = _safe_nonnegative_int(row.get("count"))
            if state in _PROVIDER_STATES and count is not None:
                connection_counts[(provider, state)] += count

    with _state_lock:
        _worker_remote_job_counts = dict(job_counts)
        _worker_remote_provider_counts = dict(provider_counts)
        _worker_remote_job_durations = job_durations
        _worker_remote_provider_durations = provider_durations
    update_provider_connection_counts(dict(connection_counts))


def update_provider_connection_counts(
    counts: dict[tuple[str, str], int],
) -> None:
    """Publish only aggregate, bounded connection state supplied by runtime code."""

    global _previous_provider_series
    current: set[tuple[str, str]] = set()
    with _state_lock:
        for (provider, state), count in counts.items():
            safe_provider = normalized_provider(provider)
            safe_state = state if state in _PROVIDER_STATES else "unavailable"
            current.add((safe_provider, safe_state))
            PROVIDER_CONNECTIONS.labels(safe_provider, safe_state).set(
                max(0, int(count))
            )
        for provider, state in _previous_provider_series - current:
            PROVIDER_CONNECTIONS.labels(provider, state).set(0)
        _previous_provider_series = current


def update_readiness_metrics(components: dict[str, dict[str, Any]]) -> None:
    """Convert the existing dependency-aware readiness snapshot to gauges."""

    global _previous_components, _previous_queues
    components_seen: set[str] = set()
    queues_seen: set[str] = set()
    any_failed = False
    with _state_lock:
        for raw_component, payload in components.items():
            component = _bounded_component(raw_component)
            components_seen.add(component)
            state = str(payload.get("status", "failed"))
            if state not in _READINESS_STATES:
                state = "failed"
            any_failed = any_failed or state == "failed"
            for candidate in _READINESS_STATES:
                READINESS_COMPONENT.labels(component, candidate).set(
                    1 if candidate == state else 0
                )

            if component == "workers":
                data = payload.get("data") or {}
                WORKER_FRESH_REPLICAS.set(max(0, int(data.get("fresh_replicas", 0))))
                missing = data.get("missing_queues") or []
                WORKER_MISSING_QUEUES.set(
                    len(missing) if isinstance(missing, list) else 0
                )
            elif component == "malware_scanner":
                data = payload.get("data") or {}
                age = _safe_nonnegative_float(data.get("signature_age_seconds"))
                MALWARE_SIGNATURE_AGE.set(age if age is not None else 0)
            elif component == "queues":
                data = payload.get("data") or {}
                for queue_payload in data.get("queues") or []:
                    if not isinstance(queue_payload, dict):
                        continue
                    queue = normalized_job_queue(str(queue_payload.get("queue", "")))
                    queues_seen.add(queue)
                    QUEUE_DUE.labels(queue).set(
                        max(0, int(queue_payload.get("due_count") or 0))
                    )
                    QUEUE_AGE.labels(queue).set(
                        max(
                            0.0, float(queue_payload.get("oldest_due_age_seconds") or 0)
                        )
                    )
                    QUEUE_DLQ.labels(queue).set(
                        max(0, int(queue_payload.get("dlq_count") or 0))
                    )

        for component in _previous_components - components_seen:
            for state in _READINESS_STATES:
                READINESS_COMPONENT.labels(component, state).set(
                    1 if state == "failed" else 0
                )
            any_failed = True
        for queue in _previous_queues - queues_seen:
            QUEUE_DUE.labels(queue).set(0)
            QUEUE_AGE.labels(queue).set(0)
            QUEUE_DLQ.labels(queue).set(0)
        _previous_components = components_seen
        _previous_queues = queues_seen
        READINESS_OVERALL.set(0 if any_failed else 1)
        SNAPSHOT_SUCCESS.set(1)


def mark_snapshot_failed() -> None:
    SNAPSHOT_SUCCESS.set(0)
    READINESS_OVERALL.set(0)


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)
