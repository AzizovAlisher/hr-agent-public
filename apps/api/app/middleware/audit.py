"""Append-only audit logging for authenticated mutations.

The middleware intentionally never reads request bodies. It stores route-level
metadata plus irreversible network/client hashes, so CVs, messages, prompts,
tokens, and credentials cannot enter the audit table by accident.
"""

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from types import MappingProxyType
from uuid import UUID, uuid4

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match

from app.auth.authorization import FieldClass, operation_policy
from app.auth.jwt import decode_access_token
from app.config import get_settings
from app.db import (
    audit_session_scope,
    bootstrap_user_by_id,
    forced_tenant_rls,
    set_tenant_context,
)
from app.models.enterprise import AuditLog
from app.models.user import User
from app.observability.metrics import observe_audit_database
from app.problem import (
    is_privacy_erasure_gate_error,
    problem_response,
    privacy_erasure_problem_response,
)

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PREFIXES = ("/public/", "/storage/")
SENSITIVE_FIELD_CLASSES = frozenset(
    {
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
        FieldClass.OFFER,
        FieldClass.EXPORT,
        FieldClass.BACKUP,
        FieldClass.AI_CONTEXT,
        FieldClass.SECRET,
        FieldClass.AUDIT,
    }
)
# A few compatibility projections redact sensitive fields inside the endpoint
# rather than requiring the entire field class at the operation boundary.
# They still receive conservative read auditing whenever the response succeeds.
EXTRA_SENSITIVE_READ_FIELDS: dict[str, frozenset[FieldClass]] = {
    "/positions": frozenset({FieldClass.COMPENSATION}),
    "/positions/{position_id}": frozenset({FieldClass.COMPENSATION}),
    "/positions/move": frozenset({FieldClass.COMPENSATION}),
    "/positions/project": frozenset({FieldClass.COMPENSATION}),
    "/candidates": frozenset({FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}),
    "/candidate-search": frozenset(
        {
            FieldClass.CONTACT,
            FieldClass.CANDIDATE_DOCUMENT,
            FieldClass.ASSESSMENT_RESULT,
        }
    ),
    "/candidate-duplicates": frozenset(
        {
            FieldClass.CONTACT,
            FieldClass.CANDIDATE_DOCUMENT,
            FieldClass.ASSESSMENT_RESULT,
        }
    ),
    "/candidate-export": frozenset({FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}),
    "/candidate-merges/preview": frozenset(
        {FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}
    ),
    "/candidates/{candidate_id}": frozenset(
        {FieldClass.CONTACT, FieldClass.CANDIDATE_DOCUMENT}
    ),
    "/candidates/{candidate_id}/applications": frozenset(
        {FieldClass.ASSESSMENT_RESULT}
    ),
    "/candidates/{candidate_id}/merged-sources": frozenset(
        {
            FieldClass.CONTACT,
            FieldClass.CANDIDATE_DOCUMENT,
            FieldClass.ASSESSMENT_RESULT,
        }
    ),
    "/candidates/{candidate_id}/activities": frozenset(
        {
            FieldClass.CONTACT,
            FieldClass.CANDIDATE_DOCUMENT,
            FieldClass.ASSESSMENT_RESULT,
        }
    ),
    "/positions/{position_id}/applications": frozenset(
        {FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}
    ),
    "/positions/{position_id}/candidates/{candidate_id}/stage": frozenset(
        {FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}
    ),
    "/talent/pools/{pool_id}/members": frozenset(
        {FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}
    ),
    "/talent/search": frozenset({FieldClass.ASSESSMENT_RESULT}),
    "/positions/{position_id}/talent-matches": frozenset(
        {FieldClass.ASSESSMENT_RESULT}
    ),
    "/my-work": frozenset({FieldClass.CONTACT, FieldClass.ASSESSMENT_RESULT}),
    "/notifications": frozenset({FieldClass.CONTACT}),
    "/notifications/{notification_id}/read": frozenset({FieldClass.CONTACT}),
    "/applications/{application_id}/assessment-invitations": frozenset(
        {FieldClass.CONTACT}
    ),
    "/assessment-invitations/{invitation_id}/attempts": frozenset(
        {FieldClass.ASSESSMENT_RESULT, FieldClass.CANDIDATE_DOCUMENT}
    ),
    "/requisitions": frozenset({FieldClass.COMPENSATION}),
    "/requisitions/{requisition_id}": frozenset({FieldClass.COMPENSATION}),
}


