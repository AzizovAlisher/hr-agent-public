from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import and_, event, false, or_, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, with_loader_criteria

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
audit_engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=_settings.audit_database_pool_size,
    max_overflow=0,
    pool_timeout=_settings.audit_database_pool_timeout_seconds,
)
AuditSessionLocal = async_sessionmaker(
    audit_engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


@dataclass(frozen=True, slots=True)
class TenantContext:
    organization_id: UUID
    actor_id: UUID | None
    identity_kind: str
    role: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityBootstrap:
    identity_id: UUID
    organization_id: UUID


@dataclass(frozen=True, slots=True)
class RefreshBootstrap:
    token_id: UUID
    session_id: UUID
    user_id: UUID
    organization_id: UUID


@dataclass(frozen=True, slots=True)
class ApiKeyBootstrap:
    api_key_id: UUID
    organization_id: UUID
    created_by_user_id: UUID | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityBootstrap:
    capability_id: UUID
    organization_id: UUID
    resource_id: UUID | None


class AssignmentScope(StrEnum):
    """Reviewed relationship boundary for an ORM resource.

    These values are deliberately domain concepts rather than inferred column
    shapes.  A new model is not assignment-safe merely because it happens to
    contain ``candidate_id`` or ``application_id``; it must be reviewed and
    added to :data:`MODEL_ASSIGNMENT_SCOPES` with an explicit strategy.
    """

    APPLICATION_SCOPED = "application_scoped"
    POSITION_SCOPED = "position_scoped"
    CANDIDATE_GLOBAL = "candidate_global"
    EXPLICIT_PARENT_SCOPED = "explicit_parent_scoped"
    ORGANIZATION_GLOBAL = "organization_global"
    DENIED_ASSIGNED = "denied_assigned"


@dataclass(frozen=True, slots=True)
class ModelAssignmentRule:
    scope: AssignmentScope
    application_field: str | None = None
    position_field: str | None = None
    candidate_field: str | None = None
    identity_field: str = "id"
    parent_scope: str | None = None
    parent_field: str | None = None
    interviewer_access: bool = False
    position_only_fallback: bool = False
    owner_field: str | None = None


def _assignment_rule(
    scope: AssignmentScope,
    *,
    application_field: str | None = None,
    position_field: str | None = None,
    candidate_field: str | None = None,
    identity_field: str = "id",
    parent_scope: str | None = None,
    parent_field: str | None = None,
    interviewer_access: bool = False,
    position_only_fallback: bool = False,
    owner_field: str | None = None,
) -> ModelAssignmentRule:
    return ModelAssignmentRule(
        scope=scope,
        application_field=application_field,
        position_field=position_field,
        candidate_field=candidate_field,
        identity_field=identity_field,
        parent_scope=parent_scope,
        parent_field=parent_field,
        interviewer_access=interviewer_access,
        position_only_fallback=position_only_fallback,
        owner_field=owner_field,
    )


# Immutable, name-based registration avoids importing ORM models while
# ``Base`` itself is still being constructed.  The completeness release gate
# resolves every name against ``Base.registry`` after model import and rejects
# both stale entries and unreviewed relationship models.
MODEL_ASSIGNMENT_SCOPES = MappingProxyType(
    {
        # Direct application-cycle records.
        "Application": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="id",
            interviewer_access=True,
        ),
        "ApplicationEvaluation": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "ApplicationStageEvent": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "AssessmentAttempt": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "AssessmentInvitation": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "AssessmentResult": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "CandidateActivity": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "CandidateHandoff": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "CandidateOffer": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "CandidateProcessingJob": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "ChatSession": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            position_field="position_id",
            candidate_field="candidate_id",
            position_only_fallback=True,
            owner_field="user_id",
        ),
        "CommunicationThread": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "HiringDecision": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "Interview": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "InterviewAppointment": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "InterviewRecording": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "ProviderAssessment": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "SchedulingLink": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "ScorecardAssignment": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            interviewer_access=True,
        ),
        "SourceCandidateReceipt": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            position_field="position_id",
            candidate_field="candidate_id",
            position_only_fallback=True,
        ),
        "TaskSubmission": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
        ),
        "WorkflowRun": _assignment_rule(
            AssignmentScope.APPLICATION_SCOPED,
            application_field="application_id",
            position_field="position_id",
            candidate_field="candidate_id",
            position_only_fallback=True,
        ),
        # Navigable assignment dimensions and position-owned resources.
        "Candidate": _assignment_rule(
            AssignmentScope.CANDIDATE_GLOBAL,
            interviewer_access=True,
        ),
        "Position": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            interviewer_access=True,
        ),
        "CommunicationBatch": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
        ),
        "HiringMission": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
            interviewer_access=True,
            owner_field="created_by_user_id",
        ),
        "ReferralLink": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
        ),
        "Requisition": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
        ),
        "ScorecardTemplate": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
        ),
        "ShortlistLink": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
        ),
        "SourcePublication": _assignment_rule(
            AssignmentScope.POSITION_SCOPED,
            position_field="position_id",
        ),
        # Candidate CRM data is not tied to one hiring cycle. Assigned roles
        # receive candidate projections through Application, but never these
        # organization-wide artifacts.
        "AiCandidateSourceObject": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "CandidateContactPoint": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "CandidateMergeBatch": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "CandidateMergeBatchMember": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "CandidateNote": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "CandidateTag": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "TalentNurtureEnrollment": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "TalentNurtureSequence": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "TalentNurtureStep": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "TalentPool": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        "TalentPoolMember": _assignment_rule(AssignmentScope.CANDIDATE_GLOBAL),
        # Children whose scope is inherited through a reviewed, exact parent.
        "AiActionBundle": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission",
            parent_field="mission_id",
        ),
        "AiAgentRun": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission",
            parent_field="mission_id",
        ),
        "AiMessage": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="ai_session",
            parent_field="session_id",
        ),
        "AssessmentAnswer": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="assessment_attempt",
            parent_field="attempt_id",
            interviewer_access=True,
        ),
        "AssessmentFile": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="assessment_attempt",
            parent_field="attempt_id",
            interviewer_access=True,
        ),
        "CommunicationMessage": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="communication_thread",
            parent_field="thread_id",
        ),
        "EvidenceClaim": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission_assessment",
            parent_field="assessment_id",
        ),
        "HiringDecisionApproval": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="hiring_decision",
            parent_field="decision_id",
        ),
        "InterviewAppointmentInterviewer": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="interview_appointment",
            parent_field="appointment_id",
            interviewer_access=True,
        ),
        "InterviewAssignment": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="interview",
            parent_field="interview_id",
            interviewer_access=True,
        ),
        "InterviewKitVersion": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="interview",
            parent_field="interview_id",
            interviewer_access=True,
        ),
        "InterviewQuestion": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="interview",
            parent_field="interview_id",
            interviewer_access=True,
        ),
        "InterviewReminder": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="interview_appointment",
            parent_field="appointment_id",
            interviewer_access=True,
        ),
        "Message": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="chat_session",
            parent_field="session_id",
        ),
        "MissionCandidateAssessment": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission_assessment",
        ),
        "MissionEvent": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission",
            parent_field="mission_id",
        ),
        "MissionPlanVersion": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission",
            parent_field="mission_id",
        ),
        "MissionStep": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission",
            parent_field="mission_id",
        ),
        "MissionStepAttempt": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="mission_step",
            parent_field="step_id",
        ),
        "OfferApproval": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="offer",
            parent_field="offer_id",
        ),
        "ProviderAssessmentResult": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="provider_assessment",
            parent_field="provider_assessment_id",
            interviewer_access=True,
        ),
        "RequisitionApproval": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="requisition",
            parent_field="requisition_id",
        ),
        "SchedulingLinkInterviewer": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="scheduling_link",
            parent_field="scheduling_link_id",
            interviewer_access=True,
        ),
        "ScorecardCriterion": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="scorecard_template",
            parent_field="template_id",
        ),
        "ScorecardRating": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="scorecard_assignment",
            parent_field="assignment_id",
            interviewer_access=True,
        ),
        "ScorecardTemplateVersion": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="scorecard_template",
            parent_field="template_id",
        ),
        # Legacy compatibility and provider control records need bespoke
        # handling and never become visible from independent ID dimensions.
        "Score": _assignment_rule(
            AssignmentScope.EXPLICIT_PARENT_SCOPED,
            parent_scope="legacy_application",
            interviewer_access=True,
        ),
        "IntegrationWebhookEvent": _assignment_rule(AssignmentScope.DENIED_ASSIGNED),
        # Organization-level roots used by assignment-scoped routes. Their
        # operation permissions remain authoritative; their descendants above
        # never infer scope from a coincidental foreign-key name.
        "AiSession": _assignment_rule(
            AssignmentScope.ORGANIZATION_GLOBAL,
            owner_field="created_by_user_id",
        ),
        "AssessmentQuestion": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "AssessmentSection": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "AssessmentTemplate": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "AssessmentTemplateVersion": _assignment_rule(
            AssignmentScope.ORGANIZATION_GLOBAL
        ),
        "CandidateSavedView": _assignment_rule(
            AssignmentScope.ORGANIZATION_GLOBAL,
            owner_field="user_id",
        ),
        "CommunicationAccount": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "CommunicationTemplate": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "Folder": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "OfferTemplate": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
        "WorkflowRule": _assignment_rule(AssignmentScope.ORGANIZATION_GLOBAL),
    }
)


