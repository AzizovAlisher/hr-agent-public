import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.llm import CompletionResult
from app.models.application import ApplicationEvaluation
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.organization import Organization
from app.models.position import Position
from app.models.score import Score
from app.models.user import User
from app.routers.activities import (
    decode_activity_cursor,
    encode_activity_cursor,
    list_candidate_activities,
)
from app.routers.candidate_notes import (
    NoteCreate,
    TagCreate,
    create_note,
    create_tag,
    delete_note,
    delete_tag,
)
from app.routers.github import attach_github, detach_github
from app.routers.interviews import (
    delete_interview,
    generate_for_candidate,
    update_interview,
)
from app.routers.positions import StageUpdate, update_candidate_stage
from app.routers.public import public_apply
from app.routers.recordings import create_recording, delete_recording, update_recording
from app.routers.tasks import create_task, delete_task, evaluate_task, update_task
from app.schemas.enrichment import (
    GitHubAttachRequest,
    RecordingCreate,
    RecordingUpdate,
    TaskCreate,
    TaskUpdate,
)
from app.schemas.interviews import GenerateRequest, InterviewUpdate
from app.services.activities import append_candidate_activity
from app.services.applications import ensure_application_link
from app.services.scoring import score_candidate

pytestmark = pytest.mark.asyncio


async def seed_org(db, suffix: str = "one"):
    org = Organization(name=f"Org {suffix}", slug=f"org-{suffix}-{uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    user = User(
        organization_id=org.id,
        email=f"{suffix}-{uuid4().hex[:8]}@example.com",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=org.id,
        source="manual",
        raw_cv="A sufficiently detailed candidate resume for integration testing. " * 2,
    )
    db.add_all([user, candidate])
    await db.flush()
    return org, user, candidate


async def test_cursor_round_trip_and_rejects_invalid_cursor():
    happened = datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
    activity_id = uuid4()
    scope = "tenant-and-filter-scope"
    encoded = encode_activity_cursor(happened, activity_id, scope=scope)
    decoded_time, decoded_id = decode_activity_cursor(encoded, scope=scope)
    assert decoded_time == happened
    assert decoded_id == activity_id
    with pytest.raises(HTTPException) as exc:
        decode_activity_cursor("not-a-cursor", scope=scope)
    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "code": "cursor_invalid",
        "detail": "Invalid activity cursor",
    }
    with pytest.raises(HTTPException) as wrong_scope:
        decode_activity_cursor(encoded, scope="another-tenant-or-filter")
    assert wrong_scope.value.status_code == 422
    assert wrong_scope.value.detail["code"] == "cursor_invalid"


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "content",
        "cv_text",
        "contact_email",
        "note_body",
        "interview_transcript",
        "llm_prompt",
        "task_content",
    ],
)
async def test_payload_guard_rejects_sensitive_content(db, sensitive_key):
    org, user, candidate = await seed_org(db)
    with pytest.raises(ValueError, match="Sensitive activity payload key"):
        append_candidate_activity(
            db,
            organization_id=org.id,
            candidate_id=candidate.id,
            actor_user_id=user.id,
            event_type="note_added",
            payload={sensitive_key: "must not be retained"},
        )


async def test_activity_rolls_back_with_its_domain_mutation(db):
    org, user, candidate = await seed_org(db, "rollback")
    candidate_id = candidate.id
    await db.commit()

    candidate.source = "referral"
    append_candidate_activity(
        db,
        organization_id=org.id,
        candidate_id=candidate_id,
        actor_user_id=user.id,
        event_type="tag_added",
        subject_type="tag",
        subject_id=uuid4(),
        payload={"label": "priority"},
    )
    await db.flush()
    await db.rollback()

    persisted_candidate = await db.get(Candidate, candidate_id)
    persisted_events = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == candidate_id
            )
        )
    ).scalars().all()
    assert persisted_candidate is not None
    assert persisted_candidate.source == "manual"
    assert persisted_events == []