@dataclass(frozen=True, slots=True)
class AuditResourceRule:
    resource_type: str
    parameter: str


# Exact templates prevent a new identifier named ``candidate_id`` or
# ``template_id`` from silently receiving the wrong audit identity. Routes
# without a meaningful single primary resource intentionally have no entry.
AUDIT_RESOURCE_REGISTRY = MappingProxyType(
    {
        "/applications/{application_id}": AuditResourceRule(
            "application", "application_id"
        ),
        "/applications/{application_id}/assessment-invitations": AuditResourceRule(
            "application", "application_id"
        ),
        "/applications/{application_id}/stage": AuditResourceRule(
            "application", "application_id"
        ),
        "/assessment-attempts/{attempt_id}": AuditResourceRule(
            "assessment_attempt", "attempt_id"
        ),
        "/assessment-attempts/{attempt_id}/manual-grade": AuditResourceRule(
            "assessment_attempt", "attempt_id"
        ),
        "/assessment-files/{file_id}/download": AuditResourceRule(
            "assessment_file", "file_id"
        ),
        "/assessment-invitations/{invitation_id}/attempts": AuditResourceRule(
            "assessment_invitation", "invitation_id"
        ),
        "/assessment-invitations/{invitation_id}/reissue": AuditResourceRule(
            "assessment_invitation", "invitation_id"
        ),
        "/assessment-invitations/{invitation_id}/retakes": AuditResourceRule(
            "assessment_invitation", "invitation_id"
        ),
        "/assessment-invitations/{invitation_id}/revoke": AuditResourceRule(
            "assessment_invitation", "invitation_id"
        ),
        "/assessment-templates/{template_id}": AuditResourceRule(
            "assessment_template", "template_id"
        ),
        "/assessment-templates/{template_id}/versions": AuditResourceRule(
            "assessment_template", "template_id"
        ),
        "/candidates/{candidate_id}": AuditResourceRule("candidate", "candidate_id"),
        "/candidates/{candidate_id}/activities": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/applications": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/appointments": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/best-positions": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/communication-threads": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/communications/preview": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/communications/send": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/contact-points": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/contact-points/{contact_point_id}": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/cv": AuditResourceRule("candidate", "candidate_id"),
        "/candidates/{candidate_id}/decisions": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/github": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/github/detect": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/handoffs": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/interviews": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/interviews/generate": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/merge-history": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/merged-sources": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/evidence": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/notes": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/notes/{note_id}": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/offers": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/processing-jobs/{job_id}": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/recordings": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/scheduling-links": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/score": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/score-all-open": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/scorecards": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/scorecards/assign": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/tags": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/tags/{tag_id}": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/tasks": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{candidate_id}/view": AuditResourceRule(
            "candidate", "candidate_id"
        ),
        "/candidates/{primary_candidate_id}/merge": AuditResourceRule(
            "candidate", "primary_candidate_id"
        ),
        "/communications/templates/{template_id}": AuditResourceRule(
            "communication_template", "template_id"
        ),
        "/handoffs/{handoff_id}": AuditResourceRule("handoff", "handoff_id"),
        "/hiring-decisions/{decision_id}/vote": AuditResourceRule(
            "hiring_decision", "decision_id"
        ),
        "/integrations/assessment/applications/{application_id}/evidence": AuditResourceRule(
            "application", "application_id"
        ),
        "/interviews/{interview_id}": AuditResourceRule("interview", "interview_id"),
        "/interviews/{interview_id}/complete": AuditResourceRule(
            "interview", "interview_id"
        ),
        "/interviews/{interview_id}/kit-versions": AuditResourceRule(
            "interview", "interview_id"
        ),
        "/interviews/{interview_id}/questions/{question_id}": AuditResourceRule(
            "interview", "interview_id"
        ),
        "/offer-templates/{template_id}": AuditResourceRule(
            "offer_template", "template_id"
        ),
        "/offers/{offer_id}": AuditResourceRule("offer", "offer_id"),
        "/offers/{offer_id}/rotate": AuditResourceRule("offer", "offer_id"),
        "/offers/{offer_id}/send": AuditResourceRule("offer", "offer_id"),
        "/offers/{offer_id}/submit": AuditResourceRule("offer", "offer_id"),
        "/offers/{offer_id}/vote": AuditResourceRule("offer", "offer_id"),
        "/offers/{offer_id}/withdraw": AuditResourceRule("offer", "offer_id"),
        "/positions/{position_id}": AuditResourceRule("position", "position_id"),
        "/positions/{position_id}/applications": AuditResourceRule(
            "position", "position_id"
        ),
        "/positions/{position_id}/candidates/{candidate_id}/stage": AuditResourceRule(
            "application_candidate", "candidate_id"
        ),
        "/positions/{position_id}/candidates/{candidate_id}/work": AuditResourceRule(
            "application_candidate", "candidate_id"
        ),
        "/positions/{position_id}/generate-profile": AuditResourceRule(
            "position", "position_id"
        ),
        "/positions/{position_id}/referral-links": AuditResourceRule(
            "position", "position_id"
        ),
        "/positions/{position_id}/rescore": AuditResourceRule(
            "position", "position_id"
        ),
        "/positions/{position_id}/shortlists": AuditResourceRule(
            "position", "position_id"
        ),
        "/positions/{position_id}/talent-matches": AuditResourceRule(
            "position", "position_id"
        ),
        "/recordings/{recording_id}": AuditResourceRule(
            "interview_recording", "recording_id"
        ),
        "/recordings/{recording_id}/file": AuditResourceRule(
            "interview_recording", "recording_id"
        ),
        "/requisitions/{requisition_id}": AuditResourceRule(
            "requisition", "requisition_id"
        ),
        "/requisitions/{requisition_id}/cancel": AuditResourceRule(
            "requisition", "requisition_id"
        ),
        "/requisitions/{requisition_id}/open": AuditResourceRule(
            "requisition", "requisition_id"
        ),
        "/requisitions/{requisition_id}/submit": AuditResourceRule(
            "requisition", "requisition_id"
        ),
        "/requisitions/{requisition_id}/vote": AuditResourceRule(
            "requisition", "requisition_id"
        ),
        "/scorecard-assignments/{assignment_id}": AuditResourceRule(
            "scorecard_assignment", "assignment_id"
        ),
        "/scorecard-assignments/{assignment_id}/submit": AuditResourceRule(
            "scorecard_assignment", "assignment_id"
        ),
        "/scorecard-templates/{template_id}": AuditResourceRule(
            "scorecard_template", "template_id"
        ),
        "/tasks/{task_id}": AuditResourceRule("task", "task_id"),
        "/tasks/{task_id}/evaluate": AuditResourceRule("task", "task_id"),
        "/tasks/{task_id}/file": AuditResourceRule("task", "task_id"),
        "/users/{user_id}": AuditResourceRule("user", "user_id"),
        "/users/{user_id}/password-reset": AuditResourceRule("user", "user_id"),
        "/workflows/rules/{rule_id}": AuditResourceRule("workflow_rule", "rule_id"),
        "/workflows/rules/{rule_id}/dry-run": AuditResourceRule(
            "workflow_rule", "rule_id"
        ),
        "/workflows/rules/{rule_id}/pause": AuditResourceRule(
            "workflow_rule", "rule_id"
        ),
        "/workflows/rules/{rule_id}/resume": AuditResourceRule(
            "workflow_rule", "rule_id"
        ),
        "/workflows/runs/{run_id}/cancel": AuditResourceRule("workflow_run", "run_id"),
        "/workflows/runs/{run_id}/replay": AuditResourceRule("workflow_run", "run_id"),
    }
)


