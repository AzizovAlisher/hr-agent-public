"""Canonical-candidate helpers and lossless, reversible duplicate merging."""

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, insert, or_, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.application import (
    Application,
    ApplicationEvaluation,
    ApplicationStageEvent,
)
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_merge import CandidateMergeBatch, CandidateMergeBatchMember
from app.models.candidate_note import CandidateNote, CandidateTag
from app.models.candidate_processing import CandidateProcessingJob
from app.models.chat_session import ChatSession
from app.models.communication import (
    CandidateContactPoint,
    CommunicationMessage,
    CommunicationThread,
)
from app.models.interview import Interview
from app.models.interview_recording import InterviewRecording
from app.models.notification import Notification
from app.models.private_object import LegacyPrivateObjectDeletion, PrivateObject
from app.models.provider_assessment import ProviderAssessment
from app.models.requisition import CandidateHandoff, CandidateOffer
from app.models.scheduling import InterviewAppointment, SchedulingLink
from app.models.score import Score
from app.models.scorecard import HiringDecision, ScorecardAssignment
from app.models.sourcing import SourceCandidateReceipt
from app.models.talent import TalentNurtureEnrollment, TalentPoolMember
from app.models.task_submission import TaskSubmission
from app.models.workflow import WorkflowRun
from app.services.activities import append_candidate_activity
from app.services.applications import (
    ensure_application_link,
    ensure_legacy_score_for_application,
)
from app.services.entitlements import record_usage
from app.services.integrations.crypto import (
    IntegrationCipher,
    IntegrationCryptoError,
)
from app.services.legacy_private_object_fence import (
    acquire_legacy_private_object_write_fence,
)
from app.storage.legacy import LegacyPrivateObjectError, normalize_legacy_private_key

SNAPSHOT_VERSION = 1
_SNAPSHOT_PURPOSE = "reversal-snapshot"


@dataclass(frozen=True, slots=True)
class CandidateMergeOutcome:
    batch_id: UUID
    primary_candidate_id: UUID
    merged_candidate_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CandidateMergeReversalOutcome:
    batch_id: UUID
    primary_candidate_id: UUID
    restored_candidate_ids: tuple[UUID, ...]
    version: int
    reversed_at: datetime


def _merged_source_document_unavailable() -> HTTPException:
    """Return one non-enumerating error for invalid historical file evidence."""

    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        "Merged source original file is unavailable.",
    )


_DIRECT_CANDIDATE_MODELS: dict[str, type] = {
    "candidate_notes": CandidateNote,
    "candidate_processing_jobs": CandidateProcessingJob,
    "candidate_tags": CandidateTag,
    "chat_sessions": ChatSession,
    "communication_threads": CommunicationThread,
    "communication_messages": CommunicationMessage,
    "contact_points": CandidateContactPoint,
    "hiring_decisions": HiringDecision,
    "interview_appointments": InterviewAppointment,
    "interview_recordings": InterviewRecording,
    "interviews": Interview,
    "nurture_enrollments": TalentNurtureEnrollment,
    "offers": CandidateOffer,
    "handoffs": CandidateHandoff,
    "pool_members": TalentPoolMember,
    "provider_assessments": ProviderAssessment,
    "scheduling_links": SchedulingLink,
    "scorecard_assignments": ScorecardAssignment,
    "source_candidate_receipts": SourceCandidateReceipt,
    "task_submissions": TaskSubmission,
    "workflow_runs": WorkflowRun,
}


def _snapshot_aad(organization_id: UUID, batch_id: UUID) -> bytes:
    """Bind ciphertext to exactly one tenant, row, purpose, and schema."""

    return (
        f"hr-agent:candidate-merge:v{SNAPSHOT_VERSION}:"
        f"{organization_id}:{batch_id}:{_SNAPSHOT_PURPOSE}"
    ).encode()


def _candidate_merge_cipher() -> IntegrationCipher:
    settings = get_settings()
    configured = settings.integration_credential_encryption_key
    if configured:
        return IntegrationCipher.from_base64(
            configured,
            key_id=settings.integration_credential_key_id,
        )
    if settings.app_environment == "production":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "candidate_merge_encryption_unavailable"},
        )
    # Production configuration rejects a missing integration key.  A stable,
    # purpose-separated development/test key keeps metadata-created test
    # databases usable without introducing an ephemeral, unrecoverable cipher.
    development_key = hashlib.sha256(
        b"hr-agent:candidate-merge:development:v1\0" + settings.jwt_secret.encode()
    ).digest()
    return IntegrationCipher(development_key)


