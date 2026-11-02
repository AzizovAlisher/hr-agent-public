"""RFC 9457-style API errors with stable machine codes and request IDs."""

import logging
from http import HTTPStatus
from uuid import UUID, uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.llm.gateway import LLMConfigurationError
from app.services.entitlements import EntitlementExceeded


logger = logging.getLogger(__name__)

PRIVACY_ERASURE_GATE_SQLSTATE = "55P03"
PRIVACY_ERASURE_GATE_MESSAGE = "Tenant privacy erasure is in progress"

STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "limit_exceeded",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def request_id_for(request: Request) -> UUID:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


def _title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request failed"


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    detail: str,
    headers: dict[str, str] | None = None,
    field_errors: list[dict[str, str]] | None = None,
    extensions: dict | None = None,
) -> JSONResponse:
    request_id = request_id_for(request)
    body: dict = {
        "type": f"urn:hr-agent:problem:{code}",
        "title": _title(status_code),
        "status": status_code,
        "detail": detail,
        "code": code,
        "request_id": str(request_id),
    }
    if field_errors:
        body["field_errors"] = field_errors
    if extensions:
        # Never allow a route-provided extension to replace the canonical
        # problem contract fields above.
        body.update({key: value for key, value in extensions.items() if key not in body})
    response_headers = dict(headers or {})
    response_headers.setdefault("X-Request-ID", str(request_id))
    response_headers.setdefault("Cache-Control", "no-store")
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers=response_headers,
        media_type="application/problem+json",
    )


async def http_problem_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    extensions: dict | None = None
    if isinstance(exc.detail, dict):
        raw_code = exc.detail.get("code")
        code = (
            raw_code
            if isinstance(raw_code, str) and raw_code
            else STATUS_CODES.get(exc.status_code, "http_error")
        )
        raw_detail = exc.detail.get("detail", exc.detail.get("message"))
        detail = raw_detail if isinstance(raw_detail, str) else _title(exc.status_code)
        extensions = {
            key: value
            for key, value in exc.detail.items()
            if key not in {"code", "detail", "message"}
        }
    else:
        code = STATUS_CODES.get(exc.status_code, "http_error")
        detail = str(exc.detail)
    return problem_response(
        request,
        status_code=exc.status_code,
        code=code,
        detail=detail,
        headers=exc.headers,
        extensions=extensions,
    )


async def validation_problem_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    field_errors: list[dict[str, str]] = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part != "body"]
        field_errors.append(
            {
                "field": ".".join(location) or "request",
                "message": str(error.get("msg", "Invalid value")),
                "code": str(error.get("type", "invalid")),
            }
        )
    return problem_response(
        request,
        status_code=422,
        code="validation_failed",
        detail="Request validation failed",
        field_errors=field_errors,
    )


async def entitlement_problem_handler(
    request: Request, exc: EntitlementExceeded
) -> JSONResponse:
    return problem_response(
        request,
        status_code=429,
        code="entitlement_exceeded",
        detail=f"Contract limit for {exc.metric} would be exceeded",
        extensions={
            "metric": exc.metric,
            "limit": exc.limit,
            "used": exc.used,
            "requested": exc.requested,
        },
    )


async def llm_configuration_problem_handler(
    request: Request, exc: LLMConfigurationError
) -> JSONResponse:
    return problem_response(
        request,
        status_code=409,
        code=exc.code,
        detail=exc.detail,
    )


def is_privacy_erasure_gate_error(exc: DBAPIError) -> bool:
    """Recognize only the schema gate, never arbitrary lock failures."""

    candidate: BaseException | None = exc.orig
    visited: set[int] = set()
    while candidate is not None and id(candidate) not in visited:
        visited.add(id(candidate))
        sqlstate = getattr(candidate, "sqlstate", None) or getattr(
            candidate, "pgcode", None
        )
        if (
            sqlstate == PRIVACY_ERASURE_GATE_SQLSTATE
            and PRIVACY_ERASURE_GATE_MESSAGE.casefold() in str(candidate).casefold()
        ):
            return True
        candidate = candidate.__cause__ or candidate.__context__
    return False


def privacy_erasure_problem_response(request: Request) -> JSONResponse:
    return problem_response(
        request,
        status_code=409,
        code="privacy_erasure_in_progress",
        detail="Organization data is temporarily unavailable while a privacy request completes.",
        headers={"Retry-After": "1"},
    )


async def database_problem_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    if is_privacy_erasure_gate_error(exc):
        return privacy_erasure_problem_response(request)
    return await unhandled_problem_handler(request, exc)


async def unhandled_problem_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request_id_for(request)
    # Do not log request paths or bodies here: public capability tokens and CV
    # material can legitimately appear in them. The request ID is sufficient
    # to correlate sanitized audit/trace data.
    logger.exception(
        "Unhandled API exception request_id=%s method=%s",
        request_id,
        request.method,
        exc_info=exc,
    )
    return problem_response(
        request,
        status_code=500,
        code="internal_error",
        detail="An unexpected error occurred",
    )
