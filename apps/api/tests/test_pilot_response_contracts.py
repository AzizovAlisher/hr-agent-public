"""Regression coverage for pilot-visible response schemas.

These assertions intentionally use the live FastAPI schema rather than the
committed OpenAPI artifact. The integration lead owns artifact regeneration;
this test prevents a router from silently returning to an untyped object.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest_asyncio

from app.main import app
from app.schemas.ai_hiring import (
    AiActionBundleOut,
    AiOrganizationSettingsOut,
    AiSessionDetailOut,
    AiSessionOut,
    HiringMissionOut,
    MissionCandidateAssessmentOut,
    MissionDetailOut,
)
from app.schemas.enrichment import GitHubOut, RecordingOut, TaskOut
from app.services.ai_hiring_runtime import (
    assessment_out,
    bundle_out,
    message_out,
    mission_out,
    session_out,
    settings_out,
)


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    """This schema-only module never opens a database connection."""

    yield


EXPECTED_RESPONSES = {
    ("/ai/v1/settings", "get"): (
        "200",
        "application/json",
        "AiOrganizationSettingsOut",
    ),
    ("/ai/v1/settings", "put"): (
        "200",
        "application/json",
        "AiOrganizationSettingsOut",
    ),
    ("/ai/v1/sessions", "post"): ("201", "application/json", "AiSessionOut"),
    ("/ai/v1/sessions/{session_id}", "get"): (
        "200",
        "application/json",
        "AiSessionDetailOut",
    ),
    ("/ai/v1/sessions/{session_id}/archive", "post"): (
        "200",
        "application/json",
        "AiSessionOut",
    ),
    ("/ai/v1/sessions/{session_id}/attachments", "post"): (
        "201",
        "application/json",
        "AiAttachmentUploadOut",
    ),
    ("/ai/v1/sessions/{session_id}/messages", "post"): (
        "200",
        "text/event-stream",
        "AiStreamEvent",
    ),
    ("/ai/v1/missions/{mission_id}", "get"): (
        "200",
        "application/json",
        "MissionDetailOut",
    ),
    ("/ai/v1/missions/{mission_id}/stop", "post"): (
        "200",
        "application/json",
        "HiringMissionOut",
    ),
    ("/ai/v1/missions/{mission_id}/retry", "post"): (
        "200",
        "application/json",
        "HiringMissionOut",
    ),
    ("/ai/v1/missions/{mission_id}/replan", "post"): (
        "200",
        "application/json",
        "HiringMissionOut",
    ),
    ("/ai/v1/action-bundles/{bundle_id}/approve", "post"): (
        "200",
        "application/json",
        "AiActionBundleOut",
    ),
    ("/ai/v1/action-bundles/{bundle_id}/reject", "post"): (
        "200",
        "application/json",
        "AiActionBundleOut",
    ),
    ("/chat/sessions/{session_id}/messages/stream", "post"): (
        "200",
        "text/event-stream",
        "ChatStreamEvent",
    ),
    ("/positions/draft/extract-text", "post"): (
        "200",
        "application/json",
        "PositionDraftExtractTextResponse",
    ),
}


def _component_ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def test_pilot_visible_operations_have_concrete_success_responses():
    schema = app.openapi()
    for (path, method), (status, media_type, component) in EXPECTED_RESPONSES.items():
        operation = schema["paths"][path][method]
        content = operation["responses"][status]["content"]
        assert content == {media_type: {"schema": _component_ref(component)}}, (
            operation["operationId"],
            content,
        )


def test_sse_responses_are_discriminated_and_terminal_events_are_typed():
    components = app.openapi()["components"]["schemas"]
    assert set(components["ChatStreamEvent"]["discriminator"]["mapping"]) == {
        "delta",
        "usage",
        "done",
        "error",
    }
    assert set(components["AiStreamEvent"]["discriminator"]["mapping"]) == {
        "message.delta",
        "message.completed",
        "mission.updated",
        "canvas.updated",
        "completed",
        "error",
    }
    assert (
        components["ChatDoneEventData"]["properties"]["message_id"]["format"] == "uuid"
    )
    assert (
        components["AiCompletedEventData"]["properties"]["mission_id"]["format"]
        == "uuid"
    )


def test_pilot_json_fields_reference_concrete_components():
    components = app.openapi()["components"]["schemas"]

    assert components["GitHubOut"]["properties"]["data"] == _component_ref(
        "GitHubDataOut"
    )
    assert components["HiringMissionOut"]["properties"]["brief"] == _component_ref(
        "HiringBriefOut"
    )
    assert components["HiringMissionOut"]["properties"]["rubric"] == _component_ref(
        "HiringRubricOut"
    )
    assert components["RecordingOut"]["properties"]["diarization"]["anyOf"][0][
        "items"
    ] == _component_ref("DiarizationTurnOut")
    assert components["TaskOut"]["properties"]["evaluation"]["anyOf"][
        0
    ] == _component_ref("TaskEvaluationOut")
    assessment = components["MissionCandidateAssessmentOut"]["properties"]
    assert assessment["candidate"]["anyOf"][0] == _component_ref(
        "MissionAssessmentCandidateOut"
    )
    assert assessment["evidence"]["items"] == _component_ref(
        "MissionAssessmentEvidenceOut"
    )


def test_ai_runtime_serializers_match_their_public_response_models():
    now = datetime.now(UTC)
    session_id = uuid4()
    mission_id = uuid4()
    user_id = uuid4()
    step = SimpleNamespace(
        id=uuid4(),
        key="discover_candidates",
        step_type="candidate_discovery",
        ordinal=1,
        dependencies=[],
        status="completed",
        safe_result={"candidate_count": 1},
        policy_outcome="auto",
        last_error_code=None,
        started_at=now,
        completed_at=now,
    )
    mission = SimpleNamespace(
        id=mission_id,
        session_id=session_id,
        position_id=None,
        title="Backend engineer",
        original_request="Hire one backend engineer",
        brief={
            "role_title": "Backend engineer",
            "headcount": 1,
            "responsibilities": ["Build services"],
            "must_haves": ["python"],
            "preferences": [],
            "location": "Dushanbe",
            "work_format": "hybrid",
            "compensation": None,
            "deadline": None,
            "shortlist_size": 5,
            "assumptions": [],
            "blockers": [],
        },
        rubric={
            "version": 1,
            "criteria": [
                {
                    "key": "python",
                    "label": "Python",
                    "description": "Relevant evidence",
                    "weight": 100,
                    "must_have": True,
                    "keywords": ["python"],
                }
            ],
            "weight_total": 100,
        },
        assumptions=[],
        blockers=[],
        headcount=1,
        shortlist_size=5,
        status="running",
        created_by_user_id=user_id,
        correlation_id=uuid4(),
        current_plan_version=1,
        last_error_code=None,
        started_at=now,
        completed_at=None,
        stopped_at=None,
        created_at=now,
        updated_at=now,
    )
    session = SimpleNamespace(
        id=session_id,
        title="Backend engineer",
        status="active",
        created_by_user_id=user_id,
        created_at=now,
        updated_at=now,
        archived_at=None,
    )
    message = SimpleNamespace(
        id=uuid4(),
        session_id=session_id,
        actor_user_id=user_id,
        agent_run_id=None,
        role="user",
        content="Hire one backend engineer",
        structured_content={},
        attachments=[],
        redaction_metadata={"raw_prompt_logged": False},
        sequence=1,
        status="completed",
        created_at=now,
        completed_at=now,
    )
    serialized_mission = mission_out(mission)
    assert HiringMissionOut.model_validate(serialized_mission)
    assert MissionDetailOut.model_validate(mission_out(mission, steps=[step]))
    assert AiSessionOut.model_validate(session_out(session, mission))
    assert AiSessionDetailOut.model_validate(
        {
            **session_out(session, mission),
            "messages": [message_out(message)],
            "mission": serialized_mission,
        }
    )
    assert AiOrganizationSettingsOut.model_validate(settings_out(None))

    assessment = SimpleNamespace(
        id=uuid4(),
        candidate_id=uuid4(),
        plan_version=1,
        assessment_version=2,
        overall_score=84.5,
        confidence="high",
        recommendation="strong_fit",
        criteria=[{"key": "python", "score": 90, "evidence_claim_ids": []}],
        concerns=[],
        missing_evidence=[],
        evidence_coverage=0.8,
        concise_rationale="Strong evidence",
        created_at=now,
    )
    candidate = SimpleNamespace(
        id=assessment.candidate_id,
        source="manual",
        source_ref="Candidate 1",
    )
    evidence = [
        {
            "id": str(uuid4()),
            "criterion_key": "python",
            "claim": "Built a Python service",
            "source_type": "cv",
            "source_id": None,
            "source_locator": {"kind": "masked_cv_excerpt"},
            "stance": "supports",
            "confidence": 0.85,
        }
    ]
    assert MissionCandidateAssessmentOut.model_validate(
        assessment_out(assessment, candidate=candidate, evidence=evidence)
    )

    bundle = SimpleNamespace(
        id=uuid4(),
        mission_id=mission_id,
        plan_version=1,
        action_type="create_vacancy",
        summary="Create a paused vacancy",
        preview={"title": "Backend engineer"},
        required_permission="candidates.write",
        status="pending",
        approved_by_user_id=None,
        decision_comment=None,
        execution_result={},
        error_code=None,
        decided_at=None,
        executed_at=None,
        created_at=now,
    )
    assert AiActionBundleOut.model_validate(bundle_out(bundle))


def test_enrichment_runtime_shapes_validate_without_opaque_response_fields():
    now = datetime.now(UTC)
    github = GitHubOut.model_validate(
        {
            "login": "octocat",
            "data": {
                "profile": {
                    "login": "octocat",
                    "name": "Octo Cat",
                    "html_url": "https://github.com/octocat",
                    "followers": 1,
                    "following": 0,
                    "public_repos": 1,
                    "created_at": now.isoformat(),
                },
                "repos": [
                    {
                        "name": "hello-world",
                        "full_name": "octocat/hello-world",
                        "html_url": "https://github.com/octocat/hello-world",
                        "language": "Python",
                        "stars": 3,
                        "forks": 1,
                        "topics": ["demo"],
                        "pushed_at": now.isoformat(),
                    }
                ],
                "languages": [{"language": "Python", "repo_count": 1}],
                "stats": {"total_stars": 3, "non_fork_repos": 1, "shown_repos": 1},
                "ai_summary": "Public evidence summary",
                "ai_summary_model": "private/model",
                "fetched_at": now.isoformat(),
            },
            "fetched_at": now,
        }
    )
    assert github.data.repos[0].stars == 3

    common = {
        "id": uuid4(),
        "candidate_id": uuid4(),
        "title": "Pilot evidence",
        "status": "evaluated",
        "created_at": now,
        "updated_at": now,
    }
    recording = RecordingOut.model_validate(
        {
            **common,
            "diarization": [
                {"speaker": "Interviewer", "start": 0, "end": 4.5, "text": "Hello"}
            ],
        }
    )
    assert recording.diarization and recording.diarization[0].end == 4.5

    task = TaskOut.model_validate(
        {
            **common,
            "submission_type": "text",
            "evaluation": {
                "score": 88,
                "summary": "Strong submission",
                "strengths": ["Clear design"],
                "concerns": [],
                "recommendation": "yes",
                "output_locale": "en",
                "decision_authority": "human",
            },
        }
    )
    assert task.evaluation and task.evaluation.recommendation == "yes"