_TENANT_CONTEXT_KEY = "hr_agent_tenant_context"
_IDENTITY_KINDS = {"user", "api_key", "public", "background", "system"}

_CAPABILITY_CLAIM_FUNCTIONS = {
    "assessment": "security.claim_assessment_invitation",
    "shortlist": "security.claim_shortlist_link",
    "scheduling": "security.claim_scheduling_link",
    "scheduling_manage": "security.claim_scheduling_management",
    "offer": "security.claim_offer",
    "referral": "security.claim_referral_link",
}

_CAPABILITY_RESOLVE_FUNCTIONS = {
    "assessment": "security.resolve_assessment_invitation",
    "shortlist": "security.resolve_shortlist_link",
    "scheduling": "security.resolve_scheduling_link",
    "scheduling_manage": "security.resolve_scheduling_management",
    "offer": "security.resolve_offer",
    "referral": "security.resolve_referral_link",
    "user_access": "security.resolve_user_access_token",
}

_RESOURCE_CLAIM_FUNCTIONS = {
    "communication_account": "security.claim_communication_account",
    "source_integration": "security.claim_source_integration",
    "integration_connection": "security.claim_integration_connection",
}


class TenantBoundaryError(RuntimeError):
    """Raised before flush when an object crosses the bound organization."""