def _encode_column(value: Any) -> dict[str, Any]:
    if value is None:
        return {"t": "null"}
    if isinstance(value, UUID):
        return {"t": "uuid", "v": str(value)}
    if isinstance(value, datetime):
        return {"t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {"t": "decimal", "v": str(value)}
    if isinstance(value, bytes):
        return {"t": "bytes", "v": base64.b64encode(value).decode("ascii")}
    return {"t": "json", "v": value}


def _decode_column(value: dict[str, Any]) -> Any:
    kind = value.get("t")
    raw = value.get("v")
    if kind == "null":
        return None
    if kind == "uuid":
        return UUID(str(raw))
    if kind == "datetime":
        return datetime.fromisoformat(str(raw))
    if kind == "date":
        return date.fromisoformat(str(raw))
    if kind == "decimal":
        return Decimal(str(raw))
    if kind == "bytes":
        return base64.b64decode(str(raw), validate=True)
    if kind == "json":
        return raw
    raise ValueError("Unknown candidate merge snapshot value")


def _row_state(row: object) -> dict[str, dict[str, Any]]:
    # A mapped attribute may intentionally differ from its physical column
    # name (Application.application_metadata -> ``metadata``). Reading the
    # column key as an attribute would resolve DeclarativeBase.metadata and
    # attempt to serialize SQLAlchemy's MetaData object.
    result: dict[str, dict[str, Any]] = {}
    for attribute in sa_inspect(type(row)).column_attrs:
        column = attribute.columns[0]
        if column.computed is None:
            result[column.key] = _encode_column(getattr(row, attribute.key))
    return result


def _decoded_row(state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {key: _decode_column(value) for key, value in state.items()}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(state)).hexdigest()


def _ids_for(state: dict[str, Any] | None, key: str) -> set[UUID]:
    if state is None:
        return set()
    return {UUID(value) for value in state.get(key, {})}


def _candidate_private_object_ids(state: dict[str, Any]) -> set[UUID]:
    """Return registry IDs referenced by serialized candidate rows."""

    result: set[UUID] = set()
    try:
        candidate_rows = state.get("candidates", {})
        if not isinstance(candidate_rows, dict):
            raise TypeError
        for row in candidate_rows.values():
            private_object_id = _decode_column(row["private_object_id"])
            if private_object_id is not None:
                if not isinstance(private_object_id, UUID):
                    raise TypeError
                result.add(private_object_id)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Candidate private-object snapshot is invalid") from exc
    return result


def _validate_candidate_private_object_bindings(
    state: dict[str, Any], *, organization_id: UUID
) -> dict[UUID, UUID]:
    """Validate every serialized candidate-to-registry ownership binding."""

    try:
        candidate_rows = state["candidates"]
        private_object_rows = state["private_objects"]
        if not isinstance(candidate_rows, dict) or not isinstance(
            private_object_rows, dict
        ):
            raise TypeError

        bindings: dict[UUID, UUID] = {}
        referenced_object_ids: set[UUID] = set()
        registered_object_keys: set[str] = set()
        unregistered_private_paths: set[str] = set()
        for candidate_key, row in candidate_rows.items():
            candidate_id = UUID(candidate_key)
            if (
                _decode_column(row["id"]) != candidate_id
                or _decode_column(row["organization_id"]) != organization_id
            ):
                raise ValueError
            private_object_id = _decode_column(row["private_object_id"])
            pdf_path = _decode_column(row["pdf_path"])
            if private_object_id is None:
                if isinstance(pdf_path, str) and pdf_path.startswith("private/"):
                    unregistered_private_paths.add(pdf_path)
                continue
            if not isinstance(private_object_id, UUID):
                raise TypeError
            bindings[candidate_id] = private_object_id
            referenced_object_ids.add(private_object_id)

            private_row = private_object_rows.get(str(private_object_id))
            object_key = (
                _decode_column(private_row["object_key"])
                if private_row is not None
                else None
            )
            if private_row is None or (
                _decode_column(private_row["id"]) != private_object_id
                or _decode_column(private_row["organization_id"]) != organization_id
                or _decode_column(private_row["category"]) != "candidates"
                or _decode_column(private_row["state"]) != "ready"
                or _decode_column(private_row["resource_type"]) != "candidate"
                or _decode_column(private_row["resource_id"]) != candidate_id
                or not isinstance(object_key, str)
                or object_key != pdf_path
            ):
                raise ValueError
            registered_object_keys.add(object_key)

        serialized_object_ids = {UUID(value) for value in private_object_rows}
        if (
            serialized_object_ids != referenced_object_ids
            or not unregistered_private_paths.issubset(registered_object_keys)
        ):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Candidate private-object snapshot is invalid") from exc
    return bindings


async def _serialized_rows(
    db: AsyncSession,
    model: type,
    condition: object,
    *,
    include_deleted: bool = False,
    lock: bool,
) -> dict[str, dict[str, dict[str, Any]]]:
    statement = (
        select(model)
        .where(condition)
        .order_by(model.id)
        .execution_options(populate_existing=True)
    )
    if include_deleted:
        statement = statement.execution_options(include_deleted=True)
    if lock:
        statement = statement.with_for_update()
    rows = list((await db.execute(statement)).scalars())
    return {str(row.id): _row_state(row) for row in rows}


async def _capture_merge_graph(
    db: AsyncSession,
    *,
    organization_id: UUID,
    candidate_ids: list[UUID],
    tracked: tuple[dict[str, Any], ...] = (),
    lock: bool,
) -> dict[str, Any]:
    """Capture every row merge mutates plus append-only application guards."""

    tracked_candidate_ids = set(candidate_ids)
    for state in tracked:
        tracked_candidate_ids.update(_ids_for(state, "candidates"))
    state: dict[str, Any] = {}
    state["candidates"] = await _serialized_rows(
        db,
        Candidate,
        (Candidate.organization_id == organization_id)
        & Candidate.id.in_(tracked_candidate_ids),
        include_deleted=True,
        lock=lock,
    )

    tracked_private_object_ids = _candidate_private_object_ids(state)
    for item in tracked:
        tracked_private_object_ids.update(_ids_for(item, "private_objects"))
        tracked_private_object_ids.update(_candidate_private_object_ids(item))
    if tracked_private_object_ids:
        state["private_objects"] = await _serialized_rows(
            db,
            PrivateObject,
            (PrivateObject.organization_id == organization_id)
            & PrivateObject.id.in_(tracked_private_object_ids),
            lock=lock,
        )
    else:
        state["private_objects"] = {}

    tracked_application_ids: set[UUID] = set()
    for item in tracked:
        tracked_application_ids.update(_ids_for(item, "applications"))
    application_condition = Application.candidate_id.in_(tracked_candidate_ids)
    if tracked_application_ids:
        application_condition = or_(
            application_condition, Application.id.in_(tracked_application_ids)
        )
    state["applications"] = await _serialized_rows(
        db,
        Application,
        (Application.organization_id == organization_id) & application_condition,
        lock=lock,
    )
    application_ids = {UUID(value) for value in state["applications"]}
    application_ids.update(tracked_application_ids)
    if application_ids:
        state["application_stage_events"] = await _serialized_rows(
            db,
            ApplicationStageEvent,
            (ApplicationStageEvent.organization_id == organization_id)
            & ApplicationStageEvent.application_id.in_(application_ids),
            lock=lock,
        )
        state["application_evaluations"] = await _serialized_rows(
            db,
            ApplicationEvaluation,
            (ApplicationEvaluation.organization_id == organization_id)
            & ApplicationEvaluation.application_id.in_(application_ids),
            lock=lock,
        )
    else:
        state["application_stage_events"] = {}
        state["application_evaluations"] = {}

    tracked_score_ids: set[UUID] = set()
    for item in tracked:
        tracked_score_ids.update(_ids_for(item, "scores"))
    score_condition = Score.candidate_id.in_(tracked_candidate_ids)
    if tracked_score_ids:
        score_condition = or_(score_condition, Score.id.in_(tracked_score_ids))
    state["scores"] = await _serialized_rows(
        db,
        Score,
        (Score.organization_id == organization_id) & score_condition,
        lock=lock,
    )

    for key, model in _DIRECT_CANDIDATE_MODELS.items():
        tracked_ids: set[UUID] = set()
        for item in tracked:
            tracked_ids.update(_ids_for(item, key))
        condition = model.candidate_id.in_(tracked_candidate_ids)
        if tracked_ids:
            condition = or_(condition, model.id.in_(tracked_ids))
        state[key] = await _serialized_rows(
            db,
            model,
            (model.organization_id == organization_id) & condition,
            lock=lock,
        )

    tracked_notification_ids: set[UUID] = set()
    for item in tracked:
        tracked_notification_ids.update(_ids_for(item, "notifications"))
    notification_condition = Notification.payload["candidate_id"].astext.in_(
        [str(candidate_id) for candidate_id in tracked_candidate_ids]
    )
    if tracked_notification_ids:
        notification_condition = or_(
            notification_condition, Notification.id.in_(tracked_notification_ids)
        )
    state["notifications"] = await _serialized_rows(
        db,
        Notification,
        (Notification.organization_id == organization_id) & notification_condition,
        lock=lock,
    )
    return state


def _application_is_active(application: Application) -> bool:
    return application.closed_at is None and application.archived_at is None


async def _rehome_application_cycles(
    db: AsyncSession,
    *,
    applications: list[Application],
    scores: list[Score],
    score_winners: dict[UUID, UUID],
    primary_candidate_id: UUID,
    actor_user_id: UUID,
) -> datetime:
    """Collapse candidate aliases without discarding an application cycle.

    Cycle numbers are meaningful only within a candidate/position history. A
    merge combines several such histories, so every position is renumbered in
    a deterministic order. If aliases each have a live application, the
    explicitly selected score winner owns the surviving live cycle; other live
    cycles become audited archives instead of disappearing.
    """

    merged_at = datetime.now(UTC)
    scores_by_position: dict[UUID, list[Score]] = {}
    for score in scores:
        scores_by_position.setdefault(score.position_id, []).append(score)
    applications_by_position: dict[UUID, list[Application]] = {}
    for application in applications:
        applications_by_position.setdefault(application.position_id, []).append(
            application
        )

    for position_id, position_applications in applications_by_position.items():
        position_scores = scores_by_position.get(position_id, [])
        selected_score: Score | None = None
        if position_scores:
            selected_candidate_id = score_winners.get(position_id)
            if selected_candidate_id is None and len(position_scores) == 1:
                selected_score = position_scores[0]
            elif selected_candidate_id is not None:
                selected_score = next(
                    score
                    for score in position_scores
                    if score.candidate_id == selected_candidate_id
                )

        active_applications = [
            application
            for application in position_applications
            if _application_is_active(application)
        ]
        retained_active: Application | None = None
        if active_applications and selected_score is not None:
            retained_active = next(
                (
                    application
                    for application in active_applications
                    if application.legacy_score_id == selected_score.id
                ),
                None,
            )
            if retained_active is None:
                retained_active = next(
                    (
                        application
                        for application in active_applications
                        if application.candidate_id == selected_score.candidate_id
                    ),
                    None,
                )
        if retained_active is None and active_applications:
            retained_active = next(
                (
                    application
                    for application in active_applications
                    if application.candidate_id == primary_candidate_id
                ),
                min(
                    active_applications,
                    key=lambda application: (
                        application.applied_at,
                        application.created_at,
                        application.id,
                    ),
                ),
            )

        selected_score_id = selected_score.id if selected_score is not None else None
        for application in position_applications:
            metadata = dict(application.application_metadata or {})
            metadata["candidate_merge"] = {
                "canonical_candidate_id": str(primary_candidate_id),
                "merged_at": merged_at.isoformat(),
                "retained_active": application is retained_active,
            }
            application.application_metadata = metadata
            if (
                _application_is_active(application)
                and application is not retained_active
            ):
                application.closed_at = merged_at
                application.archived_at = merged_at
                application.archived_by_user_id = actor_user_id
            if (
                application.legacy_score_id is not None
                and application.legacy_score_id != selected_score_id
            ):
                application.legacy_score_id = None
        await db.flush()

        # Move through values above the current range before coalescing
        # candidate IDs, avoiding transient collisions under immediate unique
        # constraints regardless of SQLAlchemy's UPDATE ordering.
        temporary_base = (
            max(application.cycle_number for application in position_applications)
            + len(position_applications)
            + 1
        )
        for offset, application in enumerate(
            sorted(position_applications, key=lambda item: item.id)
        ):
            application.cycle_number = temporary_base + offset
        await db.flush()
        for application in position_applications:
            application.candidate_id = primary_candidate_id
        await db.flush()

        ordered_cycles = sorted(
            position_applications,
            key=lambda application: (
                application is retained_active,
                application.applied_at,
                application.created_at,
                application.id,
            ),
        )
        for cycle_number, application in enumerate(ordered_cycles, start=1):
            application.cycle_number = cycle_number
        await db.flush()
    return merged_at


async def resolve_canonical_candidate(
    db: AsyncSession,
    candidate_id: UUID,
    organization_id: UUID,
    *,
    for_update: bool = False,
) -> Candidate | None:
    """Resolve a historical alias to its canonical candidate.

    Merge writes are flattened to one level, but the small loop makes old or
    manually repaired data safe without allowing cycles to spin forever.
    """

    seen: set[UUID] = set()
    current_id = candidate_id
    for _ in range(20):
        if current_id in seen:
            return None
        seen.add(current_id)
        stmt = select(Candidate).where(
            Candidate.id == current_id,
            Candidate.organization_id == organization_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        candidate = (await db.execute(stmt)).scalar_one_or_none()
        if candidate is None:
            return None
        if candidate.merged_into_candidate_id is None:
            if candidate.merged_at is not None:
                return None
            return candidate
        current_id = candidate.merged_into_candidate_id
    return None


async def candidate_history_ids(
    db: AsyncSession, canonical_id: UUID, organization_id: UUID
) -> list[UUID]:
    return list(
        (
            await db.execute(
                select(Candidate.id).where(
                    Candidate.organization_id == organization_id,
                    (
                        (Candidate.id == canonical_id)
                        | (Candidate.merged_into_candidate_id == canonical_id)
                    ),
                )
            )
        ).scalars()
    )


async def merge_candidates(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    primary_candidate_id: UUID,
    duplicate_candidate_ids: list[UUID],
    score_winners: dict[UUID, UUID],
) -> CandidateMergeOutcome:
    """Merge duplicate rows while retaining original evidence and history.

    Non-conflicting pipeline applications and all movable child records are
    rehomed.  Conflicting score rows remain on the hidden alias so no evaluation
    is silently discarded; the primary application's score is the visible one.
    """

    duplicate_ids = list(dict.fromkeys(duplicate_candidate_ids))
    if primary_candidate_id in duplicate_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The primary candidate cannot also be a duplicate.",
        )
    await acquire_legacy_private_object_write_fence(db)
    all_ids = [primary_candidate_id, *duplicate_ids]
    locked = list(
        (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.id.in_(all_ids),
                    Candidate.organization_id == organization_id,
                )
                .order_by(Candidate.id)
                .with_for_update()
            )
        ).scalars()
    )
    if len(locked) != len(all_ids):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate not found.")
    by_id = {candidate.id: candidate for candidate in locked}
    primary = by_id[primary_candidate_id]
    if primary.merged_at is not None or primary.merged_into_candidate_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Primary candidate is an alias.")
    aliases = [by_id[candidate_id] for candidate_id in duplicate_ids]
    if any(
        alias.merged_at is not None or alias.merged_into_candidate_id is not None
        for alias in aliases
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Candidate was already merged.")

    # A candidate that already owns historical aliases cannot become an alias
    # of another primary without either flattening every descendant and both
    # reversal snapshots atomically or losing the oldest source provenance.
    # Until that more complex cross-batch contract exists, reject the chained
    # merge before capturing or mutating any graph. Existing canonical
    # candidates may still accept additional fresh duplicates.
    # Use the Core table for this history invariant. Assignment-scoped ORM
    # loader criteria intentionally hide candidates outside the manager's
    # active applications, but a hidden historical alias still makes its
    # canonical parent ineligible to become another alias. PostgreSQL tenant
    # RLS and the explicit organization predicate remain authoritative.
    candidate_table = Candidate.__table__
    descendant = (
        await db.execute(
            select(candidate_table.c.id)
            .where(
                candidate_table.c.organization_id == organization_id,
                candidate_table.c.merged_into_candidate_id.in_(duplicate_ids),
            )
            .order_by(candidate_table.c.id)
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if descendant is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_history_must_remain_canonical"},
        )

    # Resolve the wrapping key and validate serializability before changing a
    # single domain row. Production therefore fails closed if durable reversal
    # cannot be guaranteed.
    cipher = _candidate_merge_cipher()
    batch_id = uuid4()
    pre_state = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=all_ids,
        lock=True,
    )
    try:
        private_object_bindings = _validate_candidate_private_object_bindings(
            pre_state,
            organization_id=organization_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_private_object_binding_invalid"},
        ) from exc
    private_objects = list(
        (
            await db.execute(
                select(PrivateObject)
                .where(
                    PrivateObject.organization_id == organization_id,
                    PrivateObject.id.in_(set(private_object_bindings.values())),
                )
                .execution_options(populate_existing=True)
                .order_by(PrivateObject.id)
                .with_for_update()
            )
        ).scalars()
    )
    private_objects_by_id = {item.id: item for item in private_objects}
    if set(private_objects_by_id) != set(private_object_bindings.values()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_private_object_binding_invalid"},
        )
    _canonical_json(pre_state)

    # Repair either side of the transitional Application/Score compatibility
    # contract before moving candidate foreign keys. This makes merge behavior
    # identical on migrated databases (where a trigger mirrors Score) and the
    # metadata-created test schema (where services perform the synchronization).
    applications = list(
        (
            await db.execute(
                select(Application)
                .where(
                    Application.organization_id == organization_id,
                    Application.candidate_id.in_(all_ids),
                )
                .order_by(Application.id)
                .with_for_update()
            )
        ).scalars()
    )
    for application in applications:
        await ensure_legacy_score_for_application(db, application)

    scores = list(
        (
            await db.execute(
                select(Score)
                .where(
                    Score.organization_id == organization_id,
                    Score.candidate_id.in_(all_ids),
                )
                .order_by(Score.id)
                .with_for_update()
            )
        ).scalars()
    )
    existing_application_score_ids = {
        application.legacy_score_id
        for application in applications
        if application.legacy_score_id is not None
    }
    application_ids = {application.id for application in applications}
    for score in scores:
        if score.id in existing_application_score_ids:
            continue
        linked = await ensure_application_link(
            db,
            organization_id=organization_id,
            candidate_id=score.candidate_id,
            position_id=score.position_id,
            source=by_id[score.candidate_id].source,
            owner_user_id=score.owner_user_id,
            actor_user_id=actor_user_id,
            actor_kind="human",
            reason="merge_reconciliation",
            occurred_at=score.scored_at,
        )
        if linked.application.id not in application_ids:
            applications.append(linked.application)
            application_ids.add(linked.application.id)
    scores_by_position: dict[UUID, list[Score]] = {}
    for score in scores:
        scores_by_position.setdefault(score.position_id, []).append(score)
    conflicts = {
        position_id: position_scores
        for position_id, position_scores in scores_by_position.items()
        if len(position_scores) > 1
    }
    missing_winners = sorted(set(conflicts) - set(score_winners))
    extra_winners = sorted(set(score_winners) - set(conflicts))
    if missing_winners or extra_winners:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "Every score conflict requires one explicit winner.",
                "missing_position_ids": [str(value) for value in missing_winners],
                "unexpected_position_ids": [str(value) for value in extra_winners],
            },
        )
    all_candidate_ids = set(all_ids)
    for position_id, winner_candidate_id in score_winners.items():
        allowed = {score.candidate_id for score in conflicts.get(position_id, [])}
        if (
            winner_candidate_id not in allowed
            or winner_candidate_id not in all_candidate_ids
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "A score winner must belong to that conflicting application.",
            )

    # The score compatibility trigger normally follows legacy Score writes.
    # This merge reconciles every historical cycle itself, so trigger-driven
    # row movement would race the deterministic renumbering below.
    await db.execute(text("SET LOCAL hr_agent.skip_application_sync = '1'"))
    merged_at = await _rehome_application_cycles(
        db,
        applications=applications,
        scores=scores,
        score_winners=score_winners,
        primary_candidate_id=primary_candidate_id,
        actor_user_id=actor_user_id,
    )
    application_ids = [application.id for application in applications]
    if application_ids:
        # Application-linked activity is part of the exact cycle identity.
        # Candidate-global history stays on its original alias until reversal.
        await db.execute(
            update(CandidateActivity)
            .where(
                CandidateActivity.organization_id == organization_id,
                CandidateActivity.application_id.in_(application_ids),
            )
            .values(candidate_id=primary_candidate_id)
        )

    # Make the chosen score row visible on the canonical candidate.  When a
    # primary score already exists and an alias wins, a transient holding row
    # lets us swap candidate FKs without violating the immediate unique
    # (candidate, position) constraint.  Score IDs and their historical
    # activity subjects remain attached to the same evaluation records.
    holding_candidate: Candidate | None = None
    for position_id, position_scores in scores_by_position.items():
        visible = next(
            (
                score
                for score in position_scores
                if score.candidate_id == primary_candidate_id
            ),
            None,
        )
        if len(position_scores) == 1:
            if visible is None:
                position_scores[0].candidate_id = primary_candidate_id
            continue
        winner_id = score_winners[position_id]
        winner = next(
            score for score in position_scores if score.candidate_id == winner_id
        )
        if winner.candidate_id == primary_candidate_id:
            continue
        if visible is None:
            winner.candidate_id = primary_candidate_id
            continue
        if holding_candidate is None:
            holding_candidate = Candidate(
                id=uuid4(),
                organization_id=organization_id,
                source="system",
                source_ref=f"merge-swap-{uuid4().hex}",
                merged_at=datetime.now(UTC),
                merged_by_user_id=actor_user_id,
            )
            db.add(holding_candidate)
            await db.flush()
        visible.candidate_id = holding_candidate.id
        await db.flush()
        winner.candidate_id = primary_candidate_id
        await db.flush()
        visible.candidate_id = winner_id
        await db.flush()
    if holding_candidate is not None:
        await db.delete(holding_candidate)
        await db.flush()

    tags = list(
        (
            await db.execute(
                select(CandidateTag)
                .where(
                    CandidateTag.organization_id == organization_id,
                    CandidateTag.candidate_id.in_(all_ids),
                )
                .order_by(CandidateTag.id)
                .with_for_update()
            )
        ).scalars()
    )
    primary_labels = {
        tag.label for tag in tags if tag.candidate_id == primary_candidate_id
    }
    for tag in tags:
        if tag.candidate_id == primary_candidate_id:
            continue
        if tag.label in primary_labels:
            await db.delete(tag)
        else:
            tag.candidate_id = primary_candidate_id
            primary_labels.add(tag.label)

    # Pool membership is current state rather than immutable history. Collapse
    # duplicate membership in the same pool while retaining a deterministic row
    # and the most advanced status. The append-only activities for removed rows
    # remain attached to the alias history.
    pool_members = list(
        (
            await db.execute(
                select(TalentPoolMember)
                .where(
                    TalentPoolMember.organization_id == organization_id,
                    TalentPoolMember.candidate_id.in_(all_ids),
                )
                .order_by(
                    TalentPoolMember.pool_id,
                    TalentPoolMember.created_at,
                    TalentPoolMember.id,
                )
                .with_for_update()
            )
        ).scalars()
    )
    members_by_pool: dict[UUID, list[TalentPoolMember]] = {}
    for member in pool_members:
        members_by_pool.setdefault(member.pool_id, []).append(member)
    member_status_priority = {"paused": 0, "active": 1, "converted": 2}
    for members in members_by_pool.values():
        retained = next(
            (
                member
                for member in members
                if member.candidate_id == primary_candidate_id
            ),
            members[0],
        )
        retained.status = max(
            (member.status for member in members),
            key=lambda value: member_status_priority[value],
        )
        for member in members:
            if member is not retained:
                await db.delete(member)
        await db.flush()
        retained.candidate_id = primary_candidate_id
        await db.flush()

    # A candidate may have been enrolled from two source records before they
    # were recognized as duplicates. Keep one enrollment associated with the
    # sequence for idempotency, and detach conflicting historical rows from the
    # sequence while preserving their snapshots and scheduled-count evidence.
    enrollments = list(
        (
            await db.execute(
                select(TalentNurtureEnrollment)
                .where(
                    TalentNurtureEnrollment.organization_id == organization_id,
                    TalentNurtureEnrollment.candidate_id.in_(all_ids),
                )
                .order_by(
                    TalentNurtureEnrollment.sequence_id,
                    TalentNurtureEnrollment.created_at,
                    TalentNurtureEnrollment.id,
                )
                .with_for_update()
            )
        ).scalars()
    )
    enrollments_by_sequence: dict[UUID | None, list[TalentNurtureEnrollment]] = {}
    for enrollment in enrollments:
        enrollments_by_sequence.setdefault(enrollment.sequence_id, []).append(
            enrollment
        )
    for sequence_id, sequence_enrollments in enrollments_by_sequence.items():
        if sequence_id is None:
            for enrollment in sequence_enrollments:
                enrollment.candidate_id = primary_candidate_id
            continue
        retained = next(
            (
                enrollment
                for enrollment in sequence_enrollments
                if enrollment.candidate_id == primary_candidate_id
            ),
            sequence_enrollments[0],
        )
        for enrollment in sequence_enrollments:
            if enrollment is retained:
                enrollment.candidate_id = primary_candidate_id
            else:
                enrollment.sequence_id = None
                enrollment.candidate_id = primary_candidate_id
    await db.flush()

    # Contact values are sensitive domain data, so duplicate contact points are
    # collapsed without copying them into audit payloads. Existing threads and
    # messages are first repointed to the retained primary contact.
    contacts = list(
        (
            await db.execute(
                select(CandidateContactPoint)
                .where(
                    CandidateContactPoint.organization_id == organization_id,
                    CandidateContactPoint.candidate_id.in_(all_ids),
                )
                .order_by(CandidateContactPoint.id)
                .with_for_update()
            )
        ).scalars()
    )
    retained_contacts = {
        (contact.channel, contact.normalized_value): contact
        for contact in contacts
        if contact.candidate_id == primary_candidate_id
    }
    for contact in contacts:
        if contact.candidate_id == primary_candidate_id:
            continue
        key = (contact.channel, contact.normalized_value)
        retained = retained_contacts.get(key)
        if retained is not None:
            await db.execute(
                update(CommunicationThread)
                .where(CommunicationThread.contact_point_id == contact.id)
                .values(contact_point_id=retained.id)
            )
            await db.execute(
                update(CommunicationMessage)
                .where(CommunicationMessage.contact_point_id == contact.id)
                .values(contact_point_id=retained.id)
            )
            retained.is_primary = retained.is_primary or contact.is_primary
            retained.is_opted_out = retained.is_opted_out or contact.is_opted_out
            if retained.opted_out_at is None:
                retained.opted_out_at = contact.opted_out_at
            await db.delete(contact)
        else:
            contact.candidate_id = primary_candidate_id
            retained_contacts[key] = contact

    for communication_model in (CommunicationThread, CommunicationMessage):
        await db.execute(
            update(communication_model)
            .where(communication_model.candidate_id.in_(duplicate_ids))
            .values(candidate_id=primary_candidate_id)
        )

    # These child tables have no uniqueness constraint on candidate_id and can
    # be rehomed directly.  Their own contents remain untouched.
    for model in (
        CandidateNote,
        CandidateProcessingJob,
        Interview,
        InterviewRecording,
        TaskSubmission,
        ChatSession,
        ScorecardAssignment,
        HiringDecision,
        SchedulingLink,
        InterviewAppointment,
        WorkflowRun,
        SourceCandidateReceipt,
        ProviderAssessment,
        CandidateOffer,
        CandidateHandoff,
    ):
        await db.execute(
            update(model)
            .where(model.candidate_id.in_(duplicate_ids))
            .values(candidate_id=primary_candidate_id)
        )

    notifications = list(
        (
            await db.execute(
                select(Notification).where(
                    Notification.organization_id == organization_id,
                    Notification.payload["candidate_id"].astext.in_(
                        [str(candidate_id) for candidate_id in duplicate_ids]
                    ),
                )
            )
        ).scalars()
    )
    for notification in notifications:
        payload = dict(notification.payload or {})
        payload["candidate_id"] = str(primary_candidate_id)
        notification.payload = payload

    # A registered CV is one candidate-owned object, not a path that may be
    # copied between rows. Move the complete binding when the primary has no
    # CV, and keep exact registry ownership for every distinct document that
    # remains on an alias. Legacy paths are likewise moved instead of copied;
    # repeated legacy references are cleared so one blob never remains
    # reachable through several merged candidate rows.
    claimed_document_keys: dict[str, tuple[str, Candidate]] = {}
    primary_has_document = (
        primary.private_object_id is not None or primary.pdf_path is not None
    )
    if primary.private_object_id is not None:
        primary_object = private_objects_by_id[primary.private_object_id]
        claimed_document_keys[primary_object.object_key] = ("registered", primary)
    elif primary.pdf_path is not None:
        claimed_document_keys[primary.pdf_path] = ("legacy", primary)

    for alias in aliases:
        if primary.raw_cv is None and alias.raw_cv is not None:
            primary.raw_cv = alias.raw_cv
        if primary.parsed_cv is None and alias.parsed_cv is not None:
            primary.parsed_cv = alias.parsed_cv
        if alias.private_object_id is not None:
            alias_object = private_objects_by_id[alias.private_object_id]
            primary_legacy_matches_registered = (
                primary.private_object_id is None
                and primary.pdf_path == alias_object.object_key
            )
            if not primary_has_document or primary_legacy_matches_registered:
                primary.pdf_path = alias.pdf_path
                primary.private_object_id = alias.private_object_id
                alias.pdf_path = None
                alias.private_object_id = None
                alias_object.resource_id = primary_candidate_id
                primary_has_document = True
                claimed_document_keys[alias_object.object_key] = (
                    "registered",
                    primary,
                )
            else:
                existing = claimed_document_keys.get(alias_object.object_key)
                if existing is not None and existing[0] == "legacy":
                    legacy_owner = existing[1]
                    if legacy_owner is not primary:
                        legacy_owner.pdf_path = None
                claimed_document_keys[alias_object.object_key] = (
                    "registered",
                    alias,
                )
        elif alias.pdf_path is not None:
            legacy_path = alias.pdf_path
            if not primary_has_document:
                primary.pdf_path = legacy_path
                alias.pdf_path = None
                primary_has_document = True
                claimed_document_keys[legacy_path] = ("legacy", primary)
            elif legacy_path in claimed_document_keys:
                alias.pdf_path = None
            else:
                claimed_document_keys[legacy_path] = ("legacy", alias)
        if primary.phone_normalized is None and alias.phone_normalized is not None:
            primary.phone_normalized = alias.phone_normalized
        if primary.email_normalized is None and alias.email_normalized is not None:
            primary.email_normalized = alias.email_normalized
        if primary.github_login is None and alias.github_login is not None:
            primary.github_login = alias.github_login
            primary.github_data = alias.github_data
            primary.github_fetched_at = alias.github_fetched_at
        primary.is_favorite = primary.is_favorite or alias.is_favorite
        if primary.viewed_at is None or (
            alias.viewed_at is not None and alias.viewed_at > primary.viewed_at
        ):
            primary.viewed_at = alias.viewed_at
        alias.merged_into_candidate_id = primary_candidate_id
        alias.merged_at = merged_at
        alias.merged_by_user_id = actor_user_id

    await db.flush()
    post_state = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=all_ids,
        tracked=(pre_state,),
        lock=False,
    )
    try:
        _validate_candidate_private_object_bindings(
            post_state,
            organization_id=organization_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_private_object_binding_invalid"},
        ) from exc
    post_digest = _state_digest(post_state)
    encrypted = cipher.encrypt_json(
        {
            "snapshot_version": SNAPSHOT_VERSION,
            "organization_id": str(organization_id),
            "batch_id": str(batch_id),
            "primary_candidate_id": str(primary_candidate_id),
            "alias_candidate_ids": [str(value) for value in duplicate_ids],
            "pre_state": pre_state,
            "post_state": post_state,
        },
        aad=_snapshot_aad(organization_id, batch_id),
    )
    batch = CandidateMergeBatch(
        id=batch_id,
        organization_id=organization_id,
        primary_candidate_id=primary_candidate_id,
        created_by_user_id=actor_user_id,
        status="merged",
        version=1,
        snapshot_version=SNAPSHOT_VERSION,
        member_count=len(all_ids),
        snapshot_ciphertext=encrypted.ciphertext,
        snapshot_nonce=encrypted.nonce,
        snapshot_key_id=encrypted.key_id,
        post_state_sha256=post_digest,
        created_at=merged_at,
    )
    db.add(batch)
    db.add_all(
        [
            CandidateMergeBatchMember(
                organization_id=organization_id,
                batch_id=batch_id,
                candidate_id=candidate_id,
                role="primary" if candidate_id == primary_candidate_id else "alias",
            )
            for candidate_id in all_ids
        ]
    )
    append_candidate_activity(
        db,
        organization_id=organization_id,
        candidate_id=primary_candidate_id,
        actor_user_id=actor_user_id,
        event_type="candidate_merged",
        subject_type="candidate",
        subject_id=primary_candidate_id,
        payload={
            "merged_candidate_ids": [
                str(candidate_id) for candidate_id in duplicate_ids
            ],
            "merged_count": len(duplicate_ids),
            "batch_id": str(batch_id),
        },
    )
    await record_usage(
        db,
        organization_id=organization_id,
        metric="candidates",
        amount=-len(duplicate_ids),
        kind="refund",
        idempotency_key=f"candidate:merge:{batch_id}",
        resource_type="candidate",
        resource_id=primary_candidate_id,
        metadata={"merged_count": len(duplicate_ids)},
        enforce_limit=False,
    )
    await db.commit()
    return CandidateMergeOutcome(
        batch_id=batch_id,
        primary_candidate_id=primary_candidate_id,
        merged_candidate_ids=tuple(duplicate_ids),
    )


