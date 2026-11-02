from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import audit_engine, engine
from app.llm.gateway import LLMConfigurationError
from app.middleware.audit import audit_http_request
from app.middleware.request_body_limit import RequestBodyLimitMiddleware
from app.observability import (
    ObservabilityMiddleware,
    build_observability_router,
    build_telemetry_runtime,
    install_correlation_log_record_factory,
)
from app.problem import (
    database_problem_handler,
    entitlement_problem_handler,
    http_problem_handler,
    llm_configuration_problem_handler,
    problem_response,
    unhandled_problem_handler,
    validation_problem_handler,
)
from app.routers import activities as activities_router
from app.routers import ai_hiring as ai_hiring_router
from app.routers import analytics as analytics_router
from app.routers import auth as auth_router
from app.routers import candidate_notes as candidate_notes_router
from app.routers import candidate_search as candidate_search_router
from app.routers import candidates as candidates_router
from app.routers import careers as careers_router
from app.routers import chat as chat_router
from app.routers import communications as communications_router
from app.routers import enterprise as enterprise_router
from app.routers import folders as folders_router
from app.routers import github as github_router
from app.routers import integrations as integrations_router
from app.routers import interviews as interviews_router
from app.routers import notifications as notifications_router
from app.routers import operations as operations_router
from app.routers import organizations as organizations_router
from app.routers import positions as positions_router
from app.routers import public as public_router
from app.routers import recordings as recordings_router
from app.routers import requisitions as requisitions_router
from app.routers import scheduling as scheduling_router
from app.routers import scorecards as scorecards_router
from app.routers import shortlists as shortlists_router
from app.routers import sourcing as sourcing_router
from app.routers import talent as talent_router
from app.routers import tasks as tasks_router
from app.routers import users as users_router
from app.routers import work as work_router
from app.routers import workflows as workflows_router
from app.services.entitlements import EntitlementExceeded
from app.services.public_abuse import (
    PublicRateLimitExceeded,
    PublicRateLimitUnavailable,
    close_public_rate_limiter,
    enforce_public_request,
    public_request_requires_limiter,
)
from app.services.uploads import ensure_storage_dir
from app.storage import PublicBrandingRedirectApp

settings = get_settings()
telemetry_runtime = build_telemetry_runtime(settings)
install_correlation_log_record_factory()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    ensure_storage_dir()
    # Durable work intentionally runs in ``app.scripts.run_workers``. Keeping
    # queues out of API lifespans prevents each API replica from silently
    # becoming a worker and gives deployments independent scaling/failure
    # domains.
    try:
        yield
    finally:
        await close_public_rate_limiter()
        await audit_engine.dispose()
        await engine.dispose()
        telemetry_runtime.shutdown()