def forced_tenant_rls() -> bool:
    return get_settings().tenant_rls_mode == "forced"


async def _bootstrap_mapping(
    session: AsyncSession,
    statement: str,
    parameters: dict[str, object],
):
    """Run one fixed broker claim before tenant RLS context exists."""

    if not forced_tenant_rls():
        return None
    return (
        (
            await session.execute(
                text(statement),
                parameters,
                execution_options={"skip_tenant_scope": True},
            )
        )
        .mappings()
        .one_or_none()
    )


async def bootstrap_user_by_id(
    session: AsyncSession, user_id: UUID
) -> IdentityBootstrap | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT user_id, organization_id FROM security.claim_user_by_id(:user_id)",
        {"user_id": user_id},
    )
    if row is None:
        return None
    return IdentityBootstrap(row["user_id"], row["organization_id"])


async def bootstrap_user_by_email(
    session: AsyncSession, email: str
) -> IdentityBootstrap | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT user_id, organization_id FROM security.claim_user_by_email(:email)",
        {"email": email},
    )
    if row is None:
        return None
    return IdentityBootstrap(row["user_id"], row["organization_id"])


async def bootstrap_refresh_token(
    session: AsyncSession, *, digest: str, now: object
) -> RefreshBootstrap | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT token_id, session_id, user_id, organization_id "
        "FROM security.claim_refresh_token(:digest, :now)",
        {"digest": digest, "now": now},
    )
    if row is None:
        return None
    return RefreshBootstrap(
        row["token_id"],
        row["session_id"],
        row["user_id"],
        row["organization_id"],
    )


async def bootstrap_api_key(
    session: AsyncSession, *, digest: str, now: object
) -> ApiKeyBootstrap | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT api_key_id, organization_id, created_by_user_id, scopes "
        "FROM security.claim_api_key(:digest, :now)",
        {"digest": digest, "now": now},
    )
    if row is None:
        return None
    return ApiKeyBootstrap(
        row["api_key_id"],
        row["organization_id"],
        row["created_by_user_id"],
        tuple(row["scopes"] or ()),
    )


async def bootstrap_capability(
    session: AsyncSession,
    *,
    kind: str,
    digest: str,
    now: object,
) -> CapabilityBootstrap | None:
    function = _CAPABILITY_CLAIM_FUNCTIONS.get(kind)
    if function is None:
        raise ValueError("Unknown public capability kind")
    row = await _bootstrap_mapping(
        session,
        f"SELECT capability_id, organization_id, resource_id "
        f"FROM {function}(:digest, :now)",
        {"digest": digest, "now": now},
    )
    if row is None:
        return None
    return CapabilityBootstrap(
        row["capability_id"], row["organization_id"], row["resource_id"]
    )