async def _update_full_row(
    db: AsyncSession,
    model: type,
    row_id: UUID,
    encoded: dict[str, dict[str, Any]],
    *,
    exclude: frozenset[str] = frozenset(),
) -> None:
    values = _decoded_row(encoded)
    values = {
        key: value
        for key, value in values.items()
        if key not in {"id", "organization_id"} and key not in exclude
    }
    if values:
        await db.execute(
            update(model.__table__)
            .where(model.__table__.c.id == row_id)
            .values(**values)
        )


async def _insert_missing_pre_rows(
    db: AsyncSession,
    model: type,
    pre_rows: dict[str, Any],
    post_rows: dict[str, Any],
) -> None:
    for row_id in sorted(set(pre_rows) - set(post_rows)):
        await db.execute(
            insert(model.__table__).values(**_decoded_row(pre_rows[row_id]))
        )


async def _restore_applications_and_scores(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    pre_state: dict[str, Any],
    post_state: dict[str, Any],
) -> None:
    pre_applications = pre_state["applications"]
    post_applications = post_state["applications"]
    created_application_ids = {
        UUID(value) for value in set(post_applications) - set(pre_applications)
    }
    if created_application_ids:
        await db.execute(
            delete(Application.__table__).where(
                Application.__table__.c.id.in_(created_application_ids),
                Application.__table__.c.organization_id == organization_id,
            )
        )

    # Break legacy score references before removing compatibility rows or
    # moving scores through temporary candidates.
    pre_application_ids = [UUID(value) for value in pre_applications]
    if pre_application_ids:
        await db.execute(
            update(Application.__table__)
            .where(
                Application.__table__.c.id.in_(pre_application_ids),
                Application.__table__.c.organization_id == organization_id,
            )
            .values(legacy_score_id=None)
        )

    pre_scores = pre_state["scores"]
    post_scores = post_state["scores"]
    created_score_ids = {UUID(value) for value in set(post_scores) - set(pre_scores)}
    if created_score_ids:
        await db.execute(
            delete(Score.__table__).where(
                Score.__table__.c.id.in_(created_score_ids),
                Score.__table__.c.organization_id == organization_id,
            )
        )

    # Clear immediate (candidate, position) score uniqueness with one bounded
    # holding identity per row, then restore every score in deterministic ID
    # order. The Score compatibility trigger is suppressed for the transaction.
    holding_ids: list[UUID] = []
    for score_id in sorted(pre_scores):
        holding_id = uuid4()
        holding_ids.append(holding_id)
        db.add(
            Candidate(
                id=holding_id,
                organization_id=organization_id,
                source="system",
                source_ref=f"merge-reversal-{holding_id.hex}",
                merged_at=datetime.now(UTC),
                merged_by_user_id=actor_user_id,
            )
        )
    if holding_ids:
        await db.flush()
        for score_id, holding_id in zip(sorted(pre_scores), holding_ids, strict=True):
            await db.execute(
                update(Score.__table__)
                .where(
                    Score.__table__.c.id == UUID(score_id),
                    Score.__table__.c.organization_id == organization_id,
                )
                .values(candidate_id=holding_id)
            )
        for score_id in sorted(pre_scores):
            await _update_full_row(db, Score, UUID(score_id), pre_scores[score_id])
        await db.execute(
            delete(Candidate.__table__).where(
                Candidate.__table__.c.id.in_(holding_ids),
                Candidate.__table__.c.organization_id == organization_id,
            )
        )

    if pre_applications:
        max_cycle = max(
            int(_decode_column(row["cycle_number"]))
            for row in [*pre_applications.values(), *post_applications.values()]
        )
        temporary_base = max_cycle + len(pre_applications) + 1
        for offset, application_id in enumerate(sorted(pre_applications)):
            desired = _decoded_row(pre_applications[application_id])
            await db.execute(
                update(Application.__table__)
                .where(
                    Application.__table__.c.id == UUID(application_id),
                    Application.__table__.c.organization_id == organization_id,
                )
                .values(
                    candidate_id=desired["candidate_id"],
                    cycle_number=temporary_base + offset,
                    legacy_score_id=None,
                )
            )
        for application_id in sorted(pre_applications):
            await _update_full_row(
                db,
                Application,
                UUID(application_id),
                pre_applications[application_id],
            )


