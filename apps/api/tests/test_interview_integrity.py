"""Application-scoped, immutable interview-kit regressions."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.llm.base import CompletionResult
from app.models.candidate import Candidate
from app.models.interview import InterviewAssignment, InterviewKitVersion
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.routers.interviews import (
    complete_interview,
    generate_for_candidate,
    get_interview,
    score_question,
)
from app.schemas.interviews import (
    GenerateRequest,
    InterviewCompleteRequest,
    QuestionScoreRequest,
)
from app.services.applications import ensure_application_link

pytestmark = pytest.mark.asyncio


async def _seed(db, suffix: str):
    organization = Organization(
        name=f"Interview integrity {suffix}",
        slug=f"interview-integrity-{suffix}-{uuid4().hex[:8]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"recruiter-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    interviewer = User(
        organization_id=organization.id,
        email=f"interviewer-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    unassigned = User(
        organization_id=organization.id,
        email=f"unassigned-{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"candidate-{suffix}",
        raw_cv="Built distributed Python services and PostgreSQL systems. " * 3,
    )
    position = Position(
        organization_id=organization.id,
        title="Platform engineer",
        description="Build reliable Python and PostgreSQL services.",
        status="open",
        created_by=recruiter.id,
    )
    db.add_all([recruiter, interviewer, unassigned, candidate, position])
    await db.flush()
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            stage="interview",
        )
    )
    await db.commit()
    link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source="manual",
        owner_user_id=recruiter.id,
        actor_user_id=recruiter.id,
        actor_kind="human",
        reason="interview_integrity_fixture",
    )
    application_id = link.application.id
    await db.commit()
    for entity in (organization, recruiter, interviewer, unassigned, candidate, position):
        await db.refresh(entity)
    return (
        organization,
        recruiter,
        interviewer,
        unassigned,
        candidate,
        position,
        application_id,
    )


async def test_interview_application_kit_completion_and_assignment_scope(db, monkeypatch):
    (
        _organization,
        recruiter,
        interviewer,
        unassigned,
        candidate,
        position,
        application_id,
    ) = await _seed(db, "primary")
    recruiter_id = recruiter.id
    interviewer_id = interviewer.id
    unassigned_id = unassigned.id
    candidate_id = candidate.id
    position_id = position.id
    (
        _other_organization,
        _other_recruiter,
        _other_interviewer,
        _other_unassigned,
        _other_candidate,
        _other_position,
        other_application_id,
    ) = await _seed(db, "other-tenant")
    recruiter = await db.get(User, recruiter_id)
    interviewer = await db.get(User, interviewer_id)
    unassigned = await db.get(User, unassigned_id)
    candidate = await db.get(Candidate, candidate_id)
    position = await db.get(Position, position_id)
    assert all(
        entity is not None
        for entity in (recruiter, interviewer, unassigned, candidate, position)
    )

    class FakeInterviewLLM:
        async def complete(self, *args, **kwargs):
            return CompletionResult(
                content=json.dumps(
                    {
                        "questions": [
                            {
                                "category": "technical",
                                "question_text": f"Grounded interview question {index}",
                                "rationale": "Tests the stated platform experience",
                                "expected_signals": "Specific implementation evidence",
                            }
                            for index in range(4)
                        ]
                    }
                ),
                model_used="test/interview-integrity",
            )

    monkeypatch.setattr(
        "app.services.interview.organization_llm_client",
        AsyncMock(return_value=FakeInterviewLLM()),
    )

    with pytest.raises(HTTPException) as cross_tenant:
        await generate_for_candidate(
            candidate.id,
            GenerateRequest(
                position_id=position.id,
                application_id=other_application_id,
                count=4,
            ),
            db=db,
            user=recruiter,
        )
    assert cross_tenant.value.status_code == 404

    interview = await generate_for_candidate(
        candidate.id,
        GenerateRequest(
            position_id=position.id,
            application_id=application_id,
            count=4,
        ),
        db=db,
        user=recruiter,
    )
    assert interview.application_id == application_id
    assert interview.kit_version == 1
    assert all(question.is_required for question in interview.questions)

    kit = (
        await db.execute(
            select(InterviewKitVersion).where(
                InterviewKitVersion.interview_id == interview.id,
                InterviewKitVersion.version == 1,
            )
        )
    ).scalar_one()
    assert len(kit.questions_snapshot) == 4
    db.add(
        InterviewAssignment(
            organization_id=recruiter.organization_id,
            interview_id=interview.id,
            interviewer_user_id=interviewer.id,
            assigned_by_user_id=recruiter.id,
        )
    )
    await db.commit()

    with pytest.raises(HTTPException) as unassigned_access:
        await get_interview(interview.id, db=db, user=unassigned)
    assert unassigned_access.value.status_code == 404
    assigned_view = await get_interview(interview.id, db=db, user=interviewer)
    assert assigned_view.application_id == application_id

    with pytest.raises(HTTPException) as incomplete:
        await complete_interview(
            interview.id,
            InterviewCompleteRequest(),
            db=db,
            user=interviewer,
        )
    assert incomplete.value.status_code == 422

    for question in assigned_view.questions:
        await score_question(
            interview.id,
            question.id,
            QuestionScoreRequest(score=4, notes="Structured evidence"),
            db=db,
            user=interviewer,
        )
    completed = await complete_interview(
        interview.id,
        InterviewCompleteRequest(),
        db=db,
        user=interviewer,
    )
    assert completed.status == "scored"
    with pytest.raises(HTTPException) as immutable_feedback:
        await score_question(
            interview.id,
            assigned_view.questions[0].id,
            QuestionScoreRequest(score=1, notes="tampered"),
            db=db,
            user=interviewer,
        )
    assert immutable_feedback.value.status_code == 409

    kit.questions_snapshot = []
    with pytest.raises(ValueError, match="append-only"):
        await db.commit()
    await db.rollback()
