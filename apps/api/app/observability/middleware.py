"""Pure-ASGI request correlation, low-cardinality metrics, and trace spans."""

from __future__ import annotations

from time import monotonic
from typing import Any
from uuid import uuid4

from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.observability.context import request_correlation
from app.observability.metrics import HTTP_IN_PROGRESS, observe_http_request
from app.observability.telemetry import TelemetryRuntime

_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}


class _HeaderGetter(Getter[dict[str, str]]):
    def get(self, carrier: dict[str, str], key: str) -> list[str] | None:
        value = carrier.get(key.lower())
        return [value] if value is not None else None

    def keys(self, carrier: dict[str, str]) -> list[str]:
        return list(carrier)


_HEADER_GETTER = _HeaderGetter()
_PROPAGATOR = TraceContextTextMapPropagator()


def _method(value: object) -> str:
    candidate = str(value).upper()
    return candidate if candidate in _METHODS else "OTHER"


def _route(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 200:
        return "_unmatched"
    return path


def _status_class(status_code: int) -> str:
    return f"{max(1, min(status_code // 100, 5))}xx"


class ObservabilityMiddleware:
    def __init__(self, app, *, runtime: TelemetryRuntime) -> None:
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4()
        scope.setdefault("state", {})["request_id"] = request_id
        method = _method(scope.get("method"))
        raw_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        parent_context = _PROPAGATOR.extract(raw_headers, getter=_HEADER_GETTER)
        started = monotonic()
        status_code = 500
        completed = False
        HTTP_IN_PROGRESS.labels(method).inc()

        with (
            request_correlation(request_id),
            self.runtime.tracer.start_as_current_span(
                "HTTP request",
                context=parent_context,
                kind=SpanKind.SERVER,
                attributes={
                    "http.request.method": method,
                    "hr.request.id": str(request_id),
                },
                record_exception=False,
                set_status_on_exception=False,
            ) as span,
        ):
            trace_context = span.get_span_context()

            async def send_with_context(message) -> None:
                nonlocal completed, status_code
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                    headers = list(message.get("headers", ()))
                    lower_names = {name.lower() for name, _value in headers}
                    if b"x-request-id" not in lower_names:
                        headers.append((b"x-request-id", str(request_id).encode()))
                    if trace_context.is_valid and b"x-trace-id" not in lower_names:
                        headers.append(
                            (b"x-trace-id", f"{trace_context.trace_id:032x}".encode())
                        )
                    message["headers"] = headers
                elif message["type"] == "http.response.body" and not message.get(
                    "more_body", False
                ):
                    completed = True
                await send(message)

            try:
                await self.app(scope, receive, send_with_context)
            except BaseException as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                route = _route(scope)
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status_code)
                if status_code >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("hr.response.completed", completed)
                observe_http_request(
                    method=method,
                    route=route,
                    status_code=status_code,
                    duration_seconds=monotonic() - started,
                )
                HTTP_IN_PROGRESS.labels(method).dec()