async def _restore_pre_merge_graph(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    pre_state: dict[str, Any],
    post_state: dict[str, Any],
) -> None:
    await db.execute(text("SET LOCAL hr_agent.skip_application_sync = '1'"))
    await _restore_applications_and_scores(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        pre_state=pre_state,
        post_state=post_state,
    )
    restored_application_ids = [UUID(value) for value in pre_state["applications"]]
    if restored_application_ids:
        restored_candidate_id = (
            select(Application.candidate_id)
            .where(
                Application.id == CandidateActivity.application_id,
                Application.organization_id == organization_id,
            )
            .scalar_subquery()
        )
        await db.execute(
            update(CandidateActivity)
            .where(
                CandidateActivity.organization_id == organization_id,
                CandidateActivity.application_id.in_(restored_application_ids),
            )
            .values(candidate_id=restored_candidate_id)
        )

    # Merge deletes only deduplicated tags, memberships, and contacts. Recreate
    # those rows first so communication FK restoration never points at a
    # missing contact and original row IDs/timestamps remain exact.
    for key, model in (
        ("candidate_tags", CandidateTag),
        ("pool_members", TalentPoolMember),
        ("contact_points", CandidateContactPoint),
    ):
        await _insert_missing_pre_rows(db, model, pre_state[key], post_state[key])

    # No other candidate child may be created or deleted by merge. The exact
    # post-state guard has already proven each row still exists unchanged since
    # merge; restore its original full state, including ownership and timestamps.
    ordered_keys = [
        "candidate_tags",
        "pool_members",
        "contact_points",
        "nurture_enrollments",
        "candidate_notes",
        "candidate_processing_jobs",
        "interviews",
        "interview_recordings",
        "task_submissions",
        "chat_sessions",
        "scorecard_assignments",
        "hiring_decisions",
        "scheduling_links",
        "interview_appointments",
        "workflow_runs",
        "source_candidate_receipts",
        "provider_assessments",
        "offers",
        "handoffs",
        "communication_threads",
        "communication_messages",
    ]
    for key in ordered_keys:
        model = _DIRECT_CANDIDATE_MODELS[key]
        for row_id in sorted(pre_state[key]):
            await _update_full_row(db, model, UUID(row_id), pre_state[key][row_id])

    for row_id in sorted(pre_state["notifications"]):
        await _update_full_row(
            db,
            Notification,
            UUID(row_id),
            pre_state["notifications"][row_id],
        )
    # New snapshots guard registry rows alongside candidate columns. Restore
    # the exact pre-merge owner only after snapshot validation has proved that
    # every object belonged to the corresponding candidate in this tenant.
    for row_id in sorted(pre_state.get("private_objects", {})):
        await _update_full_row(
            db,
            PrivateObject,
            UUID(row_id),
            pre_state["private_objects"][row_id],
        )
    for row_id in sorted(pre_state["candidates"]):
        await _update_full_row(
            db,
            Candidate,
            UUID(row_id),
            pre_state["candidates"][row_id],
        )


