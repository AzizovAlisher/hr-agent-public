"""Assignment-scope checks for reviewed hiring resources."""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AssignmentScope, tenant_context
from app.models.ai_hiring import AiActionBundle, AiSession, HiringMission
from app.models.application import Application
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentFile,
    AssessmentInvitation,
    AssessmentTemplate,
)
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_processing import CandidateProcessingJob
from app.models.candidate_saved_view import CandidateSavedView
from app.models.chat_session import ChatSession
from app.models.communication import CommunicationThread
from app.models.interview import Interview
from app.models.interview_recording import InterviewRecording
from app.models.position import Position
from app.models.provider_assessment import ProviderAssessment
from app.models.requisition import (
    CandidateHandoff,
    CandidateOffer,
    OfferTemplate,
    Requisition,
)
from app.models.scheduling import InterviewAppointment, SchedulingLink
from app.models.scorecard import (
    HiringDecision,
    ScorecardAssignment,
    ScorecardTemplate,
)
from app.models.shortlist import ShortlistLink
from app.models.sourcing import ReferralLink
from app.models.talent import TalentNurtureSequence, TalentPool
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.models.workflow import WorkflowRule, WorkflowRun


class RouteValueSource(StrEnum):
    PATH = "path"
    QUERY = "query"
    BODY = "body"
    BODY_LIST = "body_list"
    BODY_PATH = "body_path"


@dataclass(frozen=True, slots=True)
class RouteResourceBinding:
    source: RouteValueSource
    name: str
    resource: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class RouteAssignmentRule:
    scope: AssignmentScope
    bindings: tuple[RouteResourceBinding, ...] = ()
    denied_roles: frozenset[str] = frozenset()
    denied_body_actions: frozenset[str] = frozenset()


def _route_binding(
    source: RouteValueSource,
    name: str,
    resource: str,
    *,
    required: bool = False,
) -> RouteResourceBinding:
    return RouteResourceBinding(source, name, resource, required)