async def test_position_erasure_cascades_position_bound_activity(db):
    org, creator, candidate = await seed_org(db, "retention")
    creator_id = creator.id
    actor = User(
        organization_id=org.id,
        email=f"actor-{uuid4().hex[:8]}@example.com",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    position = Position(
        organization_id=org.id,
        title="Platform Engineer",
        status="open",
        created_by=creator.id,
    )
    db.add_all([actor, position])
    await db.flush()
    link = await ensure_application_link(
        db,
        organization_id=org.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=candidate.source,
        owner_user_id=creator.id,
        actor_user_id=creator.id,
        actor_kind="human",
        reason="position_erasure_activity_setup",
    )
    subject_id = uuid4()
    activity = append_candidate_activity(
        db,
        organization_id=org.id,
        candidate_id=candidate.id,
        actor_user_id=actor.id,
        application_id=link.application.id,
        position_id=position.id,
        event_type="task_deleted",
        subject_type="task",
        subject_id=subject_id,
        payload={
            "title": "API exercise",
            "status": "submitted",
            "position_title": position.title,
        },
    )
    await db.flush()
    activity_id = activity.id
    candidate_id = candidate.id
    await db.commit()

    await db.delete(actor)
    await db.delete(position)
    await db.commit()
    db.expire_all()

    # d96 makes every position-bound candidate artifact follow the same hard
    # erasure contract.  Deleting the position must remove the activity and its
    # free-form payload rather than retain a detached PII-bearing snapshot.
    assert await db.get(CandidateActivity, activity_id) is None

    creator = await db.get(User, creator_id)
    assert creator is not None
    page = await list_candidate_activities(
        candidate_id,
        limit=30,
        cursor=None,
        category="all",
        db=db,
        user=creator,
    )
    assert page.items == []


async def test_candidate_deletion_cascades_activity(db):
    org, user, candidate = await seed_org(db, "candidate-cascade")
    candidate_id = candidate.id
    activity = append_candidate_activity(
        db,
        organization_id=org.id,
        candidate_id=candidate_id,
        actor_user_id=None,
        event_type="candidate_added",
        subject_type="candidate",
        subject_id=candidate_id,
        payload={"source": "public"},
    )
    await db.flush()
    activity_id = activity.id
    await db.commit()

    await db.delete(candidate)
    await db.commit()
    db.expire_all()

    assert await db.get(CandidateActivity, activity_id) is None


async def test_organization_deletion_cascades_activity(db):
    org, _user, candidate = await seed_org(db, "organization-cascade")
    activity = append_candidate_activity(
        db,
        organization_id=org.id,
        candidate_id=candidate.id,
        event_type="candidate_added",
        subject_type="candidate",
        subject_id=candidate.id,
        payload={"source": "manual"},
    )
    await db.flush()
    activity_id = activity.id
    await db.commit()

    await db.delete(org)
    await db.commit()
    db.expire_all()

    assert await db.get(CandidateActivity, activity_id) is None


async def test_public_application_uses_system_actor_and_safe_payload(db):
    org, user, _candidate = await seed_org(db, "public-application")
    position = Position(
        organization_id=org.id,
        title="Customer Success Manager",
        description="Support enterprise customers and improve retention.",
        status="open",
        created_by=user.id,
    )
    db.add(position)
    await db.commit()

    accepted = await public_apply(
        position.id,
        name="Public Applicant",
        phone="+992 900 000 001",
        email="applicant@example.net",
        source="direct",
        file=None,
        raw_text="A public candidate resume with customer success experience. " * 2,
        db=db,
    )
    assert accepted.accepted is True
    applicant = (
        await db.execute(
            select(Candidate).where(
                Candidate.organization_id == org.id,
                Candidate.email_normalized == "applicant@example.net",
            )
        )
    ).scalar_one()
    activity = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == applicant.id,
                CandidateActivity.event_type == "candidate_added",
            )
        )
    ).scalar_one()
    assert activity.actor_user_id is None
    assert activity.position_id == position.id
    assert activity.payload == {
        "source": "public",
        "applied_via": "direct",
        "position_title": "Customer Success Manager",
    }


