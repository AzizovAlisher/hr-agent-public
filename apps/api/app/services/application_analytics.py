"""Event-derived hiring analytics with one reproducible application cohort."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application import (
    APPLICATION_STAGES,
    Application,
    ApplicationEvaluation,
    ApplicationStageEvent,
)
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentInvitation,
    AssessmentResult,
)
from app.models.candidate import Candidate
from app.models.integration import IntegrationConnection
from app.models.operations import UsageLedgerEntry
from app.models.position import Position
from app.models.provider_assessment import (
    ProviderAssessment,
    ProviderAssessmentResult,
)
from app.models.requisition import CandidateOffer
from app.models.user import User
from app.schemas.applications import (
    AnalyticsDrilldownDimension,
    ApplicationAnalyticsAiDecisionProvenance,
    ApplicationAnalyticsAiDecisions,
    ApplicationAnalyticsAiProviderUsage,
    ApplicationAnalyticsAiUsage,
    ApplicationAnalyticsAssessments,
    ApplicationAnalyticsBreakdown,
    ApplicationAnalyticsDaily,
    ApplicationAnalyticsDrilldownPage,
    ApplicationAnalyticsDrilldownRow,
    ApplicationAnalyticsFilterOptions,
    ApplicationAnalyticsIntegrationHealth,
    ApplicationAnalyticsOffers,
    ApplicationAnalyticsOut,
    ApplicationAnalyticsOwnerWorkload,
    ApplicationAnalyticsReconciliation,
    ApplicationAnalyticsSourceQuality,
    ApplicationAnalyticsTiming,
    ApplicationStageAnalytics,
)
from app.services.pagination import (
    collection_cursor_scope,
    cursor_datetime,
    cursor_uuid,
    decode_collection_cursor,
    encode_collection_cursor,
)

ACTIVE_STAGES = frozenset({"new", "screening", "interview", "offer"})
TERMINAL_STAGES = frozenset({"hired", "rejected"})
FUNNEL_STAGES = ("new", "screening", "interview", "offer", "hired")
ATTENTION_INTEGRATION_STATES = frozenset(
    {"unavailable", "disconnected", "degraded", "expired", "revoked"}
)
SAFE_PROVENANCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}$")


@dataclass(frozen=True)
class _ApplicationState:
    application: Application
    initial: ApplicationStageEvent
    current_stage: str
    current_stage_entered_at: datetime
    first_entry_at: dict[str, datetime]
    disposition_code: str | None


@dataclass(frozen=True)
class _ApplicationOutcome:
    assessment_started: bool = False
    assessment_completed: bool = False
    assessment_dropped_off: bool = False
    interviewed: bool = False
    offer_sent: bool = False
    hired: bool = False
    quality_score: float | None = None


def _normalize_window(
    start_at: datetime | None,
    end_at: datetime | None,
    timezone_name: str,
) -> tuple[datetime, datetime, ZoneInfo]:
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown analytics timezone"
        ) from exc
    now = datetime.now(UTC)
    end = end_at or now
    start = start_at or (end - timedelta(days=30))
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Analytics timestamps must include a timezone",
        )
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if start >= end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Analytics start_at must be before end_at",
        )
    if end - start > timedelta(days=366):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Analytics range cannot exceed 366 days",
        )
    return start, end, timezone


def _initial_source(
    application: Application, event: ApplicationStageEvent | None
) -> str:
    value = (event.details or {}).get("source") if event is not None else None
    # Cohort dimensions must never fall back to mutable application fields.
    # The release migration snapshots legacy initial events; an unrepairable
    # missing snapshot remains explicit rather than rewriting history later.
    return str(value or "unknown")


def _initial_owner(
    application: Application, event: ApplicationStageEvent | None
) -> UUID | None:
    value = (event.details or {}).get("owner_user_id") if event is not None else None
    raw = value
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _owner_key(owner_user_id: UUID | None) -> str:
    return str(owner_user_id) if owner_user_id is not None else "unassigned"


def _owner_label(owner_user_id: UUID | None, labels: dict[UUID, str]) -> str:
    return labels.get(owner_user_id, str(owner_user_id)) if owner_user_id else "Unassigned"


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 2)


def _median(values: Iterable[float]) -> float | None:
    materialized = list(values)
    return round(float(median(materialized)), 4) if materialized else None


def _mean(values: Iterable[float]) -> float | None:
    materialized = list(values)
    if not materialized:
        return None
    return round(sum(materialized) / len(materialized), 4)


def _quality_score(scores: object) -> float | None:
    """Return one normalized, non-sensitive 0-100 aggregate score."""

    if not isinstance(scores, dict):
        return None
    value = next(
        (
            scores[key]
            for key in ("overall", "overall_score", "percentage")
            if key in scores
        ),
        None,
    )
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not normalized.is_finite() or normalized < 0 or normalized > 100:
        return None
    return round(float(normalized), 4)


def _provenance_identifier(value: object) -> str:
    """Expose only bounded provider/model/version identifiers, never prompt text."""

    if not isinstance(value, str):
        return "unknown"
    cleaned = value.strip()
    return cleaned if SAFE_PROVENANCE_IDENTIFIER.fullmatch(cleaned) else "unknown"


def _ai_decision_analytics(
    evaluations: list[ApplicationEvaluation],
) -> ApplicationAnalyticsAiDecisions:
    recommendations = {
        row.id: row
        for row in evaluations
        if row.kind == "ai_scoring"
        and row.actor_kind == "ai"
        and row.status == "completed"
    }
    all_by_id = {row.id: row for row in evaluations}
    reviewed_recommendations: set[UUID] = set()
    overridden_recommendations: set[UUID] = set()

    def reviewed_ai_id(row: ApplicationEvaluation) -> UUID | None:
        seen: set[UUID] = set()
        current = row
        while current.id not in seen:
            seen.add(current.id)
            review = current.provenance.get("review") if current.provenance else None
            raw_target = review.get("reviewed_evaluation_id") if isinstance(review, dict) else None
            target_id: UUID | None = None
            if raw_target is not None:
                try:
                    target_id = UUID(str(raw_target))
                except (TypeError, ValueError):
                    target_id = None
            target_id = target_id or current.supersedes_evaluation_id
            target = all_by_id.get(target_id) if target_id is not None else None
            if (
                target is None
                or target.application_id != row.application_id
                or target.kind != row.kind
            ):
                return None
            if target.id in recommendations:
                return target.id
            current = target
        return None

    for row in evaluations:
        if (
            row.kind != "ai_scoring"
            or row.actor_kind != "human"
            or row.status not in {"reviewed", "voided"}
        ):
            continue
        recommendation_id = reviewed_ai_id(row)
        if recommendation_id is None:
            continue
        reviewed_recommendations.add(recommendation_id)
        recommendation = recommendations[recommendation_id]
        if row.status == "voided" or row.scores != recommendation.scores:
            overridden_recommendations.add(recommendation_id)

    grouped: dict[tuple[str, str, str], set[UUID]] = defaultdict(set)
    for recommendation_id, row in recommendations.items():
        provenance = row.provenance or {}
        key = (
            _provenance_identifier(provenance.get("provider")),
            _provenance_identifier(provenance.get("model")),
            _provenance_identifier(provenance.get("prompt_version")),
        )
        grouped[key].add(recommendation_id)

    by_provenance: list[ApplicationAnalyticsAiDecisionProvenance] = []
    for (provider, model, prompt_version), recommendation_ids in sorted(
        grouped.items()
    ):
        reviews = len(recommendation_ids & reviewed_recommendations)
        overrides = len(recommendation_ids & overridden_recommendations)
        total = len(recommendation_ids)
        by_provenance.append(
            ApplicationAnalyticsAiDecisionProvenance(
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                recommendations=total,
                human_reviews=reviews,
                human_overrides=overrides,
                review_rate_percent=_percent(reviews, total),
                override_rate_percent=_percent(overrides, total),
            )
        )

    recommendation_count = len(recommendations)
    review_count = len(reviewed_recommendations)
    override_count = len(overridden_recommendations)
    return ApplicationAnalyticsAiDecisions(
        recommendations=recommendation_count,
        human_reviews=review_count,
        human_overrides=override_count,
        review_rate_percent=_percent(review_count, recommendation_count),
        override_rate_percent=_percent(override_count, recommendation_count),
        by_provenance=by_provenance,
    )


def _breakdown(
    counter: Counter[str], labels: dict[str, str] | None = None
) -> list[ApplicationAnalyticsBreakdown]:
    labels = labels or {}
    return [
        ApplicationAnalyticsBreakdown(
            key=key,
            label=labels.get(key),
            count=count,
        )
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _event_state(
    application: Application,
    events: list[ApplicationStageEvent],
    *,
    before: datetime | None,
    stage_durations: dict[str, list[float]] | None = None,
) -> _ApplicationState | None:
    """Reconstruct state while ignoring duplicate and corrupt initial events."""

    eligible = [event for event in events if before is None or event.occurred_at < before]
    initial = next((event for event in eligible if event.from_stage is None), None)
    if initial is None:
        return None

    current_stage: str | None = None
    current_entered_at: datetime | None = None
    first_entry_at: dict[str, datetime] = {}
    disposition_code: str | None = None
    for event in eligible:
        if event.from_stage is None:
            if current_stage is not None:
                continue
        elif current_stage is None:
            continue
        if current_stage == event.to_stage:
            continue
        if (
            stage_durations is not None
            and current_stage is not None
            and current_entered_at is not None
            and event.occurred_at >= current_entered_at
        ):
            stage_durations[current_stage].append(
                (event.occurred_at - current_entered_at).total_seconds() / 3600
            )
        current_stage = event.to_stage
        current_entered_at = event.occurred_at
        first_entry_at.setdefault(current_stage, event.occurred_at)
        if current_stage == "rejected":
            value = (event.details or {}).get("disposition_code")
            disposition_code = str(value or "unspecified")

    if current_stage is None or current_entered_at is None:
        return None
    if (
        before is not None
        and stage_durations is not None
        and current_stage in ACTIVE_STAGES
        and before >= current_entered_at
    ):
        # Terminal residence is not process time; active residence is its age
        # at the report cutoff and is useful workload evidence.
        stage_durations[current_stage].append(
            (before - current_entered_at).total_seconds() / 3600
        )
    return _ApplicationState(
        application=application,
        initial=initial,
        current_stage=current_stage,
        current_stage_entered_at=current_entered_at,
        first_entry_at=first_entry_at,
        disposition_code=disposition_code,
    )


def _encode_cursor(
    applied_at: datetime, application_id: UUID, *, scope: str
) -> str:
    return encode_collection_cursor(
        "analytics.applications.drilldown",
        scope=scope,
        boundary={
            "applied_at": applied_at.astimezone(UTC).isoformat(),
            "id": str(application_id),
        },
    )


def _decode_cursor(value: str, *, scope: str) -> tuple[datetime, str]:
    payload = decode_collection_cursor(
        value,
        namespace="analytics.applications.drilldown",
        scope=scope,
        boundary_keys={"applied_at", "id"},
        detail="Invalid analytics cursor",
    )
    return (
        cursor_datetime(payload, "applied_at", detail="Invalid analytics cursor"),
        str(cursor_uuid(payload, detail="Invalid analytics cursor")),
    )


def _drilldown_page(
    states: list[_ApplicationState],
    *,
    dimension: AnalyticsDrilldownDimension | None,
    key: str | None,
    cursor: str | None,
    limit: int,
    owner_labels: dict[UUID, str],
    outcomes: dict[UUID, _ApplicationOutcome],
    cursor_scope: str,
) -> ApplicationAnalyticsDrilldownPage | None:
    if dimension is None:
        if key is not None or cursor is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "A drilldown dimension is required for key or cursor",
            )
        return None
    if dimension == "all":
        if key is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "The all drilldown does not accept a key",
            )
    elif not key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A drilldown key is required",
        )
    if dimension in {"stage_current", "stage_entered"} and key not in APPLICATION_STAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Unknown analytics stage",
        )

    def matches(state: _ApplicationState) -> bool:
        if dimension == "all":
            return True
        if dimension == "stage_current":
            return state.current_stage == key
        if dimension == "stage_entered":
            return key in state.first_entry_at
        if dimension == "source":
            return _initial_source(state.application, state.initial) == key
        if dimension == "owner":
            return _owner_key(_initial_owner(state.application, state.initial)) == key
        return state.disposition_code == key

    selected = sorted(
        (state for state in states if matches(state)),
        key=lambda state: (state.initial.occurred_at, str(state.application.id)),
        reverse=True,
    )
    total = len(selected)
    if cursor is not None:
        cursor_at, cursor_id = _decode_cursor(cursor, scope=cursor_scope)
        selected = [
            state
            for state in selected
            if (state.initial.occurred_at, str(state.application.id))
            < (cursor_at, cursor_id)
        ]
    visible = selected[: limit + 1]
    has_more = len(visible) > limit
    visible = visible[:limit]
    items = [
        ApplicationAnalyticsDrilldownRow(
            application_id=state.application.id,
            candidate_id=state.application.candidate_id,
            position_id=state.application.position_id,
            applied_at=state.initial.occurred_at,
            current_stage=state.current_stage,
            current_stage_entered_at=state.current_stage_entered_at,
            source=_initial_source(state.application, state.initial),
            owner_user_id=_initial_owner(state.application, state.initial),
            owner_label=_owner_label(
                _initial_owner(state.application, state.initial), owner_labels
            ),
            disposition_code=state.disposition_code,
            assessment_started=outcomes[state.application.id].assessment_started,
            assessment_completed=outcomes[state.application.id].assessment_completed,
            assessment_dropped_off=outcomes[
                state.application.id
            ].assessment_dropped_off,
            interviewed=outcomes[state.application.id].interviewed,
            offer_sent=outcomes[state.application.id].offer_sent,
            hired=outcomes[state.application.id].hired,
            quality_score=outcomes[state.application.id].quality_score,
        )
        for state in visible
    ]
    next_cursor = (
        _encode_cursor(
            visible[-1].initial.occurred_at,
            visible[-1].application.id,
            scope=cursor_scope,
        )
        if has_more and visible
        else None
    )
    return ApplicationAnalyticsDrilldownPage(
        dimension=dimension,
        key=key,
        total=total,
        items=items,
        next_cursor=next_cursor,
    )


async def application_analytics(
    db: AsyncSession,
    organization_id: UUID,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    timezone_name: str = "UTC",
    position_id: UUID | None = None,
    source: str | None = None,
    owner_user_id: UUID | None = None,
    owner_unassigned: bool = False,
    include_organization_operations: bool = False,
    include_quality_scores: bool = False,
    include_ai_decisions: bool = False,
    drilldown_dimension: AnalyticsDrilldownDimension | None = None,
    drilldown_key: str | None = None,
    drilldown_cursor: str | None = None,
    drilldown_limit: int = 50,
) -> ApplicationAnalyticsOut:
    """Build all cohort metrics from the earliest canonical stage event."""

    start, end, timezone = _normalize_window(start_at, end_at, timezone_name)
    if not 1 <= drilldown_limit <= 100_000:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Analytics drilldown limit is out of range",
        )

    ranked_initials = (
        select(
            ApplicationStageEvent.id.label("event_id"),
            ApplicationStageEvent.application_id.label("application_id"),
            ApplicationStageEvent.occurred_at.label("occurred_at"),
            func.row_number()
            .over(
                partition_by=ApplicationStageEvent.application_id,
                order_by=(
                    ApplicationStageEvent.occurred_at,
                    ApplicationStageEvent.id,
                ),
            )
            .label("initial_rank"),
        )
        .where(
            ApplicationStageEvent.organization_id == organization_id,
            ApplicationStageEvent.from_stage.is_(None),
        )
        .subquery()
    )
    cohort_stmt = (
        select(Application, ApplicationStageEvent)
        .join(ranked_initials, ranked_initials.c.application_id == Application.id)
        .join(ApplicationStageEvent, ApplicationStageEvent.id == ranked_initials.c.event_id)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(
            Application.organization_id == organization_id,
            Candidate.organization_id == organization_id,
            Candidate.merged_at.is_(None),
            ranked_initials.c.initial_rank == 1,
            ranked_initials.c.occurred_at >= start,
            ranked_initials.c.occurred_at < end,
        )
    )
    cohort_rows = list((await db.execute(cohort_stmt)).all())
    base_applications = {application.id: application for application, _ in cohort_rows}

    expected_stmt = (
        select(Application)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(
            Application.organization_id == organization_id,
            Candidate.organization_id == organization_id,
            Candidate.merged_at.is_(None),
            Application.applied_at >= start,
            Application.applied_at < end,
        )
    )
    expected_all = list((await db.execute(expected_stmt)).scalars())
    all_application_ids = set(base_applications) | {item.id for item in expected_all}

    events: list[ApplicationStageEvent] = []
    if all_application_ids:
        events = list(
            (
                await db.execute(
                    select(ApplicationStageEvent)
                    .where(
                        ApplicationStageEvent.organization_id == organization_id,
                        ApplicationStageEvent.application_id.in_(all_application_ids),
                    )
                    .order_by(
                        ApplicationStageEvent.application_id,
                        ApplicationStageEvent.occurred_at,
                        ApplicationStageEvent.id,
                    )
                )
            ).scalars()
        )
    events_by_application: dict[UUID, list[ApplicationStageEvent]] = defaultdict(list)
    for event in events:
        events_by_application[event.application_id].append(event)

    all_position_ids = {
        application.position_id for application in [*base_applications.values(), *expected_all]
    }
    positions: dict[UUID, Position] = {}
    if all_position_ids:
        position_rows = list(
            (
                await db.execute(
                    select(Position).where(
                        Position.organization_id == organization_id,
                        Position.id.in_(all_position_ids),
                    )
                )
            ).scalars()
        )
        positions = {position.id: position for position in position_rows}

    base_owner_ids = {
        owner_id
        for application, initial in cohort_rows
        if (owner_id := _initial_owner(application, initial)) is not None
    }
    owner_labels: dict[UUID, str] = {}
    if base_owner_ids:
        owner_rows = (
            await db.execute(
                select(User.id, User.email).where(
                    User.organization_id == organization_id,
                    User.id.in_(base_owner_ids),
                )
            )
        ).all()
        owner_labels = {row.id: row.email for row in owner_rows}

    base_states: list[_ApplicationState] = []
    for application in base_applications.values():
        state = _event_state(
            application,
            events_by_application.get(application.id, []),
            before=end,
        )
        if state is not None:
            base_states.append(state)

    source_facets = Counter(
        _initial_source(state.application, state.initial) for state in base_states
    )
    owner_facets = Counter(
        _owner_key(_initial_owner(state.application, state.initial))
        for state in base_states
    )
    position_facets = Counter(str(state.application.position_id) for state in base_states)
    owner_label_map = {
        key: _owner_label(None if key == "unassigned" else UUID(key), owner_labels)
        for key in owner_facets
    }
    position_label_map = {
        str(position_key): position.title for position_key, position in positions.items()
    }

    def included(state: _ApplicationState) -> bool:
        application = state.application
        if position_id is not None and application.position_id != position_id:
            return False
        if source is not None and _initial_source(application, state.initial) != source:
            return False
        initial_owner = _initial_owner(application, state.initial)
        if owner_unassigned and initial_owner is not None:
            return False
        if owner_user_id is not None and initial_owner != owner_user_id:
            return False
        return True

    states = [state for state in base_states if included(state)]
    cohort_ids = {state.application.id for state in states}

    def expected_included(application: Application) -> bool:
        initial = next(
            (
                event
                for event in events_by_application.get(application.id, [])
                if event.from_stage is None
            ),
            None,
        )
        if position_id is not None and application.position_id != position_id:
            return False
        if source is not None and _initial_source(application, initial) != source:
            return False
        initial_owner = _initial_owner(application, initial)
        if owner_unassigned and initial_owner is not None:
            return False
        if owner_user_id is not None and initial_owner != owner_user_id:
            return False
        return True

    expected = [application for application in expected_all if expected_included(application)]
    no_initial = 0
    duplicate_initials = 0
    stage_mismatches = 0
    for application in expected:
        application_events = events_by_application.get(application.id, [])
        initial_events = [event for event in application_events if event.from_stage is None]
        if not initial_events:
            no_initial += 1
        duplicate_initials += max(0, len(initial_events) - 1)
        latest = _event_state(application, application_events, before=None)
        if latest is not None and latest.current_stage != application.stage:
            stage_mismatches += 1

    owner_counts: Counter[str] = Counter()
    position_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()
    current_counts: Counter[str] = Counter()
    entered_applications: dict[str, set[UUID]] = {
        stage_name: set() for stage_name in APPLICATION_STAGES
    }
    time_to_stage: dict[str, list[float]] = {
        stage_name: [] for stage_name in APPLICATION_STAGES
    }
    stage_durations: dict[str, list[float]] = {
        stage_name: [] for stage_name in APPLICATION_STAGES
    }
    daily: dict[date, Counter[str]] = defaultdict(Counter)
    time_to_hire: list[float] = []
    first_hire_by_position: dict[UUID, datetime] = {}
    state_by_id: dict[UUID, _ApplicationState] = {}

    for filtered_state in states:
        application = filtered_state.application
        application_events = [
            event
            for event in events_by_application.get(application.id, [])
            if event.occurred_at < end
        ]
        state = _event_state(
            application,
            application_events,
            before=end,
            stage_durations=stage_durations,
        )
        if state is None:
            continue
        state_by_id[application.id] = state
        owner_value = _initial_owner(application, state.initial)
        owner_counts[_owner_key(owner_value)] += 1
        position_counts[str(application.position_id)] += 1
        daily[state.initial.occurred_at.astimezone(timezone).date()]["applications"] += 1
        current_counts[state.current_stage] += 1
        if state.disposition_code is not None:
            disposition_counts[state.disposition_code] += 1
        for stage_name, entered_at in state.first_entry_at.items():
            entered_applications[stage_name].add(application.id)
            elapsed = max(
                0.0,
                (entered_at - state.initial.occurred_at).total_seconds() / 3600,
            )
            time_to_stage[stage_name].append(elapsed)
        hired_at = state.first_entry_at.get("hired")
        if hired_at is not None:
            time_to_hire.append(
                max(
                    0.0,
                    (hired_at - state.initial.occurred_at).total_seconds() / 3600,
                )
            )
            previous = first_hire_by_position.get(application.position_id)
            if previous is None or hired_at < previous:
                first_hire_by_position[application.position_id] = hired_at

        reconstructed_stage: str | None = None
        for event in application_events:
            if event.from_stage is None:
                if reconstructed_stage is not None:
                    continue
            elif reconstructed_stage is None:
                continue
            if reconstructed_stage == event.to_stage:
                continue
            was_initial = reconstructed_stage is None
            reconstructed_stage = event.to_stage
            if not was_initial and start <= event.occurred_at < end:
                bucket = daily[event.occurred_at.astimezone(timezone).date()]
                bucket["transitions"] += 1
                if event.to_stage in TERMINAL_STAGES:
                    bucket[event.to_stage] += 1

    stages: list[ApplicationStageAnalytics] = []
    previous_entered: int | None = None
    for stage_name in APPLICATION_STAGES:
        entered = len(entered_applications[stage_name])
        conversion = None
        if stage_name in FUNNEL_STAGES and stage_name != "new":
            conversion = _percent(entered, previous_entered or 0)
        stages.append(
            ApplicationStageAnalytics(
                stage=stage_name,
                entered=entered,
                current=current_counts[stage_name],
                conversion_from_previous_percent=conversion,
                median_hours_from_application=_median(time_to_stage[stage_name]),
                median_hours_in_stage=_median(stage_durations[stage_name]),
            )
        )
        if stage_name in FUNNEL_STAGES:
            previous_entered = entered

    first_day = start.astimezone(timezone).date()
    last_day = (end - timedelta(microseconds=1)).astimezone(timezone).date()
    day = first_day
    daily_rows: list[ApplicationAnalyticsDaily] = []
    while day <= last_day:
        counts = daily[day]
        daily_rows.append(
            ApplicationAnalyticsDaily(
                date=day,
                applications=counts["applications"],
                transitions=counts["transitions"],
                hired=counts["hired"],
                rejected=counts["rejected"],
            )
        )
        day += timedelta(days=1)

    time_to_fill: list[float] = []
    for filled_position_id, hired_at in first_hire_by_position.items():
        position = positions.get(filled_position_id)
        if position is not None and position.created_at <= hired_at:
            time_to_fill.append(
                (hired_at - position.created_at).total_seconds() / 3600
            )

    offers_sent = offers_accepted = offers_declined = 0
    offered_application_ids: set[UUID] = set()
    if cohort_ids:
        offers = list(
            (
                await db.execute(
                    select(CandidateOffer).where(
                        CandidateOffer.organization_id == organization_id,
                        CandidateOffer.application_id.in_(cohort_ids),
                        CandidateOffer.sent_at.is_not(None),
                        CandidateOffer.sent_at < end,
                    )
                )
            ).scalars()
        )
        for offer in offers:
            offers_sent += 1
            offered_application_ids.add(offer.application_id)
            if offer.responded_at is not None and offer.responded_at < end:
                offers_accepted += int(offer.status == "accepted")
                offers_declined += int(offer.status == "declined")

    assessment_invitations = assessment_started = assessment_completed = 0
    assessment_invited_application_ids: set[UUID] = set()
    assessment_started_application_ids: set[UUID] = set()
    assessment_completed_application_ids: set[UUID] = set()
    provider_quality_candidates: list[tuple[UUID, datetime, float]] = []
    if cohort_ids:
        invitations = list(
            (
                await db.execute(
                    select(AssessmentInvitation).where(
                        AssessmentInvitation.organization_id == organization_id,
                        AssessmentInvitation.application_id.in_(cohort_ids),
                        AssessmentInvitation.created_at < end,
                    )
                )
            ).scalars()
        )
        assessment_invitations = len(invitations)
        assessment_invited_application_ids.update(
            invitation.application_id for invitation in invitations
        )
        invitation_ids = {invitation.id for invitation in invitations}
        invitation_application_ids = {
            invitation.id: invitation.application_id for invitation in invitations
        }
        attempts: list[AssessmentAttempt] = []
        if invitation_ids:
            attempts = list(
                (
                    await db.execute(
                        select(AssessmentAttempt).where(
                            AssessmentAttempt.organization_id == organization_id,
                            AssessmentAttempt.invitation_id.in_(invitation_ids),
                            AssessmentAttempt.started_at < end,
                        )
                    )
                ).scalars()
            )
        started_ids = {attempt.invitation_id for attempt in attempts}
        completed_ids = {
            attempt.invitation_id
            for attempt in attempts
            if attempt.submitted_at is not None and attempt.submitted_at < end
        }
        completed_ids.update(
            invitation.id
            for invitation in invitations
            if invitation.completed_at is not None and invitation.completed_at < end
        )
        # A durable completion is also evidence that the invitation started,
        # even if an old/externally repaired row no longer has its attempt.
        started_ids.update(completed_ids)
        assessment_started = len(started_ids)
        assessment_completed = len(completed_ids)
        assessment_started_application_ids.update(
            invitation_application_ids[invitation_id]
            for invitation_id in started_ids
            if invitation_id in invitation_application_ids
        )
        assessment_completed_application_ids.update(
            invitation_application_ids[invitation_id]
            for invitation_id in completed_ids
            if invitation_id in invitation_application_ids
        )

        # Specialist-provider invitations are independent application-bound
        # records. Count them once here instead of joining them to native
        # invitations, which would multiply either side for applications that
        # use both assessment paths.
        provider_rows = list(
            (
                await db.execute(
                    select(ProviderAssessment, ProviderAssessmentResult)
                    .outerjoin(
                        ProviderAssessmentResult,
                        ProviderAssessmentResult.provider_assessment_id
                        == ProviderAssessment.id,
                    )
                    .where(
                        ProviderAssessment.organization_id == organization_id,
                        ProviderAssessment.application_id.in_(cohort_ids),
                        ProviderAssessment.created_at < end,
                    )
                )
            ).all()
        )
        assessment_invitations += len(provider_rows)
        for provider_assessment, provider_result in provider_rows:
            assessment_invited_application_ids.add(provider_assessment.application_id)
            started_before_cutoff = (
                provider_assessment.started_at is not None
                and provider_assessment.started_at < end
            ) or (
                provider_assessment.submitted_at is not None
                and provider_assessment.submitted_at < end
            )
            completed_before_cutoff = (
                provider_assessment.submitted_at is not None
                and provider_assessment.submitted_at < end
            ) or (
                provider_result is not None
                and provider_result.completed_at < end
            )
            assessment_started += int(started_before_cutoff)
            assessment_completed += int(completed_before_cutoff)
            if started_before_cutoff or completed_before_cutoff:
                assessment_started_application_ids.add(
                    provider_assessment.application_id
                )
            if completed_before_cutoff:
                assessment_completed_application_ids.add(
                    provider_assessment.application_id
                )
            if (
                include_quality_scores
                and provider_result is not None
                and provider_result.score is not None
                and (score := _quality_score({"overall": provider_result.score}))
                is not None
            ):
                provider_quality_candidates.append(
                    (
                        provider_assessment.application_id,
                        provider_result.completed_at,
                        score,
                    )
                )

    quality_by_application: dict[UUID, float] = {}
    quality_order: dict[UUID, tuple[int, datetime, int, str]] = {}

    def remember_quality(
        application_id: UUID,
        value: float,
        occurred_at: datetime,
        *,
        priority: int,
        version: int,
        record_id: UUID,
    ) -> None:
        # ApplicationEvaluation is the canonical aggregate. Normalized
        # assessment/provider results are a fallback only when no aggregate
        # evaluation exists for the cycle.
        ordering = (priority, occurred_at, version, str(record_id))
        if ordering > quality_order.get(
            application_id,
            (-1, datetime.min.replace(tzinfo=UTC), -1, ""),
        ):
            quality_order[application_id] = ordering
            quality_by_application[application_id] = value

    for application_id, completed_at, score in provider_quality_candidates:
        remember_quality(
            application_id,
            score,
            completed_at,
            priority=1,
            version=1,
            record_id=application_id,
        )

    if include_quality_scores and cohort_ids:
        assessment_results = list(
            (
                await db.execute(
                    select(AssessmentResult).where(
                        AssessmentResult.organization_id == organization_id,
                        AssessmentResult.application_id.in_(cohort_ids),
                        AssessmentResult.graded_at < end,
                    )
                )
            ).scalars()
        )
        for result in assessment_results:
            score = _quality_score({"percentage": result.percentage})
            if score is not None:
                remember_quality(
                    result.application_id,
                    score,
                    result.graded_at,
                    priority=1,
                    version=1,
                    record_id=result.id,
                )

    evaluations: list[ApplicationEvaluation] = []
    if (include_quality_scores or include_ai_decisions) and cohort_ids:
        evaluations = list(
            (
                await db.execute(
                    select(ApplicationEvaluation)
                    .where(
                        ApplicationEvaluation.organization_id == organization_id,
                        ApplicationEvaluation.application_id.in_(cohort_ids),
                        ApplicationEvaluation.evaluated_at < end,
                    )
                    .order_by(
                        ApplicationEvaluation.evaluated_at,
                        ApplicationEvaluation.version,
                        ApplicationEvaluation.id,
                    )
                )
            ).scalars()
        )
    if include_quality_scores:
        evaluations_by_id = {evaluation.id: evaluation for evaluation in evaluations}
        for evaluation in evaluations:
            if evaluation.status not in {"completed", "reviewed"}:
                continue
            if evaluation.kind == "ai_scoring" and evaluation.actor_kind == "human":
                review = (
                    evaluation.provenance.get("review")
                    if evaluation.provenance
                    else None
                )
                raw_target = (
                    review.get("reviewed_evaluation_id")
                    if isinstance(review, dict)
                    else None
                )
                try:
                    target_id = UUID(str(raw_target)) if raw_target is not None else None
                except (TypeError, ValueError):
                    target_id = None
                target_id = target_id or evaluation.supersedes_evaluation_id
                target = evaluations_by_id.get(target_id) if target_id else None
                if (
                    target is None
                    or target.application_id != evaluation.application_id
                    or target.kind != evaluation.kind
                ):
                    continue
            score = _quality_score(evaluation.scores)
            if score is None:
                continue
            remember_quality(
                evaluation.application_id,
                score,
                evaluation.evaluated_at,
                priority=2,
                version=evaluation.version,
                record_id=evaluation.id,
            )

    ai_decision_report = (
        _ai_decision_analytics(evaluations) if include_ai_decisions else None
    )
    assessment_dropped_application_ids = (
        assessment_started_application_ids - assessment_completed_application_ids
    )
    outcomes: dict[UUID, _ApplicationOutcome] = {
        application_id: _ApplicationOutcome(
            assessment_started=application_id
            in assessment_started_application_ids,
            assessment_completed=application_id
            in assessment_completed_application_ids,
            assessment_dropped_off=application_id
            in assessment_dropped_application_ids,
            interviewed="interview" in state.first_entry_at,
            offer_sent=application_id in offered_application_ids,
            hired="hired" in state.first_entry_at,
            quality_score=(
                quality_by_application.get(application_id)
                if include_quality_scores
                else None
            ),
        )
        for application_id, state in state_by_id.items()
    }

    states_by_source: dict[str, list[_ApplicationState]] = defaultdict(list)
    for state in state_by_id.values():
        states_by_source[_initial_source(state.application, state.initial)].append(state)
    source_quality: list[ApplicationAnalyticsSourceQuality] = []
    for source_value, source_states in sorted(
        states_by_source.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        application_ids = {state.application.id for state in source_states}
        source_outcomes = [outcomes[application_id] for application_id in application_ids]
        applications = len(application_ids)
        started = sum(item.assessment_started for item in source_outcomes)
        completed = sum(item.assessment_completed for item in source_outcomes)
        dropped_off = sum(item.assessment_dropped_off for item in source_outcomes)
        interviews = sum(item.interviewed for item in source_outcomes)
        source_offers = sum(item.offer_sent for item in source_outcomes)
        hires = sum(item.hired for item in source_outcomes)
        source_scores = [
            item.quality_score
            for item in source_outcomes
            if item.quality_score is not None
        ]
        source_quality.append(
            ApplicationAnalyticsSourceQuality(
                key=source_value,
                count=applications,
                applications=applications,
                assessment_started=started,
                assessment_completed=completed,
                assessment_dropped_off=dropped_off,
                interviews=interviews,
                offers=source_offers,
                hires=hires,
                assessment_completion_rate_percent=_percent(completed, started),
                interview_rate_percent=_percent(interviews, applications),
                offer_rate_percent=_percent(source_offers, applications),
                hire_rate_percent=_percent(hires, applications),
                scored_applications=(len(source_scores) if include_quality_scores else None),
                average_quality_score=(
                    _mean(source_scores) if include_quality_scores else None
                ),
            )
        )

    workload: dict[UUID | None, Counter[str]] = defaultdict(Counter)
    for state in state_by_id.values():
        owner_id = _initial_owner(state.application, state.initial)
        workload[owner_id][state.current_stage] += 1
    owner_workload: list[ApplicationAnalyticsOwnerWorkload] = []
    for owner_id, counts in workload.items():
        active = sum(counts[stage_name] for stage_name in ACTIVE_STAGES)
        terminal = sum(counts[stage_name] for stage_name in TERMINAL_STAGES)
        owner_workload.append(
            ApplicationAnalyticsOwnerWorkload(
                owner_user_id=owner_id,
                label=_owner_label(owner_id, owner_labels),
                active=active,
                terminal=terminal,
                total=active + terminal,
                by_stage={stage_name: counts[stage_name] for stage_name in APPLICATION_STAGES},
            )
        )
    owner_workload.sort(key=lambda row: (-row.active, -row.total, row.label))

    integration_health: ApplicationAnalyticsIntegrationHealth | None = None
    ai_usage: ApplicationAnalyticsAiUsage | None = None
    if include_organization_operations:
        connections = list(
            (
                await db.execute(
                    select(IntegrationConnection).where(
                        IntegrationConnection.organization_id == organization_id
                    )
                )
            ).scalars()
        )
        integration_status_counts = Counter(
            connection.status for connection in connections
        )
        integration_provider_counts = Counter(
            connection.provider for connection in connections
        )
        integration_health = ApplicationAnalyticsIntegrationHealth(
            total=len(connections),
            connected=integration_status_counts["connected"],
            attention_required=sum(
                integration_status_counts[state_name]
                for state_name in ATTENTION_INTEGRATION_STATES
            ),
            by_status=_breakdown(integration_status_counts),
            by_provider=_breakdown(integration_provider_counts),
        )

        usage_rows = list(
            (
                await db.execute(
                    select(UsageLedgerEntry).where(
                        UsageLedgerEntry.organization_id == organization_id,
                        UsageLedgerEntry.metric.in_(("ai_tokens", "ai_cost_microusd")),
                        UsageLedgerEntry.kind == "settle",
                        UsageLedgerEntry.occurred_at >= start,
                        UsageLedgerEntry.occurred_at < end,
                    )
                )
            ).scalars()
        )
        provider_tokens: Counter[str] = Counter()
        provider_cost: Counter[str] = Counter()
        provider_calls: dict[str, set[str]] = defaultdict(set)
        all_calls: set[str] = set()
        for usage in usage_rows:
            provider = str((usage.metadata_json or {}).get("provider") or "unknown")
            call_key = str(usage.resource_id or usage.id)
            if usage.metric == "ai_tokens":
                provider_tokens[provider] += max(0, usage.amount)
                provider_calls[provider].add(call_key)
                all_calls.add(call_key)
            else:
                provider_cost[provider] += max(0, usage.amount)
        ai_providers = sorted(
            set(provider_tokens) | set(provider_cost) | set(provider_calls)
        )
        ai_usage = ApplicationAnalyticsAiUsage(
            calls=len(all_calls),
            tokens=sum(provider_tokens.values()),
            cost_microusd=sum(provider_cost.values()),
            by_provider=[
                ApplicationAnalyticsAiProviderUsage(
                    provider=provider,
                    calls=len(provider_calls[provider]),
                    tokens=provider_tokens[provider],
                    cost_microusd=provider_cost[provider],
                )
                for provider in ai_providers
            ],
        )

    drilldown_cursor_scope = collection_cursor_scope(
        "analytics.applications.drilldown",
        organization_id=organization_id,
        filters={
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "timezone": timezone_name,
            "position_id": str(position_id) if position_id else None,
            "source": source,
            "owner_user_id": str(owner_user_id) if owner_user_id else None,
            "owner_unassigned": owner_unassigned,
            "include_organization_operations": include_organization_operations,
            "include_quality_scores": include_quality_scores,
            "include_ai_decisions": include_ai_decisions,
            "dimension": drilldown_dimension,
            "key": drilldown_key,
        },
    )
    drilldown = _drilldown_page(
        list(state_by_id.values()),
        dimension=drilldown_dimension,
        key=drilldown_key,
        cursor=drilldown_cursor,
        limit=drilldown_limit,
        owner_labels=owner_labels,
        outcomes=outcomes,
        cursor_scope=drilldown_cursor_scope,
    )
    responses = offers_accepted + offers_declined
    return ApplicationAnalyticsOut(
        start_at=start,
        end_at=end,
        timezone=timezone_name,
        total_applications=len(state_by_id),
        unique_candidates=len(
            {state.application.candidate_id for state in state_by_id.values()}
        ),
        quality_score_authorized=include_quality_scores,
        by_source=source_quality,
        by_owner=_breakdown(owner_counts, owner_label_map),
        by_position=_breakdown(position_counts, position_label_map),
        dispositions=_breakdown(disposition_counts),
        stages=stages,
        daily=daily_rows,
        timing=ApplicationAnalyticsTiming(
            hired_applications=len(time_to_hire),
            filled_positions=len(time_to_fill),
            median_hours_to_hire=_median(time_to_hire),
            median_hours_to_fill=_median(time_to_fill),
        ),
        offers=ApplicationAnalyticsOffers(
            sent=offers_sent,
            accepted=offers_accepted,
            declined=offers_declined,
            response_rate_percent=_percent(responses, offers_sent),
            acceptance_rate_percent=_percent(offers_accepted, responses),
        ),
        assessments=ApplicationAnalyticsAssessments(
            invitations=assessment_invitations,
            started=assessment_started,
            completed=assessment_completed,
            dropped_off=max(0, assessment_started - assessment_completed),
            completion_rate_percent=_percent(
                assessment_completed, assessment_invitations
            ),
            applications_invited=len(assessment_invited_application_ids),
            applications_started=len(assessment_started_application_ids),
            applications_completed=len(assessment_completed_application_ids),
            applications_dropped_off=len(assessment_dropped_application_ids),
        ),
        owner_workload=owner_workload,
        integration_health=integration_health,
        ai_usage=ai_usage,
        ai_decisions=ai_decision_report,
        filter_options=ApplicationAnalyticsFilterOptions(
            sources=_breakdown(source_facets),
            owners=_breakdown(owner_facets, owner_label_map),
            positions=_breakdown(position_facets, position_label_map),
        ),
        drilldown=drilldown,
        reconciliation=ApplicationAnalyticsReconciliation(
            applications_checked=len(expected),
            applications_without_initial_event=no_initial,
            duplicate_initial_events=duplicate_initials,
            stage_mismatches=stage_mismatches,
        ),
    )


def application_analytics_csv(report: ApplicationAnalyticsOut) -> str:
    """Serialize this report and selected drilldown without formula cells."""

    output = io.StringIO(newline="")
    writer = csv.writer(output)

    def safe(value: object) -> object:
        if value is None:
            return ""
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    def row(*values: object) -> None:
        writer.writerow([safe(value) for value in values])

    row("section", "key", "label", "value", "secondary_value", "rate_percent")
    row("cohort", "start_at", "", report.start_at)
    row("cohort", "end_at", "", report.end_at)
    row("cohort", "timezone", "", report.timezone)
    row("summary", "applications", "", report.total_applications)
    row("summary", "unique_candidates", "", report.unique_candidates)
    row("timing", "median_hours_to_hire", "", report.timing.median_hours_to_hire)
    row("timing", "median_hours_to_fill", "", report.timing.median_hours_to_fill)
    row("offers", "sent", "", report.offers.sent)
    row("offers", "accepted", "", report.offers.accepted)
    row("offers", "declined", "", report.offers.declined)
    row(
        "offers",
        "acceptance",
        "",
        report.offers.accepted,
        report.offers.accepted + report.offers.declined,
        report.offers.acceptance_rate_percent,
    )
    assessment_scope = report.assessments.scope
    row("assessments", "invitations", assessment_scope, report.assessments.invitations)
    row("assessments", "started", assessment_scope, report.assessments.started)
    row("assessments", "completed", assessment_scope, report.assessments.completed)
    row("assessments", "dropped_off", assessment_scope, report.assessments.dropped_off)
    row(
        "assessment_applications",
        "invited",
        assessment_scope,
        report.assessments.applications_invited,
    )
    row(
        "assessment_applications",
        "started",
        assessment_scope,
        report.assessments.applications_started,
    )
    row(
        "assessment_applications",
        "completed",
        assessment_scope,
        report.assessments.applications_completed,
    )
    row(
        "assessment_applications",
        "dropped_off",
        assessment_scope,
        report.assessments.applications_dropped_off,
    )
    if report.ai_usage is not None:
        row("ai", "calls", "organization_window", report.ai_usage.calls)
        row("ai", "tokens", "organization_window", report.ai_usage.tokens)
        row(
            "ai",
            "cost_microusd",
            "organization_window",
            report.ai_usage.cost_microusd,
        )
    if report.ai_decisions is not None:
        row(
            "ai_decisions",
            "recommendations",
            report.ai_decisions.scope,
            report.ai_decisions.recommendations,
        )
        row(
            "ai_decisions",
            "human_reviews",
            report.ai_decisions.scope,
            report.ai_decisions.human_reviews,
            report.ai_decisions.recommendations,
            report.ai_decisions.review_rate_percent,
        )
        row(
            "ai_decisions",
            "human_overrides",
            report.ai_decisions.scope,
            report.ai_decisions.human_overrides,
            report.ai_decisions.recommendations,
            report.ai_decisions.override_rate_percent,
        )
        for provenance in report.ai_decisions.by_provenance:
            provenance_label = (
                f"{provenance.provider}/{provenance.model}/"
                f"{provenance.prompt_version}"
            )
            row(
                "ai_provenance_recommendations",
                provenance_label,
                "application_cohort",
                provenance.recommendations,
            )
            row(
                "ai_provenance_overrides",
                provenance_label,
                "application_cohort",
                provenance.human_overrides,
                provenance.recommendations,
                provenance.override_rate_percent,
            )
    for stage_row in report.stages:
        row(
            "stage",
            stage_row.stage,
            "entered/current",
            stage_row.entered,
            stage_row.current,
            stage_row.conversion_from_previous_percent,
        )
    for value in report.by_source:
        row("source_applications", value.key, value.label, value.applications)
        row(
            "source_assessments",
            value.key,
            value.label,
            value.assessment_completed,
            value.assessment_started,
            value.assessment_completion_rate_percent,
        )
        row(
            "source_assessment_dropoff",
            value.key,
            value.label,
            value.assessment_dropped_off,
        )
        row(
            "source_interviews",
            value.key,
            value.label,
            value.interviews,
            value.applications,
            value.interview_rate_percent,
        )
        row(
            "source_offers",
            value.key,
            value.label,
            value.offers,
            value.applications,
            value.offer_rate_percent,
        )
        row(
            "source_hires",
            value.key,
            value.label,
            value.hires,
            value.applications,
            value.hire_rate_percent,
        )
        if report.quality_score_authorized:
            row(
                "source_quality",
                value.key,
                value.label,
                value.average_quality_score,
                value.scored_applications,
            )
    breakdown_sections = [
        ("owner", report.by_owner),
        ("position", report.by_position),
        ("disposition", report.dispositions),
    ]
    if report.integration_health is not None:
        breakdown_sections.extend(
            (
                ("integration_status", report.integration_health.by_status),
                ("integration_provider", report.integration_health.by_provider),
            )
        )
    for section, values in breakdown_sections:
        for value in values:
            row(section, value.key, value.label, value.count)
    for value in report.owner_workload:
        row("owner_workload", value.owner_user_id, value.label, value.active, value.total)
    for value in report.daily:
        row("daily_applications", value.date, "", value.applications)
        row("daily_transitions", value.date, "", value.transitions)
    if report.drilldown is not None:
        row()
        row(
            "drilldown_dimension",
            report.drilldown.dimension,
            report.drilldown.key,
            report.drilldown.total,
        )
        row(
            "application_id",
            "candidate_id",
            "position_id",
            "applied_at",
            "current_stage",
            "source",
            "owner",
            "disposition",
            "assessment_started",
            "assessment_completed",
            "assessment_dropped_off",
            "interviewed",
            "offer_sent",
            "hired",
            "quality_score",
        )
        for item in report.drilldown.items:
            row(
                item.application_id,
                item.candidate_id,
                item.position_id,
                item.applied_at,
                item.current_stage,
                item.source,
                item.owner_label,
                item.disposition_code,
                item.assessment_started,
                item.assessment_completed,
                item.assessment_dropped_off,
                item.interviewed,
                item.offer_sent,
                item.hired,
                item.quality_score,
            )
    return "\ufeff" + output.getvalue()