def _digest(value: str | None) -> str | None:
    if not value:
        return None
    return hmac.new(
        get_settings().jwt_secret.encode(), value.encode(), hashlib.sha256
    ).hexdigest()


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _resource(
    request: Request,
    path_template: str,
) -> tuple[str | None, UUID | None]:
    rule = AUDIT_RESOURCE_REGISTRY.get(path_template)
    if rule is None:
        return None, None
    value = request.path_params.get(rule.parameter)
    if value is None:
        return rule.resource_type, None
    try:
        return rule.resource_type, UUID(str(value))
    except (TypeError, ValueError):
        return rule.resource_type, None


def _safe_path_template(request: Request) -> str:
    """Resolve a registered route template without retaining raw path secrets.

    Audit intent is written before FastAPI performs normal route resolution, so
    ``request.scope["route"]`` is not available yet.  Matching the already
    registered route table is read-only and gives us the static template (for
    example ``/auth/password-reset/{token}``) without ever copying a bearer-like
    capability from the request URL.  Unknown routes deliberately collapse to
    one fixed value rather than falling back to the raw path.
    """

    resolved = request.scope.get("route")
    path_template = getattr(resolved, "path", None)
    if isinstance(path_template, str):
        return path_template

    def match_routes(routes) -> str | None:
        for route in routes:
            try:
                match, _child_scope = route.matches(request.scope)
            except (AttributeError, TypeError, ValueError):
                continue
            if match is not Match.FULL:
                continue
            path_template = getattr(route, "path", None)
            if isinstance(path_template, str):
                return path_template

            # FastAPI 0.141+ keeps included routers behind a lightweight
            # selection wrapper. Its outer match intentionally has no path;
            # recurse into the original registered routes to recover the
            # static template without executing the endpoint.
            nested_router = getattr(route, "original_router", None)
            nested_routes = getattr(nested_router, "routes", None)
            if nested_routes:
                nested_match = match_routes(nested_routes)
                if nested_match is not None:
                    return nested_match
        return None

    router = getattr(request.app, "router", None)
    matched = match_routes(getattr(router, "routes", ()))
    if matched is not None:
        return matched
    return "/unmatched"