def _validated_snapshot_document(
    value: Any,
    *,
    batch: CandidateMergeBatch,
    member_ids: set[UUID],
) -> tuple[list[UUID], dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise TypeError("Candidate merge snapshot is invalid")
    aliases = [UUID(str(item)) for item in value.get("alias_candidate_ids", [])]
    if (
        value.get("snapshot_version") != SNAPSHOT_VERSION
        or value.get("organization_id") != str(batch.organization_id)
        or value.get("batch_id") != str(batch.id)
        or value.get("primary_candidate_id") != str(batch.primary_candidate_id)
        or len(aliases) + 1 != batch.member_count
        or set(aliases) | {batch.primary_candidate_id} != member_ids
        or not isinstance(value.get("pre_state"), dict)
        or not isinstance(value.get("post_state"), dict)
    ):
        raise ValueError("Candidate merge snapshot is invalid")
    pre_state = value["pre_state"]
    post_state = value["post_state"]
    pre_has_private_objects = "private_objects" in pre_state
    post_has_private_objects = "private_objects" in post_state
    if pre_has_private_objects != post_has_private_objects:
        raise ValueError("Candidate merge snapshot is invalid")
    if pre_has_private_objects:
        _validate_candidate_private_object_bindings(
            pre_state,
            organization_id=batch.organization_id,
        )
        _validate_candidate_private_object_bindings(
            post_state,
            organization_id=batch.organization_id,
        )
    return aliases, pre_state, post_state


def _validated_snapshot_candidate_rows(
    state: dict[str, Any],
    *,
    organization_id: UUID,
    member_ids: set[UUID],
) -> dict[UUID, dict[str, Any]]:
    """Decode exactly the candidate members covered by an authenticated snapshot."""

    try:
        rows = state["candidates"]
        if not isinstance(rows, dict) or set(rows) != {
            str(candidate_id) for candidate_id in member_ids
        }:
            raise ValueError
        decoded: dict[UUID, dict[str, Any]] = {}
        for candidate_id in member_ids:
            values = _decoded_row(rows[str(candidate_id)])
            if (
                values.get("id") != candidate_id
                or values.get("organization_id") != organization_id
            ):
                raise ValueError
            pdf_path = values.get("pdf_path")
            private_object_id = values.get("private_object_id")
            if pdf_path is not None and (not isinstance(pdf_path, str) or not pdf_path):
                raise ValueError
            if private_object_id is not None and (
                not isinstance(private_object_id, UUID) or pdf_path is None
            ):
                raise ValueError
            decoded[candidate_id] = values
        return decoded
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Candidate merge snapshot is invalid") from exc


def _snapshot_document_owner_id(
    rows: dict[UUID, dict[str, Any]],
    *,
    pdf_path: str,
    private_object_id: UUID | None,
) -> UUID:
    """Require one post-merge owner for the source document identity."""

    owners = _document_owner_ids(
        rows,
        pdf_path=pdf_path,
        private_object_id=private_object_id,
    )
    if len(owners) != 1:
        raise ValueError("Candidate merge document ownership is ambiguous")
    return owners[0]


def _document_owner_ids(
    rows: dict[UUID, dict[str, Any]],
    *,
    pdf_path: str,
    private_object_id: UUID | None,
) -> list[UUID]:
    """Return document owners without choosing among ambiguous bindings."""

    if private_object_id is not None:
        return [
            candidate_id
            for candidate_id, values in rows.items()
            if values.get("private_object_id") == private_object_id
        ]
    return [
        candidate_id
        for candidate_id, values in rows.items()
        if values.get("pdf_path") == pdf_path
    ]


async def _validated_current_candidate_documents(
    db: AsyncSession,
    *,
    organization_id: UUID,
    member_ids: set[UUID],
) -> tuple[
    dict[UUID, Candidate],
    dict[UUID, PrivateObject],
    dict[str, Any],
]:
    """Load and validate current candidate/registry bindings in one tenant."""

    candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.organization_id == organization_id,
                    Candidate.id.in_(member_ids),
                )
                .execution_options(include_deleted=True, populate_existing=True)
                .order_by(Candidate.id)
            )
        ).scalars()
    )
    if len(candidates) != len(member_ids) or any(
        candidate.deleted_at is not None for candidate in candidates
    ):
        raise ValueError("Candidate merge members are unavailable")
    candidate_rows = {
        str(candidate.id): _row_state(candidate) for candidate in candidates
    }
    private_object_ids = _candidate_private_object_ids({"candidates": candidate_rows})
    private_objects = list(
        (
            await db.execute(
                select(PrivateObject)
                .where(
                    PrivateObject.organization_id == organization_id,
                    PrivateObject.id.in_(private_object_ids),
                )
                .execution_options(populate_existing=True)
                .order_by(PrivateObject.id)
            )
        ).scalars()
    )
    current_state = {
        "candidates": candidate_rows,
        "private_objects": {
            str(private_object.id): _row_state(private_object)
            for private_object in private_objects
        },
    }
    _validate_candidate_private_object_bindings(
        current_state,
        organization_id=organization_id,
    )
    return (
        {candidate.id: candidate for candidate in candidates},
        {private_object.id: private_object for private_object in private_objects},
        current_state,
    )