app = FastAPI(title="Recruitment v2 API", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(StarletteHTTPException, http_problem_handler)
app.add_exception_handler(RequestValidationError, validation_problem_handler)
app.add_exception_handler(EntitlementExceeded, entitlement_problem_handler)
app.add_exception_handler(LLMConfigurationError, llm_configuration_problem_handler)
app.add_exception_handler(DBAPIError, database_problem_handler)
app.add_exception_handler(Exception, unhandled_problem_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=settings.request_body_max_bytes,
)
app.middleware("http")(audit_http_request)


@app.middleware("http")
async def public_careers_cors(request, call_next):
    """Allow any company website to use the intentionally public careers API.

    Internal/authenticated endpoints retain the configured WEB_ORIGIN policy.
    This narrow wildcard applies only to the versioned careers catalog and
    application intake.
    """

    is_careers_api = request.url.path.startswith("/public/v1/careers/")
    if is_careers_api and request.method == "OPTIONS":
        response = Response(status_code=204)
    else:
        response = await call_next(request)
    if is_careers_api:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "If-None-Match, Content-Type"
        response.headers["Access-Control-Expose-Headers"] = "ETag, X-Total-Count"
        response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        if "access-control-allow-credentials" in response.headers:
            del response.headers["access-control-allow-credentials"]
    return response


@app.middleware("http")
async def security_response_headers(request, call_next):
    """Apply safe browser defaults without breaking the public careers API."""

    response = await call_next(request)
    if "x-content-type-options" not in response.headers:
        response.headers["X-Content-Type-Options"] = "nosniff"
    if "referrer-policy" not in response.headers:
        response.headers["Referrer-Policy"] = "no-referrer"
    if "permissions-policy" not in response.headers:
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
    if request.headers.get("authorization") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
    if response.headers.get("content-type", "").startswith("application/json"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
    return response


@app.middleware("http")
async def public_abuse_protection(request: Request, call_next):
    """Enforce shared limits before public routes parse bodies or touch data."""

    try:
        decision = await enforce_public_request(request)
    except PublicRateLimitExceeded as exc:
        return problem_response(
            request,
            status_code=429,
            code="rate_limited",
            detail="Too many public requests. Try again later.",
            headers={
                "Retry-After": str(exc.retry_after),
                "RateLimit-Limit": str(exc.limit),
                "RateLimit-Remaining": str(exc.remaining),
            },
        )
    except PublicRateLimitUnavailable:
        # Catalog reads remain available during a limiter outage. Any public
        # operation that mutates state or consumes costly processing fails
        # closed until the shared abuse boundary recovers.
        if public_request_requires_limiter(request):
            return problem_response(
                request,
                status_code=503,
                code="abuse_protection_unavailable",
                detail="Public submission protection is temporarily unavailable.",
                headers={"Retry-After": "5"},
            )
        decision = None
    response = await call_next(request)
    if decision is not None:
        response.headers.setdefault("RateLimit-Limit", str(decision.limit))
        response.headers.setdefault("RateLimit-Remaining", str(decision.remaining))
    return response


app.add_middleware(ObservabilityMiddleware, runtime=telemetry_runtime)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth_router.router)
app.include_router(positions_router.router)
app.include_router(folders_router.router)
app.include_router(candidates_router.router)
app.include_router(candidate_search_router.router)
app.include_router(careers_router.router)
app.include_router(activities_router.router)
app.include_router(ai_hiring_router.router)
app.include_router(candidate_notes_router.router)
app.include_router(github_router.router)
app.include_router(recordings_router.router)
app.include_router(requisitions_router.router)
app.include_router(scorecards_router.router)
app.include_router(scheduling_router.router)
app.include_router(sourcing_router.router)
app.include_router(talent_router.router)
app.include_router(tasks_router.router)
app.include_router(chat_router.router)
app.include_router(communications_router.router)
app.include_router(enterprise_router.router)
app.include_router(enterprise_router.api_router)
app.include_router(interviews_router.router)
app.include_router(integrations_router.router)
app.include_router(users_router.router)
app.include_router(work_router.router)
app.include_router(workflows_router.router)
app.include_router(notifications_router.router)
app.include_router(analytics_router.router)
app.include_router(shortlists_router.router)
app.include_router(organizations_router.router)
app.include_router(public_router.router)
app.include_router(operations_router.router)
app.include_router(operations_router.enterprise_router)
app.include_router(build_observability_router(settings))

# Public static serving is intentionally limited to organization branding.
# CVs and AI attachments live elsewhere under STORAGE_DIR and must never be
# reachable through an unauthenticated static mount.
if settings.blob_store_backend == "local":
    branding_app = StaticFiles(
        directory=str(Path(settings.storage_dir) / "logos"),
        check_dir=False,
    )
else:
    # Existing web clients use the stable `/storage/logos/...` contract. In
    # S3 deployments this narrow mount redirects only validated branding keys
    # to the configured public bucket/CDN; private prefixes remain unreachable.
    branding_app = PublicBrandingRedirectApp(settings.s3_public_base_url)
app.mount("/storage/logos", branding_app, name="branding-storage")