async def resolve_capability(
    session: AsyncSession,
    *,
    kind: str,
    digest: str,
) -> CapabilityBootstrap | None:
    """Resolve capability ownership before RLS, retaining revoked/expired state.

    The broker returns only opaque IDs and tenant identity. The tenant-bound
    query that follows remains authoritative for state-specific 404/410 rules.
    """

    function = _CAPABILITY_RESOLVE_FUNCTIONS.get(kind)
    if function is None:
        raise ValueError("Unknown public capability kind")
    row = await _bootstrap_mapping(
        session,
        f"SELECT capability_id, organization_id, resource_id FROM {function}(:digest)",
        {"digest": digest},
    )
    if row is None:
        return None
    return CapabilityBootstrap(
        row["capability_id"], row["organization_id"], row["resource_id"]
    )


async def bootstrap_public_organization(
    session: AsyncSession, slug: str
) -> UUID | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT organization_id FROM security.claim_public_organization(:slug)",
        {"slug": slug},
    )
    return row["organization_id"] if row is not None else None


async def bootstrap_public_position(
    session: AsyncSession, position_id: UUID
) -> UUID | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT organization_id FROM security.claim_public_position(:position_id)",
        {"position_id": position_id},
    )
    return row["organization_id"] if row is not None else None


async def bootstrap_resource(
    session: AsyncSession, *, kind: str, resource_id: UUID
) -> IdentityBootstrap | None:
    function = _RESOURCE_CLAIM_FUNCTIONS.get(kind)
    if function is None:
        raise ValueError("Unknown tenant resource bootstrap kind")
    row = await _bootstrap_mapping(
        session,
        f"SELECT capability_id, organization_id FROM {function}(:resource_id)",
        {"resource_id": resource_id},
    )
    if row is None:
        return None
    return IdentityBootstrap(row["capability_id"], row["organization_id"])


async def bootstrap_oauth_state(
    session: AsyncSession, *, digest: str, now: object
) -> CapabilityBootstrap | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT capability_id, organization_id, resource_id "
        "FROM security.claim_integration_oauth_state(:digest, :now)",
        {"digest": digest, "now": now},
    )
    if row is None:
        return None
    return CapabilityBootstrap(
        row["capability_id"], row["organization_id"], row["resource_id"]
    )


async def resolve_oauth_state(
    session: AsyncSession, *, digest: str
) -> CapabilityBootstrap | None:
    row = await _bootstrap_mapping(
        session,
        "SELECT capability_id, organization_id, resource_id "
        "FROM security.resolve_integration_oauth_state(:digest)",
        {"digest": digest},
    )
    if row is None:
        return None
    return CapabilityBootstrap(
        row["capability_id"], row["organization_id"], row["resource_id"]
    )


@event.listens_for(Session, "before_flush")
def _reject_cross_tenant_writes(session, _flush_context, _instances) -> None:
    context = session.info.get(_TENANT_CONTEXT_KEY)
    if not isinstance(context, TenantContext):
        return
    for instance in session.new.union(session.dirty).union(session.deleted):
        if not hasattr(instance, "organization_id"):
            continue
        value = getattr(instance, "organization_id", None)
        if value != context.organization_id:
            raise TenantBoundaryError(
                f"Tenant-bound session cannot write {type(instance).__name__} "
                "for another organization"
            )


def _apply_postgres_tenant_settings(connection, context: TenantContext) -> None:
    """Set transaction-local PostgreSQL context without contaminating a pool."""

    if connection.dialect.name != "postgresql":
        return
    values = {
        "organization_id": str(context.organization_id),
        "actor_id": str(context.actor_id) if context.actor_id is not None else "",
        "identity_kind": context.identity_kind,
        "actor_role": context.role or "",
    }
    for setting, value in values.items():
        connection.execute(
            text(f"SELECT set_config('app.{setting}', :value, true)"),
            {"value": value},
        )


@event.listens_for(Session, "after_begin")
def _restore_transaction_local_tenant_context(
    session, _transaction, connection
) -> None:
    """Reapply ``SET LOCAL`` after a handler commits and starts a new tx."""

    context = session.info.get(_TENANT_CONTEXT_KEY)
    if isinstance(context, TenantContext):
        _apply_postgres_tenant_settings(connection, context)


