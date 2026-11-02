"""Stage privacy retention erasure without stranding tenant-private objects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_candidate_source import AiCandidateSourceObject
from app.models.ai_hiring import (
    AiActionBundle,
    AiAgentRun,
    AiMessage,
    AiSession,
    EvidenceClaim,
    HiringMission,
    MissionEvent,
    MissionCandidateAssessment,
    MissionPlanVersion,
    MissionStep,
    MissionStepAttempt,
)
from app.models.application import Application, ApplicationStageEvent
from app.models.assessment import AssessmentFile, AssessmentInvitation
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_note import CandidateTag
from app.models.candidate_saved_view import CandidateSavedView
from app.models.chat_session import ChatSession
from app.models.communication import (
    CommunicationBatch,
    CommunicationMessage,
    CommunicationTemplate,
)
from app.models.enterprise import AuditLog
from app.models.integration import IntegrationConnection, IntegrationJob
from app.models.interview_recording import InterviewRecording
from app.models.message import Message
from app.models.notification import Notification
from app.models.position import Position
from app.models.private_object import LegacyPrivateObjectDeletion, PrivateObject
from app.models.provider_assessment import ProviderAssessment
from app.models.requisition import CandidateOffer, OfferApproval, OfferTemplate
from app.models.scheduling import InterviewAppointment
from app.models.sourcing import SourceCandidateReceipt, SourceImportRun
from app.models.task_submission import TaskSubmission
from app.models.workflow import WorkflowRule, WorkflowRun
from app.schemas.integrations import AssessmentProviderErasureRequest
from app.services.ai_hiring_attachments import (
    AiAttachmentInventoryError,
    extract_unsubmitted_ai_attachment_inventory,
)
from app.services.integrations.crypto import IntegrationCipher, IntegrationCryptoError
from app.services.integrations.jobs import (
    IntegrationJobError,
    cancel_job,
    enqueue_job,
)
from app.services.privacy import (
    CandidatePrivacyContext,
    PrivacyErasureError,
    build_candidate_privacy_context,
    contains_candidate_marker,
    erase_candidate_live_store_copies,
)
from app.storage import (
    BlobStore,
    LegacyPrivateObjectError,
    LocalBlobStore,
    enqueue_legacy_private_object_deletion,
    get_blob_store,
    get_legacy_blob_store,
    require_tenant_private_key,
)
from app.storage.legacy import normalize_legacy_private_key, snapshot_legacy_file
from app.storage.intake import StoredUpload
from app.storage.registry import (
    adopt_legacy_private_object_for_erasure,
    enqueue_private_object_deletion,
)
from app.storage.scanner import ScanResult
from app.storage.validation import canonical_media_type


class RetentionStorageError(RuntimeError):
    """Retention cannot prove complete private-object handling."""


@dataclass(frozen=True, slots=True)
class LegacyRetentionObject:
    reference: str
    category: str


@dataclass(frozen=True, slots=True)
class RetentionErasurePlan:
    candidate_ids: frozenset[UUID]
    position_ids: frozenset[UUID]
    soft_deleted_candidates: int
    active_candidates: int
    soft_deleted_positions: int
    held_candidate_roots: int
    held_positions: int
    registry_deletions: int
    adopted_legacy_objects: int
    missing_legacy_objects: int
    legacy_objects: tuple[LegacyRetentionObject, ...]


@dataclass(frozen=True, slots=True)
class _Dependencies:
    applications: tuple[tuple[UUID, UUID, UUID], ...]
    invitation_applications: tuple[tuple[UUID, UUID], ...]
    assessment_files: tuple[AssessmentFile, ...]
    tasks: tuple[TaskSubmission, ...]
    recordings: tuple[InterviewRecording, ...]
    ai_candidate_sources: tuple[AiCandidateSourceObject, ...]
    candidate_ai_sessions: tuple[tuple[UUID, UUID], ...]


def _closure(root: UUID, children: dict[UUID, set[UUID]]) -> set[UUID]:
    found: set[UUID] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(children.get(current, ()))
    return found


async def _unsubmitted_ai_attachment_text(
    store: BlobStore,
    item: PrivateObject,
) -> str:
    """Materialize and safely extract one bounded unsubmitted AI attachment."""

    try:
        return await extract_unsubmitted_ai_attachment_inventory(
            store,
            object_key=item.object_key,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
        )
    except AiAttachmentInventoryError as exc:
        raise RetentionStorageError(
            "Unsubmitted AI attachment could not be inventoried safely"
        ) from exc


async def _dependencies(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: set[UUID],
    position_ids: set[UUID],
    store: BlobStore,
    attachment_text_cache: dict[UUID, str],
) -> _Dependencies:
    if not candidate_ids and not position_ids:
        return _Dependencies((), (), (), (), (), (), ())
    application_predicates = []
    if candidate_ids:
        application_predicates.append(Application.candidate_id.in_(candidate_ids))
    if position_ids:
        application_predicates.append(Application.position_id.in_(position_ids))
    applications = tuple(
        (
            await db.execute(
                select(
                    Application.id,
                    Application.candidate_id,
                    Application.position_id,
                ).where(
                    Application.organization_id == organization_id,
                    or_(*application_predicates),
                )
            )
        ).tuples()
    )
    application_ids = {item[0] for item in applications}
    invitation_applications: tuple[tuple[UUID, UUID], ...] = ()
    if application_ids:
        invitation_applications = tuple(
            (
                await db.execute(
                    select(
                        AssessmentInvitation.id,
                        AssessmentInvitation.application_id,
                    ).where(
                        AssessmentInvitation.organization_id == organization_id,
                        AssessmentInvitation.application_id.in_(application_ids),
                    )
                )
            ).tuples()
        )
    invitation_ids = {item[0] for item in invitation_applications}
    assessment_files: tuple[AssessmentFile, ...] = ()
    if invitation_ids:
        assessment_files = tuple(
            (
                await db.execute(
                    select(AssessmentFile).where(
                        AssessmentFile.organization_id == organization_id,
                        AssessmentFile.invitation_id.in_(invitation_ids),
                    )
                )
            ).scalars()
        )
    tasks: tuple[TaskSubmission, ...] = ()
    recordings: tuple[InterviewRecording, ...] = ()
    ai_candidate_sources: tuple[AiCandidateSourceObject, ...] = ()
    candidate_ai_sessions: set[tuple[UUID, UUID]] = set()
    artifact_predicates = []
    if candidate_ids:
        artifact_predicates.append(TaskSubmission.candidate_id.in_(candidate_ids))
    if application_ids:
        artifact_predicates.append(TaskSubmission.application_id.in_(application_ids))
    if artifact_predicates:
        tasks = tuple(
            (
                await db.execute(
                    select(TaskSubmission).where(
                        TaskSubmission.organization_id == organization_id,
                        or_(*artifact_predicates),
                    )
                )
            ).scalars()
        )
    recording_predicates = []
    if candidate_ids:
        recording_predicates.append(
            InterviewRecording.candidate_id.in_(candidate_ids)
        )
    if application_ids:
        recording_predicates.append(
            InterviewRecording.application_id.in_(application_ids)
        )
    if recording_predicates:
        recordings = tuple(
            (
                await db.execute(
                    select(InterviewRecording).where(
                        InterviewRecording.organization_id == organization_id,
                        or_(*recording_predicates),
                    )
                )
            ).scalars()
        )
    if candidate_ids:
        ai_candidate_sources = tuple(
            (
                await db.execute(
                    select(AiCandidateSourceObject).where(
                        AiCandidateSourceObject.organization_id == organization_id,
                        AiCandidateSourceObject.candidate_id.in_(candidate_ids),
                    )
                )
            ).scalars()
        )
        if ai_candidate_sources:
            source_object_ids = {
                item.private_object_id for item in ai_candidate_sources
            }
            source_sessions = dict(
                list(
                    (
                        await db.execute(
                            select(PrivateObject.id, PrivateObject.resource_id).where(
                                PrivateObject.organization_id == organization_id,
                                PrivateObject.id.in_(source_object_ids),
                                PrivateObject.resource_type == "ai_attachment",
                            )
                        )
                    ).tuples()
                )
            )
            candidate_ai_sessions.update(
                (item.candidate_id, source_sessions[item.private_object_id])
                for item in ai_candidate_sources
                if item.private_object_id in source_sessions
            )

        mission_sessions = list(
            (
                await db.execute(
                    select(
                        MissionCandidateAssessment.candidate_id,
                        HiringMission.session_id,
                    )
                    .join(
                        HiringMission,
                        HiringMission.id == MissionCandidateAssessment.mission_id,
                    )
                    .where(
                        MissionCandidateAssessment.organization_id == organization_id,
                        MissionCandidateAssessment.candidate_id.in_(candidate_ids),
                    )
                )
            ).tuples()
        )
        evidence_sessions = list(
            (
                await db.execute(
                    select(EvidenceClaim.candidate_id, HiringMission.session_id)
                    .join(HiringMission, HiringMission.id == EvidenceClaim.mission_id)
                    .where(
                        EvidenceClaim.organization_id == organization_id,
                        EvidenceClaim.candidate_id.in_(candidate_ids),
                    )
                )
            ).tuples()
        )
        candidate_ai_sessions.update(mission_sessions)
        candidate_ai_sessions.update(evidence_sessions)

        # One-release compatibility for AI sessions created before explicit
        # source provenance. Structured payloads and generated content use
        # canonical candidate UUIDs, so an exact UUID match can still recover
        # the containing session without fuzzy PII matching.
        candidate_strings = {str(item): item for item in candidate_ids}
        try:
            privacy_context = await build_candidate_privacy_context(
                db,
                organization_id=organization_id,
                candidate_ids=candidate_ids,
            )
        except PrivacyErasureError as exc:
            raise RetentionStorageError(
                "Candidate privacy inventory could not be completed"
            ) from exc

        def associate_candidate_session(session_id: UUID, searchable: object) -> None:
            if not contains_candidate_marker(searchable, privacy_context.markers):
                return
            serialized = json.dumps(searchable, sort_keys=True).casefold()
            matched_ids = {
                candidate_id
                for identifier, candidate_id in candidate_strings.items()
                if identifier in serialized
            }
            # Older/unbound session roots may contain identity evidence but no
            # UUID. Associate the containing session with every root in this
            # erasure batch so messages and registered attachments cannot be
            # stranded outside the explicit provenance graph.
            for candidate_id in matched_ids or candidate_ids:
                candidate_ai_sessions.add((candidate_id, session_id))

        submitted_ai_object_ids = set(
            (
                await db.execute(
                    select(AiCandidateSourceObject.private_object_id).where(
                        AiCandidateSourceObject.organization_id == organization_id
                    )
                )
            ).scalars()
        )
        unsubmitted_statement = (
            select(PrivateObject)
            .where(
                PrivateObject.organization_id == organization_id,
                PrivateObject.resource_type == "ai_attachment",
                PrivateObject.state.in_(("ready", "delete_pending")),
            )
            .order_by(PrivateObject.id)
            .with_for_update()
        )
        if submitted_ai_object_ids:
            unsubmitted_statement = unsubmitted_statement.where(
                PrivateObject.id.not_in(submitted_ai_object_ids)
            )
        unsubmitted_ai_objects = list(
            (await db.execute(unsubmitted_statement)).scalars()
        )
        for private_object in unsubmitted_ai_objects:
            filename_inventory = {
                "original_filename": private_object.original_filename,
                # An address used as a filename (candidate@example.test.txt)
                # would otherwise be parsed as a larger syntactically valid
                # address. Include the final-extension-free stem so the
                # canonical candidate email remains a boundary-equal token.
                "filename_stem": Path(private_object.original_filename).stem,
            }
            if contains_candidate_marker(
                filename_inventory,
                privacy_context.markers,
            ):
                associate_candidate_session(
                    private_object.resource_id,
                    filename_inventory,
                )
                continue
            extracted_text = attachment_text_cache.get(private_object.id)
            if extracted_text is None:
                extracted_text = await _unsubmitted_ai_attachment_text(
                    store,
                    private_object,
                )
                attachment_text_cache[private_object.id] = extracted_text
            associate_candidate_session(
                private_object.resource_id,
                {"attachment_text": extracted_text},
            )

        # A session title and mission root can exist before the first message.
        # Lock both roots so retention sees a stable tenant-scoped snapshot.
        ai_sessions = list(
            (
                await db.execute(
                    select(AiSession)
                    .where(AiSession.organization_id == organization_id)
                    .order_by(AiSession.id)
                    .with_for_update()
                )
            ).scalars()
        )
        for session in ai_sessions:
            associate_candidate_session(session.id, {"title": session.title})

        missions = list(
            (
                await db.execute(
                    select(HiringMission)
                    .where(HiringMission.organization_id == organization_id)
                    .order_by(HiringMission.id)
                    .with_for_update()
                )
            ).scalars()
        )
        for mission in missions:
            associate_candidate_session(
                mission.session_id,
                {
                    "title": mission.title,
                    "original_request": mission.original_request,
                    "brief": mission.brief,
                    "rubric": mission.rubric,
                    "assumptions": mission.assumptions,
                    "blockers": mission.blockers,
                },
            )

        mission_sessions = {mission.id: mission.session_id for mission in missions}

        def associate_mission_root(mission_id: UUID, searchable: object) -> None:
            session_id = mission_sessions.get(mission_id)
            if session_id is not None:
                associate_candidate_session(session_id, searchable)

        mission_ids = set(mission_sessions)
        if mission_ids:
            plan_versions = list(
                (
                    await db.execute(
                        select(MissionPlanVersion)
                        .where(
                            MissionPlanVersion.organization_id == organization_id,
                            MissionPlanVersion.mission_id.in_(mission_ids),
                        )
                        .order_by(MissionPlanVersion.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for plan_version in plan_versions:
                associate_mission_root(
                    plan_version.mission_id,
                    {
                        "brief_snapshot": plan_version.brief_snapshot,
                        "rubric_snapshot": plan_version.rubric_snapshot,
                        "plan": plan_version.plan,
                    },
                )

            steps = list(
                (
                    await db.execute(
                        select(MissionStep)
                        .where(
                            MissionStep.organization_id == organization_id,
                            MissionStep.mission_id.in_(mission_ids),
                        )
                        .order_by(MissionStep.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            step_missions = {step.id: step.mission_id for step in steps}
            for step in steps:
                associate_mission_root(
                    step.mission_id,
                    {"input_data": step.input_data, "safe_result": step.safe_result},
                )

            if step_missions:
                step_attempts = list(
                    (
                        await db.execute(
                            select(MissionStepAttempt)
                            .where(
                                MissionStepAttempt.organization_id == organization_id,
                                MissionStepAttempt.step_id.in_(step_missions),
                            )
                            .order_by(MissionStepAttempt.id)
                            .with_for_update()
                        )
                    ).scalars()
                )
                for attempt in step_attempts:
                    associate_mission_root(
                        step_missions[attempt.step_id],
                        {"safe_result": attempt.safe_result},
                    )

            agent_runs = list(
                (
                    await db.execute(
                        select(AiAgentRun)
                        .where(
                            AiAgentRun.organization_id == organization_id,
                            AiAgentRun.mission_id.in_(mission_ids),
                        )
                        .order_by(AiAgentRun.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for run in agent_runs:
                associate_mission_root(
                    run.mission_id,
                    {
                        "safe_input_metadata": run.safe_input_metadata,
                        "safe_result": run.safe_result,
                    },
                )

            assessments = list(
                (
                    await db.execute(
                        select(MissionCandidateAssessment)
                        .where(
                            MissionCandidateAssessment.organization_id
                            == organization_id,
                            MissionCandidateAssessment.mission_id.in_(mission_ids),
                        )
                        .order_by(MissionCandidateAssessment.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for assessment in assessments:
                associate_mission_root(
                    assessment.mission_id,
                    {
                        "candidate_id": assessment.candidate_id,
                        "criteria": assessment.criteria,
                        "concerns": assessment.concerns,
                        "missing_evidence": assessment.missing_evidence,
                        "concise_rationale": assessment.concise_rationale,
                    },
                )

            evidence_claims = list(
                (
                    await db.execute(
                        select(EvidenceClaim)
                        .where(
                            EvidenceClaim.organization_id == organization_id,
                            EvidenceClaim.mission_id.in_(mission_ids),
                        )
                        .order_by(EvidenceClaim.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for claim in evidence_claims:
                associate_mission_root(
                    claim.mission_id,
                    {
                        "candidate_id": claim.candidate_id,
                        "claim": claim.claim,
                        "source_id": claim.source_id,
                        "source_locator": claim.source_locator,
                    },
                )

            action_bundles = list(
                (
                    await db.execute(
                        select(AiActionBundle)
                        .where(
                            AiActionBundle.organization_id == organization_id,
                            AiActionBundle.mission_id.in_(mission_ids),
                        )
                        .order_by(AiActionBundle.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for bundle in action_bundles:
                associate_mission_root(
                    bundle.mission_id,
                    {
                        "summary": bundle.summary,
                        "preview": bundle.preview,
                        "action_payload": bundle.action_payload,
                        "approval_idempotency_key": bundle.approval_idempotency_key,
                        "decision_comment": bundle.decision_comment,
                        "execution_result": bundle.execution_result,
                    },
                )

            mission_events = list(
                (
                    await db.execute(
                        select(MissionEvent)
                        .where(
                            MissionEvent.organization_id == organization_id,
                            MissionEvent.mission_id.in_(mission_ids),
                        )
                        .order_by(MissionEvent.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            for event in mission_events:
                associate_mission_root(
                    event.mission_id,
                    {"subject_id": event.subject_id, "payload": event.payload},
                )

        legacy_messages = list(
            (
                await db.execute(
                    select(
                        AiMessage.session_id,
                        AiMessage.content,
                        AiMessage.structured_content,
                    ).where(AiMessage.organization_id == organization_id)
                )
            ).tuples()
        )
        for session_id, content, structured in legacy_messages:
            associate_candidate_session(
                session_id,
                {"content": content, "structured": structured or {}},
            )
    return _Dependencies(
        applications,
        invitation_applications,
        assessment_files,
        tasks,
        recordings,
        ai_candidate_sources,
        tuple(sorted(candidate_ai_sessions, key=lambda item: (item[0], item[1]))),
    )


async def _owned_registry_objects(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: set[UUID],
    dependencies: _Dependencies,
) -> tuple[PrivateObject, ...]:
    predicates = []
    if candidate_ids:
        predicates.append(
            and_(
                PrivateObject.resource_type == "candidate",
                PrivateObject.resource_id.in_(candidate_ids),
            )
        )
    assessment_ids = {item.id for item in dependencies.assessment_files}
    if assessment_ids:
        predicates.append(
            and_(
                PrivateObject.resource_type == "assessment_file",
                PrivateObject.resource_id.in_(assessment_ids),
            )
        )
    task_ids = {item.id for item in dependencies.tasks}
    if task_ids:
        predicates.append(
            and_(
                PrivateObject.resource_type == "task_submission",
                PrivateObject.resource_id.in_(task_ids),
            )
        )
    recording_ids = {item.id for item in dependencies.recordings}
    if recording_ids:
        predicates.append(
            and_(
                PrivateObject.resource_type == "interview_recording",
                PrivateObject.resource_id.in_(recording_ids),
            )
        )
    ai_source_object_ids = {
        item.private_object_id for item in dependencies.ai_candidate_sources
    }
    if ai_source_object_ids:
        predicates.append(PrivateObject.id.in_(ai_source_object_ids))
    ai_session_ids = {item[1] for item in dependencies.candidate_ai_sessions}
    if ai_session_ids:
        predicates.append(
            and_(
                PrivateObject.resource_type == "ai_attachment",
                PrivateObject.resource_id.in_(ai_session_ids),
            )
        )
    if not predicates:
        return ()
    return tuple(
        (
            await db.execute(
                select(PrivateObject).where(
                    PrivateObject.organization_id == organization_id,
                    or_(*predicates),
                )
            )
        ).scalars()
    )


def _scrub_candidate_object_metadata(item: PrivateObject) -> None:
    """Remove live descriptive PII while retaining deletion retry invariants."""

    item.original_filename = "erased"
    item.media_type = "application/octet-stream"
    item.scanner_engine = None
    item.scanner_version = None
    item.scanner_verdict = None


def _held_resources(
    objects: tuple[PrivateObject, ...],
    dependencies: _Dependencies,
) -> tuple[set[UUID], set[UUID]]:
    task_targets = {
        item.id: (item.candidate_id, item.application_id)
        for item in dependencies.tasks
    }
    recording_targets = {
        item.id: (item.candidate_id, item.application_id)
        for item in dependencies.recordings
    }
    file_invitations = {
        item.id: item.invitation_id for item in dependencies.assessment_files
    }
    invitation_applications = dict(dependencies.invitation_applications)
    application_candidates = {item[0]: item[1] for item in dependencies.applications}
    application_positions = {item[0]: item[2] for item in dependencies.applications}
    ai_source_candidates: dict[UUID, set[UUID]] = {}
    for source in dependencies.ai_candidate_sources:
        ai_source_candidates.setdefault(source.private_object_id, set()).add(
            source.candidate_id
        )
    ai_session_candidates: dict[UUID, set[UUID]] = {}
    for candidate_id, session_id in dependencies.candidate_ai_sessions:
        ai_session_candidates.setdefault(session_id, set()).add(candidate_id)

    candidates: set[UUID] = set()
    positions: set[UUID] = set()
    for item in objects:
        if (
            item.state not in {"ready", "delete_pending"}
            or item.legal_hold_at is None
        ):
            continue
        if item.resource_type == "candidate":
            candidates.add(item.resource_id)
        elif item.resource_type == "task_submission":
            target = task_targets.get(item.resource_id)
            if target is not None:
                candidate_id, application_id = target
                candidates.add(candidate_id)
                if application_id is not None:
                    positions.add(application_positions[application_id])
        elif item.resource_type == "assessment_file":
            invitation_id = file_invitations.get(item.resource_id)
            application_id = invitation_applications.get(invitation_id)
            if application_id is not None:
                candidates.add(application_candidates[application_id])
                positions.add(application_positions[application_id])
        elif item.resource_type == "interview_recording":
            target = recording_targets.get(item.resource_id)
            if target is not None:
                candidate_id, application_id = target
                candidates.add(candidate_id)
                if application_id is not None:
                    positions.add(application_positions[application_id])
        candidates.update(ai_source_candidates.get(item.id, ()))
        if item.resource_type == "ai_attachment":
            candidates.update(ai_session_candidates.get(item.resource_id, ()))
    return candidates, positions


async def _erase_candidate_derivatives(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: set[UUID],
    dependencies: _Dependencies,
    privacy_context: CandidatePrivacyContext,
) -> int:
    """Remove denormalized candidate copies before the candidate FK vanishes.

    Most candidate-owned rows use ``ON DELETE CASCADE``. These records
    deliberately do not (or keep candidate IDs inside JSON), so relying on the
    final candidate delete would orphan personal data in chat, AI, workflow,
    sourcing, notification, and batch-history stores.
    """

    if not candidate_ids:
        return 0
    candidate_strings = {str(item) for item in candidate_ids}
    erased = 0

    # Free chat has no candidate/application FK by design, but a recruiter can
    # still paste a candidate email, CV excerpt, or other inventoried identity
    # marker into it. Lock the tenant's unbound free sessions before scanning
    # so a concurrent message insert cannot race the erasure commit. Deleting
    # the containing session removes its complete Message history through the
    # database cascade; the basic chat model has no separate attachment store.
    free_sessions = list(
        (
            await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.organization_id == organization_id,
                    ChatSession.scope == "free",
                    ChatSession.candidate_id.is_(None),
                    ChatSession.application_id.is_(None),
                )
                .with_for_update()
            )
        ).scalars()
    )
    free_session_ids = {item.id for item in free_sessions}
    candidate_bearing_free_sessions = {
        item.id
        for item in free_sessions
        if contains_candidate_marker(item.title, privacy_context.markers)
    }
    if free_session_ids:
        free_messages = list(
            (
                await db.execute(
                    select(Message.session_id, Message.content).where(
                        Message.organization_id == organization_id,
                        Message.session_id.in_(free_session_ids),
                    )
                )
            ).tuples()
        )
        candidate_bearing_free_sessions.update(
            session_id
            for session_id, content in free_messages
            if session_id is not None
            and contains_candidate_marker(content, privacy_context.markers)
        )
    if candidate_bearing_free_sessions:
        erased += int(
            (
                await db.execute(
                    delete(ChatSession).where(
                        ChatSession.organization_id == organization_id,
                        ChatSession.id.in_(candidate_bearing_free_sessions),
                    )
                )
            ).rowcount
            or 0
        )

    # Saved candidate-search views are navigation state rather than candidate
    # children, so they have no candidate FK. Their arbitrary display name and
    # typed filters can nevertheless retain an exact email, CV excerpt, or
    # other inventoried marker. Lock and remove only matching views inside the
    # tenant; unrelated views and another tenant's data remain untouched.
    saved_views = list(
        (
            await db.execute(
                select(CandidateSavedView)
                .where(CandidateSavedView.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    candidate_bearing_view_ids = {
        item.id
        for item in saved_views
        if contains_candidate_marker(
            {"name": item.name, "filters": item.filters},
            privacy_context.markers,
        )
    }
    if candidate_bearing_view_ids:
        erased += int(
            (
                await db.execute(
                    delete(CandidateSavedView).where(
                        CandidateSavedView.organization_id == organization_id,
                        CandidateSavedView.id.in_(candidate_bearing_view_ids),
                    )
                )
            ).rowcount
            or 0
        )

    ai_session_ids = {item[1] for item in dependencies.candidate_ai_sessions}
    if ai_session_ids:
        erased += int(
            (
                await db.execute(
                    delete(AiSession).where(
                        AiSession.organization_id == organization_id,
                        AiSession.id.in_(ai_session_ids),
                    )
                )
            ).rowcount
            or 0
        )
    for model in (ChatSession, WorkflowRun, SourceCandidateReceipt):
        erased += int(
            (
                await db.execute(
                    delete(model).where(
                        model.organization_id == organization_id,
                        model.candidate_id.in_(candidate_ids),
                    )
                )
            ).rowcount
            or 0
        )

    erased += int(
        (
            await db.execute(
                delete(Notification).where(
                    Notification.organization_id == organization_id,
                    Notification.payload["candidate_id"]
                    .as_string()
                    .in_(candidate_strings),
                )
            )
        ).rowcount
        or 0
    )

    source_object_ids = {
        item.private_object_id for item in dependencies.ai_candidate_sources
    }
    audit_predicates = [
        and_(
            AuditLog.resource_type == "candidate",
            AuditLog.resource_id.in_(candidate_ids),
        ),
        AuditLog.metadata_json["canonical_candidate_id"]
        .as_string()
        .in_(candidate_strings),
    ]
    if source_object_ids:
        audit_predicates.append(
            and_(
                AuditLog.resource_type == "ai_attachment",
                AuditLog.resource_id.in_(source_object_ids),
            )
        )
    erased += int(
        (
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.organization_id == organization_id,
                    or_(*audit_predicates),
                )
            )
        ).rowcount
        or 0
    )

    workflow_rules = list(
        (
            await db.execute(
                select(WorkflowRule)
                .where(WorkflowRule.organization_id == organization_id)
                .order_by(WorkflowRule.id)
                .with_for_update()
            )
        ).scalars()
    )
    matching_rule_ids = {
        item.id
        for item in workflow_rules
        if contains_candidate_marker(
            {
                "name": item.name,
                "conditions": item.conditions,
                "actions": item.actions,
            },
            privacy_context.markers,
        )
    }
    if matching_rule_ids:
        erased += int(
            (
                await db.execute(
                    delete(WorkflowRule).where(
                        WorkflowRule.organization_id == organization_id,
                        WorkflowRule.id.in_(matching_rule_ids),
                    )
                )
            ).rowcount
            or 0
        )

    workflow_runs = list(
        (
            await db.execute(
                select(WorkflowRun)
                .where(WorkflowRun.organization_id == organization_id)
                .order_by(WorkflowRun.id)
                .with_for_update()
            )
        ).scalars()
    )
    for run in workflow_runs:
        changed = False
        if contains_candidate_marker(run.rule_name_snapshot, privacy_context.markers):
            run.rule_name_snapshot = "privacy-erased"
            changed = True
        if contains_candidate_marker(run.conditions_snapshot, privacy_context.markers):
            run.conditions_snapshot = {"privacy_erased": True}
            changed = True
        if contains_candidate_marker(run.actions_snapshot, privacy_context.markers):
            run.actions_snapshot = [{"privacy_erased": True}]
            changed = True
        if contains_candidate_marker(run.action_results, privacy_context.markers):
            run.action_results = [{"privacy_erased": True}]
            changed = True
        if changed:
            # Workflow runs are operational/audit evidence for a potentially
            # different candidate. Preserve run status, timing, and identity;
            # only tombstone the copied candidate-bearing definition/result.
            erased += 1

    # Reusable workflow actions can materialize their arbitrary strings into
    # otherwise unrelated candidates' stage history, tags, activities, and
    # tasks. These rows have no provenance link back to the erased definition,
    # so scan the complete tenant under row locks and tombstone fields only.
    stage_events = list(
        (
            await db.execute(
                select(ApplicationStageEvent)
                .where(ApplicationStageEvent.organization_id == organization_id)
                .order_by(ApplicationStageEvent.id)
                .with_for_update()
            )
        ).scalars()
    )
    stage_event_updates: list[tuple[UUID, dict[str, object]]] = []
    for event in stage_events:
        values: dict[str, object] = {}
        if contains_candidate_marker(event.reason, privacy_context.markers):
            values["reason"] = None
        if contains_candidate_marker(event.details, privacy_context.markers):
            values["details"] = {"privacy_erased": True}
        if values:
            stage_event_updates.append((event.id, values))
    if stage_event_updates:
        # ``prepare_retention_erasure`` runs before the router's final delete
        # phase enables erasure. Bracket only these fixed redactions with the
        # transaction-local trigger capability and turn it off immediately.
        await db.execute(select(func.set_config("app.allow_erasure", "on", True)))
        for event_id, values in stage_event_updates:
            # e07b3d5f9a21 permits only reason=NULL and/or the exact details
            # tombstone. Lifecycle, tenant, actor, stage, and timestamp
            # evidence remains immutable.
            result = await db.execute(
                update(ApplicationStageEvent)
                .where(
                    ApplicationStageEvent.organization_id == organization_id,
                    ApplicationStageEvent.id == event_id,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise RetentionStorageError(
                    "Application stage privacy redaction was not durable"
                )
            erased += 1
        await db.execute(select(func.set_config("app.allow_erasure", "off", True)))

    candidate_tags = list(
        (
            await db.execute(
                select(CandidateTag)
                .where(CandidateTag.organization_id == organization_id)
                .order_by(CandidateTag.id)
                .with_for_update()
            )
        ).scalars()
    )
    for tag in candidate_tags:
        if contains_candidate_marker(tag.label, privacy_context.markers):
            tag.label = f"privacy-erased-{tag.id.hex[:16]}"
            erased += 1

    candidate_activities = list(
        (
            await db.execute(
                select(CandidateActivity)
                .where(CandidateActivity.organization_id == organization_id)
                .order_by(CandidateActivity.id)
                .with_for_update()
            )
        ).scalars()
    )
    for activity in candidate_activities:
        if contains_candidate_marker(activity.payload, privacy_context.markers):
            activity.payload = {"privacy_erased": True}
            erased += 1

    task_submissions = list(
        (
            await db.execute(
                select(TaskSubmission)
                .where(TaskSubmission.organization_id == organization_id)
                .order_by(TaskSubmission.id)
                .with_for_update()
            )
        ).scalars()
    )
    for task in task_submissions:
        changed = False
        if contains_candidate_marker(task.title, privacy_context.markers):
            task.title = "privacy-erased"
            changed = True
        if contains_candidate_marker(task.prompt, privacy_context.markers):
            task.prompt = None
            changed = True
        if changed:
            erased += 1

    communication_templates = list(
        (
            await db.execute(
                select(CommunicationTemplate)
                .where(CommunicationTemplate.organization_id == organization_id)
                .order_by(CommunicationTemplate.id)
                .with_for_update()
            )
        ).scalars()
    )
    matching_communication_template_ids = {
        item.id
        for item in communication_templates
        if contains_candidate_marker(
            {
                "name": item.name,
                "subject": item.subject_template,
                "body": item.body_template,
            },
            privacy_context.markers,
        )
    }
    if matching_communication_template_ids:
        erased += int(
            (
                await db.execute(
                    delete(CommunicationTemplate).where(
                        CommunicationTemplate.organization_id == organization_id,
                        CommunicationTemplate.id.in_(
                            matching_communication_template_ids
                        ),
                    )
                )
            ).rowcount
            or 0
        )

    communication_messages = list(
        (
            await db.execute(
                select(CommunicationMessage)
                .where(CommunicationMessage.organization_id == organization_id)
                .order_by(CommunicationMessage.id)
                .with_for_update()
            )
        ).scalars()
    )
    for message in communication_messages:
        changed = False
        if contains_candidate_marker(message.subject, privacy_context.markers):
            message.subject = "[privacy erased]"
            changed = True
        if contains_candidate_marker(message.body, privacy_context.markers):
            message.body = "[privacy erased]"
            changed = True
        if contains_candidate_marker(message.idempotency_key, privacy_context.markers):
            message.idempotency_key = f"privacy-erased:{message.id}"
            changed = True
        if contains_candidate_marker(
            message.provider_message_id, privacy_context.markers
        ):
            message.provider_message_id = None
            changed = True
        if contains_candidate_marker(
            {
                "event_id": message.webhook_event_id,
                "payload_sha256": message.webhook_payload_sha256,
            },
            privacy_context.markers,
        ):
            message.webhook_event_id = None
            message.webhook_payload_sha256 = None
            changed = True
        if changed:
            # Keep delivery state/accounting for the message recipient while
            # removing only the rendered template copy belonging to another
            # erased candidate.
            erased += 1

    offer_templates = list(
        (
            await db.execute(
                select(OfferTemplate)
                .where(OfferTemplate.organization_id == organization_id)
                .order_by(OfferTemplate.id)
                .with_for_update()
            )
        ).scalars()
    )
    matching_offer_template_ids = {
        item.id
        for item in offer_templates
        if contains_candidate_marker(
            {
                "name": item.name,
                "subject": item.subject_template,
                "body": item.body_template,
            },
            privacy_context.markers,
        )
    }
    if matching_offer_template_ids:
        erased += int(
            (
                await db.execute(
                    delete(OfferTemplate).where(
                        OfferTemplate.organization_id == organization_id,
                        OfferTemplate.id.in_(matching_offer_template_ids),
                    )
                )
            ).rowcount
            or 0
        )

    candidate_offers = list(
        (
            await db.execute(
                select(CandidateOffer)
                .where(CandidateOffer.organization_id == organization_id)
                .order_by(CandidateOffer.id)
                .with_for_update()
            )
        ).scalars()
    )
    for offer in candidate_offers:
        changed = False
        if contains_candidate_marker(
            offer.template_name_snapshot, privacy_context.markers
        ):
            offer.template_name_snapshot = None
            changed = True
        if contains_candidate_marker(
            offer.subject_template_snapshot, privacy_context.markers
        ):
            offer.subject_template_snapshot = "[privacy erased]"
            changed = True
        if contains_candidate_marker(
            offer.body_template_snapshot, privacy_context.markers
        ):
            offer.body_template_snapshot = "[privacy erased]"
            changed = True
        if contains_candidate_marker(offer.subject, privacy_context.markers):
            offer.subject = "[privacy erased]"
            changed = True
        if contains_candidate_marker(offer.body, privacy_context.markers):
            offer.body = "[privacy erased]"
            changed = True
        if contains_candidate_marker(offer.signature_name, privacy_context.markers):
            offer.signature_name = None
            changed = True
        if changed:
            # Offers may already be accepted. Preserve compensation, status,
            # approvals and timestamps while removing copied template PII.
            erased += 1

    offer_approvals = list(
        (
            await db.execute(
                select(OfferApproval)
                .where(OfferApproval.organization_id == organization_id)
                .order_by(OfferApproval.id)
                .with_for_update()
            )
        ).scalars()
    )
    approval_updates: list[tuple[UUID, str | None, dict]] = []
    for approval in offer_approvals:
        if not isinstance(approval.offer_snapshot, dict):
            raise RetentionStorageError(
                "Offer approval snapshot could not be inventoried safely"
            )
        snapshot = dict(approval.offer_snapshot)
        snapshot_changed = False
        for field in ("subject", "body"):
            if contains_candidate_marker(
                snapshot.get(field), privacy_context.markers
            ):
                snapshot[field] = "[privacy erased]"
                snapshot_changed = True
        comment = approval.comment
        comment_changed = contains_candidate_marker(
            comment, privacy_context.markers
        )
        if snapshot_changed or comment_changed:
            approval_updates.append(
                (approval.id, None if comment_changed else comment, snapshot)
            )
    if approval_updates:
        await db.execute(select(func.set_config("app.allow_erasure", "on", True)))
        for approval_id, comment, snapshot in approval_updates:
            # Use narrowly-columned SQL so SQLAlchemy's mapped ``updated_at``
            # onupdate hook cannot alter append-only history. The schema
            # trigger permits only comment=NULL and subject/body tombstones;
            # vote, identity, version/cycle, compensation, and time stay exact.
            result = await db.execute(
                text(
                    """
                    UPDATE offer_approvals
                    SET comment = :comment,
                        offer_snapshot = CAST(:offer_snapshot AS JSONB)
                    WHERE organization_id = :organization_id
                      AND id = :approval_id
                    """
                ),
                {
                    "comment": comment,
                    "offer_snapshot": json.dumps(
                        snapshot,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    "organization_id": organization_id,
                    "approval_id": approval_id,
                },
            )
            if result.rowcount != 1:
                raise RetentionStorageError(
                    "Offer approval privacy redaction was not durable"
                )
            erased += 1
        await db.execute(select(func.set_config("app.allow_erasure", "off", True)))

    batches = list(
        (
            await db.execute(
                select(CommunicationBatch)
                .where(CommunicationBatch.organization_id == organization_id)
                .with_for_update()
            )
        ).scalars()
    )
    for batch in batches:
        current = list(batch.skip_summary or [])
        retained = [
            item
            for item in current
            if not contains_candidate_marker(item, privacy_context.markers)
        ]
        removed = len(current) - len(retained)
        if removed:
            batch.skip_summary = retained
            batch.skipped_count = max(0, batch.skipped_count - removed)
            batch.requested_count = max(0, batch.requested_count - removed)
            erased += removed
        if contains_candidate_marker(
            batch.idempotency_key, privacy_context.markers
        ):
            # Preserve delivery accounting/history while severing an
            # arbitrary user-supplied idempotency key from the data subject.
            batch.idempotency_key = f"privacy-erased:{batch.id}"
            erased += 1

    source_import_runs = list(
        (
            await db.execute(
                select(SourceImportRun)
                .where(SourceImportRun.organization_id == organization_id)
                .order_by(SourceImportRun.id)
                .with_for_update()
            )
        ).scalars()
    )
    for run in source_import_runs:
        if contains_candidate_marker(
            run.idempotency_key,
            privacy_context.markers,
        ):
            # Preserve operational import counts and history, but never retain
            # a raw caller-controlled key that identifies the erased subject.
            run.idempotency_key = f"privacy-erased:{run.id}"
            erased += 1
    return erased


def _calendar_cleanup_cipher() -> IntegrationCipher | None:
    settings = get_settings()
    key = settings.integration_credential_encryption_key
    if key is None:
        return None
    try:
        return IntegrationCipher.from_base64(
            key,
            key_id=settings.integration_credential_key_id,
        )
    except IntegrationCryptoError:
        return None


def _record_calendar_cleanup_failure(
    db: AsyncSession,
    *,
    organization_id: UUID,
    connection_id: UUID | None,
    appointment_digest: str,
    failure_code: str,
) -> None:
    db.add(
        AuditLog(
            organization_id=organization_id,
            actor_user_id=None,
            actor_email_snapshot=None,
            action="candidate_erasure.calendar_cleanup_failed",
            resource_type="integration_connection",
            resource_id=connection_id,
            path_template="/enterprise/retention/run",
            method="SYSTEM",
            status_code=503,
            request_id=uuid4(),
            metadata_json={
                "appointment_digest": appointment_digest,
                "failure_code": failure_code,
            },
            occurred_at=datetime.now(UTC),
        )
    )


async def _stage_calendar_cleanup_for_erasure(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: set[UUID],
) -> None:
    """Serialize provider work, scrub candidate refs, and retain delete work."""

    if not candidate_ids:
        return
    appointments = list(
        (
            await db.execute(
                select(InterviewAppointment).where(
                    InterviewAppointment.organization_id == organization_id,
                    InterviewAppointment.candidate_id.in_(candidate_ids),
                    InterviewAppointment.calendar_connection_id.is_not(None),
                )
            )
        ).scalars()
    )
    cipher = _calendar_cleanup_cipher()
    for appointment in sorted(appointments, key=lambda item: item.id):
        lock_key = f"calendar-sync:{organization_id}:{appointment.id}"
        await db.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        await db.refresh(appointment)
        appointment_digest = hashlib.sha256(
            f"{organization_id}:{appointment.id}".encode()
        ).hexdigest()
        sync_jobs = list(
            (
                await db.execute(
                    select(IntegrationJob)
                    .where(
                        IntegrationJob.organization_id == organization_id,
                        IntegrationJob.operation == "calendar.sync",
                        IntegrationJob.reference_payload["appointment_id"].as_string()
                        == str(appointment.id),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        ambiguous_running_create = any(
            job.status == "running" and appointment.external_event_id is None
            for job in sync_jobs
        )
        for job in sync_jobs:
            if job.status in {"queued", "running", "retrying"}:
                await cancel_job(db, job=job)
            job.reference_payload = {
                "kind": "appointment_erased",
                "appointment_digest": appointment_digest,
            }
            job.payload_ciphertext = None
            job.payload_nonce = None
            job.payload_key_id = None

        if not appointment.external_event_id:
            if ambiguous_running_create:
                _record_calendar_cleanup_failure(
                    db,
                    organization_id=organization_id,
                    connection_id=appointment.calendar_connection_id,
                    appointment_digest=appointment_digest,
                    failure_code="calendar_create_outcome_unknown",
                )
                raise RetentionStorageError(
                    "Calendar create outcome is unknown; provider erasure is unproven"
                )
            continue
        connection = (
            await db.execute(
                select(IntegrationConnection).where(
                    IntegrationConnection.id == appointment.calendar_connection_id,
                    IntegrationConnection.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        event_digest = hashlib.sha256(
            appointment.external_event_id.encode()
        ).hexdigest()
        if connection is None or cipher is None:
            _record_calendar_cleanup_failure(
                db,
                organization_id=organization_id,
                connection_id=(connection.id if connection is not None else None),
                appointment_digest=appointment_digest,
                failure_code=(
                    "connection_not_found"
                    if connection is None
                    else "credential_encryption_unavailable"
                ),
            )
            raise RetentionStorageError(
                "Calendar provider deletion could not be durably staged"
            )
        try:
            delete_job, _created = await enqueue_job(
                db,
                connection=connection,
                actor_user_id=None,
                operation="calendar.delete",
                idempotency_key=(
                    f"candidate-erasure:{appointment_digest[:32]}:{event_digest[:32]}"
                ),
                references={
                    "kind": "candidate_erasure_calendar_cleanup",
                    "appointment_digest": appointment_digest,
                },
                sensitive_payload={"event_id": appointment.external_event_id},
                cipher=cipher,
                max_attempts=5,
                allow_unready=True,
            )
            if delete_job.status in {"dead_letter", "cancelled"}:
                _record_calendar_cleanup_failure(
                    db,
                    organization_id=organization_id,
                    connection_id=connection.id,
                    appointment_digest=appointment_digest,
                    failure_code="calendar_delete_not_runnable",
                )
                raise RetentionStorageError(
                    "Calendar provider deletion is not runnable"
                )
        except IntegrationJobError as exc:
            _record_calendar_cleanup_failure(
                db,
                organization_id=organization_id,
                connection_id=connection.id,
                appointment_digest=appointment_digest,
                failure_code=exc.code,
            )
            raise RetentionStorageError(
                "Calendar provider deletion could not be durably staged"
            ) from exc


async def _record_assessment_cleanup_wait(
    db: AsyncSession,
    *,
    organization_id: UUID,
    job_id: UUID,
    assessment_digest: str,
) -> None:
    existing = (
        await db.execute(
            select(AuditLog.id).where(
                AuditLog.organization_id == organization_id,
                AuditLog.action == "candidate_erasure.provider_cleanup_pending",
                AuditLog.resource_type == "integration_job",
                AuditLog.resource_id == job_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        AuditLog(
            organization_id=organization_id,
            actor_user_id=None,
            actor_email_snapshot=None,
            action="candidate_erasure.provider_cleanup_pending",
            resource_type="integration_job",
            resource_id=job_id,
            path_template="/enterprise/retention/run",
            method="SYSTEM",
            status_code=202,
            request_id=uuid4(),
            metadata_json={
                "kind": "candidate_erasure_assessment_cleanup",
                "assessment_digest": assessment_digest,
                "reason": "provider_invitation_in_flight",
            },
            occurred_at=datetime.now(UTC),
        )
    )


async def _stage_provider_assessment_cleanup_for_erasure(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: set[UUID],
) -> set[UUID]:
    """Stage remote assessment deletion and return candidates still pending.

    A specialist provider receives the candidate UUID as its opaque subject
    reference when an invitation is created.  Deletion can therefore remain
    complete even when an invitation response was ambiguous and no provider
    invitation ID was stored.  Local erasure is deliberately two phase: a
    candidate remains outside the erasure plan until every applicable provider
    deletion job has reached confirmed success.
    """

    if not candidate_ids:
        return set()
    records = list(
        (
            await db.execute(
                select(ProviderAssessment)
                .where(
                    ProviderAssessment.organization_id == organization_id,
                    ProviderAssessment.candidate_id.in_(candidate_ids),
                )
                .order_by(ProviderAssessment.id)
                .with_for_update()
            )
        ).scalars()
    )
    if not records:
        return set()
    cipher = _calendar_cleanup_cipher()
    pending: set[UUID] = set()
    for record in records:
        assessment_digest = hashlib.sha256(
            f"{organization_id}:{record.id}".encode()
        ).hexdigest()
        existing_delete = (
            await db.execute(
                select(IntegrationJob)
                .where(
                    IntegrationJob.organization_id == organization_id,
                    IntegrationJob.connection_id == record.connection_id,
                    IntegrationJob.operation == "assessment.delete",
                    IntegrationJob.reference_payload["assessment_digest"].as_string()
                    == assessment_digest,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing_delete is not None:
            if existing_delete.status == "succeeded":
                continue
            if existing_delete.status == "cancelled":
                raise RetentionStorageError(
                    "Assessment provider deletion was cancelled and is unproven"
                )
            pending.add(record.candidate_id)
            continue

        invitation_job = (
            await db.execute(
                select(IntegrationJob)
                .where(
                    IntegrationJob.id == record.invitation_job_id,
                    IntegrationJob.organization_id == organization_id,
                    IntegrationJob.connection_id == record.connection_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invitation_job is None:
            raise RetentionStorageError(
                "Assessment provider invitation history is incomplete"
            )
        if invitation_job.status == "running":
            # Do not race an external create with its privacy deletion.  The
            # durable invitation job and audit row keep the wait visible while
            # the local candidate remains intact.
            await _record_assessment_cleanup_wait(
                db,
                organization_id=organization_id,
                job_id=invitation_job.id,
                assessment_digest=assessment_digest,
            )
            pending.add(record.candidate_id)
            continue

        remote_copy_possible = bool(
            record.provider_invitation_id
            or record.provider_attempt_id
            or invitation_job.attempt_count > 0
            or invitation_job.status in {"succeeded", "dead_letter"}
        )
        if not remote_copy_possible:
            if invitation_job.status in {"queued", "retrying"}:
                await cancel_job(db, job=invitation_job)
            continue

        if invitation_job.status in {"queued", "retrying"}:
            await cancel_job(db, job=invitation_job)
        connection = (
            await db.execute(
                select(IntegrationConnection).where(
                    IntegrationConnection.id == record.connection_id,
                    IntegrationConnection.organization_id == organization_id,
                    IntegrationConnection.provider == "assessment_generic",
                )
            )
        ).scalar_one_or_none()
        if connection is None or cipher is None:
            raise RetentionStorageError(
                "Assessment provider deletion could not be durably staged"
            )
        request = AssessmentProviderErasureRequest(
            subject_ref=record.candidate_id,
            provider_invitation_id=record.provider_invitation_id,
            provider_attempt_id=record.provider_attempt_id,
        )
        try:
            delete_job, _created = await enqueue_job(
                db,
                connection=connection,
                actor_user_id=None,
                operation="assessment.delete",
                idempotency_key=f"candidate-erasure-assessment:{assessment_digest}",
                references={
                    "kind": "candidate_erasure_assessment_cleanup",
                    "assessment_digest": assessment_digest,
                },
                sensitive_payload=request.model_dump(mode="json", exclude_none=True),
                cipher=cipher,
                max_attempts=5,
                allow_unready=True,
            )
        except IntegrationJobError as exc:
            raise RetentionStorageError(
                "Assessment provider deletion could not be durably staged"
            ) from exc
        if delete_job.status == "succeeded":
            continue
        if delete_job.status == "cancelled":
            raise RetentionStorageError(
                "Assessment provider deletion was cancelled and is unproven"
            )
        pending.add(record.candidate_id)
    return pending


async def _hash_object(store: BlobStore, key: str) -> str:
    digest = hashlib.sha256()
    async for chunk in store.iter_bytes(key):
        digest.update(chunk)
    return digest.hexdigest()


async def _validate_outstanding_legacy_deletions(
    deletions: list[LegacyPrivateObjectDeletion],
) -> None:
    """Prove pending cutover sources still match before subject erasure.

    A digest mismatch must never be "adopted" by rewriting the deletion
    snapshot: the replacement at that opaque path is not proven to be the
    candidate's old object. Fail the enclosing retention transaction closed
    instead. An absent source is already physically erased and remains an
    idempotent worker success.
    """

    pending = [item for item in deletions if item.completed_at is None]
    if not pending:
        return
    try:
        store = get_legacy_blob_store()
        for item in pending:
            key = normalize_legacy_private_key(
                item.normalized_key,
                organization_id=item.organization_id,
                category=item.category,
                root=store.root,
            )
            if key != item.normalized_key:
                raise RetentionStorageError(
                    "Legacy private-object identity could not be verified"
                )
            snapshot = await snapshot_legacy_file(store, key)
            if snapshot is not None and (
                snapshot.size_bytes != item.size_bytes
                or snapshot.sha256 != item.sha256_snapshot
            ):
                raise RetentionStorageError(
                    "Legacy private object changed before candidate erasure"
                )
    except RetentionStorageError:
        raise
    except LegacyPrivateObjectError as exc:
        raise RetentionStorageError(
            "Legacy private object could not be verified before candidate erasure"
        ) from exc


async def _adopt_or_stage_legacy(
    db: AsyncSession,
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: UUID,
    category: str,
    reference: str,
    store: BlobStore,
) -> tuple[PrivateObject | None, LegacyRetentionObject | None, bool]:
    try:
        key = require_tenant_private_key(
            reference,
            organization_id=organization_id,
            category=category,
        )
    except ValueError:
        key = None
    if key is None:
        try:
            queued = await enqueue_legacy_private_object_deletion(
                db,
                organization_id=organization_id,
                category=category,
                resource_type=resource_type,
                resource_id=resource_id,
                legacy_reference=reference,
                reason="candidate_erasure",
                store=store if isinstance(store, LocalBlobStore) else None,
            )
        except LegacyPrivateObjectError as exc:
            raise RetentionStorageError(
                "Legacy private object could not be durably staged for erasure"
            ) from exc
        return (
            None,
            LegacyRetentionObject(reference, category) if queued is not None else None,
            queued is None,
        )

    existing = (
        await db.execute(
            select(PrivateObject).where(
                PrivateObject.organization_id == organization_id,
                PrivateObject.object_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.resource_type != resource_type
            or existing.resource_id != resource_id
            or existing.category != category
        ):
            raise RetentionStorageError(
                "Legacy private object is bound to a different tenant resource"
            )
        return existing, None, False
    metadata = await store.stat(key)
    if metadata is None:
        return None, None, True
    if not 1 <= metadata.size_bytes <= 15 * 1024 * 1024:
        raise RetentionStorageError("Legacy private object has an invalid size")
    suffix = Path(key).suffix.casefold()
    try:
        media_type = canonical_media_type(suffix)
    except ValueError as exc:
        raise RetentionStorageError(
            "Legacy private object has an unsupported file type"
        ) from exc
    stored = StoredUpload(
        key=key,
        original_filename=Path(key).name,
        suffix=suffix,
        media_type=media_type,
        size_bytes=metadata.size_bytes,
        sha256=await _hash_object(store, key),
        scan=ScanResult(engine="legacy", result="erasure-only"),
    )
    record = await adopt_legacy_private_object_for_erasure(
        db,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        stored=stored,
    )
    return record, None, False


async def prepare_retention_erasure(
    db: AsyncSession,
    *,
    organization_id: UUID,
    deleted_cutoff: datetime,
    active_cutoff: datetime,
    include_active_candidates: bool,
    store: BlobStore | None = None,
) -> RetentionErasurePlan:
    """Lock targets, honor object holds, and stage every registered deletion."""

    # The schema's tenant write-gate triggers take a shared try-lock for every
    # organization-scoped mutation. Retention takes the matching exclusive
    # transaction lock before its first inventory read, preventing an unbound
    # free-chat/view/template/etc. insert from committing after its scan and
    # before the candidate erasure commits. Different tenants remain parallel.
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                func.privacy_erasure_gate_key(organization_id)
            )
        )
    )
    backend = store or get_blob_store()
    attachment_text_cache: dict[UUID, str] = {}
    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(Candidate.organization_id == organization_id)
                .execution_options(include_deleted=True)
                .with_for_update()
            )
        ).scalars()
    )
    positions = list(
        (
            await db.execute(
                select(Position)
                .where(Position.organization_id == organization_id)
                .execution_options(include_deleted=True)
                .with_for_update()
            )
        ).scalars()
    )
    initial_candidates = {
        item.id
        for item in candidates
        if (item.deleted_at is not None and item.deleted_at < deleted_cutoff)
        or (
            include_active_candidates
            and item.deleted_at is None
            and item.created_at < active_cutoff
        )
    }
    initial_positions = {
        item.id
        for item in positions
        if item.deleted_at is not None and item.deleted_at < deleted_cutoff
    }
    children: dict[UUID, set[UUID]] = {}
    for item in candidates:
        if item.merged_into_candidate_id is not None:
            children.setdefault(item.merged_into_candidate_id, set()).add(item.id)
    candidate_closures = {root: _closure(root, children) for root in initial_candidates}
    provisional_candidates = (
        set().union(*candidate_closures.values()) if candidate_closures else set()
    )
    provisional_dependencies = await _dependencies(
        db,
        organization_id=organization_id,
        candidate_ids=provisional_candidates,
        position_ids=initial_positions,
        store=backend,
        attachment_text_cache=attachment_text_cache,
    )
    provisional_objects = await _owned_registry_objects(
        db,
        organization_id=organization_id,
        candidate_ids=provisional_candidates,
        dependencies=provisional_dependencies,
    )
    held_candidates, held_positions = _held_resources(
        provisional_objects,
        provisional_dependencies,
    )
    safe_roots = {
        root
        for root, closure in candidate_closures.items()
        if not closure.intersection(held_candidates)
    }
    legal_hold_safe_roots = set(safe_roots)
    candidate_ids = (
        set().union(*(candidate_closures[root] for root in safe_roots))
        if safe_roots
        else set()
    )
    position_ids = initial_positions - held_positions
    dependencies = await _dependencies(
        db,
        organization_id=organization_id,
        candidate_ids=candidate_ids,
        position_ids=position_ids,
        store=backend,
        attachment_text_cache=attachment_text_cache,
    )
    objects = await _owned_registry_objects(
        db,
        organization_id=organization_id,
        candidate_ids=candidate_ids,
        dependencies=dependencies,
    )
    if any(
        item.state in {"ready", "delete_pending"}
        and item.legal_hold_at is not None
        for item in objects
    ):
        raise RetentionStorageError("A legal-held object remained in an erasure set")

    provider_pending = await _stage_provider_assessment_cleanup_for_erasure(
        db,
        organization_id=organization_id,
        candidate_ids=candidate_ids,
    )
    if provider_pending:
        # Merge roots are indivisible.  If one member still has an unconfirmed
        # provider copy, retain the complete root and recompute all downstream
        # dependencies before staging any local deletion.
        safe_roots = {
            root
            for root in safe_roots
            if not candidate_closures[root].intersection(provider_pending)
        }
        candidate_ids = (
            set().union(*(candidate_closures[root] for root in safe_roots))
            if safe_roots
            else set()
        )
        dependencies = await _dependencies(
            db,
            organization_id=organization_id,
            candidate_ids=candidate_ids,
            position_ids=position_ids,
            store=backend,
            attachment_text_cache=attachment_text_cache,
        )
        objects = await _owned_registry_objects(
            db,
            organization_id=organization_id,
            candidate_ids=candidate_ids,
            dependencies=dependencies,
        )

    erased_resource_ids = set(candidate_ids)
    erased_resource_ids.update(item.id for item in dependencies.assessment_files)
    erased_resource_ids.update(item.id for item in dependencies.tasks)
    erased_resource_ids.update(item.id for item in dependencies.recordings)
    legacy_deletions = list(
        (
            await db.execute(
                select(LegacyPrivateObjectDeletion)
                .where(
                    LegacyPrivateObjectDeletion.organization_id == organization_id,
                    LegacyPrivateObjectDeletion.resource_id.in_(erased_resource_ids),
                )
                .with_for_update()
            )
        ).scalars()
    )
    # Validate outstanding cutover copies before any subject rows or registry
    # metadata are scrubbed. A failure rolls back the complete retention unit.
    await _validate_outstanding_legacy_deletions(legacy_deletions)

    registry_ids: set[UUID] = set()
    for item in objects:
        if item.state == "ready":
            queued = await enqueue_private_object_deletion(
                db,
                organization_id=organization_id,
                private_object_id=item.id,
                reason="candidate_erasure",
            )
            if queued is not None:
                registry_ids.add(item.id)
        _scrub_candidate_object_metadata(item)

    candidate_by_id = {item.id: item for item in candidates}
    legacy_specs: list[tuple[str, UUID, str, str]] = []
    for candidate_id in candidate_ids:
        item = candidate_by_id[candidate_id]
        if item.private_object_id is None and item.pdf_path:
            legacy_specs.append(("candidate", item.id, "candidates", item.pdf_path))
    for item in dependencies.assessment_files:
        if item.private_object_id is None and item.storage_path:
            legacy_specs.append(
                ("assessment_file", item.id, "assessments", item.storage_path)
            )
    for item in dependencies.tasks:
        if item.private_object_id is None and item.file_path:
            legacy_specs.append(("task_submission", item.id, "tasks", item.file_path))
    for item in dependencies.recordings:
        if item.private_object_id is None and item.media_path:
            legacy_specs.append(
                (
                    "interview_recording",
                    item.id,
                    "recordings",
                    item.media_path,
                )
            )

    legacy_objects: list[LegacyRetentionObject] = []
    adopted = 0
    missing = 0
    for resource_type, resource_id, category, reference in legacy_specs:
        record, legacy, absent = await _adopt_or_stage_legacy(
            db,
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            category=category,
            reference=reference,
            store=backend,
        )
        if record is not None and record.state in {"ready", "delete_pending"}:
            if record.state == "ready":
                await enqueue_private_object_deletion(
                    db,
                    organization_id=organization_id,
                    private_object_id=record.id,
                    reason="candidate_erasure",
                )
            registry_ids.add(record.id)
            _scrub_candidate_object_metadata(record)
            adopted += 1
        if legacy is not None:
            legacy_objects.append(legacy)
        if absent:
            missing += 1

    # Legacy-only resources above may have created additional durable rows.
    # Refresh the complete locked set and validate it once more before the
    # live-store erasure boundary; this also closes changes after the earlier
    # cutover-source check.
    legacy_deletions = list(
        (
            await db.execute(
                select(LegacyPrivateObjectDeletion)
                .where(
                    LegacyPrivateObjectDeletion.organization_id == organization_id,
                    LegacyPrivateObjectDeletion.resource_id.in_(erased_resource_ids),
                )
                .with_for_update()
            )
        ).scalars()
    )
    await _validate_outstanding_legacy_deletions(legacy_deletions)

    try:
        privacy_context = await build_candidate_privacy_context(
            db,
            organization_id=organization_id,
            candidate_ids=candidate_ids,
        )
    except PrivacyErasureError as exc:
        raise RetentionStorageError(
            "Candidate privacy inventory could not be completed"
        ) from exc

    await _stage_calendar_cleanup_for_erasure(
        db,
        organization_id=organization_id,
        candidate_ids=candidate_ids,
    )
    try:
        await erase_candidate_live_store_copies(
            db,
            organization_id=organization_id,
            context=privacy_context,
        )
    except PrivacyErasureError as exc:
        raise RetentionStorageError(
            "Candidate live-store erasure could not be proven"
        ) from exc
    await _erase_candidate_derivatives(
        db,
        organization_id=organization_id,
        candidate_ids=candidate_ids,
        dependencies=dependencies,
        privacy_context=privacy_context,
    )

    # Registry/deletion tombstones need an opaque resource handle for worker
    # reconciliation, not the erased candidate or child-resource UUID.  Keep
    # the object and content hashes needed to prove physical deletion while
    # severing the data-subject link before commit.
    for item in objects:
        if item.resource_id in erased_resource_ids:
            item.resource_id = uuid4()
    erasure_requested_at = datetime.now(UTC)
    for deletion_record in legacy_deletions:
        # A storage cutover can already own the old local copy through a
        # ``replacement`` outbox row after the resource has switched to its
        # canonical private-object binding. Candidate erasure must adopt and
        # accelerate every such outstanding copy; otherwise the live legacy
        # bytes and the subject's resource UUID could survive a successful
        # erasure response until the original replacement schedule runs.
        if deletion_record.completed_at is None:
            deletion_record.reason = "candidate_erasure"
            deletion_record.not_before = min(
                deletion_record.not_before,
                erasure_requested_at,
            )
            deletion_record.next_attempt_at = min(
                deletion_record.next_attempt_at,
                erasure_requested_at,
            )
            # Retention is a new, explicit erasure obligation. Reactivate a
            # replacement row even if its earlier storage retries reached the
            # dead-letter ceiling; otherwise the subject could be removed
            # from PostgreSQL while the legacy bytes remain permanently
            # unclaimable. Row locking also invalidates any stale worker claim
            # before the deletion is re-driven from a clean attempt budget.
            deletion_record.attempt_count = 0
            deletion_record.claimed_at = None
            deletion_record.claim_token = None
            deletion_record.last_error_code = None
        deletion_record.resource_id = uuid4()

    soft_deleted = sum(
        1
        for identifier in candidate_ids
        if candidate_by_id[identifier].deleted_at is not None
    )
    active = len(candidate_ids) - soft_deleted
    return RetentionErasurePlan(
        candidate_ids=frozenset(candidate_ids),
        position_ids=frozenset(position_ids),
        soft_deleted_candidates=soft_deleted,
        active_candidates=active,
        soft_deleted_positions=len(position_ids),
        held_candidate_roots=len(initial_candidates - legal_hold_safe_roots),
        held_positions=len(initial_positions - position_ids),
        registry_deletions=len(registry_ids),
        adopted_legacy_objects=adopted,
        missing_legacy_objects=missing,
        legacy_objects=tuple(legacy_objects),
    )


__all__ = [
    "LegacyRetentionObject",
    "RetentionErasurePlan",
    "RetentionStorageError",
    "prepare_retention_erasure",
]