def _audit_unavailable_response(request_id: UUID) -> JSONResponse:
    response = JSONResponse(
        status_code=503,
        content={
            "type": "about:blank",
            "title": "Service Unavailable",
            "status": 503,
            "detail": "The audit service is temporarily unavailable.",
            "code": "audit_unavailable",
            "request_id": str(request_id),
        },
        media_type="application/problem+json",
    )
    response.headers["X-Request-ID"] = str(request_id)
    return response


@asynccontextmanager
async def _audited_session(phase: str) -> AsyncIterator[AsyncSession]:
    started_at = perf_counter()
    try:
        async with audit_session_scope() as db:
            yield db
    except Exception:
        observe_audit_database(
            phase=phase,
            outcome="failed",
            duration_seconds=perf_counter() - started_at,
        )
        raise
    else:
        observe_audit_database(
            phase=phase,
            outcome="succeeded",
            duration_seconds=perf_counter() - started_at,
        )


async def _bind_audit_actor(db: AsyncSession, user_id: UUID) -> User | None:
    """Resolve an audit actor without reading across the tenant boundary."""

    if forced_tenant_rls():
        bootstrap = await bootstrap_user_by_id(db, user_id)
        if bootstrap is None:
            return None
        await set_tenant_context(
            db,
            organization_id=bootstrap.organization_id,
            actor_id=bootstrap.identity_id,
            identity_kind="user",
        )
        user = (
            await db.execute(
                select(User).where(
                    User.id == bootstrap.identity_id,
                    User.organization_id == bootstrap.organization_id,
                )
            )
        ).scalar_one_or_none()
    else:
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    # Rebind with only database-verified attributes. This also gives the audit
    # INSERT the tenant context required by the forced-RLS WITH CHECK policy.
    await set_tenant_context(
        db,
        organization_id=user.organization_id,
        actor_id=user.id,
        identity_kind="user",
        role=user.role,
    )
    return user