_ASSIGNMENT_ROUTE_GROUPS = MappingProxyType(
    {
        AssignmentScope.APPLICATION_SCOPED: frozenset(
            {
                ("DELETE", "/applications/{application_id}"),
                ("GET", "/applications"),
                ("GET", "/applications/{application_id}"),
                ("GET", "/applications/{application_id}/assessment-invitations"),
                ("GET", "/assessment-attempts/{attempt_id}"),
                ("GET", "/assessment-files/{file_id}/download"),
                ("GET", "/assessment-invitations/{invitation_id}/attempts"),
                (
                    "GET",
                    "/integrations/assessment/applications/{application_id}/evidence",
                ),
                ("PATCH", "/applications/{application_id}"),
                ("POST", "/applications"),
                ("POST", "/applications/{application_id}/assessment-invitations"),
                ("POST", "/applications/{application_id}/stage"),
                ("POST", "/assessment-attempts/{attempt_id}/manual-grade"),
                ("POST", "/assessment-invitations/{invitation_id}/reissue"),
                ("POST", "/assessment-invitations/{invitation_id}/retakes"),
                ("POST", "/assessment-invitations/{invitation_id}/revoke"),
            }
        ),
        AssignmentScope.POSITION_SCOPED: frozenset(
            {
                ("DELETE", "/positions/{position_id}"),
                ("GET", "/positions"),
                ("GET", "/positions/{position_id}"),
                ("GET", "/positions/{position_id}/applications"),
                ("GET", "/positions/{position_id}/referral-links"),
                ("GET", "/positions/{position_id}/rescore-capability"),
                ("GET", "/positions/{position_id}/shortlists"),
                ("GET", "/positions/{position_id}/talent-matches"),
                ("GET", "/requisitions"),
                ("GET", "/scorecard-templates"),
                ("PATCH", "/positions/move"),
                ("PATCH", "/positions/{position_id}"),
                (
                    "PATCH",
                    "/positions/{position_id}/candidates/{candidate_id}/stage",
                ),
                (
                    "PATCH",
                    "/positions/{position_id}/candidates/{candidate_id}/work",
                ),
                ("PATCH", "/requisitions/{requisition_id}"),
                ("PATCH", "/scorecard-templates/{template_id}"),
                ("POST", "/positions"),
                ("POST", "/positions/draft"),
                ("POST", "/positions/draft/extract-text"),
                ("POST", "/positions/project"),
                ("POST", "/positions/project/extract"),
                ("POST", "/positions/{position_id}/generate-profile"),
                ("POST", "/positions/{position_id}/referral-links"),
                ("POST", "/positions/{position_id}/rescore"),
                ("POST", "/positions/{position_id}/shortlists"),
                ("POST", "/referral-links/{link_id}/revoke"),
                ("POST", "/referral-links/{link_id}/rotate"),
                ("POST", "/requisitions"),
                ("POST", "/requisitions/{requisition_id}/cancel"),
                ("POST", "/requisitions/{requisition_id}/open"),
                ("POST", "/requisitions/{requisition_id}/submit"),
                ("POST", "/requisitions/{requisition_id}/vote"),
                ("POST", "/scorecard-templates"),
                ("POST", "/shortlists/{shortlist_id}/revoke"),
                ("POST", "/shortlists/{shortlist_id}/rotate"),
            }
        ),
        AssignmentScope.CANDIDATE_GLOBAL: frozenset(
            {
                ("DELETE", "/candidate-views/{view_id}"),
                ("DELETE", "/candidates/{candidate_id}"),
                ("DELETE", "/candidates/{candidate_id}/github"),
                ("DELETE", "/candidates/{candidate_id}/notes/{note_id}"),
                ("DELETE", "/candidates/{candidate_id}/tags/{tag_id}"),
                ("DELETE", "/talent/pools/{pool_id}"),
                ("DELETE", "/talent/pools/{pool_id}/members/{candidate_id}"),
                ("GET", "/candidate-duplicates"),
                ("GET", "/candidate-search"),
                ("GET", "/candidate-views"),
                ("GET", "/candidates"),
                ("GET", "/candidates/{candidate_id}"),
                ("GET", "/candidates/{candidate_id}/activities"),
                ("GET", "/candidates/{candidate_id}/applications"),
                ("GET", "/candidates/{candidate_id}/appointments"),
                ("GET", "/candidates/{candidate_id}/best-positions"),
                ("GET", "/candidates/{candidate_id}/communication-threads"),
                ("GET", "/candidates/{candidate_id}/contact-points"),
                ("GET", "/candidates/{candidate_id}/cv"),
                ("GET", "/candidates/{candidate_id}/github/detect"),
                ("GET", "/candidates/{candidate_id}/handoffs"),
                ("GET", "/candidates/{candidate_id}/interviews"),
                ("GET", "/candidates/{candidate_id}/merged-sources"),
                (
                    "GET",
                    "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv",
                ),
                (
                    "GET",
                    "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/evidence",
                ),
                ("GET", "/candidates/{candidate_id}/notes"),
                ("GET", "/candidates/{candidate_id}/offers"),
                ("GET", "/candidates/{candidate_id}/processing-jobs/{job_id}"),
                ("GET", "/candidates/{candidate_id}/recordings"),
                ("GET", "/candidates/{candidate_id}/scheduling-links"),
                ("GET", "/candidates/{candidate_id}/scorecards"),
                ("GET", "/candidates/{candidate_id}/tags"),
                ("GET", "/candidates/{candidate_id}/tasks"),
                ("GET", "/talent/nurture-sequences"),
                ("GET", "/talent/pools"),
                ("GET", "/talent/pools/{pool_id}/members"),
                ("GET", "/talent/search"),
                ("PATCH", "/candidate-views/{view_id}"),
                ("PATCH", "/candidates/{candidate_id}"),
                (
                    "PATCH",
                    "/candidates/{candidate_id}/contact-points/{contact_point_id}",
                ),
                ("PATCH", "/talent/pools/{pool_id}"),
                ("POST", "/candidate-bulk"),
                ("POST", "/candidate-export"),
                ("POST", "/candidate-merges/preview"),
                ("POST", "/candidate-views"),
                ("POST", "/candidates/upload/file"),
                ("POST", "/candidates/upload/hh"),
                ("POST", "/candidates/upload/text"),
                ("POST", "/candidates/{candidate_id}/communications/preview"),
                ("POST", "/candidates/{candidate_id}/communications/send"),
                ("POST", "/candidates/{candidate_id}/contact-points"),
                ("POST", "/candidates/{candidate_id}/decisions"),
                ("POST", "/candidates/{candidate_id}/interviews"),
                ("POST", "/candidates/{candidate_id}/interviews/generate"),
                ("POST", "/candidates/{candidate_id}/notes"),
                ("POST", "/candidates/{candidate_id}/offers"),
                ("POST", "/candidates/{candidate_id}/recordings"),
                ("POST", "/candidates/{candidate_id}/scheduling-links"),
                ("POST", "/candidates/{candidate_id}/score"),
                ("POST", "/candidates/{candidate_id}/score-all-open"),
                ("POST", "/candidates/{candidate_id}/scorecards/assign"),
                ("POST", "/candidates/{candidate_id}/tags"),
                ("POST", "/candidates/{candidate_id}/tasks"),
                ("POST", "/candidates/{candidate_id}/view"),
                ("POST", "/candidates/{primary_candidate_id}/merge"),
                ("POST", "/talent/nurture-sequences"),
                ("POST", "/talent/nurture-sequences/{sequence_id}/launch"),
                ("POST", "/talent/pools"),
                ("POST", "/talent/pools/{pool_id}/members"),
                ("PUT", "/candidates/{candidate_id}/github"),
            }
        ),
        AssignmentScope.EXPLICIT_PARENT_SCOPED: frozenset(
            {
                (
                    "DELETE",
                    "/ai/v1/sessions/{session_id}/attachments/{attachment_id}",
                ),
                ("DELETE", "/calendar/appointments/{appointment_id}/manage-token"),
                ("DELETE", "/interviews/{interview_id}"),
                ("DELETE", "/recordings/{recording_id}"),
                ("DELETE", "/recordings/{recording_id}/file"),
                ("DELETE", "/scheduling-links/{link_id}"),
                ("DELETE", "/tasks/{task_id}"),
                ("DELETE", "/tasks/{task_id}/file"),
                ("GET", "/ai/v1/action-bundles"),
                ("GET", "/ai/v1/missions"),
                ("GET", "/ai/v1/missions/{mission_id}"),
                ("GET", "/ai/v1/missions/{mission_id}/candidates"),
                ("GET", "/ai/v1/missions/{mission_id}/events"),
                ("GET", "/ai/v1/sessions"),
                ("GET", "/ai/v1/sessions/{session_id}"),
                (
                    "GET",
                    "/ai/v1/sessions/{session_id}/attachments/{attachment_id}",
                ),
                ("GET", "/chat/capability"),
                ("GET", "/chat/sessions"),
                ("GET", "/chat/sessions/{session_id}"),
                ("GET", "/communications/templates"),
                ("GET", "/communications/threads"),
                ("GET", "/communications/threads/{thread_id}/messages"),
                ("GET", "/interviews/{interview_id}"),
                ("GET", "/recordings/{recording_id}"),
                ("GET", "/recordings/{recording_id}/file"),
                ("GET", "/tasks/{task_id}"),
                ("GET", "/tasks/{task_id}/file"),
                ("PATCH", "/handoffs/{handoff_id}"),
                ("PATCH", "/interviews/{interview_id}"),
                ("PATCH", "/interviews/{interview_id}/questions/{question_id}"),
                ("PATCH", "/offers/{offer_id}"),
                ("PATCH", "/recordings/{recording_id}"),
                ("PATCH", "/scorecard-assignments/{assignment_id}"),
                ("PATCH", "/tasks/{task_id}"),
                ("POST", "/ai/v1/action-bundles/{bundle_id}/approve"),
                ("POST", "/ai/v1/action-bundles/{bundle_id}/reject"),
                ("POST", "/ai/v1/missions/{mission_id}/replan"),
                ("POST", "/ai/v1/missions/{mission_id}/retry"),
                ("POST", "/ai/v1/missions/{mission_id}/stop"),
                ("POST", "/ai/v1/sessions"),
                ("POST", "/ai/v1/sessions/{session_id}/archive"),
                ("POST", "/ai/v1/sessions/{session_id}/attachments"),
                ("POST", "/ai/v1/sessions/{session_id}/messages"),
                (
                    "POST",
                    "/calendar/appointments/{appointment_id}/manage-token/rotate",
                ),
                ("POST", "/chat/sessions"),
                ("POST", "/chat/sessions/{session_id}/messages"),
                ("POST", "/chat/sessions/{session_id}/messages/stream"),
                ("POST", "/communications/bulk"),
                ("POST", "/communications/bulk/preview"),
                ("POST", "/hiring-decisions/{decision_id}/vote"),
                (
                    "POST",
                    "/integrations/{connection_id}/assessment/attempts/result",
                ),
                (
                    "POST",
                    "/integrations/{connection_id}/assessment/attempts/status",
                ),
                (
                    "POST",
                    "/integrations/{connection_id}/assessment/invitations",
                ),
                ("POST", "/interviews/{interview_id}/complete"),
                ("POST", "/interviews/{interview_id}/kit-versions"),
                ("POST", "/offers/{offer_id}/rotate"),
                ("POST", "/offers/{offer_id}/send"),
                ("POST", "/offers/{offer_id}/submit"),
                ("POST", "/offers/{offer_id}/vote"),
                ("POST", "/offers/{offer_id}/withdraw"),
                ("POST", "/recordings/{recording_id}/file"),
                ("POST", "/scheduling-links/{link_id}/rotate"),
                ("POST", "/scorecard-assignments/{assignment_id}/submit"),
                ("POST", "/tasks/{task_id}/evaluate"),
                ("POST", "/tasks/{task_id}/file"),
                ("GET", "/workflows/rules"),
                ("GET", "/workflows/rules/{rule_id}"),
                ("GET", "/workflows/runs"),
                ("PATCH", "/workflows/rules/{rule_id}"),
                ("DELETE", "/workflows/rules/{rule_id}"),
                ("POST", "/workflows/rules"),
                ("POST", "/workflows/rules/{rule_id}/dry-run"),
                ("POST", "/workflows/rules/{rule_id}/pause"),
                ("POST", "/workflows/rules/{rule_id}/resume"),
                ("POST", "/workflows/process"),
                ("POST", "/workflows/runs/{run_id}/cancel"),
                ("POST", "/workflows/runs/{run_id}/replay"),
            }
        ),
        AssignmentScope.ORGANIZATION_GLOBAL: frozenset(
            {
                ("GET", "/offer-templates"),
                ("PATCH", "/offer-templates/{template_id}"),
                ("POST", "/offer-templates"),
                ("POST", "/assessment-templates"),
                ("PATCH", "/assessment-templates/{template_id}"),
                ("POST", "/assessment-templates/{template_id}/versions"),
            }
        ),
    }
)


