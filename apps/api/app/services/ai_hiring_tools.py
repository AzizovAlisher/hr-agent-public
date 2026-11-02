"""Allowlisted, typed tools for approved AI Hiring Assistant actions.

The language model never invokes these handlers.  It may propose an action
bundle, after which deterministic API code validates the bundle, the approving
human's permission, mission freshness, and the typed payload before execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate import Candidate
from app.models.position import Position
from app.models.score import Score
from app.schemas.applications import ApplicationEvaluationCreate
from app.services.activities import append_candidate_activity
from app.services.applications import (
    append_application_evaluation,
    reconcile_application_from_score,
)


class CreateVacancyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    requirements: dict[str, Any] = Field(default_factory=dict)


class ApplyShortlistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[UUID] = Field(min_length=1, max_length=20)


@dataclass(frozen=True)
class ToolExecutionContext:
    db: AsyncSession
    organization_id: UUID
    mission_id: UUID
    plan_version_id: UUID
    approved_by_user_id: UUID
    agent_run_id: UUID | None


ToolHandler = Callable[[ToolExecutionContext, BaseModel], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    input_model: type[BaseModel]
    required_permission: str
    handler: ToolHandler


class AiToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise RuntimeError(f"Duplicate AI tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"AI action is not allowlisted: {name}") from exc

    @property
    def definitions(self) -> tuple[RegisteredTool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

    async def execute(
        self,
        name: str,
        *,
        payload: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        tool = self.get(name)
        validated = tool.input_model.model_validate(payload)
        return await tool.handler(context, validated)


async def _mission_for_update(context: ToolExecutionContext):
    # Imported lazily so this module can be statically checked before the AI
    # persistence migration is installed.
    from app.models.ai_hiring import HiringMission

    return (
        await context.db.execute(
            select(HiringMission)
            .where(
                HiringMission.id == context.mission_id,
                HiringMission.organization_id == context.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _create_vacancy(
    context: ToolExecutionContext, raw: BaseModel
) -> dict[str, Any]:
    body = CreateVacancyInput.model_validate(raw)
    mission = await _mission_for_update(context)
    if mission is None:
        raise ValueError("Mission not found")
    # The mission FK is the idempotency anchor. Replaying the same approved
    # bundle returns the existing vacancy rather than creating another row.
    if mission.position_id is not None:
        existing = (
            await context.db.execute(
                select(Position).where(
                    Position.id == mission.position_id,
                    Position.organization_id == context.organization_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"position_id": str(existing.id), "created": False}
    position = Position(
        organization_id=context.organization_id,
        title=body.title,
        description=body.description,
        requirements=body.requirements,
        # Approval creates an internal, paused vacancy. Publishing is a
        # separate consequential action and is not an MVP tool.
        status="paused",
        public_apply_enabled=False,
        created_by=context.approved_by_user_id,
    )
    context.db.add(position)
    await context.db.flush()
    mission.position_id = position.id
    return {"position_id": str(position.id), "created": True}


async def _apply_shortlist(
    context: ToolExecutionContext, raw: BaseModel
) -> dict[str, Any]:
    from app.models.ai_hiring import MissionCandidateAssessment

    body = ApplyShortlistInput.model_validate(raw)
    mission = await _mission_for_update(context)
    if mission is None:
        raise ValueError("Mission not found")
    if mission.position_id is None:
        raise ValueError("Create the mission vacancy before applying a shortlist")
    position = (
        await context.db.execute(
            select(Position).where(
                Position.id == mission.position_id,
                Position.organization_id == context.organization_id,
            )
        )
    ).scalar_one_or_none()
    if position is None:
        raise ValueError("Mission vacancy not found")

    candidate_ids = list(dict.fromkeys(body.candidate_ids))
    candidates = list(
        (
            await context.db.execute(
                select(Candidate).where(
                    Candidate.organization_id == context.organization_id,
                    Candidate.id.in_(candidate_ids),
                    Candidate.merged_into_candidate_id.is_(None),
                )
            )
        ).scalars()
    )
    if len(candidates) != len(candidate_ids):
        raise ValueError("Shortlist contains an unavailable candidate")

    # Only assessments from the approved immutable plan may enter the Human
    # Platform. If a mission was replanned, the old action bundle is rejected
    # before this handler is called.
    assessments = list(
        (
            await context.db.execute(
                select(MissionCandidateAssessment).where(
                    MissionCandidateAssessment.organization_id
                    == context.organization_id,
                    MissionCandidateAssessment.mission_id == context.mission_id,
                    MissionCandidateAssessment.plan_version_id
                    == context.plan_version_id,
                    MissionCandidateAssessment.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    newest: dict[UUID, Any] = {}
    for assessment in sorted(
        assessments,
        key=lambda item: (item.assessment_version, item.created_at, item.id),
    ):
        newest[assessment.candidate_id] = assessment
    if set(newest) != set(candidate_ids):
        raise ValueError("Every shortlisted candidate requires an assessment")

    existing_ids = set(
        (
            await context.db.execute(
                select(Score.candidate_id).where(
                    Score.organization_id == context.organization_id,
                    Score.position_id == position.id,
                    Score.candidate_id.in_(candidate_ids),
                )
            )
        ).scalars()
    )
    created_ids: list[str] = []
    created_scores: list[Score] = []
    for candidate_id in candidate_ids:
        if candidate_id in existing_ids:
            continue
        assessment = newest[candidate_id]
        score = Score(
                organization_id=context.organization_id,
                candidate_id=candidate_id,
                position_id=position.id,
                overall_score=round(float(assessment.overall_score)),
                overall_recommendation=assessment.concise_rationale,
                model_used="ai-hiring-assistant:v1",
                stage="new",
            )
        context.db.add(score)
        created_scores.append(score)
        created_ids.append(str(candidate_id))
    await context.db.flush()
    for score in created_scores:
        application = await reconcile_application_from_score(
            context.db,
            score,
            source="ai_assistant",
            actor_user_id=context.approved_by_user_id,
            actor_kind="automation",
            reason="ai_shortlist_applied",
        )
        assessment = newest[score.candidate_id]
        await append_application_evaluation(
            context.db,
            organization_id=context.organization_id,
            application_id=application.id,
            body=ApplicationEvaluationCreate(
                kind="ai_scoring",
                evidence={
                    "mission_candidate_assessment_id": str(assessment.id),
                    "concise_rationale": assessment.concise_rationale,
                },
                scores={"overall": score.overall_score},
                provenance={
                    "model": score.model_used,
                    "mission_id": str(context.mission_id),
                    "plan_version_id": str(context.plan_version_id),
                    "assessment_version": assessment.assessment_version,
                    "agent_run_id": (
                        str(context.agent_run_id) if context.agent_run_id else None
                    ),
                },
                evaluated_at=assessment.created_at,
            ),
            actor_user_id=context.approved_by_user_id,
            actor_kind="ai",
        )
        append_candidate_activity(
            context.db,
            organization_id=context.organization_id,
            candidate_id=score.candidate_id,
            actor_kind="ai",
            agent_run_id=context.agent_run_id,
            application_id=application.id,
            position_id=position.id,
            event_type="candidate_scored",
            subject_type="score",
            subject_id=score.id,
            payload={
                "overall_score": score.overall_score,
                "model_used": score.model_used,
                "position_title": position.title,
                "resulting_stage": "new",
            },
        )
    return {
        "position_id": str(position.id),
        "created_candidate_ids": created_ids,
        "existing_candidate_ids": sorted(str(value) for value in existing_ids),
    }


AI_TOOL_REGISTRY = AiToolRegistry()
AI_TOOL_REGISTRY.register(
    RegisteredTool(
        name="create_vacancy",
        input_model=CreateVacancyInput,
        required_permission="requisitions.manage",
        handler=_create_vacancy,
    )
)
AI_TOOL_REGISTRY.register(
    RegisteredTool(
        name="apply_shortlist",
        input_model=ApplyShortlistInput,
        required_permission="candidates.write",
        handler=_apply_shortlist,
    )
)