async def test_non_scoring_mutations_emit_once_and_skip_no_ops(db, monkeypatch):
    org, user, candidate = await seed_org(db, "mutation-events")
    position = Position(
        organization_id=org.id,
        title="Platform Engineer",
        description="Build and operate reliable Python services.",
        status="open",
        created_by=user.id,
    )
    db.add(position)
    await db.commit()

    note = await create_note(
        candidate.id,
        NoteCreate(content="Private team note that must stay out of the timeline."),
        db=db,
        user=user,
    )
    await delete_note(candidate.id, note.id, db=db, user=user)
    tag = await create_tag(
        candidate.id,
        TagCreate(label="priority"),
        db=db,
        user=user,
    )
    await delete_tag(candidate.id, tag.id, db=db, user=user)

    async def fake_fetch_github(login):
        return {"profile": {"login": login}, "repositories": []}

    async def fake_summarize_github(data, *, db, organization_id):
        return "Safe profile summary", "test/github"

    monkeypatch.setattr(
        "app.services.github_enrich.fetch_github", fake_fetch_github
    )
    monkeypatch.setattr(
        "app.services.github_enrich.summarize_github", fake_summarize_github
    )
    await attach_github(
        candidate.id,
        GitHubAttachRequest(login="octocat"),
        db=db,
        user=user,
    )
    await detach_github(candidate.id, db=db, user=user)
    await detach_github(candidate.id, db=db, user=user)

    recording = await create_recording(
        candidate.id,
        RecordingCreate(title="Screening call", position_id=position.id),
        db=db,
        user=user,
    )
    await update_recording(
        recording.id,
        RecordingUpdate(status="pending"),
        db=db,
        user=user,
    )
    await update_recording(
        recording.id,
        RecordingUpdate(status="processed"),
        db=db,
        user=user,
    )
    await delete_recording(recording.id, db=db, user=user)

    task = await create_task(
        candidate.id,
        TaskCreate(
            title="API exercise",
            prompt="Implement the private exercise described in the task record.",
            content="A candidate submission with enough implementation detail to evaluate.",
            position_id=position.id,
        ),
        db=db,
        user=user,
    )

    class FakeTaskLLM:
        async def complete(self, *args, **kwargs):
            return CompletionResult(
                content=json.dumps(
                    {
                        "score": 88,
                        "summary": "Strong solution",
                        "strengths": ["Clear structure"],
                        "concerns": [],
                        "recommendation": "yes",
                    }
                ),
                model_used="test/task",
            )

    monkeypatch.setattr(
        "app.services.task_submission.organization_llm_client",
        AsyncMock(return_value=FakeTaskLLM()),
    )
    await evaluate_task(task.id, db=db, user=user)
    await update_task(
        task.id,
        TaskUpdate(status="evaluated"),
        db=db,
        user=user,
    )
    await update_task(
        task.id,
        TaskUpdate(status="reviewed"),
        db=db,
        user=user,
    )
    await delete_task(task.id, db=db, user=user)

    class FakeInterviewLLM:
        async def complete(self, *args, **kwargs):
            questions = [
                {
                    "category": "technical",
                    "question_text": f"Grounded platform question {index}",
                    "rationale": "Tests a stated requirement",
                    "expected_signals": "Specific implementation choices",
                }
                for index in range(4)
            ]
            return CompletionResult(
                content=json.dumps({"questions": questions}),
                model_used="test/interview",
            )

    monkeypatch.setattr(
        "app.services.interview.organization_llm_client",
        AsyncMock(return_value=FakeInterviewLLM()),
    )
    interview = await generate_for_candidate(
        candidate.id,
        GenerateRequest(position_id=position.id, count=4),
        db=db,
        user=user,
    )
    await update_interview(
        interview.id,
        InterviewUpdate(notes="Private notes are stored only on the interview."),
        db=db,
        user=user,
    )
    await update_interview(
        interview.id,
        InterviewUpdate(status="in_progress"),
        db=db,
        user=user,
    )
    await update_interview(
        interview.id,
        InterviewUpdate(status="in_progress"),
        db=db,
        user=user,
    )
    await delete_interview(interview.id, db=db, user=user)

    event_types = (
        await db.execute(
            select(CandidateActivity.event_type).where(
                CandidateActivity.candidate_id == candidate.id
            )
        )
    ).scalars().all()
    expected_once = {
        "note_added",
        "note_deleted",
        "tag_added",
        "tag_removed",
        "github_attached",
        "github_detached",
        "recording_added",
        "recording_status_changed",
        "recording_deleted",
        "task_added",
        "task_evaluated",
        "task_status_changed",
        "task_deleted",
        "interview_generated",
        "interview_status_changed",
        "interview_deleted",
    }
    assert set(event_types) == expected_once
    assert all(event_types.count(event_type) == 1 for event_type in expected_once)

    payloads = (
        await db.execute(
            select(CandidateActivity.payload).where(
                CandidateActivity.candidate_id == candidate.id
            )
        )
    ).scalars().all()
    serialized_payloads = json.dumps(payloads).lower()
    assert "private team note" not in serialized_payloads
    assert "candidate submission" not in serialized_payloads
    assert "private exercise" not in serialized_payloads