async def _legacy_cutover_document_owner_id(
    db: AsyncSession,
    *,
    organization_id: UUID,
    snapshot_reference: str,
    expected_owner_id: UUID,
    current_candidates: dict[UUID, Candidate],
    current_private_objects: dict[UUID, PrivateObject],
) -> UUID:
    """Prove a legacy reference became the owner's current registered object."""

    owner = current_candidates.get(expected_owner_id)
    if owner is None or owner.private_object_id is None or owner.pdf_path is None:
        raise ValueError("Legacy cutover owner is unavailable")
    private_object = current_private_objects.get(owner.private_object_id)
    if private_object is None:
        raise ValueError("Legacy cutover registry binding is unavailable")
    try:
        normalized_key = normalize_legacy_private_key(
            snapshot_reference,
            organization_id=organization_id,
            category="candidates",
        )
    except LegacyPrivateObjectError as exc:
        raise ValueError("Legacy cutover provenance is invalid") from exc
    replacements = list(
        (
            await db.execute(
                select(LegacyPrivateObjectDeletion)
                .where(
                    LegacyPrivateObjectDeletion.organization_id == organization_id,
                    LegacyPrivateObjectDeletion.category == "candidates",
                    LegacyPrivateObjectDeletion.resource_type == "candidate",
                    LegacyPrivateObjectDeletion.resource_id == expected_owner_id,
                    LegacyPrivateObjectDeletion.normalized_key == normalized_key,
                    LegacyPrivateObjectDeletion.reason == "replacement",
                )
                .limit(2)
            )
        ).scalars()
    )
    if len(replacements) != 1:
        raise ValueError("Legacy cutover provenance is unavailable")
    replacement = replacements[0]
    if (
        replacement.size_bytes != private_object.size_bytes
        or replacement.sha256_snapshot != private_object.sha256
    ):
        raise ValueError("Legacy cutover provenance does not match the object")
    return expected_owner_id


