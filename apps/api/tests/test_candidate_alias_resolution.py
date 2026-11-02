"""Historical candidate IDs resolve to their canonical candidate everywhere."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.llm import CompletionResult
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.interview import Interview
from app.models.interview_recording import InterviewRecording
from app.models.organization import Organization
from app.models.position import Position
from app.models.task_submission import TaskSubmission
from app.models.user import User
from app.routers.candidate_notes import (
    NoteCreate,
    TagCreate,
    create_note,
    create_tag,
    delete_note,
    delete_tag,
    list_notes,
    list_tags,
)
from app.routers.chat import create_session, list_chat_sessions
from app.routers.github import attach_github, detach_github, detect_github
from app.routers.interviews import (
    generate_for_candidate,
    list_for_candidate,
    update_interview,
)
from app.routers.recordings import create_recording, list_recordings, update_recording
from app.routers.tasks import create_task, list_tasks, update_task
from app.schemas.chat import ChatSessionCreate
from app.schemas.enrichment import (
    GitHubAttachRequest,
    RecordingCreate,
    RecordingUpdate,
    TaskCreate,
    TaskUpdate,
)
from app.schemas.interviews import GenerateRequest, InterviewUpdate
from app.services.applications import ensure_application_link
from app.services.chat import _candidate_summary

pytestmark = pytest.mark.asyncio


async def seed_alias(db, label: str = "alias"):
    token = uuid4().hex[:8]
    organization = Organization(
        name=f"Organization {label}", slug=f"organization-{label}-{token}"
    )
    db.add(organization)
    await db.flush()
    user = User(
        organization_id=organization.id,
        email=f"{label}-{token}@example.test",
        password_hash="not-used",
        role="recruiter",
        locale="en",
    )
    canonical = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"canonical-{label}",
        raw_cv="Canonical Python platform engineer. https://github.com/canonical-dev",
    )
    db.add_all([user, canonical])
    await db.flush()
    alias = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"historical-{label}",
        raw_cv="Historical duplicate CV",
        merged_into_candidate_id=canonical.id,
        merged_at=datetime.now(UTC),
        merged_by_user_id=user.id,
    )
    position = Position(
        organization_id=organization.id,
        title=f"Platform role {label}",
        description="Build reliable Python services.",
        status="open",
        created_by=user.id,
    )
    db.add_all([alias, position])
    await db.commit()
    return organization, user, canonical, alias, position


async def test_alias_notes_and_tags_read_write_and_audit_canonical_candidate(db):
    organization, user, canonical, alias, _position = await seed_alias(db, "notes")

    note = await create_note(
        alias.id,
        NoteCreate(content="Created through an old candidate URL"),
        db=db,
        user=user,
    )
    tag = await create_tag(
        alias.id,
        TagCreate(label="canonical"),
        db=db,
        user=user,
    )
    assert note.candidate_id == canonical.id
    assert tag.candidate_id == canonical.id
    notes = await list_notes(alias.id, db=db, user=user)
    tags = await list_tags(alias.id, db=db, user=user)
    assert [item.id for item in notes.items] == [
        note.id
    ]
    assert [item.id for item in tags.items] == [
        tag.id
    ]

    await delete_note(alias.id, note.id, db=db, user=user)
    await delete_tag(alias.id, tag.id, db=db, user=user)
    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert {event.event_type for event in events} == {
        "note_added",
        "note_deleted",
        "tag_added",
        "tag_removed",
    }
    assert {event.candidate_id for event in events} == {canonical.id}


async def test_alias_interviews_recordings_and_tasks_use_canonical_parent(
    db, monkeypatch
):
    organization, user, canonical, alias, position = await seed_alias(db, "evidence")

    recording = await create_recording(
        alias.id,
        RecordingCreate(title="Alias recording", position_id=position.id),
        db=db,
        user=user,
    )
    task = await create_task(
        alias.id,
        TaskCreate(
            title="Alias task",
            content="A sufficiently detailed submitted solution for testing.",
            position_id=position.id,
        ),
        db=db,
        user=user,
    )
    assert recording.candidate_id == canonical.id
    assert task.candidate_id == canonical.id
    recordings = await list_recordings(alias.id, db=db, user=user)
    tasks = await list_tasks(alias.id, db=db, user=user)
    assert [row.id for row in recordings.items] == [
        recording.id
    ]
    assert [row.id for row in tasks.items] == [
        task.id
    ]

    class FakeInterviewLLM:
        async def complete(self, *args, **kwargs):
            return CompletionResult(
                content=json.dumps(
                    {
                        "questions": [
                            {
                                "category": "technical",
                                "question_text": f"Canonical question {index}",
                                "rationale": "Grounded in the canonical CV",
                                "expected_signals": "Concrete implementation detail",
                            }
                            for index in range(4)
                        ]
                    }
                ),
                model_used="test/interview",
            )

    monkeypatch.setattr(
        "app.services.interview.organization_llm_client",
        AsyncMock(return_value=FakeInterviewLLM()),
    )
    interview = await generate_for_candidate(
        alias.id,
        GenerateRequest(position_id=position.id, count=4),
        db=db,
        user=user,
    )
    assert interview.candidate_id == canonical.id
    listed = await list_for_candidate(alias.id, db=db, user=user)
    assert [row.id for row in listed.items] == [interview.id]

    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert {event.event_type for event in events} >= {
        "recording_added",
        "task_added",
        "interview_generated",
    }
    assert {event.candidate_id for event in events} == {canonical.id}


async def test_application_bound_child_mutations_keep_canonical_candidate(db):
    organization, user, canonical, alias, position = await seed_alias(
        db, "legacy-children"
    )
    link = await ensure_application_link(
        db,
        organization_id=organization.id,
        candidate_id=canonical.id,
        position_id=position.id,
        source=canonical.source,
        owner_user_id=None,
        actor_user_id=user.id,
        actor_kind="human",
        reason="legacy_child_test_setup",
    )
    recording = InterviewRecording(
        organization_id=organization.id,
        candidate_id=canonical.id,
        position_id=position.id,
        application_id=link.application.id,
        created_by=user.id,
        title="Legacy recording",
        status="pending",
    )
    task = TaskSubmission(
        organization_id=organization.id,
        candidate_id=canonical.id,
        position_id=position.id,
        application_id=link.application.id,
        created_by=user.id,
        title="Legacy task",
        submission_type="text",
        status="sent",
    )
    interview = Interview(
        organization_id=organization.id,
        application_id=link.application.id,
        # New structured-hiring artifacts are always application-bound and
        # canonical; historical aliases remain valid only at route boundaries.
        candidate_id=canonical.id,
        position_id=position.id,
        created_by=user.id,
        status="draft",
    )
    db.add_all([recording, task, interview])
    await db.commit()

    recording = await update_recording(
        recording.id,
        RecordingUpdate(status="transcribed"),
        db=db,
        user=user,
    )
    task = await update_task(
        task.id,
        TaskUpdate(status="submitted"),
        db=db,
        user=user,
    )
    interview_out = await update_interview(
        interview.id,
        InterviewUpdate(status="archived"),
        db=db,
        user=user,
    )

    assert recording.candidate_id == canonical.id
    assert task.candidate_id == canonical.id
    assert interview_out.candidate_id == canonical.id
    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.organization_id == organization.id
                )
            )
        ).scalars()
    )
    assert {event.event_type for event in events} == {
        "recording_status_changed",
        "task_status_changed",
        "interview_status_changed",
    }
    assert {event.candidate_id for event in events} == {canonical.id}


async def test_alias_github_and_chat_resolve_canonical_context(db, monkeypatch):
    organization, user, canonical, alias, _position = await seed_alias(db, "context")

    async def fake_fetch_github(login):
        return {"profile": {"login": login}, "repos": [], "languages": []}

    async def fake_summarize_github(data, *, db, organization_id):
        return "Canonical GitHub summary", "test/github"

    monkeypatch.setattr("app.services.github_enrich.fetch_github", fake_fetch_github)
    monkeypatch.setattr(
        "app.services.github_enrich.summarize_github", fake_summarize_github
    )
    await attach_github(
        alias.id,
        GitHubAttachRequest(login="canonical-dev"),
        db=db,
        user=user,
    )
    detected = await detect_github(alias.id, db=db, user=user)
    assert detected.suggested_login == "canonical-dev"
    await detach_github(alias.id, db=db, user=user)

    session = await create_session(
        ChatSessionCreate(scope="candidate", candidate_id=alias.id),
        db=db,
        user=user,
    )
    assert session.candidate_id == canonical.id
    sessions = await list_chat_sessions(
        scope="candidate",
        position_id=None,
        candidate_id=alias.id,
        db=db,
        user=user,
    )
    assert [row.id for row in sessions.items] == [session.id]
    assert sessions.items[0].candidate_id == canonical.id
    summary = await _candidate_summary(db, alias.id, organization.id)
    candidate_context = json.loads(summary)
    assert candidate_context["classification"] == "untrusted_candidate_evidence"
    assert "canonical-context" not in candidate_context["content"]
    assert "Canonical Python platform engineer" in candidate_context["content"]
    assert "github.com/canonical-dev" not in candidate_context["content"]

    events = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.event_type.in_(
                        ["github_attached", "github_detached"]
                    )
                )
            )
        ).scalars()
    )
    assert len(events) == 2
    assert {event.candidate_id for event in events} == {canonical.id}
    assert {event.subject_id for event in events} == {canonical.id}


async def test_alias_resolution_keeps_cross_tenant_candidate_ids_at_404(db):
    _organization, _user, _canonical, alias, _position = await seed_alias(
        db, "tenant-source"
    )
    _other_organization, outsider, _other_canonical, _other_alias, _other_position = (
        await seed_alias(db, "tenant-other")
    )

    with pytest.raises(HTTPException) as notes_error:
        await list_notes(alias.id, db=db, user=outsider)
    assert notes_error.value.status_code == 404

    with pytest.raises(HTTPException) as chat_error:
        await create_session(
            ChatSessionCreate(scope="candidate", candidate_id=alias.id),
            db=db,
            user=outsider,
        )
    assert chat_error.value.status_code == 404
