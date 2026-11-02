"""Runtime artifacts remain bound to one authoritative Application cycle."""

from datetime import time
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.application import Application
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_processing import CandidateProcessingJob
from app.models.communication import CommunicationThread
from app.models.organization import Organization
from app.models.position import Position
from app.models.scheduling import SchedulingLink
from app.models.user import User
from app.routers.chat import create_session
from app.routers.communications import (
    create_candidate_contact_point,
    send_candidate_communication,
)
from app.routers.recordings import create_recording, list_recordings, update_recording
from app.routers.tasks import create_task, list_tasks, update_task
from app.schemas.applications import ApplicationCreate, ApplicationStageTransition
from app.schemas.chat import ChatSessionCreate
from app.schemas.communications import (
    CandidateCommunicationSend,
    CandidateContactPointCreate,
)
from app.schemas.enrichment import (
    RecordingCreate,
    RecordingUpdate,
    TaskCreate,
    TaskUpdate,
)
from app.schemas.scheduling import AvailabilityInterval, SchedulingLinkCreate
from app.services.applications import create_application, transition_application_stage
from app.services.candidate_processing import enqueue_candidate_scoring_job
from app.services.scheduling import create_scheduling_link, replace_user_availability


pytestmark = pytest.mark.asyncio


async def _cycle(db, user: User, candidate: Candidate, position: Position) -> Application:
    created = await create_application(
        db,
        user,
        ApplicationCreate(
            candidate_id=candidate.id,
            position_id=position.id,
            source=candidate.source,
        ),
    )
    application = await db.get(Application, created.id)
    assert application is not None
    return application


async def _reject(db, user: User, application: Application) -> None:
    await db.refresh(application)
    await transition_application_stage(
        db,
        user,
        application.id,
        ApplicationStageTransition(
            stage="rejected",
            expected_version=application.updated_at,
            disposition_code="not_qualified",
            reason="Cycle completed before reapplication",
        ),
    )