async def resolve_merged_source_original_document(
    db: AsyncSession,
    *,
    organization_id: UUID,
    canonical_candidate_id: UUID,
    source_candidate_id: UUID,
) -> Candidate | None:
    """Resolve an original merged-source document through authenticated history.

    Snapshot paths are evidence only. The caller receives the single live
    candidate owner and must still resolve that owner's tenant-bound registry
    binding (or constrained legacy reference) before reading storage.
    """

    canonical = await resolve_canonical_candidate(
        db,
        canonical_candidate_id,
        organization_id,
    )
    if canonical is None:
        raise _merged_source_document_unavailable()

    if source_candidate_id == canonical.id:
        try:
            (
                current_candidates,
                _private_objects,
                _current_state,
            ) = await _validated_current_candidate_documents(
                db,
                organization_id=organization_id,
                member_ids={canonical.id},
            )
        except ValueError as exc:
            raise _merged_source_document_unavailable() from exc
        owner = current_candidates[canonical.id]
        if owner.pdf_path is None and owner.private_object_id is None:
            return None
        if owner.pdf_path is None:
            raise _merged_source_document_unavailable()
        return owner

    batches = list(
        (
            await db.execute(
                select(CandidateMergeBatch)
                .join(
                    CandidateMergeBatchMember,
                    and_(
                        CandidateMergeBatchMember.organization_id
                        == CandidateMergeBatch.organization_id,
                        CandidateMergeBatchMember.batch_id == CandidateMergeBatch.id,
                    ),
                )
                .where(
                    CandidateMergeBatch.organization_id == organization_id,
                    CandidateMergeBatch.primary_candidate_id == canonical.id,
                    CandidateMergeBatch.status == "merged",
                    CandidateMergeBatchMember.organization_id == organization_id,
                    CandidateMergeBatchMember.candidate_id == source_candidate_id,
                    CandidateMergeBatchMember.role == "alias",
                )
                .order_by(
                    CandidateMergeBatch.created_at.desc(),
                    CandidateMergeBatch.id.desc(),
                )
                .limit(2)
            )
        ).scalars()
    )
    if len(batches) != 1:
        raise _merged_source_document_unavailable()
    batch = batches[0]

    members = list(
        (
            await db.execute(
                select(CandidateMergeBatchMember)
                .where(
                    CandidateMergeBatchMember.organization_id == organization_id,
                    CandidateMergeBatchMember.batch_id == batch.id,
                )
                .order_by(CandidateMergeBatchMember.candidate_id)
            )
        ).scalars()
    )
    member_ids = {member.candidate_id for member in members}
    roles = {member.candidate_id: member.role for member in members}
    if (
        batch.version != 1
        or batch.snapshot_version != SNAPSHOT_VERSION
        or batch.reversed_at is not None
        or len(members) != batch.member_count
        or len(member_ids) != len(members)
        or roles.get(batch.primary_candidate_id) != "primary"
        or roles.get(source_candidate_id) != "alias"
        or sum(role == "primary" for role in roles.values()) != 1
    ):
        raise _merged_source_document_unavailable()

    try:
        decrypted = _candidate_merge_cipher().decrypt_json(
            batch.snapshot_ciphertext,
            batch.snapshot_nonce,
            aad=_snapshot_aad(organization_id, batch.id),
            key_id=batch.snapshot_key_id,
        )
        aliases, pre_state, post_state = _validated_snapshot_document(
            decrypted,
            batch=batch,
            member_ids=member_ids,
        )
        if source_candidate_id not in aliases:
            raise ValueError
        if _state_digest(post_state) != batch.post_state_sha256:
            raise ValueError
        pre_rows = _validated_snapshot_candidate_rows(
            pre_state,
            organization_id=organization_id,
            member_ids=member_ids,
        )
        post_rows = _validated_snapshot_candidate_rows(
            post_state,
            organization_id=organization_id,
            member_ids=member_ids,
        )
        source_pre_state = pre_rows[source_candidate_id]
        pdf_path = source_pre_state.get("pdf_path")
        private_object_id = source_pre_state.get("private_object_id")
        has_original_document = pdf_path is not None or private_object_id is not None
        if has_original_document and not isinstance(pdf_path, str):
            raise ValueError
        if private_object_id is not None and "private_objects" not in pre_state:
            raise ValueError

        (
            current_candidates,
            current_private_objects,
            current_state,
        ) = await _validated_current_candidate_documents(
            db,
            organization_id=organization_id,
            member_ids=member_ids,
        )
        primary = current_candidates[batch.primary_candidate_id]
        if (
            primary.merged_into_candidate_id is not None
            or primary.merged_at is not None
        ):
            raise ValueError
        if any(
            current_candidates[alias_id].merged_into_candidate_id
            != batch.primary_candidate_id
            or current_candidates[alias_id].merged_at is None
            for alias_id in aliases
        ):
            raise ValueError
        if not has_original_document:
            return None
        if not isinstance(pdf_path, str):
            raise ValueError
        post_owner_id = _snapshot_document_owner_id(
            post_rows,
            pdf_path=pdf_path,
            private_object_id=private_object_id,
        )
        current_rows = {
            UUID(candidate_id): _decoded_row(row)
            for candidate_id, row in current_state["candidates"].items()
        }
        if private_object_id is not None:
            current_owner_id = _snapshot_document_owner_id(
                current_rows,
                pdf_path=pdf_path,
                private_object_id=private_object_id,
            )
        else:
            current_legacy_owners = _document_owner_ids(
                current_rows,
                pdf_path=pdf_path,
                private_object_id=None,
            )
            if len(current_legacy_owners) > 1:
                raise ValueError
            current_owner_id = (
                current_legacy_owners[0]
                if current_legacy_owners
                else await _legacy_cutover_document_owner_id(
                    db,
                    organization_id=organization_id,
                    snapshot_reference=pdf_path,
                    expected_owner_id=post_owner_id,
                    current_candidates=current_candidates,
                    current_private_objects=current_private_objects,
                )
            )
        if current_owner_id != post_owner_id:
            raise ValueError

        if private_object_id is not None:
            current_object = current_private_objects.get(private_object_id)
            post_object_row = post_state["private_objects"].get(str(private_object_id))
            if current_object is None or post_object_row is None:
                raise ValueError
            expected_object = _decoded_row(post_object_row)
            current_object_values = _decoded_row(
                current_state["private_objects"][str(private_object_id)]
            )
            stable_fields = (
                "id",
                "organization_id",
                "category",
                "object_key",
                "size_bytes",
                "sha256",
                "state",
                "resource_type",
                "resource_id",
            )
            if any(
                current_object_values.get(field) != expected_object.get(field)
                for field in stable_fields
            ):
                raise ValueError

        owner = current_candidates[current_owner_id]
        if owner.pdf_path is None:
            raise ValueError
        return owner
    except (IntegrationCryptoError, KeyError, TypeError, ValueError) as exc:
        raise _merged_source_document_unavailable() from exc