async def audit_http_request(request: Request, call_next) -> Response:
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, UUID):
        request_id = uuid4()
        request.state.request_id = request_id
    path = request.url.path
    safe_path_template = _safe_path_template(request)
    mutation_requires_audit = not (
        request.method in SAFE_METHODS
        or path.startswith("/health")
        or path == "/auth/login"
        or path.startswith(PUBLIC_PREFIXES)
    )
    token = _bearer_token(request)
    user_id: UUID | None = None
    audit_started = False
    if mutation_requires_audit and token is not None:
        try:
            payload = decode_access_token(token)
            user_id = UUID(payload["sub"])
        except (ValueError, KeyError, TypeError):
            user_id = None

    # Persist an audit-intent row before an authenticated mutation executes.
    # This prevents a database/audit outage from silently allowing an
    # unaudited successful write. The row is finalized after routing; if that
    # second update fails, the durable `audit_state=started` record still
    # proves the request occurred and can trigger operator reconciliation.
    if user_id is not None:
        try:
            async with _audited_session("preflight") as db:
                user = await _bind_audit_actor(db, user_id)
                if user is None:
                    # A validly shaped bearer whose actor is missing/disabled
                    # must not proceed to a mutation. The actor could otherwise
                    # be re-enabled between this preflight and route auth,
                    # producing a committed write with no durable audit intent.
                    return problem_response(
                        request,
                        status_code=401,
                        code="unauthorized",
                        detail="Invalid bearer token",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                db.add(
                    AuditLog(
                        organization_id=user.organization_id,
                        actor_user_id=user.id,
                        actor_email_snapshot=user.email,
                        action=(f"{request.method.lower()}:{safe_path_template}")[:160],
                        resource_type=None,
                        resource_id=None,
                        path_template=safe_path_template[:512],
                        method=request.method,
                        # 102 denotes a durable request intent awaiting the
                        # endpoint's terminal response status.
                        status_code=102,
                        request_id=request_id,
                        ip_hash=_digest(request.client.host if request.client else None),
                        user_agent_hash=_digest(request.headers.get("user-agent")),
                        metadata_json={
                            "query_keys": sorted(set(request.query_params.keys())),
                            "audit_state": "started",
                        },
                        occurred_at=datetime.now(UTC),
                    )
                )
                audit_started = True
        except DBAPIError as exc:
            if is_privacy_erasure_gate_error(exc):
                return privacy_erasure_problem_response(request)
            logger.exception(
                "Mutation audit preflight failed",
                extra={"request_id": str(request_id), "method": request.method},
            )
            return _audit_unavailable_response(request_id)
        except Exception:
            logger.exception(
                "Mutation audit preflight failed",
                extra={"request_id": str(request_id), "method": request.method},
            )
            return _audit_unavailable_response(request_id)

    response = await call_next(request)
    response.headers.setdefault("X-Request-ID", str(request_id))
    sensitive_audit_metadata: dict[str, object] | None = None

    # Successful authenticated accesses that return sensitive field classes must be
    # durable before the response body is released to the caller. Endpoint
    # implementations may add richer domain audit records; this central row
    # guarantees complete path/actor coverage without retaining response data.
    if (
        response.status_code < 400
        and token is not None
        and not path.startswith(PUBLIC_PREFIXES)
    ):
        try:
            payload = decode_access_token(token)
            read_user_id = UUID(payload["sub"])
            route = request.scope.get("route")
            path_template = getattr(route, "path", safe_path_template)
            policy = operation_policy(request.method, path_template)
            fields: set[FieldClass] = set()
            fields.update(EXTRA_SENSITIVE_READ_FIELDS.get(path_template, ()))
            if policy is not None:
                fields.update(policy.fields & SENSITIVE_FIELD_CLASSES)
            if fields:
                async with _audited_session("sensitive_read") as db:
                    user = await _bind_audit_actor(db, read_user_id)
                    if user is None:
                        # The route authenticated successfully, so losing or
                        # disabling its actor between endpoint completion and
                        # audit persistence is a real authorization race. Never
                        # release the already-produced sensitive body without a
                        # tenant-bound durable row.
                        raise RuntimeError("Sensitive-read audit actor is unavailable")
                    endpoint = request.scope.get("endpoint")
                    endpoint_name = getattr(endpoint, "__name__", None)
                    resource_type, resource_id = _resource(request, path_template)
                    sensitive_audit_metadata = {
                        "sensitive_read": True,
                        "field_classes": sorted(field.value for field in fields),
                        "query_keys": sorted(set(request.query_params.keys())),
                    }
                    sensitive_action = (
                        f"sensitive_read.{endpoint_name}"
                        if endpoint_name
                        else "sensitive_read"
                    )[:160]
                    if audit_started and user_id == read_user_id:
                        result = await db.execute(
                            update(AuditLog)
                            .where(
                                AuditLog.organization_id == user.organization_id,
                                AuditLog.request_id == request_id,
                            )
                            .values(
                                action=sensitive_action,
                                resource_type=resource_type,
                                resource_id=resource_id,
                                path_template=path_template[:512],
                                method=request.method,
                                status_code=response.status_code,
                                metadata_json=sensitive_audit_metadata,
                            )
                        )
                        if result.rowcount != 1:
                            raise RuntimeError(
                                "Sensitive mutation audit intent is missing"
                            )
                    else:
                        db.add(
                            AuditLog(
                                organization_id=user.organization_id,
                                actor_user_id=user.id,
                                actor_email_snapshot=user.email,
                                action=sensitive_action,
                                resource_type=resource_type,
                                resource_id=resource_id,
                                path_template=path_template[:512],
                                method=request.method,
                                status_code=response.status_code,
                                request_id=request_id,
                                ip_hash=_digest(
                                    request.client.host if request.client else None
                                ),
                                user_agent_hash=_digest(
                                    request.headers.get("user-agent")
                                ),
                                metadata_json=sensitive_audit_metadata,
                                occurred_at=datetime.now(UTC),
                            )
                        )
        except (ValueError, KeyError, TypeError):
            # Invalid bearer material will be rejected by the route's auth
            # dependency; do not shadow that response with an audit error.
            pass
        except DBAPIError as exc:
            if is_privacy_erasure_gate_error(exc):
                return privacy_erasure_problem_response(request)
            logger.exception(
                "Sensitive-read audit failed",
                extra={"request_id": str(request_id), "method": request.method},
            )
            return _audit_unavailable_response(request_id)
        except Exception:
            logger.exception(
                "Sensitive-read audit failed",
                extra={"request_id": str(request_id), "method": request.method},
            )
            return _audit_unavailable_response(request_id)

    if not audit_started or user_id is None:
        return response

    try:
        async with _audited_session("finalize") as db:
            user = await _bind_audit_actor(db, user_id)
            if user is None:
                return response
            route = request.scope.get("route")
            path_template = getattr(route, "path", safe_path_template)
            endpoint = request.scope.get("endpoint")
            endpoint_name = getattr(endpoint, "__name__", None)
            resource_type, resource_id = _resource(request, path_template)
            await db.execute(
                update(AuditLog)
                .where(
                    AuditLog.organization_id == user.organization_id,
                    AuditLog.request_id == request_id,
                )
                .values(
                    action=endpoint_name or f"{request.method.lower()}:{path_template}",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    path_template=path_template[:512],
                    status_code=response.status_code,
                    metadata_json={
                        "query_keys": sorted(set(request.query_params.keys())),
                    }
                    if sensitive_audit_metadata is None
                    else sensitive_audit_metadata,
                )
            )
    except Exception:
        logger.exception(
            "Mutation audit finalization failed",
            extra={"request_id": str(request_id), "method": request.method},
        )
    return response