async def set_tenant_context(
    session: AsyncSession,
    *,
    organization_id: UUID,
    actor_id: UUID | None = None,
    identity_kind: str,
    role: str | None = None,
) -> TenantContext:
    """Bind an identity to a session and its current/future transactions.

    ``set_config(..., true)`` is transaction-local.  PostgreSQL clears it at
    commit/rollback, and ``after_begin`` reapplies it only while this logical
    SQLAlchemy session remains bound, which is safe with pooled connections.
    """

    if identity_kind not in _IDENTITY_KINDS:
        raise ValueError(f"Unknown tenant identity kind: {identity_kind}")
    context = TenantContext(organization_id, actor_id, identity_kind, role)
    session.info[_TENANT_CONTEXT_KEY] = context
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # Authentication usually began the transaction before tenant identity
        # was known, so set the current transaction explicitly as well.
        await session.execute(
            text("SELECT set_config('app.organization_id', :value, true)"),
            {"value": str(organization_id)},
            execution_options={"skip_tenant_scope": True},
        )
        await session.execute(
            text("SELECT set_config('app.actor_id', :value, true)"),
            {"value": str(actor_id) if actor_id is not None else ""},
            execution_options={"skip_tenant_scope": True},
        )
        await session.execute(
            text("SELECT set_config('app.identity_kind', :value, true)"),
            {"value": identity_kind},
            execution_options={"skip_tenant_scope": True},
        )
        await session.execute(
            text("SELECT set_config('app.actor_role', :value, true)"),
            {"value": role or ""},
            execution_options={"skip_tenant_scope": True},
        )
    return context


def tenant_context(session: AsyncSession) -> TenantContext | None:
    value = session.info.get(_TENANT_CONTEXT_KEY)
    return value if isinstance(value, TenantContext) else None


def clear_tenant_context(session: AsyncSession) -> None:
    session.info.pop(_TENANT_CONTEXT_KEY, None)