async def reverse_candidate_merge(
    db: AsyncSession,
    *,
    organization_id: UUID,
    actor_user_id: UUID,
    actor_role: str,
    batch_id: UUID,
    expected_version: int,
) -> CandidateMergeReversalOutcome:
    """Atomically restore an unchanged merge exactly once.

    Reversal is deliberately stricter than an ordinary edit. Any later change
    in the involved hiring graph makes the batch stale and requires a human to
    resolve that new state instead of having undo overwrite it.
    """

    await acquire_legacy_private_object_write_fence(db)
    batch = (
        await db.execute(
            select(CandidateMergeBatch)
            .where(
                CandidateMergeBatch.id == batch_id,
                CandidateMergeBatch.organization_id == organization_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate merge not found.")
    if batch.created_by_user_id != actor_user_id and actor_role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Merge reversal is not allowed.")
    if batch.status != "merged":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_already_reversed"},
        )
    if batch.version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "candidate_merge_stale_version",
                "current_version": batch.version,
            },
        )

    members = list(
        (
            await db.execute(
                select(CandidateMergeBatchMember)
                .where(
                    CandidateMergeBatchMember.organization_id == organization_id,
                    CandidateMergeBatchMember.batch_id == batch.id,
                )
                .order_by(CandidateMergeBatchMember.candidate_id)
                .with_for_update()
            )
        ).scalars()
    )
    member_ids = {member.candidate_id for member in members}
    if len(members) != batch.member_count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_snapshot_unavailable"},
        )
    locked_candidates = list(
        (
            await db.execute(
                select(Candidate)
                .where(
                    Candidate.organization_id == organization_id,
                    Candidate.id.in_(member_ids),
                )
                .execution_options(include_deleted=True)
                .order_by(Candidate.id)
                .with_for_update()
            )
        ).scalars()
    )
    if len(locked_candidates) != len(member_ids) or any(
        candidate.deleted_at is not None for candidate in locked_candidates
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_snapshot_unavailable"},
        )

    try:
        decrypted = _candidate_merge_cipher().decrypt_json(
            batch.snapshot_ciphertext,
            batch.snapshot_nonce,
            aad=_snapshot_aad(organization_id, batch.id),
            key_id=batch.snapshot_key_id,
        )
        aliases, pre_state, post_state = _validated_snapshot_document(
            decrypted,
            batch=batch,
            member_ids=member_ids,
        )
    except (IntegrationCryptoError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_snapshot_unavailable"},
        ) from exc

    if _state_digest(post_state) != batch.post_state_sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_snapshot_unavailable"},
        )
    current_state = await _capture_merge_graph(
        db,
        organization_id=organization_id,
        candidate_ids=list(member_ids),
        tracked=(pre_state, post_state),
        lock=True,
    )
    try:
        _validate_candidate_private_object_bindings(
            current_state,
            organization_id=organization_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_diverged"},
        ) from exc
    guarded_current_state = current_state
    if "private_objects" not in post_state:
        # Snapshots created before registry-aware candidate merging did not
        # mutate or digest private_objects. Keep those batches reversible while
        # still requiring every current registered reference to have an exact
        # tenant/category/resource binding.
        guarded_current_state = dict(current_state)
        guarded_current_state.pop("private_objects", None)
    if _state_digest(guarded_current_state) != batch.post_state_sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "candidate_merge_diverged"},
        )

    reversed_at = datetime.now(UTC)
    async with db.begin_nested():
        await _restore_pre_merge_graph(
            db,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            pre_state=pre_state,
            post_state=post_state,
        )
        batch.status = "reversed"
        batch.version = 2
        batch.reversed_at = reversed_at
        batch.reversed_by_user_id = actor_user_id
        for candidate_id in [batch.primary_candidate_id, *aliases]:
            append_candidate_activity(
                db,
                organization_id=organization_id,
                candidate_id=candidate_id,
                actor_user_id=actor_user_id,
                event_type="candidate_merge_reversed",
                subject_type="candidate_merge_batch",
                subject_id=batch.id,
                payload={
                    "batch_id": str(batch.id),
                    "restored_count": len(aliases),
                },
                occurred_at=reversed_at,
            )
        await record_usage(
            db,
            organization_id=organization_id,
            metric="candidates",
            amount=len(aliases),
            kind="adjustment",
            idempotency_key=f"candidate:merge-reversal:{batch.id}",
            resource_type="candidate_merge_batch",
            resource_id=batch.id,
            metadata={"restored_count": len(aliases)},
            enforce_limit=False,
        )
        await db.flush()
    await db.commit()
    return CandidateMergeReversalOutcome(
        batch_id=batch.id,
        primary_candidate_id=batch.primary_candidate_id,
        restored_candidate_ids=tuple(aliases),
        version=2,
        reversed_at=reversed_at,
    )