# Exact resource ownership for every assignment-scoped route that accepts an
# assignment-sensitive path/query identifier. Child identifiers such as a
# question or attachment are deliberately omitted only when the registered
# parent in the same path is the authoritative boundary.
_ROUTE_RESOURCE_PARAMETERS = MappingProxyType(
    {
        "/ai/v1/action-bundles": (("mission_id", "mission"),),
        "/ai/v1/action-bundles/{bundle_id}/approve": (("bundle_id", "bundle"),),
        "/ai/v1/action-bundles/{bundle_id}/reject": (("bundle_id", "bundle"),),
        "/ai/v1/missions/{mission_id}": (("mission_id", "mission"),),
        "/ai/v1/missions/{mission_id}/candidates": (("mission_id", "mission"),),
        "/ai/v1/missions/{mission_id}/events": (("mission_id", "mission"),),
        "/ai/v1/missions/{mission_id}/replan": (("mission_id", "mission"),),
        "/ai/v1/missions/{mission_id}/retry": (("mission_id", "mission"),),
        "/ai/v1/missions/{mission_id}/stop": (("mission_id", "mission"),),
        "/ai/v1/sessions/{session_id}": (("session_id", "ai_session"),),
        "/ai/v1/sessions/{session_id}/archive": (("session_id", "ai_session"),),
        "/ai/v1/sessions/{session_id}/attachments": (("session_id", "ai_session"),),
        "/ai/v1/sessions/{session_id}/attachments/{attachment_id}": (
            ("session_id", "ai_session"),
        ),
        "/ai/v1/sessions/{session_id}/messages": (("session_id", "ai_session"),),
        "/applications": (
            ("candidate_id", "candidate"),
            ("position_id", "position"),
        ),
        "/applications/{application_id}": (("application_id", "application"),),
        "/applications/{application_id}/assessment-invitations": (
            ("application_id", "application"),
        ),
        "/applications/{application_id}/stage": (("application_id", "application"),),
        "/assessment-attempts/{attempt_id}": (("attempt_id", "attempt"),),
        "/assessment-attempts/{attempt_id}/manual-grade": (("attempt_id", "attempt"),),
        "/assessment-files/{file_id}/download": (("file_id", "assessment_file"),),
        "/assessment-invitations/{invitation_id}/attempts": (
            ("invitation_id", "invitation"),
        ),
        "/assessment-invitations/{invitation_id}/reissue": (
            ("invitation_id", "invitation"),
        ),
        "/assessment-invitations/{invitation_id}/retakes": (
            ("invitation_id", "invitation"),
        ),
        "/assessment-invitations/{invitation_id}/revoke": (
            ("invitation_id", "invitation"),
        ),
        "/assessment-templates/{template_id}": (
            ("template_id", "assessment_template"),
        ),
        "/assessment-templates/{template_id}/versions": (
            ("template_id", "assessment_template"),
        ),
        "/calendar/appointments/{appointment_id}/manage-token": (
            ("appointment_id", "appointment"),
        ),
        "/calendar/appointments/{appointment_id}/manage-token/rotate": (
            ("appointment_id", "appointment"),
        ),
        "/candidate-search": (("position_id", "position"),),
        "/candidate-views/{view_id}": (("view_id", "candidate_view"),),
        "/candidates/{candidate_id}": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/activities": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/applications": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/appointments": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/best-positions": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/communication-threads": (
            ("candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/communications/preview": (
            ("candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/communications/send": (
            ("candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/contact-points": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/contact-points/{contact_point_id}": (
            ("candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/cv": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/decisions": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/github": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/github/detect": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/handoffs": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/interviews": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/interviews/generate": (
            ("candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/merged-sources": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/cv": (
            ("candidate_id", "candidate"),
            ("source_candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/merged-sources/{source_candidate_id}/evidence": (
            ("candidate_id", "candidate"),
            ("source_candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/notes": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/notes/{note_id}": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/offers": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/processing-jobs/{job_id}": (
            ("candidate_id", "candidate"),
            ("job_id", "processing_job"),
        ),
        "/candidates/{candidate_id}/recordings": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/scheduling-links": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/score": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/score-all-open": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/scorecards": (
            ("candidate_id", "candidate"),
            ("position_id", "position"),
            ("application_id", "application"),
        ),
        "/candidates/{candidate_id}/scorecards/assign": (
            ("candidate_id", "candidate"),
        ),
        "/candidates/{candidate_id}/tags": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/tags/{tag_id}": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/tasks": (("candidate_id", "candidate"),),
        "/candidates/{candidate_id}/view": (("candidate_id", "candidate"),),
        "/candidates/{primary_candidate_id}/merge": (
            ("primary_candidate_id", "candidate"),
        ),
        "/chat/capability": (
            ("candidate_id", "candidate"),
            ("position_id", "position"),
        ),
        "/chat/sessions": (
            ("candidate_id", "candidate"),
            ("position_id", "position"),
        ),
        "/chat/sessions/{session_id}": (("session_id", "chat_session"),),
        "/chat/sessions/{session_id}/messages": (("session_id", "chat_session"),),
        "/chat/sessions/{session_id}/messages/stream": (
            ("session_id", "chat_session"),
        ),
        "/communications/threads/{thread_id}/messages": (
            ("thread_id", "communication_thread"),
        ),
        "/handoffs/{handoff_id}": (("handoff_id", "handoff"),),
        "/hiring-decisions/{decision_id}/vote": (("decision_id", "decision"),),
        "/integrations/assessment/applications/{application_id}/evidence": (
            ("application_id", "application"),
        ),
        "/interviews/{interview_id}": (("interview_id", "interview"),),
        "/interviews/{interview_id}/complete": (("interview_id", "interview"),),
        "/interviews/{interview_id}/kit-versions": (("interview_id", "interview"),),
        "/interviews/{interview_id}/questions/{question_id}": (
            ("interview_id", "interview"),
        ),
        "/offer-templates/{template_id}": (("template_id", "offer_template"),),
        "/offers/{offer_id}": (("offer_id", "offer"),),
        "/offers/{offer_id}/rotate": (("offer_id", "offer"),),
        "/offers/{offer_id}/send": (("offer_id", "offer"),),
        "/offers/{offer_id}/submit": (("offer_id", "offer"),),
        "/offers/{offer_id}/vote": (("offer_id", "offer"),),
        "/offers/{offer_id}/withdraw": (("offer_id", "offer"),),
        "/positions/{position_id}": (("position_id", "position"),),
        "/positions/{position_id}/applications": (
            ("position_id", "position"),
            ("candidate_id", "candidate"),
        ),
        "/positions/{position_id}/candidates/{candidate_id}/stage": (
            ("position_id", "position"),
            ("candidate_id", "candidate"),
        ),
        "/positions/{position_id}/candidates/{candidate_id}/work": (
            ("position_id", "position"),
            ("candidate_id", "candidate"),
        ),
        "/positions/{position_id}/generate-profile": (("position_id", "position"),),
        "/positions/{position_id}/referral-links": (("position_id", "position"),),
        "/positions/{position_id}/rescore-capability": (
            ("position_id", "position"),
        ),
        "/positions/{position_id}/rescore": (("position_id", "position"),),
        "/positions/{position_id}/shortlists": (("position_id", "position"),),
        "/positions/{position_id}/talent-matches": (("position_id", "position"),),
        "/recordings/{recording_id}": (("recording_id", "recording"),),
        "/recordings/{recording_id}/file": (("recording_id", "recording"),),
        "/referral-links/{link_id}/revoke": (("link_id", "referral_link"),),
        "/referral-links/{link_id}/rotate": (("link_id", "referral_link"),),
        "/requisitions/{requisition_id}": (("requisition_id", "requisition"),),
        "/requisitions/{requisition_id}/cancel": (("requisition_id", "requisition"),),
        "/requisitions/{requisition_id}/open": (("requisition_id", "requisition"),),
        "/requisitions/{requisition_id}/submit": (("requisition_id", "requisition"),),
        "/requisitions/{requisition_id}/vote": (("requisition_id", "requisition"),),
        "/scheduling-links/{link_id}": (("link_id", "scheduling_link"),),
        "/scheduling-links/{link_id}/rotate": (("link_id", "scheduling_link"),),
        "/scorecard-assignments/{assignment_id}": (
            ("assignment_id", "scorecard_assignment"),
        ),
        "/scorecard-assignments/{assignment_id}/submit": (
            ("assignment_id", "scorecard_assignment"),
        ),
        "/scorecard-templates": (("position_id", "position"),),
        "/scorecard-templates/{template_id}": (("template_id", "scorecard_template"),),
        "/shortlists/{shortlist_id}/revoke": (("shortlist_id", "shortlist"),),
        "/shortlists/{shortlist_id}/rotate": (("shortlist_id", "shortlist"),),
        "/talent/nurture-sequences/{sequence_id}/launch": (
            ("sequence_id", "nurture_sequence"),
        ),
        "/talent/pools/{pool_id}": (("pool_id", "talent_pool"),),
        "/talent/pools/{pool_id}/members": (("pool_id", "talent_pool"),),
        "/talent/pools/{pool_id}/members/{candidate_id}": (
            ("pool_id", "talent_pool"),
            ("candidate_id", "candidate"),
        ),
        "/talent/search": (("pool_id", "talent_pool"),),
        "/tasks/{task_id}": (("task_id", "task"),),
        "/tasks/{task_id}/evaluate": (("task_id", "task"),),
        "/tasks/{task_id}/file": (("task_id", "task"),),
        "/workflows/process": (("rule_id", "workflow_rule"),),
        "/workflows/rules/{rule_id}": (("rule_id", "workflow_rule"),),
        "/workflows/rules/{rule_id}/dry-run": (("rule_id", "workflow_rule"),),
        "/workflows/rules/{rule_id}/pause": (("rule_id", "workflow_rule"),),
        "/workflows/rules/{rule_id}/resume": (("rule_id", "workflow_rule"),),
        "/workflows/runs": (("rule_id", "workflow_rule"),),
        "/workflows/runs/{run_id}/cancel": (("run_id", "workflow_run"),),
        "/workflows/runs/{run_id}/replay": (("run_id", "workflow_run"),),
    }
)


_ROUTE_BODY_PARAMETERS = MappingProxyType(
    {
        ("POST", "/applications"): (
            _route_binding(RouteValueSource.BODY, "candidate_id", "candidate"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidate-bulk"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
        ),
        ("POST", "/candidate-export"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidate-merges/preview"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
        ),
        ("POST", "/candidates/{primary_candidate_id}/merge"): (
            _route_binding(RouteValueSource.BODY_LIST, "duplicate_ids", "candidate"),
        ),
        ("PATCH", "/positions/move"): (
            _route_binding(
                RouteValueSource.BODY_LIST,
                "position_ids",
                "position",
                required=True,
            ),
        ),
        ("POST", "/chat/sessions"): (
            _route_binding(RouteValueSource.BODY, "candidate_id", "candidate"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
        ),
        ("POST", "/candidates/{candidate_id}/communications/preview"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/communications/send"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/decisions"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/interviews"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/interviews/generate"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/offers"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/recordings"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/scheduling-links"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
            _route_binding(RouteValueSource.BODY, "interview_id", "interview"),
        ),
        ("POST", "/candidates/{candidate_id}/score"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/candidates/{candidate_id}/scorecards/assign"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
            _route_binding(RouteValueSource.BODY, "template_id", "scorecard_template"),
        ),
        ("POST", "/candidates/{candidate_id}/tasks"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("PATCH", "/recordings/{recording_id}"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("PATCH", "/tasks/{task_id}"): (
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/communications/bulk"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/communications/bulk/preview"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/integrations/{connection_id}/assessment/invitations"): (
            _route_binding(
                RouteValueSource.BODY,
                "application_id",
                "application",
                required=True,
            ),
        ),
        ("POST", "/integrations/{connection_id}/assessment/attempts/result"): (
            _route_binding(
                RouteValueSource.BODY,
                "provider_assessment_id",
                "provider_assessment",
                required=True,
            ),
        ),
        ("POST", "/integrations/{connection_id}/assessment/attempts/status"): (
            _route_binding(
                RouteValueSource.BODY,
                "provider_assessment_id",
                "provider_assessment",
                required=True,
            ),
        ),
        ("POST", "/scorecard-templates"): (
            _route_binding(
                RouteValueSource.BODY, "position_id", "position", required=True
            ),
        ),
        ("POST", "/talent/nurture-sequences/{sequence_id}/launch"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
        ),
        ("POST", "/talent/nurture-sequences"): (
            _route_binding(RouteValueSource.BODY, "pool_id", "talent_pool"),
        ),
        ("POST", "/talent/pools/{pool_id}/members"): (
            _route_binding(RouteValueSource.BODY_LIST, "candidate_ids", "candidate"),
        ),
        ("POST", "/workflows/rules"): (
            _route_binding(
                RouteValueSource.BODY_PATH,
                "conditions.position_id",
                "position",
                required=True,
            ),
        ),
        ("PATCH", "/workflows/rules/{rule_id}"): (
            _route_binding(
                RouteValueSource.BODY_PATH,
                "conditions.position_id",
                "position",
            ),
        ),
        ("POST", "/workflows/rules/{rule_id}/dry-run"): (
            _route_binding(RouteValueSource.BODY, "candidate_id", "candidate"),
            _route_binding(RouteValueSource.BODY, "position_id", "position"),
            _route_binding(RouteValueSource.BODY, "application_id", "application"),
            _route_binding(
                RouteValueSource.BODY,
                "source_activity_id",
                "candidate_activity",
            ),
        ),
        (
            "PATCH",
            "/positions/{position_id}/candidates/{candidate_id}/stage",
        ): (
            _route_binding(
                RouteValueSource.BODY,
                "application_id",
                "application",
            ),
        ),
        (
            "PATCH",
            "/positions/{position_id}/candidates/{candidate_id}/work",
        ): (
            _route_binding(
                RouteValueSource.BODY,
                "application_id",
                "application",
            ),
        ),
    }
)


_DENIED_ASSIGNED_ROUTES = frozenset(
    {
        ("POST", "/candidates/upload/file"),
        ("POST", "/candidates/upload/hh"),
        ("POST", "/candidates/upload/text"),
        ("POST", "/candidates/{candidate_id}/notes"),
        ("DELETE", "/candidates/{candidate_id}/notes/{note_id}"),
        ("POST", "/candidates/{candidate_id}/tags"),
        ("DELETE", "/candidates/{candidate_id}/tags/{tag_id}"),
        ("POST", "/candidates/{candidate_id}/contact-points"),
        ("PATCH", "/candidates/{candidate_id}/contact-points/{contact_point_id}"),
        ("PUT", "/candidates/{candidate_id}/github"),
        ("DELETE", "/candidates/{candidate_id}/github"),
        ("POST", "/candidates/{candidate_id}/view"),
        ("POST", "/candidates/{primary_candidate_id}/merge"),
        ("POST", "/candidate-merges/preview"),
        ("POST", "/talent/pools"),
        ("PATCH", "/talent/pools/{pool_id}"),
        ("DELETE", "/talent/pools/{pool_id}"),
        ("POST", "/talent/pools/{pool_id}/members"),
        ("DELETE", "/talent/pools/{pool_id}/members/{candidate_id}"),
        ("POST", "/talent/nurture-sequences"),
        ("POST", "/talent/nurture-sequences/{sequence_id}/launch"),
    }
)

_DENIED_INTERVIEWER_ROUTES = frozenset(
    {
        ("POST", "/applications"),
        ("POST", "/chat/sessions"),
        ("POST", "/communications/bulk"),
        ("POST", "/communications/bulk/preview"),
        ("POST", "/candidates/{candidate_id}/communications/preview"),
        ("POST", "/candidates/{candidate_id}/communications/send"),
        ("POST", "/candidates/{candidate_id}/recordings"),
        ("POST", "/candidates/{candidate_id}/tasks"),
        ("POST", "/candidates/{candidate_id}/score"),
        ("POST", "/candidates/{candidate_id}/score-all-open"),
        ("GET", "/workflows/rules"),
        ("GET", "/workflows/rules/{rule_id}"),
        ("GET", "/workflows/runs"),
        ("POST", "/workflows/rules"),
        ("PATCH", "/workflows/rules/{rule_id}"),
        ("DELETE", "/workflows/rules/{rule_id}"),
        ("POST", "/workflows/rules/{rule_id}/dry-run"),
        ("POST", "/workflows/rules/{rule_id}/pause"),
        ("POST", "/workflows/rules/{rule_id}/resume"),
        ("POST", "/workflows/process"),
        ("POST", "/workflows/runs/{run_id}/cancel"),
        ("POST", "/workflows/runs/{run_id}/replay"),
    }
)


def _build_assignment_route_registry():
    registry: dict[tuple[str, str], RouteAssignmentRule] = {}
    for assignment_scope, routes in _ASSIGNMENT_ROUTE_GROUPS.items():
        for key in routes:
            if key in registry:
                raise RuntimeError(f"Duplicate assignment route registry entry: {key}")
            _, path = key
            bindings = []
            for name, resource in _ROUTE_RESOURCE_PARAMETERS.get(path, ()):
                source = (
                    RouteValueSource.PATH
                    if "{" + name + "}" in path
                    else RouteValueSource.QUERY
                )
                bindings.append(
                    _route_binding(
                        source,
                        name,
                        resource,
                        required=source is RouteValueSource.PATH,
                    )
                )
            bindings.extend(_ROUTE_BODY_PARAMETERS.get(key, ()))
            denied_roles = set()
            if key in _DENIED_ASSIGNED_ROUTES:
                denied_roles.update({"hiring_manager", "interviewer"})
            if key in _DENIED_INTERVIEWER_ROUTES:
                denied_roles.add("interviewer")
            registry[key] = RouteAssignmentRule(
                scope=assignment_scope,
                bindings=tuple(bindings),
                denied_roles=frozenset(denied_roles),
                denied_body_actions=(
                    frozenset({"tag_add", "tag_remove"})
                    if key == ("POST", "/candidate-bulk")
                    else frozenset()
                ),
            )
    return MappingProxyType(registry)


ROUTE_ASSIGNMENT_SCOPES = _build_assignment_route_registry()


def _uuid(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except (TypeError, ValueError):
        return None


async def _model_exists(db: AsyncSession, model, identifier: UUID) -> bool:
    return (
        await db.execute(select(model.id).where(model.id == identifier))
    ).scalar_one_or_none() is not None


async def _candidate_position_pair_exists(
    db: AsyncSession,
    candidate_id: UUID,
    position_id: UUID,
) -> bool:
    """Check the relationship, not the two independently visible dimensions.

    The session-level Application criterion limits this query to the exact
    application IDs assigned to an interviewer.  This is deliberately not two
    Candidate/Position existence checks: assignments A/X and B/Y must never
    authorize A/Y or B/X.
    """

    return (
        await db.execute(
            select(Application.id).where(
                Application.candidate_id == candidate_id,
                Application.position_id == position_id,
            )
        )
    ).scalar_one_or_none() is not None


async def _application_relationship_exists(
    db: AsyncSession,
    application_id: UUID,
    *,
    candidate_id: UUID | None = None,
    position_id: UUID | None = None,
) -> bool:
    statement = select(Application.id).where(Application.id == application_id)
    if candidate_id is not None:
        statement = statement.where(Application.candidate_id == candidate_id)
    if position_id is not None:
        statement = statement.where(Application.position_id == position_id)
    return (await db.execute(statement)).scalar_one_or_none() is not None


async def _assessment_invitation_exists(db: AsyncSession, identifier: UUID) -> bool:
    return (
        await db.execute(
            select(AssessmentInvitation.id)
            .join(Application, Application.id == AssessmentInvitation.application_id)
            .where(AssessmentInvitation.id == identifier)
        )
    ).scalar_one_or_none() is not None


async def _assessment_attempt_exists(db: AsyncSession, identifier: UUID) -> bool:
    return (
        await db.execute(
            select(AssessmentAttempt.id)
            .join(
                AssessmentInvitation,
                AssessmentInvitation.id == AssessmentAttempt.invitation_id,
            )
            .join(Application, Application.id == AssessmentInvitation.application_id)
            .where(AssessmentAttempt.id == identifier)
        )
    ).scalar_one_or_none() is not None


async def _assessment_file_exists(db: AsyncSession, identifier: UUID) -> bool:
    return (
        await db.execute(
            select(AssessmentFile.id)
            .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentFile.attempt_id)
            .join(
                AssessmentInvitation,
                AssessmentInvitation.id == AssessmentAttempt.invitation_id,
            )
            .join(Application, Application.id == AssessmentInvitation.application_id)
            .where(AssessmentFile.id == identifier)
        )
    ).scalar_one_or_none() is not None


async def _action_bundle_exists(db: AsyncSession, identifier: UUID) -> bool:
    return (
        await db.execute(
            select(AiActionBundle.id)
            .join(HiringMission, HiringMission.id == AiActionBundle.mission_id)
            .join(AiSession, AiSession.id == HiringMission.session_id)
            .where(AiActionBundle.id == identifier)
        )
    ).scalar_one_or_none() is not None


async def _workflow_rule_exists(db: AsyncSession, identifier: UUID) -> bool:
    """Resolve a manager's rule through its reviewed nested position condition."""

    context = tenant_context(db)
    if context is None or context.role != "hiring_manager" or context.actor_id is None:
        return False
    managed_positions = select(cast(Requisition.position_id, String)).where(
        Requisition.organization_id == context.organization_id,
        Requisition.hiring_manager_user_id == context.actor_id,
        Requisition.position_id.is_not(None),
    )
    return (
        await db.execute(
            select(WorkflowRule.id).where(
                WorkflowRule.id == identifier,
                WorkflowRule.organization_id == context.organization_id,
                WorkflowRule.conditions["position_id"].astext.in_(managed_positions),
            )
        )
    ).scalar_one_or_none() is not None


_RESOURCE_MODELS = MappingProxyType(
    {
        "ai_session": AiSession,
        "application": Application,
        "appointment": InterviewAppointment,
        "assessment_template": AssessmentTemplate,
        "candidate": Candidate,
        "candidate_activity": CandidateActivity,
        "candidate_view": CandidateSavedView,
        "chat_session": ChatSession,
        "communication_thread": CommunicationThread,
        "decision": HiringDecision,
        "handoff": CandidateHandoff,
        "interview": Interview,
        "mission": HiringMission,
        "nurture_sequence": TalentNurtureSequence,
        "offer": CandidateOffer,
        "offer_template": OfferTemplate,
        "position": Position,
        "processing_job": CandidateProcessingJob,
        "provider_assessment": ProviderAssessment,
        "recording": InterviewRecording,
        "referral_link": ReferralLink,
        "requisition": Requisition,
        "scheduling_link": SchedulingLink,
        "scorecard_assignment": ScorecardAssignment,
        "scorecard_template": ScorecardTemplate,
        "shortlist": ShortlistLink,
        "talent_pool": TalentPool,
        "task": TaskSubmission,
        "workflow_run": WorkflowRun,
    }
)


_SPECIAL_RESOURCE_CHECKS = MappingProxyType(
    {
        "attempt": _assessment_attempt_exists,
        "assessment_file": _assessment_file_exists,
        "bundle": _action_bundle_exists,
        "invitation": _assessment_invitation_exists,
        "workflow_rule": _workflow_rule_exists,
    }
)


async def _registered_resource_exists(
    db: AsyncSession,
    resource: str,
    identifier: UUID,
) -> bool:
    check = _SPECIAL_RESOURCE_CHECKS.get(resource)
    if check is not None:
        return await check(db, identifier)
    model = _RESOURCE_MODELS.get(resource)
    if model is None:  # pragma: no cover - registry completeness gate catches it
        raise RuntimeError(f"Unknown assignment route resource: {resource}")
    return await _model_exists(db, model, identifier)


async def enforce_hiring_assignment_scope(
    db: AsyncSession,
    user: User,
    request: Request,
) -> None:
    """Fail closed using exact, reviewed route/resource registrations."""

    if user.role not in {"hiring_manager", "interviewer"}:
        return
    path_template = getattr(request.scope.get("route"), "path", request.url.path)
    key = (request.method.upper(), path_template)
    rule = ROUTE_ASSIGNMENT_SCOPES.get(key)
    if rule is None:
        # Only ResourceScope.HIRING_ASSIGNMENT calls this dependency. A route
        # added without completing the explicit registry is unavailable until
        # reviewed, even before the completeness suite reaches CI.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")

    body_values: dict[str, object] = {}
    if (
        request.method in {"POST", "PUT", "PATCH"}
        and request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "application/json"
    ):
        try:
            parsed_body = await request.json()
        except (TypeError, ValueError):
            parsed_body = None
        if isinstance(parsed_body, dict):
            body_values = parsed_body

    if (
        user.role in rule.denied_roles
        or body_values.get("action") in rule.denied_body_actions
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")

    identifiers: list[tuple[str, UUID]] = []
    for binding in rule.bindings:
        if binding.source is RouteValueSource.PATH:
            raw_values = [request.path_params.get(binding.name)]
        elif binding.source is RouteValueSource.QUERY:
            raw_values = request.query_params.getlist(binding.name)
        elif binding.source is RouteValueSource.BODY:
            raw_values = [body_values.get(binding.name)]
        elif binding.source is RouteValueSource.BODY_PATH:
            raw: object = body_values
            for component in binding.name.split("."):
                if not isinstance(raw, dict):
                    raw = None
                    break
                raw = raw.get(component)
            raw_values = [raw]
        else:
            raw = body_values.get(binding.name)
            raw_values = raw if isinstance(raw, list) else []

        valid = []
        for raw in raw_values:
            identifier = _uuid(str(raw) if raw is not None else None)
            if identifier is not None:
                valid.append(identifier)
        if binding.required and not valid:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
        identifiers.extend((binding.resource, identifier) for identifier in valid)

    # Every concrete resource is checked through its exact registered model or
    # reviewed parent join. Session loader criteria make wrong-tenant,
    # unassigned, NULL-parent, and denied interviewer rows indistinguishable.
    for resource, identifier in dict.fromkeys(identifiers):
        if not await _registered_resource_exists(db, resource, identifier):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")

    application_ids = [
        identifier for resource, identifier in identifiers if resource == "application"
    ]
    candidate_ids = [
        identifier for resource, identifier in identifiers if resource == "candidate"
    ]
    position_ids = [
        identifier for resource, identifier in identifiers if resource == "position"
    ]
    if application_ids:
        for application_id in dict.fromkeys(application_ids):
            candidate_values = tuple(dict.fromkeys(candidate_ids)) or (None,)
            position_values = tuple(dict.fromkeys(position_ids)) or (None,)
            for candidate_id in candidate_values:
                for position_id in position_values:
                    if not await _application_relationship_exists(
                        db,
                        application_id,
                        candidate_id=candidate_id,
                        position_id=position_id,
                    ):
                        raise HTTPException(
                            status.HTTP_404_NOT_FOUND, "Resource not found"
                        )
    elif candidate_ids and position_ids:
        for candidate_id in dict.fromkeys(candidate_ids):
            for position_id in dict.fromkeys(position_ids):
                if not await _candidate_position_pair_exists(
                    db,
                    candidate_id,
                    position_id,
                ):
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "Resource not found")