@asynccontextmanager
async def tenant_session_scope(
    *,
    organization_id: UUID,
    actor_id: UUID | None = None,
    identity_kind: str,
    role: str | None = None,
) -> AsyncIterator[AsyncSession]:
    """Tenant-bound unit of work for API keys, workers, and background jobs."""

    async with SessionLocal() as session:
        await set_tenant_context(
            session,
            organization_id=organization_id,
            actor_id=actor_id,
            identity_kind=identity_kind,
            role=role,
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            clear_tenant_context(session)


def _assignment_subqueries(context: TenantContext):
    """Return reviewed position/candidate/application assignment subqueries.

    Application access must be derived from an assigned candidate/position
    *pair*.  Treating the two dimensions as independent sets permits an
    interviewer assigned to ``candidate A / position X`` and ``candidate B /
    position Y`` to see the unrelated cross-product applications ``A/Y`` and
    ``B/X``.  Downstream assessment records inherit the exact application
    scope through their ``application_id`` foreign key.
    """

    from app.models.application import Application
    from app.models.interview import Interview, InterviewAssignment
    from app.models.requisition import CandidateOffer, Requisition
    from app.models.score import Score
    from app.models.scorecard import ScorecardAssignment

    application_table = Application.__table__
    requisition_table = Requisition.__table__
    score_table = Score.__table__
    offer_table = CandidateOffer.__table__

    if context.role == "hiring_manager":
        positions = select(requisition_table.c.position_id).where(
            requisition_table.c.organization_id == context.organization_id,
            requisition_table.c.hiring_manager_user_id == context.actor_id,
            requisition_table.c.position_id.is_not(None),
        )
        candidates = (
            select(application_table.c.candidate_id)
            .where(
                application_table.c.organization_id == context.organization_id,
                application_table.c.position_id.in_(positions),
            )
            .union(
                select(score_table.c.candidate_id).where(
                    score_table.c.organization_id == context.organization_id,
                    score_table.c.position_id.in_(positions),
                ),
                select(offer_table.c.candidate_id).where(
                    offer_table.c.organization_id == context.organization_id,
                    offer_table.c.position_id.in_(positions),
                ),
            )
        )
        applications = select(application_table.c.id).where(
            application_table.c.organization_id == context.organization_id,
            application_table.c.position_id.in_(positions),
        )
        return positions, candidates, None, applications
    if context.role == "interviewer":
        # Use Core tables here so ORM loader criteria cannot inject the
        # Interview criterion back into its own assignment subquery and recurse
        # during SQL compilation.
        scorecard_table = ScorecardAssignment.__table__
        interview_assignment_table = InterviewAssignment.__table__
        interview_table = Interview.__table__
        scorecard_assignments = select(scorecard_table.c.id).where(
            scorecard_table.c.organization_id == context.organization_id,
            scorecard_table.c.assigned_to_user_id == context.actor_id,
        )
        assigned_interviews = select(interview_assignment_table.c.interview_id).where(
            interview_assignment_table.c.organization_id == context.organization_id,
            interview_assignment_table.c.interviewer_user_id == context.actor_id,
        )
        positions = (
            select(scorecard_table.c.position_id)
            .where(
                scorecard_table.c.id.in_(scorecard_assignments),
                scorecard_table.c.position_id.is_not(None),
            )
            .union(
                select(interview_table.c.position_id).where(
                    interview_table.c.organization_id == context.organization_id,
                    interview_table.c.id.in_(assigned_interviews),
                    interview_table.c.position_id.is_not(None),
                )
            )
        )
        candidates = (
            select(scorecard_table.c.candidate_id)
            .where(scorecard_table.c.id.in_(scorecard_assignments))
            .union(
                select(interview_table.c.candidate_id).where(
                    interview_table.c.organization_id == context.organization_id,
                    interview_table.c.id.in_(assigned_interviews),
                )
            )
        )
        # Persisted child FKs are the authorization boundary. Joining by the
        # candidate/position pair would expose every historical reapplication
        # cycle for that pair to an interviewer assigned to only one cycle.
        scorecard_applications = select(scorecard_table.c.application_id).where(
            scorecard_table.c.organization_id == context.organization_id,
            scorecard_table.c.assigned_to_user_id == context.actor_id,
            scorecard_table.c.application_id.is_not(None),
        )
        interview_applications = select(interview_table.c.application_id).where(
            interview_table.c.organization_id == context.organization_id,
            interview_table.c.id.in_(assigned_interviews),
            interview_table.c.application_id.is_not(None),
        )
        applications = scorecard_applications.union(interview_applications)
        return positions, candidates, assigned_interviews, applications
    return None


def _assignment_loader_options(
    context: TenantContext,
    *,
    positions,
    candidates,
    assigned_interviews,
    applications,
):
    """Build loader criteria exclusively from the reviewed model registry."""

    mapped = {mapper.class_.__name__: mapper.class_ for mapper in Base.registry.mappers}

    def model(name: str):
        try:
            return mapped[name]
        except KeyError as exc:  # pragma: no cover - completeness test is earlier
            raise RuntimeError(
                f"Unknown assignment model registry entry: {name}"
            ) from exc

    def table(name: str):
        return model(name).__table__

    application_table = table("Application")
    interview_table = table("Interview")
    scorecard_table = table("ScorecardAssignment")
    appointment_table = table("InterviewAppointment")
    scheduling_link_table = table("SchedulingLink")
    assessment_attempt_table = table("AssessmentAttempt")
    communication_thread_table = table("CommunicationThread")
    offer_table = table("CandidateOffer")
    decision_table = table("HiringDecision")
    provider_assessment_table = table("ProviderAssessment")
    requisition_table = table("Requisition")
    scorecard_template_table = table("ScorecardTemplate")
    mission_table = table("HiringMission")
    mission_assessment_table = table("MissionCandidateAssessment")
    mission_step_table = table("MissionStep")
    ai_session_table = table("AiSession")
    chat_session_table = table("ChatSession")

    allowed_interview_ids = select(interview_table.c.id).where(
        interview_table.c.application_id.in_(applications)
    )
    allowed_scorecard_ids = select(scorecard_table.c.id).where(
        scorecard_table.c.application_id.in_(applications)
    )
    allowed_appointment_ids = select(appointment_table.c.id).where(
        appointment_table.c.application_id.in_(applications)
    )
    allowed_scheduling_link_ids = select(scheduling_link_table.c.id).where(
        scheduling_link_table.c.application_id.in_(applications)
    )
    if context.role == "interviewer":
        # An Application assignment never expands into every interview or
        # scorecard in that cycle. These two resources retain their explicit
        # assignee boundary, and scheduling children inherit the same parent.
        allowed_interview_ids = allowed_interview_ids.where(
            interview_table.c.id.in_(assigned_interviews)
        )
        allowed_scorecard_ids = allowed_scorecard_ids.where(
            scorecard_table.c.assigned_to_user_id == context.actor_id
        )
        allowed_appointment_ids = allowed_appointment_ids.where(
            appointment_table.c.interview_id.in_(allowed_interview_ids)
        )
        allowed_scheduling_link_ids = allowed_scheduling_link_ids.where(
            scheduling_link_table.c.interview_id.in_(allowed_interview_ids)
        )

    allowed_attempt_ids = select(assessment_attempt_table.c.id).where(
        assessment_attempt_table.c.application_id.in_(applications)
    )
    allowed_thread_ids = select(communication_thread_table.c.id).where(
        communication_thread_table.c.application_id.in_(applications)
    )
    allowed_offer_ids = select(offer_table.c.id).where(
        offer_table.c.application_id.in_(applications)
    )
    allowed_decision_ids = select(decision_table.c.id).where(
        decision_table.c.application_id.in_(applications)
    )
    allowed_provider_assessment_ids = select(provider_assessment_table.c.id).where(
        provider_assessment_table.c.application_id.in_(applications)
    )
    allowed_requisition_ids = select(requisition_table.c.id).where(
        requisition_table.c.position_id.in_(positions)
    )
    if context.role == "hiring_manager":
        allowed_requisition_ids = allowed_requisition_ids.where(
            requisition_table.c.hiring_manager_user_id == context.actor_id
        )
    else:
        allowed_requisition_ids = allowed_requisition_ids.where(false())
    allowed_scorecard_template_ids = select(scorecard_template_table.c.id).where(
        or_(
            scorecard_template_table.c.position_id.is_(None),
            scorecard_template_table.c.position_id.in_(positions),
        )
    )
    allowed_mission_ids = select(mission_table.c.id).where(
        or_(
            mission_table.c.position_id.in_(positions),
            mission_table.c.created_by_user_id == context.actor_id,
        )
    )
    allowed_mission_assessment_ids = (
        select(mission_assessment_table.c.id)
        .join(
            mission_table,
            mission_table.c.id == mission_assessment_table.c.mission_id,
        )
        .where(
            false()
            if context.role == "interviewer"
            else select(application_table.c.id)
            .where(
                application_table.c.id.in_(applications),
                application_table.c.candidate_id
                == mission_assessment_table.c.candidate_id,
                application_table.c.position_id == mission_table.c.position_id,
            )
            .exists()
        )
    )
    allowed_mission_step_ids = select(mission_step_table.c.id).where(
        mission_step_table.c.mission_id.in_(allowed_mission_ids)
    )
    allowed_ai_session_ids = select(ai_session_table.c.id).where(
        ai_session_table.c.created_by_user_id == context.actor_id
    )
    allowed_chat_session_ids = select(chat_session_table.c.id).where(
        chat_session_table.c.application_id.in_(applications)
    )
    allowed_legacy_score_ids = select(application_table.c.legacy_score_id).where(
        application_table.c.id.in_(applications),
        application_table.c.legacy_score_id.is_not(None),
    )

    parent_ids = {
        "ai_session": allowed_ai_session_ids,
        "assessment_attempt": allowed_attempt_ids,
        "chat_session": allowed_chat_session_ids,
        "communication_thread": allowed_thread_ids,
        "hiring_decision": allowed_decision_ids,
        "interview": allowed_interview_ids,
        "interview_appointment": allowed_appointment_ids,
        "mission": allowed_mission_ids,
        "mission_assessment": allowed_mission_assessment_ids,
        "mission_step": allowed_mission_step_ids,
        "offer": allowed_offer_ids,
        "provider_assessment": allowed_provider_assessment_ids,
        "requisition": allowed_requisition_ids,
        "scheduling_link": allowed_scheduling_link_ids,
        "scorecard_assignment": allowed_scorecard_ids,
        "scorecard_template": allowed_scorecard_template_ids,
    }

    options = []
    for model_name, rule in MODEL_ASSIGNMENT_SCOPES.items():
        resource = model(model_name)
        criterion = None

        if rule.scope is AssignmentScope.DENIED_ASSIGNED:
            criterion = false()
        elif rule.scope is AssignmentScope.CANDIDATE_GLOBAL:
            criterion = (
                resource.id.in_(candidates) if model_name == "Candidate" else false()
            )
        elif rule.scope is AssignmentScope.POSITION_SCOPED:
            if context.role == "interviewer" and not rule.interviewer_access:
                criterion = false()
            elif model_name == "Position":
                criterion = resource.id.in_(positions)
            elif model_name == "Requisition":
                criterion = resource.id.in_(allowed_requisition_ids)
            elif model_name == "HiringMission":
                criterion = resource.id.in_(allowed_mission_ids)
            elif model_name == "ScorecardTemplate":
                criterion = resource.id.in_(allowed_scorecard_template_ids)
            else:
                position_column = getattr(resource, rule.position_field)
                criterion = position_column.in_(positions)
        elif rule.scope is AssignmentScope.APPLICATION_SCOPED:
            if context.role == "interviewer" and not rule.interviewer_access:
                criterion = false()
            elif model_name == "Interview":
                criterion = resource.id.in_(allowed_interview_ids)
            elif model_name == "ScorecardAssignment":
                criterion = resource.id.in_(allowed_scorecard_ids)
            elif model_name == "InterviewAppointment":
                criterion = resource.id.in_(allowed_appointment_ids)
            elif model_name == "SchedulingLink":
                criterion = resource.id.in_(allowed_scheduling_link_ids)
            else:
                application_column = getattr(resource, rule.application_field)
                criterion = application_column.in_(applications)
                if rule.position_only_fallback:
                    position_column = getattr(resource, rule.position_field)
                    candidate_column = getattr(resource, rule.candidate_field)
                    criterion = or_(
                        criterion,
                        and_(
                            application_column.is_(None),
                            candidate_column.is_(None),
                            position_column.in_(positions),
                        ),
                    )
                if rule.owner_field is not None:
                    owner_column = getattr(resource, rule.owner_field)
                    null_shape = [application_column.is_(None)]
                    if rule.position_field is not None:
                        null_shape.append(
                            getattr(resource, rule.position_field).is_(None)
                        )
                    if rule.candidate_field is not None:
                        null_shape.append(
                            getattr(resource, rule.candidate_field).is_(None)
                        )
                    criterion = or_(
                        criterion,
                        and_(*null_shape, owner_column == context.actor_id),
                    )
        elif rule.scope is AssignmentScope.EXPLICIT_PARENT_SCOPED:
            if context.role == "interviewer" and not rule.interviewer_access:
                criterion = false()
            elif rule.parent_scope == "legacy_application":
                criterion = resource.id.in_(allowed_legacy_score_ids)
            elif model_name == "MissionCandidateAssessment":
                criterion = resource.id.in_(allowed_mission_assessment_ids)
            else:
                parent_column = getattr(resource, rule.parent_field)
                criterion = parent_column.in_(parent_ids[rule.parent_scope])
        elif (
            rule.scope is AssignmentScope.ORGANIZATION_GLOBAL
            and rule.owner_field is not None
        ):
            criterion = getattr(resource, rule.owner_field) == context.actor_id

        if criterion is not None:
            options.append(
                with_loader_criteria(
                    resource,
                    criterion,
                    include_aliases=True,
                )
            )
    return options


@event.listens_for(Session, "do_orm_execute")
def _enforce_session_tenant_and_assignment_scope(execute_state) -> None:
    """Defense-in-depth tenant and assignment filters for ORM operations.

    PostgreSQL RLS remains the production boundary.  These criteria make the
    same fail-closed behavior testable before the schema steward enables RLS
    and protect ordinary ORM reads during the staged rollout.
    """

    if execute_state.execution_options.get("skip_tenant_scope", False):
        return
    context = execute_state.session.info.get(_TENANT_CONTEXT_KEY)
    if not isinstance(context, TenantContext):
        return

    if execute_state.is_update or execute_state.is_delete:
        mapper = execute_state.bind_arguments.get("mapper")
        model = mapper.class_ if mapper is not None else None
        if model is not None and hasattr(model, "organization_id"):
            execute_state.statement = execute_state.statement.where(
                model.organization_id == context.organization_id
            )
        return
    if not execute_state.is_select:
        return

    options = []
    for mapper in tuple(Base.registry.mappers):
        model = mapper.class_
        if hasattr(model, "organization_id"):
            options.append(
                with_loader_criteria(
                    model,
                    model.organization_id == context.organization_id,
                    include_aliases=True,
                )
            )

    assignment = _assignment_subqueries(context)
    if assignment is not None:
        positions, candidates, assigned_interviews, applications = assignment
        options.extend(
            _assignment_loader_options(
                context,
                positions=positions,
                candidates=candidates,
                assigned_interviews=assigned_interviews,
                applications=applications,
            )
        )
    if options:
        execute_state.statement = execute_state.statement.options(*options)


@event.listens_for(Session, "do_orm_execute")
def _hide_soft_deleted_rows(execute_state) -> None:
    """Hide deleted candidates and positions from every ordinary SELECT.

    Administrative trash/retention queries opt in with
    ``execution_options(include_deleted=True)``. Keeping this at the session
    layer prevents a new endpoint from accidentally exposing deleted records.
    """

    if not execute_state.is_select or execute_state.execution_options.get(
        "include_deleted", False
    ):
        return
    # Imported lazily to avoid a db -> models -> db import cycle at startup.
    from app.models.candidate import Candidate
    from app.models.position import Position

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            Candidate,
            lambda model: model.deleted_at.is_(None),
            include_aliases=True,
        ),
        with_loader_criteria(
            Position,
            lambda model: model.deleted_at.is_(None),
            include_aliases=True,
        ),
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def audit_session_scope() -> AsyncIterator[AsyncSession]:
    """Commit one central audit operation on its reserved connection pool.

    FastAPI request dependencies remain alive until their response bodies have
    been sent. Central audit middleware must persist before releasing sensitive
    bodies, so borrowing the route pool here creates a cyclic wait under load.
    Keeping this factory audit-only preserves fail-closed behavior without
    increasing or sharing the application pool.
    """

    async with AuditSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            clear_tenant_context(session)