async def test_activity_endpoint_orders_paginates_filters_and_isolates_tenants(db):
    org, user, candidate = await seed_org(db, "primary")
    other_org, other_user, other_candidate = await seed_org(db, "other")
    happened = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    ids = [UUID(int=1), UUID(int=2), UUID(int=3)]
    db.add_all(
        [
            CandidateActivity(
                id=ids[0],
                organization_id=org.id,
                candidate_id=candidate.id,
                actor_user_id=user.id,
                event_type="candidate_added",
                payload={"source": "manual"},
                occurred_at=happened,
            ),
            CandidateActivity(
                id=ids[1],
                organization_id=org.id,
                candidate_id=candidate.id,
                actor_user_id=user.id,
                event_type="stage_changed",
                payload={"from_stage": "new", "to_stage": "screening"},
                occurred_at=happened,
            ),
            CandidateActivity(
                id=ids[2],
                organization_id=org.id,
                candidate_id=candidate.id,
                actor_user_id=user.id,
                event_type="note_added",
                payload={},
                occurred_at=happened,
            ),
            # A valid activity for a different tenant. Composite tenant FKs
            # prevent constructing an organization/candidate mismatch.
            CandidateActivity(
                id=UUID(int=4),
                organization_id=other_org.id,
                candidate_id=other_candidate.id,
                actor_user_id=other_user.id,
                event_type="note_added",
                payload={"label": "must-not-leak"},
                occurred_at=happened,
            ),
        ]
    )
    await db.commit()

    first = await list_candidate_activities(
        candidate.id, limit=2, cursor=None, category="all", db=db, user=user
    )
    assert [item.id for item in first.items] == [ids[2], ids[1]]
    assert first.next_cursor is not None
    second = await list_candidate_activities(
        candidate.id,
        limit=2,
        cursor=first.next_cursor,
        category="all",
        db=db,
        user=user,
    )
    assert [item.id for item in second.items] == [ids[0]]
    assert second.next_cursor is None

    collaboration = await list_candidate_activities(
        candidate.id,
        limit=30,
        cursor=None,
        category="collaboration",
        db=db,
        user=user,
    )
    assert [item.event_type for item in collaboration.items] == ["note_added"]

    with pytest.raises(HTTPException) as exc:
        await list_candidate_activities(
            other_candidate.id,
            limit=30,
            cursor=None,
            category="all",
            db=db,
            user=user,
        )
    assert exc.value.status_code == 404
    assert other_org.id != org.id and other_user.id != user.id