async def test_two_cycles_isolate_chat_evidence_communications_scheduling_and_jobs(db):
    organization = Organization(
        name="Runtime cycle isolation",
        slug=f"runtime-cycle-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = User(
        organization_id=organization.id,
        email=f"runtime-{uuid4().hex[:8]}@example.test",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref=f"runtime-candidate-{uuid4().hex[:8]}",
        raw_cv="Exact-cycle runtime candidate profile with enough evidence. " * 2,
    )
    position = Position(
        organization_id=organization.id,
        title="Runtime application cycles",
        status="open",
    )
    db.add_all([recruiter, candidate, position])
    await db.flush()
    position.created_by = recruiter.id
    await db.commit()
    await create_candidate_contact_point(
        candidate.id,
        CandidateContactPointCreate(
            channel="email",
            value="cycle-runtime@example.test",
            is_primary=True,
        ),
        db=db,
        user=recruiter,
    )
    await replace_user_availability(
        db,
        organization_id=organization.id,
        user_id=recruiter.id,
        timezone="UTC",
        intervals=[
            AvailabilityInterval(
                weekday=weekday,
                start_time=time(9, 0),
                end_time=time(17, 0),
            )
            for weekday in range(7)
        ],
    )
    await db.commit()

    first = await _cycle(db, recruiter, candidate, position)
    first_chat = await create_session(
        ChatSessionCreate(
            scope="candidate",
            candidate_id=candidate.id,
            position_id=position.id,
            application_id=first.id,
            title="First cycle",
        ),
        db=db,
        user=recruiter,
    )
    first_task = await create_task(
        candidate.id,
        TaskCreate(
            title="First-cycle task",
            content="First-cycle submitted work",
            position_id=position.id,
            application_id=first.id,
        ),
        db=db,
        user=recruiter,
    )
    first_recording = await create_recording(
        candidate.id,
        RecordingCreate(
            title="First-cycle recording",
            position_id=position.id,
            application_id=first.id,
        ),
        db=db,
        user=recruiter,
    )
    first_message = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            position_id=position.id,
            application_id=first.id,
            body="First-cycle message",
            idempotency_key="cycle-bound-message",
        ),
        db=db,
        user=recruiter,
    )
    first_link_issue = await create_scheduling_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        actor_user_id=recruiter.id,
        body=SchedulingLinkCreate(
            position_id=position.id,
            application_id=first.id,
            interviewer_user_ids=[recruiter.id],
            min_notice_minutes=0,
        ),
    )
    first_job, first_job_created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=first.id,
        actor_user_id=recruiter.id,
        idempotency_key="cycle-one-score",
    )
    assert first_job_created is True
    await db.commit()

    await _reject(db, recruiter, first)
    first_link = await db.get(SchedulingLink, first_link_issue.link.id)
    first_job = await db.get(CandidateProcessingJob, first_job.id)
    await db.refresh(first_link)
    await db.refresh(first_job)
    assert first_link is not None and first_link.revoked_at is not None
    assert first_job is not None and first_job.status == "cancelled"

    second = await _cycle(db, recruiter, candidate, position)
    assert second.cycle_number == 2
    second_chat = await create_session(
        ChatSessionCreate(
            scope="candidate",
            candidate_id=candidate.id,
            position_id=position.id,
            # Omission is safe because exactly one active cycle exists.
            title="Second cycle",
        ),
        db=db,
        user=recruiter,
    )
    second_task = await create_task(
        candidate.id,
        TaskCreate(
            title="Second-cycle task",
            content="Second-cycle submitted work",
            position_id=position.id,
            application_id=second.id,
        ),
        db=db,
        user=recruiter,
    )
    second_recording = await create_recording(
        candidate.id,
        RecordingCreate(
            title="Second-cycle recording",
            position_id=position.id,
            application_id=second.id,
        ),
        db=db,
        user=recruiter,
    )

    with pytest.raises(HTTPException) as reused_across_cycles:
        await send_candidate_communication(
            candidate.id,
            CandidateCommunicationSend(
                channel="email",
                position_id=position.id,
                application_id=second.id,
                body="Second-cycle message",
                idempotency_key="cycle-bound-message",
            ),
            db=db,
            user=recruiter,
        )
    assert reused_across_cycles.value.status_code == 409
    second_message = await send_candidate_communication(
        candidate.id,
        CandidateCommunicationSend(
            channel="email",
            position_id=position.id,
            application_id=second.id,
            body="Second-cycle message",
            idempotency_key="cycle-two-message",
        ),
        db=db,
        user=recruiter,
    )
    second_link_issue = await create_scheduling_link(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        actor_user_id=recruiter.id,
        body=SchedulingLinkCreate(
            position_id=position.id,
            interviewer_user_ids=[recruiter.id],
            min_notice_minutes=0,
        ),
    )
    second_job, second_job_created = await enqueue_candidate_scoring_job(
        db,
        organization_id=organization.id,
        candidate_id=candidate.id,
        position_id=position.id,
        application_id=second.id,
        actor_user_id=recruiter.id,
        idempotency_key="cycle-two-score",
    )
    assert second_job_created is True
    await db.commit()

    assert first_chat.application_id == first.id
    assert second_chat.application_id == second.id
    assert first_task.application_id == first.id
    assert second_task.application_id == second.id
    assert first_recording.application_id == first.id
    assert second_recording.application_id == second.id
    assert first_link_issue.link.application_id == first.id
    assert second_link_issue.link.application_id == second.id
    assert first_job.application_id == first.id
    assert second_job.application_id == second.id

    first_thread = await db.get(CommunicationThread, first_message.thread_id)
    second_thread = await db.get(CommunicationThread, second_message.thread_id)
    assert first_thread is not None and first_thread.application_id == first.id
    assert second_thread is not None and second_thread.application_id == second.id
    assert first_thread.id != second_thread.id

    with pytest.raises(HTTPException) as task_retarget:
        await update_task(
            first_task.id,
            TaskUpdate(application_id=second.id),
            db=db,
            user=recruiter,
        )
    assert task_retarget.value.status_code == 409
    with pytest.raises(HTTPException) as recording_retarget:
        await update_recording(
            first_recording.id,
            RecordingUpdate(application_id=second.id),
            db=db,
            user=recruiter,
        )
    assert recording_retarget.value.status_code == 409
    # Historical callbacks may update their own immutable cycle link.
    updated_task = await update_task(
        first_task.id,
        TaskUpdate(status="evaluated"),
        db=db,
        user=recruiter,
    )
    updated_recording = await update_recording(
        first_recording.id,
        RecordingUpdate(status="transcribed", transcript="Historical transcript"),
        db=db,
        user=recruiter,
    )
    assert updated_task.application_id == first.id
    assert updated_recording.application_id == first.id

    with pytest.raises(HTTPException) as closed_cycle_create:
        await create_task(
            candidate.id,
            TaskCreate(
                title="Invalid historical task",
                position_id=position.id,
                application_id=first.id,
            ),
            db=db,
            user=recruiter,
        )
    assert closed_cycle_create.value.status_code == 409

    task_page = await list_tasks(
        candidate.id,
        limit=20,
        cursor=None,
        db=db,
        user=recruiter,
    )
    recording_page = await list_recordings(
        candidate.id,
        limit=20,
        cursor=None,
        db=db,
        user=recruiter,
    )
    assert {item.application_id for item in task_page.items} == {first.id, second.id}
    assert {item.application_id for item in recording_page.items} == {
        first.id,
        second.id,
    }
    pair_activities = list(
        (
            await db.execute(
                select(CandidateActivity).where(
                    CandidateActivity.organization_id == organization.id,
                    CandidateActivity.candidate_id == candidate.id,
                    CandidateActivity.position_id == position.id,
                )
            )
        ).scalars()
    )
    assert pair_activities
    assert all(activity.application_id in {first.id, second.id} for activity in pair_activities)