async def test_stage_change_is_atomic_and_no_op_does_not_duplicate(db):
    org, user, candidate = await seed_org(db, "stage")
    position = Position(
        organization_id=org.id,
        title="Backend Engineer",
        status="open",
        created_by=user.id,
    )
    db.add(position)
    await db.flush()
    score = Score(
        organization_id=org.id,
        candidate_id=candidate.id,
        position_id=position.id,
        overall_score=75,
        stage="screening",
    )
    db.add(score)
    await db.flush()
    link = await ensure_application_link(
        db,
        organization_id=org.id,
        candidate_id=candidate.id,
        position_id=position.id,
        source=candidate.source,
        owner_user_id=None,
        actor_user_id=user.id,
        actor_kind="human",
        reason="stage_change_test_setup",
    )
    await db.commit()
    await db.refresh(link.application)

    transitioned = await update_candidate_stage(
        position.id,
        candidate.id,
        StageUpdate(
            stage="interview",
            expected_version=link.application.updated_at,
        ),
        db=db,
        user=user,
    )
    events = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == candidate.id,
                CandidateActivity.event_type == "stage_changed",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["from_stage"] == "screening"
    assert events[0].payload["to_stage"] == "interview"

    await update_candidate_stage(
        position.id,
        candidate.id,
        StageUpdate(
            stage="interview",
            expected_version=transitioned.application_version,
        ),
        db=db,
        user=user,
    )
    count_after_noop = (
        await db.execute(
            select(CandidateActivity).where(
                CandidateActivity.candidate_id == candidate.id,
                CandidateActivity.event_type == "stage_changed",
            )
        )
    ).scalars().all()
    assert len(count_after_noop) == 1


async def test_scoring_emits_new_rescore_and_real_automatic_stage_change(db, monkeypatch):
    org, user, candidate = await seed_org(db, "scoring")
    position = Position(
        organization_id=org.id,
        title="Data Engineer",
        description="Build reliable PostgreSQL and Python data pipelines.",
        status="open",
        created_by=user.id,
    )
    db.add(position)
    await db.flush()
    # Public intake creates this unscored association immediately so the
    # candidate is visible in the vacancy while AI scoring is pending.
    db.add(
        Score(
            organization_id=org.id,
            candidate_id=candidate.id,
            position_id=position.id,
            stage="new",
        )
    )
    await db.commit()

    class FakeLLM:
        async def complete(self, *args, **kwargs):
            return CompletionResult(
                content=json.dumps(
                    {
                        "technical_skills": {"score": 80, "comment": "Технический комментарий"},
                        "quality_of_projects": {"score": 70, "comment": "Комментарий по проектам"},
                        "previous_experience": {"score": 60, "comment": "Комментарий по опыту"},
                        "impact": {"score": 50, "comment": "Комментарий по вкладу"},
                    }
                ),
                model_used="test/model",
            )

    monkeypatch.setattr(
        "app.services.scoring.organization_llm_client",
        AsyncMock(return_value=FakeLLM()),
    )
    await score_candidate(
        candidate_id=candidate.id,
        position_id=position.id,
        db=db,
        organization_id=org.id,
        actor_user_id=user.id,
    )

    await score_candidate(
        candidate_id=candidate.id,
        position_id=position.id,
        db=db,
        organization_id=org.id,
        actor_user_id=user.id,
    )
    events = (
        await db.execute(
            select(CandidateActivity.event_type)
            .where(CandidateActivity.candidate_id == candidate.id)
            .order_by(CandidateActivity.occurred_at, CandidateActivity.id)
        )
    ).scalars().all()
    assert events.count("candidate_scored") == 1
    assert events.count("candidate_rescored") == 1
    assert events.count("stage_changed") == 1
    evaluations = list(
        (
            await db.execute(
                select(ApplicationEvaluation)
                .where(ApplicationEvaluation.organization_id == org.id)
                .order_by(
                    ApplicationEvaluation.kind,
                    ApplicationEvaluation.version,
                )
            )
        ).scalars()
    )
    assert [(item.kind, item.version) for item in evaluations] == [
        ("ai_scoring", 1),
        ("ai_scoring", 2),
        ("deterministic_scoring", 1),
        ("deterministic_scoring", 2),
    ]
    assert evaluations[0].scores["technical_skills"] == 80
    assert evaluations[0].provenance["model"] == "test/model"
    assert evaluations[-1].scores["overall"] == 70
