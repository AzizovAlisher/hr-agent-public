"""Regression coverage for legacy score redaction and exact interviewer pairs."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.auth.authorization import FieldClass, operation_policy
from app.auth.jwt import encode_access_token
from app.db import clear_tenant_context, set_tenant_context
from app.llm import CompletionResult
from app.llm.gateway import BudgetedLLMClient
from app.llm.logging import log_completion
from app.main import app
from app.middleware.audit import EXTRA_SENSITIVE_READ_FIELDS
from app.models.application import Application
from app.models.ai_hiring import (
    AiOrganizationSettings,
    AiSession,
    EvidenceClaim,
    HiringMission,
    MissionCandidateAssessment,
    MissionPlanVersion,
)
from app.models.assessment import (
    AssessmentAttempt,
    AssessmentFile,
    AssessmentInvitation,
    AssessmentQuestion,
    AssessmentResult,
    AssessmentSection,
    AssessmentTemplate,
    AssessmentTemplateVersion,
)
from app.models.candidate import Candidate
from app.models.candidate_activity import CandidateActivity
from app.models.candidate_note import CandidateNote, CandidateTag
from app.models.candidate_processing import CandidateProcessingJob
from app.models.chat_session import ChatSession
from app.models.communication import (
    CandidateContactPoint,
    CommunicationAccount,
    CommunicationMessage,
    CommunicationThread,
)
from app.models.enterprise import AuditLog, EnterprisePolicy, PermissionOverride
from app.models.folder import Folder
from app.models.interview import Interview, InterviewAssignment
from app.models.interview_recording import InterviewRecording
from app.models.message import Message
from app.models.operations import UsageLedgerEntry
from app.models.organization import Organization
from app.models.notification import Notification
from app.models.position import Position
from app.models.requisition import Requisition
from app.models.score import Score
from app.models.scheduling import InterviewAppointment, SchedulingLink
from app.models.sourcing import SourceCandidateReceipt
from app.models.task_submission import TaskSubmission
from app.models.talent import (
    TalentNurtureEnrollment,
    TalentNurtureSequence,
    TalentPool,
    TalentPoolMember,
)
from app.models.user import User
from app.models.workflow import WorkflowRun
from app.schemas.position_draft import DraftMessage
from app.services.communications import process_claimed_message
from app.services.evidence import build_candidate_evidence
from app.services.position_draft import converse, extract_project
from app.services.privacy import (
    build_candidate_privacy_context,
    erase_candidate_live_store_copies,
)
from app.services.public_applications import submit_public_application

pytestmark = pytest.mark.asyncio


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _user(db, organization_id, role: str, label: str) -> User:
    user = User(
        organization_id=organization_id,
        email=f"{label}-{uuid4().hex[:10]}@example.test",
        password_hash="unused",
        role=role,
        locale="en",
    )
    db.add(user)
    await db.flush()
    return user


async def _redaction_graph(db):
    organization = Organization(
        name="Assessment redaction tenant",
        slug=f"assessment-redaction-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = await _user(db, organization.id, "recruiter", "redacted-recruiter")
    admin = await _user(db, organization.id, "admin", "redaction-admin")

    position = Position(
        organization_id=organization.id,
        title="Python platform engineer",
        description="Python distributed systems",
        requirements={"skills": ["Python", "PostgreSQL"]},
        created_by=recruiter.id,
    )
    other_position = Position(
        organization_id=organization.id,
        title="Python data engineer",
        description="Python analytics",
        requirements={"skills": ["Python"]},
        created_by=recruiter.id,
    )
    db.add_all([position, other_position])
    await db.flush()

    newer_low = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Newer low-score Python candidate",
        raw_cv="Python PostgreSQL distributed systems candidate profile",
        parsed_cv={"applicant_name": "Parsed Newer Applicant"},
        email_normalized="duplicate@example.test",
    )
    older_high = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Older high-score Python candidate",
        raw_cv="Python analytics PostgreSQL candidate profile",
        email_normalized="duplicate@example.test",
    )
    merged_alias = Candidate(
        organization_id=organization.id,
        source="hh",
        source_ref="Merged Python source",
        raw_cv="Python merged evidence",
    )
    db.add_all([newer_low, older_high, merged_alias])
    await db.flush()
    merged_alias.merged_into_candidate_id = newer_low.id
    merged_alias.merged_at = datetime.now(UTC)
    merged_alias.merged_by_user_id = recruiter.id

    low_score = Score(
        organization_id=organization.id,
        candidate_id=newer_low.id,
        position_id=position.id,
        overall_score=7,
        score_technical_skills=8,
        score_quality_of_projects=6,
        score_previous_experience=7,
        score_impact=7,
        comment_technical_skills="ASSESSMENT-CANARY-LOW",
        overall_recommendation="ASSESSMENT-CANARY-RECOMMENDATION",
        model_used="private-model-canary-low",
        stage="screening",
    )
    high_score = Score(
        organization_id=organization.id,
        candidate_id=older_high.id,
        position_id=position.id,
        overall_score=99,
        score_technical_skills=99,
        comment_technical_skills="ASSESSMENT-CANARY-HIGH",
        model_used="private-model-canary-high",
        stage="interview",
    )
    alias_score = Score(
        organization_id=organization.id,
        candidate_id=merged_alias.id,
        position_id=other_position.id,
        overall_score=88,
        comment_technical_skills="ASSESSMENT-CANARY-MERGED",
        model_used="private-model-canary-merged",
        stage="screening",
    )
    db.add_all([low_score, high_score, alias_score])
    await db.flush()
    now = datetime.now(UTC)
    generated_applications = list(
        (
            await db.execute(
                select(Application).where(
                    Application.legacy_score_id.in_(
                        [low_score.id, high_score.id, alias_score.id]
                    )
                )
            )
        ).scalars()
    )
    by_score_id = {
        application.legacy_score_id: application
        for application in generated_applications
    }
    newer_application = by_score_id[low_score.id]
    newer_application.stage = "screening"
    newer_application.applied_at = now
    older_application = by_score_id[high_score.id]
    older_application.stage = "interview"
    older_application.applied_at = now - timedelta(days=2)
    alias_application = by_score_id[alias_score.id]
    alias_application.stage = "screening"
    alias_application.applied_at = now - timedelta(days=1)
    pool = TalentPool(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        name="Python talent",
    )
    db.add(pool)
    await db.flush()
    db.add_all(
        [
            TalentPoolMember(
                organization_id=organization.id,
                pool_id=pool.id,
                candidate_id=newer_low.id,
                added_by_user_id=recruiter.id,
            ),
            TalentPoolMember(
                organization_id=organization.id,
                pool_id=pool.id,
                candidate_id=older_high.id,
                added_by_user_id=recruiter.id,
            ),
            EnterprisePolicy(
                organization_id=organization.id,
                field_visibility={
                    "candidate_contacts": ["admin", "recruiter"],
                    "candidate_cv": ["admin", "recruiter"],
                    "assessment_results": ["admin"],
                },
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="recruiter",
                permission="assessments.results",
                allowed=False,
            ),
        ]
    )
    await db.commit()
    return {
        "organization": organization,
        "recruiter": recruiter,
        "admin": admin,
        "position": position,
        "newer_low": newer_low,
        "older_high": older_high,
        "pool": pool,
    }


def _assert_score_projection_redacted(item: dict) -> None:
    for field in (
        "overall_score",
        "score_technical_skills",
        "score_quality_of_projects",
        "score_previous_experience",
        "score_impact",
        "comment_technical_skills",
        "comment_quality_of_projects",
        "comment_previous_experience",
        "comment_impact",
        "model_used",
    ):
        if field in item:
            assert item[field] is None, field


async def test_assessment_override_redacts_all_mixed_legacy_projections_and_side_channels(
    db,
):
    seeded = await _redaction_graph(db)
    headers = _auth(seeded["recruiter"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        candidates = await client.get("/candidates?limit=100", headers=headers)
        candidate_applications = await client.get(
            f"/candidates/{seeded['newer_low'].id}/applications", headers=headers
        )
        first_pipeline_page = await client.get(
            f"/positions/{seeded['position'].id}/applications?limit=1",
            headers=headers,
        )
        first_body = first_pipeline_page.json()
        second_pipeline_page = await client.get(
            f"/positions/{seeded['position'].id}/applications",
            headers=headers,
            params={"limit": 1, "cursor": first_body["next_cursor"]},
        )
        search = await client.get("/candidate-search?limit=100", headers=headers)
        stage_search = await client.get(
            "/candidate-search",
            headers=headers,
            params={"stage": "screening", "limit": 100},
        )
        score_filter = await client.get(
            "/candidate-search?score_min=50", headers=headers
        )
        unscored_filter = await client.get(
            "/candidate-search?unscored_only=true", headers=headers
        )
        duplicates = await client.get("/candidate-duplicates", headers=headers)
        merge_preview = await client.post(
            "/candidate-merges/preview",
            headers=headers,
            json={
                "candidate_ids": [
                    str(seeded["newer_low"].id),
                    str(seeded["older_high"].id),
                ]
            },
        )
        best_positions = await client.get(
            f"/candidates/{seeded['newer_low'].id}/best-positions",
            headers=headers,
        )
        merged_sources = await client.get(
            f"/candidates/{seeded['newer_low'].id}/merged-sources",
            headers=headers,
        )
        pool_members = await client.get(
            f"/talent/pools/{seeded['pool'].id}/members", headers=headers
        )
        talent_search = await client.get(
            "/talent/search", headers=headers, params={"q": "Python"}
        )
        talent_matches = await client.get(
            f"/positions/{seeded['position'].id}/talent-matches", headers=headers
        )

    assert candidates.status_code == 200
    for item in candidates.json()["items"]:
        assert item["best_score"] is None
        assert item["best_position_id"] is None
        assert item["best_position_title"] is None
        assert item["score_count"] == 0

    assert candidate_applications.status_code == 200
    assert len(candidate_applications.json()["items"]) == 1
    _assert_score_projection_redacted(candidate_applications.json()["items"][0])

    # Hidden assessment scores cannot influence order or the next cursor.  The
    # newest application (score 7) must precede the older score-99 application.
    assert first_pipeline_page.status_code == 200
    assert first_body["items"][0]["candidate_id"] == str(seeded["newer_low"].id)
    assert first_body["items"][0]["display_name"] == "Parsed Newer Applicant"
    assert first_body["next_cursor"] is not None
    _assert_score_projection_redacted(first_body["items"][0])
    assert second_pipeline_page.status_code == 200
    assert second_pipeline_page.json()["items"][0]["candidate_id"] == str(
        seeded["older_high"].id
    )
    assert (
        second_pipeline_page.json()["items"][0]["display_name"]
        == "Older high-score Python candidate"
    )
    _assert_score_projection_redacted(second_pipeline_page.json()["items"][0])

    assert search.status_code == 200
    for item in search.json()["items"]:
        assert item["best_score"] is None
        assert item["best_position_id"] is None
        assert item["best_position_title"] is None
        assert item["score_count"] == 0
        for application in item["applications"]:
            assert application["score_id"] is None
            assert application["overall_score"] is None
    assert stage_search.status_code == 200
    assert [item["id"] for item in stage_search.json()["items"]] == [
        str(seeded["newer_low"].id)
    ]
    assert score_filter.status_code == 403
    assert unscored_filter.status_code == 403

    assert duplicates.status_code == 200
    assert duplicates.json()["items"]
    for group in duplicates.json()["items"]:
        for candidate in group["candidates"]:
            assert candidate["best_score"] is None
            assert candidate["best_position_title"] is None
    assert merge_preview.status_code == 403
    assert best_positions.status_code == 403

    assert merged_sources.status_code == 200
    assert len(merged_sources.json()["items"]) == 2
    assert all(item["scores"] == [] for item in merged_sources.json()["items"])
    for response in (pool_members, talent_search, talent_matches):
        assert response.status_code == 200
        for item in response.json()["items"]:
            assert item["best_score"] is None
            assert item["best_position_id"] is None
            assert item["best_position_title"] is None

    combined = "\n".join(
        response.text
        for response in (
            candidates,
            candidate_applications,
            first_pipeline_page,
            second_pipeline_page,
            search,
            duplicates,
            merged_sources,
            pool_members,
            talent_search,
            talent_matches,
        )
    )
    assert "ASSESSMENT-CANARY" not in combined
    assert "private-model-canary" not in combined


async def _pair_graph(db):
    organization = Organization(
        name="Exact interviewer pair tenant",
        slug=f"exact-pair-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = await _user(db, organization.id, "recruiter", "pair-recruiter")
    interviewer = await _user(db, organization.id, "interviewer", "pair-interviewer")
    candidate_a = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Candidate A",
        raw_cv="Candidate A has enough Python experience for scoring operations.",
    )
    candidate_b = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Candidate B",
        raw_cv="Candidate B has enough PostgreSQL experience for scoring operations.",
    )
    position_x = Position(
        organization_id=organization.id,
        title="Position X",
        created_by=recruiter.id,
    )
    position_y = Position(
        organization_id=organization.id,
        title="Position Y",
        created_by=recruiter.id,
    )
    db.add_all([candidate_a, candidate_b, position_x, position_y])
    await db.flush()

    pairs = {}
    for candidate, position, score_value in (
        (candidate_a, position_x, 91),
        (candidate_a, position_y, 12),
        (candidate_b, position_x, 13),
        (candidate_b, position_y, 82),
    ):
        score = Score(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            overall_score=score_value,
            comment_technical_skills=f"pair-score-{score_value}",
            model_used=f"pair-model-{score_value}",
        )
        db.add(score)
        await db.flush()
        application = (
            await db.execute(
                select(Application).where(Application.legacy_score_id == score.id)
            )
        ).scalar_one()
        pairs[(candidate.id, position.id)] = (application, score)

    assigned_interviews = {}
    for application in (
        pairs[(candidate_a.id, position_x.id)][0],
        pairs[(candidate_b.id, position_y.id)][0],
    ):
        interview = Interview(
            organization_id=organization.id,
            application_id=application.id,
            candidate_id=application.candidate_id,
            position_id=application.position_id,
            created_by=recruiter.id,
        )
        db.add(interview)
        await db.flush()
        assigned_interviews[(application.candidate_id, application.position_id)] = interview
        db.add(
            InterviewAssignment(
                organization_id=organization.id,
                interview_id=interview.id,
                interviewer_user_id=interviewer.id,
                assigned_by_user_id=recruiter.id,
            )
        )
    db.add_all(
        [
            PermissionOverride(
                organization_id=organization.id,
                role="interviewer",
                permission="applications.disposition",
                allowed=True,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="interviewer",
                permission="candidates.write",
                allowed=True,
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="interviewer",
                permission="applications.write",
                allowed=True,
            ),
        ]
    )
    await db.commit()
    return {
        "organization": organization,
        "recruiter": recruiter,
        "interviewer": interviewer,
        "candidate_a": candidate_a,
        "candidate_b": candidate_b,
        "position_x": position_x,
        "position_y": position_y,
        "pairs": pairs,
        "assigned_interviews": assigned_interviews,
    }


async def test_interviewer_scope_uses_exact_candidate_position_pairs_for_reads_and_writes(
    db,
):
    seeded = await _pair_graph(db)
    interviewer = seeded["interviewer"]
    candidate_a = seeded["candidate_a"]
    candidate_b = seeded["candidate_b"]
    position_x = seeded["position_x"]
    position_y = seeded["position_y"]
    pairs = seeded["pairs"]
    position_x.folder = "assigned-original"
    unassigned_position = Position(
        organization_id=seeded["organization"].id,
        title="Unassigned position Z",
        folder="unassigned-original",
        created_by=seeded["recruiter"].id,
    )
    db.add(unassigned_position)
    await db.commit()
    await db.refresh(position_x)
    await db.refresh(unassigned_position)

    await set_tenant_context(
        db,
        organization_id=seeded["organization"].id,
        actor_id=interviewer.id,
        identity_kind="user",
        role="interviewer",
    )
    visible_score_ids = set((await db.execute(select(Score.id))).scalars())
    assert visible_score_ids == {
        pairs[(candidate_a.id, position_x.id)][1].id,
        pairs[(candidate_b.id, position_y.id)][1].id,
    }
    clear_tenant_context(db)

    crossed_application = pairs[(candidate_a.id, position_y.id)][0]
    crossed_application.stage = "rejected"
    crossed_application.closed_at = datetime.now(UTC)
    db.add_all(
        [
            PermissionOverride(
                organization_id=seeded["organization"].id,
                role="interviewer",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "ai.use",
                "ai.sensitive_context",
                "candidates.contact",
                "candidates.documents",
                "requisitions.compensation",
            )
        ]
        + [
            EnterprisePolicy(
                organization_id=seeded["organization"].id,
                field_visibility={
                    FieldClass.AI_CONTEXT.value: ["interviewer"],
                    FieldClass.CONTACT.value: ["interviewer"],
                    FieldClass.CANDIDATE_DOCUMENT.value: ["interviewer"],
                    FieldClass.ASSESSMENT_RESULT.value: ["interviewer"],
                    FieldClass.COMPENSATION.value: ["interviewer"],
                },
            )
        ]
    )
    await db.commit()
    await db.refresh(crossed_application)
    crossed_version = crossed_application.updated_at.isoformat()
    valid_application = pairs[(candidate_a.id, position_x.id)][0]
    headers = _auth(interviewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        applications = await client.get("/applications", headers=headers)
        assigned_interview = await client.get(
            f"/interviews/{seeded['assigned_interviews'][(candidate_a.id, position_x.id)].id}",
            headers=headers,
        )
        position_x_pipeline = await client.get(
            f"/positions/{position_x.id}/applications", headers=headers
        )
        candidate_a_apps = await client.get(
            f"/candidates/{candidate_a.id}/applications", headers=headers
        )
        y_search = await client.get(
            "/candidate-search",
            headers=headers,
            params={"position_id": str(position_y.id)},
        )
        candidate_a_best_positions = await client.get(
            f"/candidates/{candidate_a.id}/best-positions", headers=headers
        )
        unassigned_position_read = await client.get(
            f"/positions/{unassigned_position.id}", headers=headers
        )
        crossed_move = await client.patch(
            "/positions/move",
            headers=headers,
            json={
                "position_ids": [str(position_x.id), str(unassigned_position.id)],
                "folder_id": None,
            },
        )
        crossed_stage = await client.patch(
            f"/positions/{position_y.id}/candidates/{candidate_a.id}/stage",
            headers=headers,
            json={
                "stage": "screening",
                "expected_version": crossed_version,
                "reason": "crossed pair must not mutate",
            },
        )
        crossed_bulk = await client.post(
            "/candidate-bulk",
            headers=headers,
            json={
                "candidate_ids": [str(candidate_a.id)],
                "action": "stage",
                "position_id": str(position_y.id),
                "stage": "screening",
                "reason": "body-only crossed pair must not mutate",
            },
        )
        crossed_create = await client.post(
            "/applications",
            headers=headers,
            json={
                "candidate_id": str(candidate_a.id),
                "position_id": str(position_y.id),
                "source": "manual",
            },
        )
        crossed_chat = await client.post(
            "/chat/sessions",
            headers=headers,
            json={
                "scope": "candidate",
                "candidate_id": str(candidate_a.id),
                "position_id": str(position_y.id),
                "title": "crossed relationship must fail",
            },
        )
        valid_chat = await client.post(
            "/chat/sessions",
            headers=headers,
            json={
                "scope": "candidate",
                "candidate_id": str(candidate_a.id),
                "position_id": str(position_x.id),
                "title": "assigned relationship",
            },
        )
        valid_stage = await client.patch(
            f"/positions/{position_x.id}/candidates/{candidate_a.id}/stage",
            headers=headers,
            json={
                "stage": "screening",
                "expected_version": valid_application.updated_at.isoformat(),
                "reason": "assigned pair may mutate",
            },
        )

    assert applications.status_code == 200
    assert assigned_interview.status_code == 200
    assert {
        (item["candidate_id"], item["position_id"])
        for item in applications.json()["items"]
    } == {
        (str(candidate_a.id), str(position_x.id)),
        (str(candidate_b.id), str(position_y.id)),
    }
    assert position_x_pipeline.status_code == 200
    assert [
        item["candidate_id"] for item in position_x_pipeline.json()["items"]
    ] == [str(candidate_a.id)]
    assert candidate_a_apps.status_code == 200
    assert [
        item["position_id"] for item in candidate_a_apps.json()["items"]
    ] == [str(position_x.id)]
    assert y_search.status_code == 200
    assert [item["id"] for item in y_search.json()["items"]] == [
        str(candidate_b.id)
    ]
    assert candidate_a_best_positions.status_code == 200
    assert [
        item["position_id"]
        for item in candidate_a_best_positions.json()["items"]
    ] == [str(position_x.id)]
    assert unassigned_position_read.status_code == 404
    assert crossed_move.status_code == 404
    assert crossed_stage.status_code == 404
    assert crossed_bulk.status_code == 404
    assert crossed_create.status_code == 404
    assert crossed_chat.status_code == 404
    # ChatSession has no authoritative application FK. Even a matching A/X
    # row is ambiguous across reapplication cycles and is unavailable to the
    # interviewer pilot role.
    assert valid_chat.status_code == 404
    assert valid_stage.status_code == 200
    assert valid_stage.json()["stage"] == "screening"

    await db.refresh(crossed_application)
    assert crossed_application.stage == "rejected"
    crossed_pair_count = len(
        list(
            (
                await db.execute(
                    select(Application.id).where(
                        Application.organization_id == seeded["organization"].id,
                        Application.candidate_id == candidate_a.id,
                        Application.position_id == position_y.id,
                    )
                )
            ).scalars()
        )
    )
    assert crossed_pair_count == 1
    chat_pairs = set(
        (
            await db.execute(
                select(ChatSession.candidate_id, ChatSession.position_id).where(
                    ChatSession.organization_id == seeded["organization"].id,
                    ChatSession.user_id == interviewer.id,
                )
            )
        ).all()
    )
    assert chat_pairs == set()
    await db.refresh(position_x)
    await db.refresh(unassigned_position)
    assert position_x.folder == "assigned-original"
    assert unassigned_position.folder == "unassigned-original"


async def test_interviewer_task_routes_reject_crossed_pair_without_side_effects(
    db, monkeypatch
):
    seeded = await _pair_graph(db)
    interviewer = seeded["interviewer"]
    candidate_a = seeded["candidate_a"]
    position_x = seeded["position_x"]
    position_y = seeded["position_y"]
    application_x = seeded["pairs"][(candidate_a.id, position_x.id)][0]
    application_y = seeded["pairs"][(candidate_a.id, position_y.id)][0]
    valid_task = TaskSubmission(
        organization_id=seeded["organization"].id,
        candidate_id=candidate_a.id,
        position_id=position_x.id,
        application_id=application_x.id,
        created_by=seeded["recruiter"].id,
        title="Assigned A/X task",
        submission_type="text",
        content="ASSIGNED-SUBMISSION",
        status="submitted",
    )
    crossed_task = TaskSubmission(
        organization_id=seeded["organization"].id,
        candidate_id=candidate_a.id,
        position_id=position_y.id,
        application_id=application_y.id,
        created_by=seeded["recruiter"].id,
        title="CROSSED-SUBMISSION-SECRET",
        submission_type="file",
        file_path="private/crossed-task-canary.txt",
        content="CROSSED-SUBMISSION-SECRET",
        status="submitted",
        ai_score=99,
        ai_summary="CROSSED-TASK-EVALUATION-SECRET",
    )
    valid_recording = InterviewRecording(
        organization_id=seeded["organization"].id,
        candidate_id=candidate_a.id,
        position_id=position_x.id,
        application_id=application_x.id,
        created_by=seeded["recruiter"].id,
        title="Assigned A/X recording",
        status="pending",
    )
    db.add_all(
        [
            valid_task,
            crossed_task,
            valid_recording,
            EnterprisePolicy(
                organization_id=seeded["organization"].id,
                field_visibility={
                    FieldClass.ASSESSMENT_RESULT.value: ["interviewer"],
                    FieldClass.CANDIDATE_DOCUMENT.value: ["interviewer"],
                    FieldClass.CONTACT.value: ["interviewer"],
                    FieldClass.AI_CONTEXT.value: ["interviewer"],
                    FieldClass.COMPENSATION.value: ["interviewer"],
                },
            ),
        ]
        + [
            PermissionOverride(
                organization_id=seeded["organization"].id,
                role="interviewer",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "interviews.manage",
                "assessments.grade",
                "ai.use",
                "ai.sensitive_context",
                "candidates.contact",
                "candidates.documents",
                "requisitions.compensation",
            )
        ]
    )
    await db.commit()
    crossed_id = crossed_task.id

    async def forbidden_store(*_args, **_kwargs):
        raise AssertionError("crossed task upload reached private storage")

    monkeypatch.setattr("app.routers.tasks.store_private_upload", forbidden_store)
    headers = _auth(interviewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get(
            f"/candidates/{candidate_a.id}/tasks", headers=headers
        )
        valid_detail = await client.get(
            f"/tasks/{valid_task.id}", headers=headers
        )
        crossed_detail = await client.get(f"/tasks/{crossed_id}", headers=headers)
        crossed_patch = await client.patch(
            f"/tasks/{crossed_id}",
            headers=headers,
            json={"title": "MUTATED-CROSSED-TASK"},
        )
        crossed_evaluate = await client.post(
            f"/tasks/{crossed_id}/evaluate", headers=headers
        )
        crossed_upload = await client.post(
            f"/tasks/{crossed_id}/file",
            headers=headers,
            files={"file": ("evidence.txt", b"crossed upload", "text/plain")},
        )
        crossed_download = await client.get(
            f"/tasks/{crossed_id}/file", headers=headers
        )
        crossed_file_delete = await client.delete(
            f"/tasks/{crossed_id}/file", headers=headers
        )
        crossed_delete = await client.delete(f"/tasks/{crossed_id}", headers=headers)
        valid_retarget = await client.patch(
            f"/tasks/{valid_task.id}",
            headers=headers,
            json={"position_id": str(position_y.id)},
        )
        positionless_task_create = await client.post(
            f"/candidates/{candidate_a.id}/tasks",
            headers=headers,
            json={"title": "Invisible task must not be created"},
        )
        positionless_task_patch = await client.patch(
            f"/tasks/{valid_task.id}",
            headers=headers,
            json={"position_id": None},
        )
        positionless_recording_create = await client.post(
            f"/candidates/{candidate_a.id}/recordings",
            headers=headers,
            json={"title": "Invisible recording must not be created"},
        )
        positionless_recording_patch = await client.patch(
            f"/recordings/{valid_recording.id}",
            headers=headers,
            json={"position_id": None},
        )
        incomplete_chats = [
            await client.post(
                "/chat/sessions",
                headers=headers,
                json={"scope": "free", "title": "Invisible free chat"},
            ),
            await client.post(
                "/chat/sessions",
                headers=headers,
                json={
                    "scope": "candidate",
                    "candidate_id": str(candidate_a.id),
                    "title": "Invisible candidate chat",
                },
            ),
            await client.post(
                "/chat/sessions",
                headers=headers,
                json={
                    "scope": "position",
                    "position_id": str(position_x.id),
                    "title": "Invisible position chat",
                },
            ),
        ]
        crossed_merge = await client.post(
            f"/candidates/{seeded['candidate_b'].id}/merge",
            headers=headers,
            json={"duplicate_ids": [str(candidate_a.id)], "score_winners": []},
        )

    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert "CROSSED-SUBMISSION-SECRET" not in listed.text
    for response in (
        crossed_detail,
        valid_detail,
        crossed_patch,
        crossed_evaluate,
        crossed_upload,
        crossed_download,
        crossed_file_delete,
        crossed_delete,
        valid_retarget,
        positionless_task_create,
        positionless_task_patch,
        positionless_recording_create,
        positionless_recording_patch,
        *incomplete_chats,
    ):
        assert response.status_code == 404, response.text
    # Assignment middleware rejects organization-wide identity rewrites before
    # the handler and keeps the denied resource non-enumerating.
    assert crossed_merge.status_code == 404

    persisted = await db.get(TaskSubmission, crossed_id)
    assert persisted is not None
    assert persisted.title == "CROSSED-SUBMISSION-SECRET"
    assert persisted.file_path == "private/crossed-task-canary.txt"
    assert persisted.candidate_id == candidate_a.id
    await db.refresh(valid_task)
    assert valid_task.position_id == position_x.id
    await db.refresh(valid_recording)
    assert valid_recording.position_id == position_x.id


async def test_all_legacy_candidate_position_artifacts_use_exact_assigned_pairs(db):
    seeded = await _pair_graph(db)
    organization_id = seeded["organization"].id
    recruiter = seeded["recruiter"]
    interviewer = seeded["interviewer"]
    candidate_a = seeded["candidate_a"]
    position_x = seeded["position_x"]
    position_y = seeded["position_y"]
    applications_by_position_id = {
        position_x.id: seeded["pairs"][(candidate_a.id, position_x.id)][0],
        position_y.id: seeded["pairs"][(candidate_a.id, position_y.id)][0],
    }
    assigned_interview = seeded["assigned_interviews"][(candidate_a.id, position_x.id)]
    now = datetime.now(UTC)

    def application_id_for(position: Position) -> UUID:
        return applications_by_position_id[position.id].id

    def activity(position: Position, label: str) -> CandidateActivity:
        return CandidateActivity(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            actor_user_id=recruiter.id,
            actor_kind="human",
            position_id=position.id,
            application_id=application_id_for(position),
            event_type="note_added",
            subject_type="candidate",
            subject_id=candidate_a.id,
            payload={"canary": label},
            occurred_at=now,
        )

    def processing_job(position: Position, label: str) -> CandidateProcessingJob:
        return CandidateProcessingJob(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            created_by_user_id=recruiter.id,
            kind="score",
            status="queued",
            idempotency_key=f"pair-job:{label}:{uuid4()}",
        )

    def chat(position: Position, label: str) -> ChatSession:
        return ChatSession(
            organization_id=organization_id,
            user_id=interviewer.id,
            scope="candidate",
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            title=label,
        )

    def thread(position: Position, _label: str) -> CommunicationThread:
        return CommunicationThread(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            channel="email",
            status="open",
        )

    def recording(position: Position, label: str) -> InterviewRecording:
        return InterviewRecording(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            created_by=recruiter.id,
            title=label,
            status="transcribed",
            transcript=label,
        )

    def scheduling_link(position: Position, label: str) -> SchedulingLink:
        return SchedulingLink(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            created_by_user_id=recruiter.id,
            organizer_user_id=recruiter.id,
            token_digest=f"{label[:1]}{uuid4().hex}".ljust(64, "0")[:64],
            duration_minutes=60,
            slot_interval_minutes=30,
            min_notice_minutes=0,
            booking_window_days=14,
            buffer_minutes=0,
            location_mode="video",
            reminder_offsets=[60],
            expires_at=now + timedelta(days=7),
        )

    def appointment(position: Position, label: str) -> InterviewAppointment:
        return InterviewAppointment(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            organizer_user_id=recruiter.id,
            status="scheduled",
            starts_at=now + timedelta(days=1),
            ends_at=now + timedelta(days=1, hours=1),
            candidate_timezone="Asia/Dushanbe",
            location_mode="video",
            position_title_snapshot=label,
            provider="local",
            sync_status="local",
            manage_token_digest=uuid4().hex.ljust(64, "0"),
            manage_token_expires_at=now + timedelta(days=30),
            idempotency_key=f"pair-appointment:{label}:{uuid4()}",
        )

    def receipt(position: Position, label: str) -> SourceCandidateReceipt:
        return SourceCandidateReceipt(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            external_id=f"pair-receipt:{label}:{uuid4()}",
            provider_snapshot="csv",
            source_url=f"https://example.test/{label}",
            received_at=now,
        )

    def task(position: Position, label: str) -> TaskSubmission:
        return TaskSubmission(
            organization_id=organization_id,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            created_by=recruiter.id,
            title=label,
            submission_type="text",
            content=label,
            status="submitted",
        )

    def workflow_run(position: Position, label: str) -> WorkflowRun:
        return WorkflowRun(
            organization_id=organization_id,
            rule_id_snapshot=uuid4(),
            rule_version_snapshot=1,
            candidate_id=candidate_a.id,
            position_id=position.id,
            application_id=application_id_for(position),
            rule_name_snapshot=label,
            trigger_snapshot="candidate_added",
            conditions_snapshot={},
            actions_snapshot=[],
            dedupe_key=f"pair-run:{label}:{uuid4()}",
            status="queued",
            action_results=[],
            started_at=now,
        )

    factories = (
        (CandidateActivity, activity),
        (CandidateProcessingJob, processing_job),
        (ChatSession, chat),
        (CommunicationThread, thread),
        (InterviewAppointment, appointment),
        (InterviewRecording, recording),
        (SchedulingLink, scheduling_link),
        (SourceCandidateReceipt, receipt),
        (TaskSubmission, task),
        (WorkflowRun, workflow_run),
    )
    valid_rows: dict[type, object] = {}
    crossed_rows: dict[type, object] = {}
    rows = []
    for model, factory in factories:
        valid = factory(position_x, f"ASSIGNED-{model.__name__}")
        crossed = factory(position_y, f"CROSSED-{model.__name__}-SECRET")
        rows.extend((valid, crossed))
        valid_rows[model] = valid
        crossed_rows[model] = crossed
    db.add_all(rows)
    await db.flush()
    expected_ids = {model: row.id for model, row in valid_rows.items()}
    crossed_ids = {model: row.id for model, row in crossed_rows.items()}
    valid_thread = valid_rows[CommunicationThread]
    crossed_thread = crossed_rows[CommunicationThread]
    valid_message = CommunicationMessage(
        organization_id=organization_id,
        thread_id=valid_thread.id,
        candidate_id=candidate_a.id,
        actor_user_id=recruiter.id,
        direction="outbound",
        channel="email",
        status="sent",
        body="ASSIGNED-COMMUNICATION-MESSAGE",
        idempotency_key=f"pair-message:{uuid4()}",
    )
    crossed_message = CommunicationMessage(
        organization_id=organization_id,
        thread_id=crossed_thread.id,
        candidate_id=candidate_a.id,
        actor_user_id=recruiter.id,
        direction="outbound",
        channel="email",
        status="sent",
        body="CROSSED-COMMUNICATION-MESSAGE-SECRET",
        idempotency_key=f"pair-message:{uuid4()}",
    )
    crossed_chat_message = Message(
        organization_id=organization_id,
        session_id=crossed_rows[ChatSession].id,
        role="assistant",
        content="CROSSED-CHAT-MESSAGE-SECRET",
        purpose="chat",
    )
    positionless_task = TaskSubmission(
        organization_id=organization_id,
        candidate_id=candidate_a.id,
        position_id=None,
        created_by=recruiter.id,
        title="POSITIONLESS-TASK-SECRET",
        submission_type="text",
        content="POSITIONLESS-TASK-SECRET",
        status="submitted",
    )
    positionless_activity = CandidateActivity(
        organization_id=organization_id,
        candidate_id=candidate_a.id,
        actor_user_id=recruiter.id,
        actor_kind="human",
        position_id=None,
        event_type="note_added",
        subject_type="candidate",
        subject_id=candidate_a.id,
        payload={"canary": "POSITIONLESS-ACTIVITY-SECRET"},
        occurred_at=now,
    )
    candidate_note = CandidateNote(
        organization_id=organization_id,
        candidate_id=candidate_a.id,
        author_user_id=recruiter.id,
        content="CANDIDATE-GLOBAL-NOTE-SECRET",
    )
    candidate_tag = CandidateTag(
        organization_id=organization_id,
        candidate_id=candidate_a.id,
        label="global-secret",
        created_by=recruiter.id,
    )
    contact_point = CandidateContactPoint(
        organization_id=organization_id,
        candidate_id=candidate_a.id,
        created_by_user_id=recruiter.id,
        channel="email",
        value="global-secret@example.test",
        normalized_value="global-secret@example.test",
        source="manual",
        is_primary=True,
    )
    talent_pool = TalentPool(
        organization_id=organization_id,
        created_by_user_id=recruiter.id,
        name=f"Global candidate pool {uuid4().hex[:8]}",
    )
    db.add(talent_pool)
    await db.flush()
    talent_member = TalentPoolMember(
        organization_id=organization_id,
        pool_id=talent_pool.id,
        candidate_id=candidate_a.id,
        added_by_user_id=recruiter.id,
        status="active",
    )
    unassigned_application = applications_by_position_id[position_y.id]
    unassigned_interview = Interview(
        organization_id=organization_id,
        application_id=unassigned_application.id,
        candidate_id=candidate_a.id,
        position_id=position_y.id,
        created_by=recruiter.id,
    )
    db.add(unassigned_interview)
    await db.flush()
    linked_scheduling = scheduling_link(
        position_x, "ASSIGNED-INTERVIEW-SCHEDULING"
    )
    linked_scheduling.interview_id = assigned_interview.id
    linked_appointment = appointment(
        position_x, "ASSIGNED-INTERVIEW-APPOINTMENT"
    )
    linked_appointment.interview_id = assigned_interview.id
    unassigned_parent_scheduling = scheduling_link(
        position_y, "CROSSED-UNASSIGNED-PARENT-SCHEDULING-SECRET"
    )
    unassigned_parent_scheduling.interview_id = unassigned_interview.id
    unassigned_parent_appointment = appointment(
        position_y, "CROSSED-UNASSIGNED-PARENT-APPOINTMENT-SECRET"
    )
    unassigned_parent_appointment.interview_id = unassigned_interview.id
    db.add_all(
        [
            valid_message,
            crossed_message,
            crossed_chat_message,
            positionless_task,
            positionless_activity,
            candidate_note,
            candidate_tag,
            contact_point,
            talent_member,
            linked_scheduling,
            linked_appointment,
            unassigned_parent_scheduling,
            unassigned_parent_appointment,
            EnterprisePolicy(
                organization_id=organization_id,
                field_visibility={
                    FieldClass.CONTACT.value: ["interviewer"],
                    FieldClass.CANDIDATE_DOCUMENT.value: ["interviewer"],
                    FieldClass.ASSESSMENT_RESULT.value: ["interviewer"],
                    FieldClass.AI_CONTEXT.value: ["interviewer"],
                    FieldClass.COMPENSATION.value: ["interviewer"],
                },
            ),
        ]
        + [
            PermissionOverride(
                organization_id=organization_id,
                role="interviewer",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "ai.use",
                "ai.sensitive_context",
                "candidates.contact",
                "candidates.documents",
                "communications.read",
                "scheduling.manage",
                "talent.read",
                "workflows.read",
                "requisitions.compensation",
            )
        ]
    )
    await db.commit()

    await set_tenant_context(
        db,
        organization_id=organization_id,
        actor_id=interviewer.id,
        identity_kind="user",
        role="interviewer",
    )
    for model, _factory in factories:
        visible_ids = set((await db.execute(select(model.id))).scalars())
        assert expected_ids[model] not in visible_ids
        assert crossed_ids[model] not in visible_ids
    visible_message_ids = set(
        (await db.execute(select(CommunicationMessage.id))).scalars()
    )
    assert valid_message.id not in visible_message_ids
    assert crossed_message.id not in visible_message_ids
    assert linked_scheduling.id in set(
        (await db.execute(select(SchedulingLink.id))).scalars()
    )
    assert linked_appointment.id in set(
        (await db.execute(select(InterviewAppointment.id))).scalars()
    )
    assert unassigned_parent_scheduling.id not in set(
        (await db.execute(select(SchedulingLink.id))).scalars()
    )
    assert unassigned_parent_appointment.id not in set(
        (await db.execute(select(InterviewAppointment.id))).scalars()
    )
    for model, identifier in (
        (TaskSubmission, positionless_task.id),
        (CandidateActivity, positionless_activity.id),
        (CandidateNote, candidate_note.id),
        (CandidateTag, candidate_tag.id),
        (CandidateContactPoint, contact_point.id),
        (TalentPoolMember, talent_member.id),
    ):
        assert identifier not in set((await db.execute(select(model.id))).scalars())
    clear_tenant_context(db)

    headers = _auth(interviewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        activities = await client.get(
            f"/candidates/{candidate_a.id}/activities", headers=headers
        )
        notes = await client.get(
            f"/candidates/{candidate_a.id}/notes", headers=headers
        )
        tags = await client.get(
            f"/candidates/{candidate_a.id}/tags", headers=headers
        )
        contacts = await client.get(
            f"/candidates/{candidate_a.id}/contact-points", headers=headers
        )
        tasks = await client.get(
            f"/candidates/{candidate_a.id}/tasks", headers=headers
        )
        recordings = await client.get(
            f"/candidates/{candidate_a.id}/recordings", headers=headers
        )
        crossed_recording = await client.get(
            f"/recordings/{crossed_ids[InterviewRecording]}", headers=headers
        )
        valid_recording = await client.get(
            f"/recordings/{expected_ids[InterviewRecording]}", headers=headers
        )
        crossed_job = await client.get(
            f"/candidates/{candidate_a.id}/processing-jobs/"
            f"{crossed_ids[CandidateProcessingJob]}",
            headers=headers,
        )
        valid_job = await client.get(
            f"/candidates/{candidate_a.id}/processing-jobs/"
            f"{expected_ids[CandidateProcessingJob]}",
            headers=headers,
        )
        links = await client.get(
            f"/candidates/{candidate_a.id}/scheduling-links", headers=headers
        )
        appointments = await client.get(
            f"/candidates/{candidate_a.id}/appointments", headers=headers
        )
        threads = await client.get(
            f"/candidates/{candidate_a.id}/communication-threads", headers=headers
        )
        inbox = await client.get("/communications/threads", headers=headers)
        crossed_messages = await client.get(
            f"/communications/threads/{crossed_thread.id}/messages",
            headers=headers,
        )
        valid_messages = await client.get(
            f"/communications/threads/{valid_thread.id}/messages",
            headers=headers,
        )
        runs = await client.get("/workflows/runs", headers=headers)
        chat_sessions = await client.get("/chat/sessions", headers=headers)
        crossed_chat = await client.get(
            f"/chat/sessions/{crossed_rows[ChatSession].id}", headers=headers
        )
        valid_chat = await client.get(
            f"/chat/sessions/{valid_rows[ChatSession].id}", headers=headers
        )
        pool_members = await client.get(
            f"/talent/pools/{talent_pool.id}/members", headers=headers
        )

    for response in (
        activities,
        notes,
        tags,
        contacts,
        tasks,
        recordings,
        links,
        appointments,
        threads,
        inbox,
        chat_sessions,
    ):
        assert response.status_code == 200, (
            response.request.method,
            str(response.request.url),
            response.text,
        )
        assert "CROSSED-" not in response.text
        assert "POSITIONLESS-" not in response.text
        assert "GLOBAL" not in response.text.upper()
    assert runs.status_code == 404
    assert "CROSSED-" not in runs.text
    assert pool_members.status_code == 404
    assert "global-secret" not in pool_members.text
    assert crossed_recording.status_code == 404
    assert valid_recording.status_code == 404
    assert crossed_job.status_code == 404
    assert valid_job.status_code == 404
    assert crossed_messages.status_code == 404
    assert valid_messages.status_code == 404
    assert crossed_chat.status_code == 404
    assert valid_chat.status_code == 404
    assert "CROSSED-CHAT-MESSAGE-SECRET" not in chat_sessions.text
    assert str(linked_scheduling.id) in links.text
    assert str(linked_appointment.id) in appointments.text
    assert "CROSSED-UNASSIGNED-PARENT" not in links.text
    assert "CROSSED-UNASSIGNED-PARENT" not in appointments.text


async def test_score_all_open_never_enqueues_crossed_candidate_position_pair(db):
    seeded = await _pair_graph(db)
    candidate_a = seeded["candidate_a"]
    position_y = seeded["position_y"]
    crossed_application, crossed_score = seeded["pairs"][
        (candidate_a.id, position_y.id)
    ]
    await db.delete(crossed_score)
    crossed_application.legacy_score_id = None
    db.add_all(
        [
            EnterprisePolicy(
                organization_id=seeded["organization"].id,
                field_visibility={
                    FieldClass.AI_CONTEXT.value: ["interviewer"],
                    FieldClass.CONTACT.value: ["interviewer"],
                    FieldClass.CANDIDATE_DOCUMENT.value: ["interviewer"],
                    FieldClass.ASSESSMENT_RESULT.value: ["interviewer"],
                    FieldClass.COMPENSATION.value: ["interviewer"],
                },
            ),
        ]
        + [
            PermissionOverride(
                organization_id=seeded["organization"].id,
                role="interviewer",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "ai.use",
                "ai.sensitive_context",
                "candidates.contact",
                "candidates.documents",
                "requisitions.compensation",
            )
        ]
    )
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/candidates/{candidate_a.id}/score-all-open",
            headers=_auth(seeded["interviewer"]),
        )
    assert response.status_code == 404, response.text
    jobs = list(
        (
            await db.execute(
                select(CandidateProcessingJob).where(
                    CandidateProcessingJob.organization_id
                    == seeded["organization"].id,
                    CandidateProcessingJob.candidate_id == candidate_a.id,
                    CandidateProcessingJob.position_id == position_y.id,
                )
            )
        ).scalars()
    )
    assert jobs == []


async def test_interviewer_candidate_global_mutations_fail_before_side_effects(db):
    seeded = await _pair_graph(db)
    organization_id = seeded["organization"].id
    interviewer = seeded["interviewer"]
    hiring_manager = await _user(
        db, organization_id, "hiring_manager", "global-mutation-manager"
    )
    candidate = seeded["candidate_a"]
    position = seeded["position_x"]
    pool = TalentPool(
        organization_id=organization_id,
        created_by_user_id=seeded["recruiter"].id,
        name=f"Protected CRM pool {uuid4().hex[:8]}",
    )
    sequence = TalentNurtureSequence(
        organization_id=organization_id,
        pool_id=pool.id,
        created_by_user_id=seeded["recruiter"].id,
        name=f"Protected nurture {uuid4().hex[:8]}",
    )
    original_contact = CandidateContactPoint(
        organization_id=organization_id,
        candidate_id=candidate.id,
        created_by_user_id=seeded["recruiter"].id,
        channel="email",
        value="original-primary@example.test",
        normalized_value="original-primary@example.test",
        source="manual",
        is_primary=True,
    )
    requisition = Requisition(
        organization_id=organization_id,
        created_by_user_id=seeded["recruiter"].id,
        hiring_manager_user_id=hiring_manager.id,
        position_id=position.id,
        title=position.title,
        business_justification="Assigned position for global mutation regression",
    )
    db.add_all(
        [
            pool,
            sequence,
            original_contact,
            requisition,
            EnterprisePolicy(
                organization_id=organization_id,
                field_visibility={
                    FieldClass.CONTACT.value: ["interviewer", "hiring_manager"]
                },
            ),
        ]
        + [
            PermissionOverride(
                organization_id=organization_id,
                role="interviewer",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "candidates.contact",
                "talent.manage",
                "communications.read",
                "communications.send",
                "scheduling.manage",
            )
        ]
        + [
            PermissionOverride(
                organization_id=organization_id,
                role="hiring_manager",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "candidates.write",
                "candidates.contact",
                "talent.manage",
                "communications.read",
                "communications.send",
                "scheduling.manage",
            )
        ]
    )
    await db.commit()

    headers = _auth(interviewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.post(
                f"/candidates/{candidate.id}/notes",
                headers=headers,
                json={"content": "MUST-NOT-BE-CREATED"},
            ),
            await client.post(
                f"/candidates/{candidate.id}/tags",
                headers=headers,
                json={"label": "must-not-be-created"},
            ),
            await client.post(
                f"/candidates/{candidate.id}/contact-points",
                headers=headers,
                json={
                    "channel": "email",
                    "value": "must-not-be-created@example.test",
                    "is_primary": True,
                },
            ),
            await client.post(
                f"/talent/pools/{pool.id}/members",
                headers=headers,
                json={"candidate_ids": [str(candidate.id)]},
            ),
            await client.post(
                "/candidate-bulk",
                headers=headers,
                json={
                    "candidate_ids": [str(candidate.id)],
                    "action": "tag_add",
                    "tag": "must-not-be-created-bulk",
                },
            ),
            await client.post(
                f"/candidates/{candidate.id}/communications/preview",
                headers=headers,
                json={
                    "channel": "email",
                    "position_id": str(position.id),
                    "body": "MUST-NOT-BE-SENT",
                },
            ),
            await client.post(
                f"/candidates/{candidate.id}/scheduling-links",
                headers=headers,
                json={
                    "position_id": str(position.id),
                    "interviewer_user_ids": [str(interviewer.id)],
                    "min_notice_minutes": 0,
                },
            ),
            await client.post(
                f"/candidates/{candidate.id}/view",
                headers=headers,
            ),
            await client.post(
                f"/talent/nurture-sequences/{sequence.id}/launch",
                headers=headers,
                json={
                    "candidate_ids": [str(candidate.id)],
                    "position_id": str(position.id),
                },
            ),
        ]
    assert all(response.status_code == 404 for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    manager_headers = _auth(hiring_manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        manager_responses = [
            await client.post(
                f"/candidates/{candidate.id}/notes",
                headers=manager_headers,
                json={"content": "MANAGER-MUST-NOT-BE-CREATED"},
            ),
            await client.post(
                f"/candidates/{candidate.id}/contact-points",
                headers=manager_headers,
                json={
                    "channel": "email",
                    "value": "manager-must-not-be-created@example.test",
                    "is_primary": True,
                },
            ),
            await client.post(
                f"/talent/pools/{pool.id}/members",
                headers=manager_headers,
                json={"candidate_ids": [str(candidate.id)]},
            ),
            await client.post(
                f"/candidates/{candidate.id}/view",
                headers=manager_headers,
            ),
        ]
    assert all(response.status_code == 404 for response in manager_responses), [
        (response.status_code, response.text) for response in manager_responses
    ]

    assert (
        await db.execute(
            select(CandidateNote.id).where(
                CandidateNote.organization_id == organization_id,
                CandidateNote.candidate_id == candidate.id,
            )
        )
    ).scalars().all() == []
    assert (
        await db.execute(
            select(TalentNurtureEnrollment.id).where(
                TalentNurtureEnrollment.organization_id == organization_id,
                TalentNurtureEnrollment.sequence_id == sequence.id,
            )
        )
    ).scalars().all() == []
    await db.refresh(candidate)
    assert candidate.viewed_at is None
    assert (
        await db.execute(
            select(CandidateTag.id).where(
                CandidateTag.organization_id == organization_id,
                CandidateTag.candidate_id == candidate.id,
            )
        )
    ).scalars().all() == []
    contacts = list(
        (
            await db.execute(
                select(CandidateContactPoint).where(
                    CandidateContactPoint.organization_id == organization_id,
                    CandidateContactPoint.candidate_id == candidate.id,
                )
            )
        ).scalars()
    )
    assert [(contact.id, contact.is_primary) for contact in contacts] == [
        (original_contact.id, True)
    ]
    assert (
        await db.execute(
            select(TalentPoolMember.id).where(
                TalentPoolMember.organization_id == organization_id,
                TalentPoolMember.pool_id == pool.id,
            )
        )
    ).scalars().all() == []
    assert (
        await db.execute(
            select(SchedulingLink.id).where(
                SchedulingLink.organization_id == organization_id,
                SchedulingLink.created_by_user_id == interviewer.id,
            )
        )
    ).scalars().all() == []


async def test_legacy_evidence_is_position_bound_and_fails_closed_across_cycles(db):
    seeded = await _pair_graph(db)
    candidate = seeded["candidate_a"]
    position_x = seeded["position_x"]
    position_y = seeded["position_y"]
    application_x = seeded["pairs"][(candidate.id, position_x.id)][0]
    application_y = seeded["pairs"][(candidate.id, position_y.id)][0]
    now = datetime.now(UTC)
    db.add_all(
        [
            TaskSubmission(
                organization_id=seeded["organization"].id,
                candidate_id=candidate.id,
                position_id=position_x.id,
                application_id=application_x.id,
                created_by=seeded["recruiter"].id,
                title="POSITION-X-EVIDENCE",
                submission_type="text",
                content="x",
                status="submitted",
                ai_score=80,
                ai_summary="POSITION-X-EVIDENCE-SUMMARY",
            ),
            TaskSubmission(
                organization_id=seeded["organization"].id,
                candidate_id=candidate.id,
                position_id=position_y.id,
                application_id=application_y.id,
                created_by=seeded["recruiter"].id,
                title="POSITION-Y-EVIDENCE-SECRET",
                submission_type="text",
                content="y",
                status="submitted",
                ai_score=99,
                ai_summary="POSITION-Y-EVIDENCE-SECRET",
            ),
            InterviewRecording(
                organization_id=seeded["organization"].id,
                candidate_id=candidate.id,
                position_id=position_x.id,
                application_id=application_x.id,
                created_by=seeded["recruiter"].id,
                title="POSITION-X-RECORDING",
                status="scored",
                interview_score=75,
                interview_summary="POSITION-X-RECORDING-SUMMARY",
            ),
            InterviewRecording(
                organization_id=seeded["organization"].id,
                candidate_id=candidate.id,
                position_id=position_y.id,
                application_id=application_y.id,
                created_by=seeded["recruiter"].id,
                title="POSITION-Y-RECORDING-SECRET",
                status="scored",
                interview_score=100,
                interview_summary="POSITION-Y-RECORDING-SECRET",
            ),
        ]
    )
    await db.commit()

    position_x_evidence = await build_candidate_evidence(
        db,
        candidate,
        application_id=application_x.id,
        position_id=position_x.id,
    )
    assert "POSITION-X-EVIDENCE" in position_x_evidence
    assert "POSITION-X-RECORDING" in position_x_evidence
    assert "POSITION-Y-EVIDENCE-SECRET" not in position_x_evidence
    assert "POSITION-Y-RECORDING-SECRET" not in position_x_evidence

    application_x.stage = "rejected"
    application_x.closed_at = now
    second_cycle = Application(
        organization_id=seeded["organization"].id,
        candidate_id=candidate.id,
        position_id=position_x.id,
        stage="new",
        source="manual",
        cycle_number=2,
        created_by_user_id=seeded["recruiter"].id,
    )
    db.add(second_cycle)
    await db.commit()
    ambiguous_evidence = await build_candidate_evidence(
        db,
        candidate,
        application_id=second_cycle.id,
        position_id=position_x.id,
    )
    assert "POSITION-X-EVIDENCE" not in ambiguous_evidence
    assert "POSITION-X-RECORDING" not in ambiguous_evidence


async def test_mission_candidate_assessments_and_evidence_use_exact_pair(db):
    seeded = await _pair_graph(db)
    organization_id = seeded["organization"].id
    interviewer = seeded["interviewer"]
    candidate_a = seeded["candidate_a"]
    candidate_b = seeded["candidate_b"]
    position_y = seeded["position_y"]
    session = AiSession(
        organization_id=organization_id,
        created_by_user_id=interviewer.id,
        title="Exact pair mission",
        status="active",
    )
    db.add(session)
    await db.flush()
    mission = HiringMission(
        organization_id=organization_id,
        session_id=session.id,
        created_by_user_id=interviewer.id,
        position_id=position_y.id,
        title="Position Y mission",
        original_request="Rank assigned candidates for position Y",
        brief={},
        rubric={},
        assumptions=[],
        blockers=[],
        headcount=1,
        shortlist_size=2,
        status="running",
        current_plan_version=1,
    )
    db.add(mission)
    await db.flush()
    plan = MissionPlanVersion(
        organization_id=organization_id,
        mission_id=mission.id,
        version=1,
        status="active",
        brief_snapshot={},
        rubric_snapshot={},
        plan={},
        input_hash="a" * 64,
    )
    db.add(plan)
    await db.flush()

    valid_assessment = MissionCandidateAssessment(
        organization_id=organization_id,
        mission_id=mission.id,
        plan_version_id=plan.id,
        plan_version=1,
        candidate_id=candidate_b.id,
        assessment_version=1,
        overall_score=Decimal("81"),
        confidence="high",
        recommendation="fit",
        criteria=[],
        concerns=[],
        missing_evidence=[],
        evidence_coverage=Decimal("0.8"),
        concise_rationale="ASSIGNED-MISSION-ASSESSMENT",
    )
    crossed_assessment = MissionCandidateAssessment(
        organization_id=organization_id,
        mission_id=mission.id,
        plan_version_id=plan.id,
        plan_version=1,
        candidate_id=candidate_a.id,
        assessment_version=1,
        overall_score=Decimal("99"),
        confidence="high",
        recommendation="strong_fit",
        criteria=[],
        concerns=[],
        missing_evidence=[],
        evidence_coverage=Decimal("1"),
        concise_rationale="CROSSED-MISSION-ASSESSMENT-SECRET",
    )
    db.add_all([valid_assessment, crossed_assessment])
    await db.flush()
    valid_claim = EvidenceClaim(
        organization_id=organization_id,
        mission_id=mission.id,
        plan_version_id=plan.id,
        assessment_id=valid_assessment.id,
        candidate_id=candidate_b.id,
        criterion_key="experience",
        claim="ASSIGNED-MISSION-EVIDENCE",
        source_type="candidate_cv",
        source_locator={},
        stance="supports",
        confidence=Decimal("0.9"),
        content_hash="b" * 64,
    )
    crossed_claim = EvidenceClaim(
        organization_id=organization_id,
        mission_id=mission.id,
        plan_version_id=plan.id,
        assessment_id=crossed_assessment.id,
        candidate_id=candidate_a.id,
        criterion_key="experience",
        claim="CROSSED-MISSION-EVIDENCE-SECRET",
        source_type="candidate_cv",
        source_locator={},
        stance="supports",
        confidence=Decimal("1"),
        content_hash="c" * 64,
    )
    db.add_all(
        [
            valid_claim,
            crossed_claim,
            AiOrganizationSettings(
                organization_id=organization_id,
                feature_enabled=True,
                default_headcount=1,
                default_shortlist_size=5,
                hiring_defaults={},
                autonomy_policy={},
            ),
            EnterprisePolicy(
                organization_id=organization_id,
                field_visibility={
                    FieldClass.AI_CONTEXT.value: ["interviewer"],
                    FieldClass.CONTACT.value: ["interviewer"],
                    FieldClass.CANDIDATE_DOCUMENT.value: ["interviewer"],
                    FieldClass.ASSESSMENT_RESULT.value: ["interviewer"],
                    FieldClass.COMPENSATION.value: ["interviewer"],
                },
            ),
        ]
        + [
            PermissionOverride(
                organization_id=organization_id,
                role="interviewer",
                permission=permission,
                allowed=True,
            )
            for permission in (
                "ai.use",
                "ai.sensitive_context",
                "candidates.contact",
                "candidates.documents",
                "requisitions.compensation",
            )
        ]
    )
    await db.commit()

    await set_tenant_context(
        db,
        organization_id=organization_id,
        actor_id=interviewer.id,
        identity_kind="user",
        role="interviewer",
    )
    visible_assessments = set(
        (await db.execute(select(MissionCandidateAssessment.id))).scalars()
    )
    visible_claims = set((await db.execute(select(EvidenceClaim.id))).scalars())
    assert visible_assessments == set()
    assert visible_claims == set()
    clear_tenant_context(db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/ai/v1/missions/{mission.id}/candidates",
            headers=_auth(interviewer),
        )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert "CROSSED-MISSION" not in response.text


async def test_score_bearing_routes_are_catalogued_for_sensitive_read_audit(db):
    seeded = await _redaction_graph(db)
    assessment = FieldClass.ASSESSMENT_RESULT
    mixed_routes = {
        "/candidates",
        "/candidate-search",
        "/candidate-duplicates",
        "/candidate-export",
        "/candidates/{candidate_id}/applications",
        "/candidates/{candidate_id}/merged-sources",
        "/positions/{position_id}/applications",
        "/positions/{position_id}/candidates/{candidate_id}/stage",
        "/talent/pools/{pool_id}/members",
        "/talent/search",
        "/positions/{position_id}/talent-matches",
    }
    assert all(
        assessment in EXTRA_SENSITIVE_READ_FIELDS[path] for path in mixed_routes
    )
    for method, path in (
        ("GET", "/candidates/{candidate_id}/best-positions"),
        ("POST", "/candidate-merges/preview"),
        ("POST", "/candidates/{primary_candidate_id}/merge"),
    ):
        policy = operation_policy(method, path)
        assert policy is not None
        assert assessment in policy.fields

    headers = _auth(seeded["admin"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        candidate_read = await client.get("/candidates", headers=headers)
        pipeline_read = await client.get(
            f"/positions/{seeded['position'].id}/applications", headers=headers
        )
        best_read = await client.get(
            f"/candidates/{seeded['newer_low'].id}/best-positions",
            headers=headers,
        )
    assert candidate_read.status_code == 200
    assert pipeline_read.status_code == 200
    assert best_read.status_code == 200

    audit_rows = list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == seeded["organization"].id,
                    AuditLog.actor_user_id == seeded["admin"].id,
                )
            )
        ).scalars()
    )
    by_path = {row.path_template: row for row in audit_rows}
    for path in (
        "/candidates",
        "/positions/{position_id}/applications",
        "/candidates/{candidate_id}/best-positions",
    ):
        assert by_path[path].metadata_json["sensitive_read"] is True
        assert "assessment_results" in by_path[path].metadata_json["field_classes"]


async def test_contact_document_and_activity_redaction_blocks_all_candidate_side_channels(
    db,
):
    seeded = await _redaction_graph(db)
    recruiter = seeded["recruiter"]
    candidate = seeded["newer_low"]
    phone_canary = "+992900001337"
    source_canary = f"public · Applicant Canary {phone_canary}"
    candidate.source_ref = source_canary
    candidate.parsed_cv = {"applicant_name": "Parsed Applicant Canary"}
    candidate.phone_normalized = phone_canary
    candidate.github_login = "private-github-canary"
    candidate.github_data = {"bio": "PRIVATE-GITHUB-EVIDENCE"}
    candidate.github_fetched_at = datetime.now(UTC)
    policy = (
        await db.execute(
            select(EnterprisePolicy).where(
                EnterprisePolicy.organization_id == seeded["organization"].id
            )
        )
    ).scalar_one()
    policy.field_visibility = {
        FieldClass.CONTACT.value: ["admin"],
        FieldClass.CANDIDATE_DOCUMENT.value: ["admin"],
        FieldClass.ASSESSMENT_RESULT.value: ["admin"],
    }
    candidate_score = (
        await db.execute(
            select(Score).where(
                Score.organization_id == seeded["organization"].id,
                Score.candidate_id == candidate.id,
                Score.position_id == seeded["position"].id,
            )
        )
    ).scalar_one()
    candidate_application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == seeded["organization"].id,
                Application.legacy_score_id == candidate_score.id,
            )
        )
    ).scalar_one()
    candidate_score.owner_user_id = recruiter.id
    candidate_score.stage = "screening"
    for index, (event_type, payload) in enumerate(
        (
            (
                "candidate_scored",
                {
                    "score": 91,
                    "ai_score": 93,
                    "recommendation": "SCORE-PAYLOAD-CANARY",
                    "status": "recommended",
                },
            ),
            (
                "task_evaluated",
                {
                    "score": 88,
                    "evidence": "TASK-EVIDENCE-CANARY",
                    "status": "evaluated",
                },
            ),
            (
                "hiring_decision_voted",
                {
                    "vote": "strong_yes",
                    "recommendation": "DECISION-CANARY",
                    "status": "open",
                },
            ),
        )
    ):
        db.add(
            CandidateActivity(
                organization_id=seeded["organization"].id,
                candidate_id=candidate.id,
                actor_user_id=recruiter.id,
                actor_kind="human",
                position_id=seeded["position"].id,
                application_id=candidate_application.id,
                event_type=event_type,
                subject_type="candidate",
                subject_id=candidate.id,
                payload=payload,
                occurred_at=datetime.now(UTC) + timedelta(seconds=index),
            )
        )
    legacy_notification = Notification(
        organization_id=seeded["organization"].id,
        user_id=recruiter.id,
        type="candidate_applied",
        payload={
            "candidate_id": str(candidate.id),
            "applicant_name": "Applicant Canary",
            "applicant_phone": phone_canary,
            "source_ref": source_canary,
            "nested": {"contact_phone": phone_canary},
        },
    )
    db.add(legacy_notification)
    await db.commit()
    await db.refresh(legacy_notification)

    headers = _auth(recruiter)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        detail = await client.get(f"/candidates/{candidate.id}", headers=headers)
        candidates = await client.get("/candidates", headers=headers)
        search = await client.get("/candidate-search", headers=headers)
        search_by_phone = await client.get(
            "/candidate-search", headers=headers, params={"q": phone_canary}
        )
        duplicates = await client.get("/candidate-duplicates", headers=headers)
        merged = await client.get(
            f"/candidates/{candidate.id}/merged-sources", headers=headers
        )
        pipeline = await client.get(
            f"/positions/{seeded['position'].id}/applications", headers=headers
        )
        pool = await client.get(
            f"/talent/pools/{seeded['pool'].id}/members", headers=headers
        )
        talent_search = await client.get(
            "/talent/search", headers=headers, params={"q": "Python"}
        )
        talent_matches = await client.get(
            f"/positions/{seeded['position'].id}/talent-matches", headers=headers
        )
        my_work = await client.get("/my-work", headers=headers)
        activities = await client.get(
            f"/candidates/{candidate.id}/activities", headers=headers
        )
        notifications = await client.get("/notifications", headers=headers)
        marked = await client.post(
            f"/notifications/{legacy_notification.id}/read", headers=headers
        )

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["source_ref"] is None
    assert detail_body["raw_cv"] is None
    assert detail_body["parsed_cv"] is None
    assert detail_body["has_original_file"] is False
    assert detail_body["github_login"] is None
    assert detail_body["github_data"] is None
    assert detail_body["github_fetched_at"] is None
    assert candidates.status_code == 200
    assert all(item["source_ref"] is None for item in candidates.json()["items"])
    assert search.status_code == 200
    assert all(item["source_ref"] is None for item in search.json()["items"])
    assert all(item["duplicate_count"] == 0 for item in search.json()["items"])
    assert search_by_phone.status_code == 403
    assert duplicates.status_code == 200
    assert duplicates.json()["items"] == []
    assert merged.status_code == 200
    for item in merged.json()["items"]:
        assert item["source_ref"] is None
        assert item["has_cv"] is False
        assert item["has_contact"] is False
        assert item["github_login"] is None
        assert item["scores"] == []
    assert pipeline.status_code == 200
    assert all(item["source_ref"] is None for item in pipeline.json()["items"])
    assert all(item["display_name"] is None for item in pipeline.json()["items"])
    assert pool.status_code == 200
    assert all(item["source_ref"] is None for item in pool.json()["items"])
    assert talent_search.status_code == 403
    assert talent_matches.status_code == 403
    assert my_work.status_code == 200
    work_item = next(
        item
        for item in my_work.json()["items"]
        if item["candidate"]["id"] == str(candidate.id)
    )
    assert work_item["candidate"]["source_ref"] is None
    assert work_item["overall_score"] is None
    assert activities.status_code == 200
    sensitive_events = {
        item["event_type"]: item
        for item in activities.json()["items"]
        if item["event_type"]
        in {"candidate_scored", "task_evaluated", "hiring_decision_voted"}
    }
    assert set(sensitive_events) == {
        "candidate_scored",
        "task_evaluated",
        "hiring_decision_voted",
    }
    assert all(item["payload"] == {} for item in sensitive_events.values())
    assert notifications.status_code == 200
    notification = next(
        item
        for item in notifications.json()["items"]
        if item["id"] == str(legacy_notification.id)
    )
    assert notification["payload"] == {"candidate_id": str(candidate.id), "nested": {}}
    assert marked.status_code == 200
    assert marked.json()["payload"] == {
        "candidate_id": str(candidate.id),
        "nested": {},
    }

    response_text = "\n".join(
        response.text
        for response in (
            detail,
            candidates,
            search,
            duplicates,
            merged,
            pipeline,
            pool,
            my_work,
            activities,
            notifications,
            marked,
        )
    )
    for canary in (
        phone_canary,
        "Applicant Canary",
        "PRIVATE-GITHUB-EVIDENCE",
        "SCORE-PAYLOAD-CANARY",
        "TASK-EVIDENCE-CANARY",
        "DECISION-CANARY",
    ):
        assert canary not in response_text

    audit_rows = list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == seeded["organization"].id,
                    AuditLog.actor_user_id == recruiter.id,
                    AuditLog.status_code < 400,
                )
            )
        ).scalars()
    )
    fields_by_path = {
        row.path_template: set(row.metadata_json.get("field_classes", []))
        for row in audit_rows
        if row.metadata_json.get("sensitive_read")
    }
    assert {"candidate_contacts", "candidate_cv"}.issubset(
        fields_by_path["/candidates/{candidate_id}"]
    )
    assert "assessment_results" in fields_by_path["/my-work"]
    assert "assessment_results" in fields_by_path[
        "/candidates/{candidate_id}/activities"
    ]
    assert "candidate_contacts" in fields_by_path["/notifications"]

    context = await build_candidate_privacy_context(
        db,
        organization_id=seeded["organization"].id,
        candidate_ids={candidate.id},
    )
    summary = await erase_candidate_live_store_copies(
        db,
        organization_id=seeded["organization"].id,
        context=context,
    )
    await db.commit()
    assert summary.notifications_erased >= 1
    assert await db.get(Notification, legacy_notification.id) is None


async def test_public_intake_stores_opaque_source_and_notifies_only_contact_readers(db):
    organization = Organization(
        name="Public notification privacy tenant",
        slug=f"public-notification-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    admin = await _user(db, organization.id, "admin", "public-contact-admin")
    recruiter = await _user(
        db, organization.id, "recruiter", "public-contact-denied"
    )
    interviewer = await _user(
        db, organization.id, "interviewer", "public-interviewer"
    )
    restricted = await _user(db, organization.id, "restricted", "public-restricted")
    inactive_admin = await _user(
        db, organization.id, "admin", "public-inactive-admin"
    )
    inactive_admin.is_active = False
    position = Position(
        organization_id=organization.id,
        title="Public intake role",
        description="A public hiring role",
        status="open",
        created_by=admin.id,
    )
    db.add(position)
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            field_visibility={FieldClass.CONTACT.value: ["admin"]},
        )
    )
    await db.commit()

    name_canary = "Public Applicant Canary"
    phone_canary = "+992900004242"
    email_canary = "public-applicant-canary@example.test"
    malformed_source = "source-canary@example.test-" + "x" * 37
    assert len(malformed_source) == 64
    result = await submit_public_application(
        position=position,
        name=name_canary,
        phone=phone_canary,
        email=email_canary,
        source=malformed_source,
        file=None,
        raw_text=(
            "Experienced engineer with Python, PostgreSQL, distributed systems, "
            "security, testing, and reliable platform delivery."
        ),
        privacy_notice_version="pilot-v1",
        privacy_acknowledged=True,
        db=db,
    )

    candidate = await db.get(Candidate, result.candidate_id)
    application = await db.get(Application, result.application_id)
    assert candidate is not None
    assert application is not None
    assert candidate.source == "public"
    assert candidate.source_ref is not None
    assert ":public-application:" in candidate.source_ref
    assert len(application.source) <= 16
    assert application.source.startswith("other-")
    stored_provenance = f"{candidate.source_ref} {application.source}"
    for canary in (name_canary, phone_canary, email_canary, malformed_source):
        assert canary not in stored_provenance

    notifications = list(
        (
            await db.execute(
                select(Notification).where(
                    Notification.organization_id == organization.id,
                    Notification.type == "candidate_applied",
                )
            )
        ).scalars()
    )
    assert {notification.user_id for notification in notifications} == {admin.id}
    assert recruiter.id not in {notification.user_id for notification in notifications}
    assert interviewer.id not in {
        notification.user_id for notification in notifications
    }
    assert restricted.id not in {
        notification.user_id for notification in notifications
    }
    assert inactive_admin.id not in {
        notification.user_id for notification in notifications
    }
    serialized_notifications = repr(
        [notification.payload for notification in notifications]
    )
    for canary in (name_canary, phone_canary, email_canary, malformed_source):
        assert canary not in serialized_notifications


async def test_shortlist_requires_share_fields_and_revokes_on_policy_change(db):
    organization = Organization(
        name="Shortlist policy tenant",
        slug=f"shortlist-policy-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = await _user(db, organization.id, "recruiter", "shortlist-recruiter")
    position = Position(
        organization_id=organization.id,
        title="Shortlist role",
        description="Share-safe role",
        status="open",
        created_by=recruiter.id,
    )
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Shortlist Contact Canary +992900005555",
        raw_cv="Private shortlist CV evidence",
        parsed_cv={
            "applicant_name": "Shortlist Contact Canary",
            "applicant_phone": "+992900005555",
        },
    )
    db.add_all([position, candidate])
    await db.flush()
    db.add(
        Score(
            organization_id=organization.id,
            candidate_id=candidate.id,
            position_id=position.id,
            overall_score=91,
            comment_technical_skills="SHORTLIST-ASSESSMENT-CANARY",
            stage="screening",
        )
    )
    policy = EnterprisePolicy(
        organization_id=organization.id,
        field_visibility={
            FieldClass.EXPORT.value: ["recruiter"],
            FieldClass.ASSESSMENT_RESULT.value: ["recruiter"],
            FieldClass.CONTACT.value: ["admin"],
        },
    )
    db.add(policy)
    await db.commit()

    headers = _auth(recruiter)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        contact_denied = await client.post(
            f"/positions/{position.id}/shortlists",
            headers=headers,
            json={"top_n": 5, "include_contact": True, "expires_in_days": 14},
        )
        anonymous_created = await client.post(
            f"/positions/{position.id}/shortlists",
            headers=headers,
            json={"top_n": 5, "include_contact": False, "expires_in_days": 14},
        )
        assert anonymous_created.status_code == 201, anonymous_created.text
        token = anonymous_created.json()["token"]
        public_allowed = await client.get(f"/public/shortlists/{token}")

    assert contact_denied.status_code == 403
    assert public_allowed.status_code == 200
    assert public_allowed.json()["candidates"][0]["overall_score"] == 91
    assert "SHORTLIST-ASSESSMENT-CANARY" not in public_allowed.text
    assert "+992900005555" not in public_allowed.text

    export_override = PermissionOverride(
        organization_id=organization.id,
        role="recruiter",
        permission="candidates.export",
        allowed=False,
    )
    db.add(export_override)
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        permission_revoked = await client.get(f"/public/shortlists/{token}")
    assert permission_revoked.status_code == 410
    assert permission_revoked.json()["code"] == "shortlist_authorization_revoked"

    export_override.allowed = True
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        permission_restored = await client.get(f"/public/shortlists/{token}")
    assert permission_restored.status_code == 200

    policy.field_visibility = {
        **policy.field_visibility,
        FieldClass.ASSESSMENT_RESULT.value: ["admin"],
    }
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        field_revoked = await client.get(f"/public/shortlists/{token}")
    assert field_revoked.status_code == 410
    assert field_revoked.json()["code"] == "shortlist_authorization_revoked"

    public_audits = list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == organization.id,
                    AuditLog.path_template == "/public/shortlists/{token}",
                    AuditLog.status_code == 200,
                )
            )
        ).scalars()
    )
    assert len(public_audits) == 2
    assert all(
        row.metadata_json.get("field_classes") == ["assessment_results"]
        for row in public_audits
    )


async def test_nurture_launch_and_scheduled_delivery_recheck_contact_authority(
    db, monkeypatch
):
    organization = Organization(
        name="Nurture authorization tenant",
        slug=f"nurture-authorization-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = await _user(db, organization.id, "recruiter", "nurture-recruiter")
    admin = await _user(db, organization.id, "admin", "nurture-admin")
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="Nurture candidate",
        raw_cv="Candidate record for nurture authorization",
    )
    sequence = TalentNurtureSequence(
        organization_id=organization.id,
        created_by_user_id=recruiter.id,
        name=f"Nurture {uuid4().hex[:8]}",
        is_active=True,
    )
    db.add_all([candidate, sequence])
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            field_visibility={FieldClass.CONTACT.value: ["admin"]},
        )
    )
    await db.commit()

    launch_policy = operation_policy(
        "POST", "/talent/nurture-sequences/{sequence_id}/launch"
    )
    assert launch_policy is not None
    assert set(launch_policy.actions) == {
        "talent.manage",
        "communications.send",
        "candidates.contact",
    }
    assert launch_policy.fields == frozenset({FieldClass.CONTACT})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied_launch = await client.post(
            f"/talent/nurture-sequences/{sequence.id}/launch",
            headers=_auth(recruiter),
            json={"candidate_ids": [str(candidate.id)]},
        )
    assert denied_launch.status_code == 403
    assert list(
        (
            await db.execute(
                select(CommunicationMessage).where(
                    CommunicationMessage.organization_id == organization.id
                )
            )
        ).scalars()
    ) == []

    contact = CandidateContactPoint(
        organization_id=organization.id,
        candidate_id=candidate.id,
        created_by_user_id=admin.id,
        channel="email",
        value="nurture-candidate@example.test",
        normalized_value="nurture-candidate@example.test",
        source="manual",
        is_primary=True,
    )
    account = CommunicationAccount(
        organization_id=organization.id,
        created_by_user_id=admin.id,
        channel="email",
        provider="sandbox",
        name=f"Nurture sandbox {uuid4().hex[:8]}",
        configuration={},
        is_active=True,
    )
    db.add_all([contact, account])
    await db.flush()
    thread = CommunicationThread(
        organization_id=organization.id,
        candidate_id=candidate.id,
        contact_point_id=contact.id,
        account_id=account.id,
        channel="email",
        status="open",
    )
    db.add(thread)
    await db.flush()
    now = datetime.now(UTC)
    permission_revoked_message = CommunicationMessage(
        organization_id=organization.id,
        thread_id=thread.id,
        candidate_id=candidate.id,
        contact_point_id=contact.id,
        account_id=account.id,
        actor_user_id=recruiter.id,
        direction="outbound",
        channel="email",
        status="queued",
        subject="Must not send",
        body="Permission-revoked nurture body",
        idempotency_key=f"nurture:{sequence.id}:{candidate.id}:{uuid4()}",
        scheduled_for=now - timedelta(seconds=1),
    )
    user_revoked_message = CommunicationMessage(
        organization_id=organization.id,
        thread_id=thread.id,
        candidate_id=candidate.id,
        contact_point_id=contact.id,
        account_id=account.id,
        actor_user_id=admin.id,
        direction="outbound",
        channel="email",
        status="queued",
        subject="Must not send",
        body="Disabled-user nurture body",
        idempotency_key=f"nurture:{sequence.id}:{candidate.id}:{uuid4()}",
        scheduled_for=now - timedelta(seconds=1),
    )
    db.add_all([permission_revoked_message, user_revoked_message])
    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="recruiter",
            permission="candidates.contact",
            allowed=False,
        )
    )
    admin.is_active = False
    await db.commit()

    provider_calls = 0

    async def forbidden_delivery(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("revoked nurture delivery reached the provider")

    monkeypatch.setattr(
        "app.services.communications.deliver_message", forbidden_delivery
    )
    for message in (permission_revoked_message, user_revoked_message):
        result = await process_claimed_message(
            db,
            organization_id=organization.id,
            message_id=message.id,
            now=now,
        )
        assert result == "failed"
    await db.commit()
    assert provider_calls == 0
    for message in (permission_revoked_message, user_revoked_message):
        await db.refresh(message)
        assert message.status == "failed"
        assert message.failure_code == "authorization_revoked"


async def _assessment_review_graph(db):
    seeded = await _pair_graph(db)
    candidate = seeded["candidate_a"]
    position = seeded["position_x"]
    application = seeded["pairs"][(candidate.id, position.id)][0]
    template = AssessmentTemplate(
        organization_id=seeded["organization"].id,
        created_by_user_id=seeded["recruiter"].id,
        name=f"Reviewer security {uuid4().hex[:8]}",
    )
    db.add(template)
    await db.flush()
    version = AssessmentTemplateVersion(
        organization_id=seeded["organization"].id,
        template_id=template.id,
        version=1,
        title="Reviewer security assessment",
        passing_percentage=Decimal("50"),
        created_by_user_id=seeded["recruiter"].id,
    )
    db.add(version)
    await db.flush()
    section = AssessmentSection(
        organization_id=seeded["organization"].id,
        version_id=version.id,
        title="Evidence",
        position=0,
    )
    db.add(section)
    await db.flush()
    question = AssessmentQuestion(
        organization_id=seeded["organization"].id,
        version_id=version.id,
        section_id=section.id,
        key="evidence",
        question_type="file",
        prompt="Upload evidence",
        required=False,
        position=0,
        max_score=Decimal("10"),
    )
    db.add(question)
    await db.flush()
    invitation = AssessmentInvitation(
        organization_id=seeded["organization"].id,
        application_id=application.id,
        template_version_id=version.id,
        token_digest=uuid4().hex + uuid4().hex,
        token_prefix="asmt_review",
        recipient_email="assessment-contact-canary@example.test",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        max_attempts=1,
        authorized_attempts=1,
        accommodations={
            "extra_time_minutes": 30,
            "breaks_allowed": True,
            "screen_reader_optimized": True,
        },
        invited_by_user_id=seeded["recruiter"].id,
    )
    db.add(invitation)
    await db.flush()
    now = datetime.now(UTC)
    attempt = AssessmentAttempt(
        organization_id=seeded["organization"].id,
        invitation_id=invitation.id,
        application_id=application.id,
        template_version_id=version.id,
        sequence=1,
        status="submitted",
        started_at=now - timedelta(minutes=10),
        last_saved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=30),
        submitted_at=now,
    )
    db.add(attempt)
    await db.flush()
    result = AssessmentResult(
        organization_id=seeded["organization"].id,
        attempt_id=attempt.id,
        application_id=application.id,
        raw_score=Decimal("9"),
        max_score=Decimal("10"),
        percentage=Decimal("90"),
        passed=True,
        knocked_out=False,
        knockout_question_id=None,
        knockout_reason="ASSESSMENT-RESULT-CANARY",
        needs_manual_review=False,
        breakdown=[{"evidence": "ASSESSMENT-BREAKDOWN-CANARY"}],
        graded_at=now,
    )
    file = AssessmentFile(
        organization_id=seeded["organization"].id,
        invitation_id=invitation.id,
        attempt_id=attempt.id,
        question_id=question.id,
        original_filename="PRIVATE-ASSESSMENT-FILE-CANARY.pdf",
        media_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
        storage_path=f"assessments/{uuid4()}.pdf",
        status="ready",
        ready_at=now,
    )
    db.add_all([result, file])
    await db.commit()
    seeded.update(
        {
            "application": application,
            "invitation": invitation,
            "attempt": attempt,
            "assessment_file": file,
        }
    )
    return seeded


async def test_native_assessment_review_requires_results_redacts_contact_and_files(db):
    seeded = await _assessment_review_graph(db)
    interviewer = seeded["interviewer"]
    headers = _auth(interviewer)
    attempts_policy = operation_policy(
        "GET", "/assessment-invitations/{invitation_id}/attempts"
    )
    assert attempts_policy is not None
    assert attempts_policy.actions == ("assessments.results",)
    assert FieldClass.ASSESSMENT_RESULT in attempts_policy.fields

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        invitations = await client.get(
            f"/applications/{seeded['application'].id}/assessment-invitations",
            headers=headers,
        )
        attempts = await client.get(
            f"/assessment-invitations/{seeded['invitation'].id}/attempts",
            headers=headers,
        )

    assert invitations.status_code == 200
    assert len(invitations.json()["items"]) == 1
    invitation_body = invitations.json()["items"][0]
    assert invitation_body["recipient_email"] is None
    assert invitation_body["accommodations"] is None
    assert attempts.status_code == 200
    attempt_body = attempts.json()["items"][0]
    assert attempt_body["result"]["percentage"] == "90.0000"
    assert attempt_body["files"] == []
    assert "PRIVATE-ASSESSMENT-FILE-CANARY" not in attempts.text

    db.add(
        PermissionOverride(
            organization_id=seeded["organization"].id,
            role="interviewer",
            permission="assessments.results",
            allowed=False,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.get(
            f"/assessment-invitations/{seeded['invitation'].id}/attempts",
            headers=headers,
        )
    assert denied.status_code == 403

    audits = list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == seeded["organization"].id,
                    AuditLog.actor_user_id == interviewer.id,
                )
            )
        ).scalars()
    )
    fields_by_path = {
        row.path_template: set(row.metadata_json.get("field_classes", []))
        for row in audits
        if row.metadata_json.get("sensitive_read")
    }
    assert "candidate_contacts" in fields_by_path[
        "/applications/{application_id}/assessment-invitations"
    ]
    assert {"assessment_results", "candidate_cv"}.issubset(
        fields_by_path["/assessment-invitations/{invitation_id}/attempts"]
    )


async def _ai_policy_graph(db):
    organization = Organization(
        name="AI field isolation tenant",
        slug=f"ai-field-isolation-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    recruiter = await _user(db, organization.id, "recruiter", "ai-field-recruiter")
    candidate = Candidate(
        organization_id=organization.id,
        source="manual",
        source_ref="AI candidate contact canary +992900009999",
        raw_cv=(
            "A detailed confidential candidate document for AI authorization "
            "testing with enough content to satisfy scoring input validation."
        ),
    )
    position = Position(
        organization_id=organization.id,
        title="Confidential AI position",
        description="Confidential business context",
        created_by=recruiter.id,
    )
    db.add_all([candidate, position])
    await db.flush()
    await db.commit()
    return organization, recruiter, candidate, position


@pytest.mark.parametrize(
    "denied_field",
    [
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    ],
    ids=lambda field: field.value,
)
async def test_every_ai_business_context_field_independently_fails_closed(
    db, denied_field
):
    organization, recruiter, candidate, position = await _ai_policy_graph(db)
    protected_fields = (
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    )
    field_visibility = {field.value: ["recruiter"] for field in protected_fields}
    field_visibility[denied_field.value] = ["admin"]
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            field_visibility=field_visibility,
        )
    )
    await db.commit()

    for method, path in (
        ("GET", "/chat/sessions"),
        ("GET", "/ai/v1/missions"),
        ("POST", "/candidates/{candidate_id}/score"),
    ):
        policy = operation_policy(method, path)
        assert policy is not None
        assert set(protected_fields).issubset(policy.fields)

    headers = _auth(recruiter)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        chat = await client.get("/chat/sessions", headers=headers)
        missions = await client.get("/ai/v1/missions", headers=headers)
        scoring = await client.post(
            f"/candidates/{candidate.id}/score",
            headers=headers,
            json={"position_id": str(position.id)},
        )
    assert chat.status_code == 403
    assert missions.status_code == 403
    assert scoring.status_code == 403


async def test_ai_chat_surfaces_fail_closed_for_compensation_canaries(db):
    organization, recruiter, _candidate, _position = await _ai_policy_graph(db)
    session = ChatSession(
        organization_id=organization.id,
        user_id=recruiter.id,
        scope="free",
        title="Compensation review",
    )
    db.add(session)
    await db.flush()
    compensation_canaries = (
        "Pay is USD 120000",
        "оклад 200000 TJS",
        "$120k",
    )
    for canary in compensation_canaries:
        db.add(
            Message(
                organization_id=organization.id,
                session_id=session.id,
                role="user",
                content=canary,
                purpose="chat",
            )
        )
    db.add(
        EnterprisePolicy(
            organization_id=organization.id,
            field_visibility={
                FieldClass.AI_CONTEXT.value: ["recruiter"],
                FieldClass.CONTACT.value: ["recruiter"],
                FieldClass.CANDIDATE_DOCUMENT.value: ["recruiter"],
                FieldClass.ASSESSMENT_RESULT.value: ["recruiter"],
                FieldClass.COMPENSATION.value: ["recruiter"],
            },
        )
    )
    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="recruiter",
            permission="requisitions.compensation",
            allowed=True,
        )
    )
    await db.commit()

    protected_ai_fields = {
        FieldClass.AI_CONTEXT,
        FieldClass.CONTACT,
        FieldClass.CANDIDATE_DOCUMENT,
        FieldClass.ASSESSMENT_RESULT,
        FieldClass.COMPENSATION,
    }
    for method, path in (
        ("GET", "/chat/sessions"),
        ("GET", "/chat/sessions/{session_id}"),
        ("POST", "/chat/sessions/{session_id}/messages"),
        ("POST", "/chat/sessions/{session_id}/messages/stream"),
        ("GET", "/ai/v1/sessions"),
        ("GET", "/ai/v1/sessions/{session_id}"),
        ("POST", "/ai/v1/sessions/{session_id}/messages"),
    ):
        policy = operation_policy(method, path)
        assert policy is not None
        assert protected_ai_fields.issubset(policy.fields)

    headers = _auth(recruiter)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed_list = await client.get("/chat/sessions", headers=headers)
        allowed_detail = await client.get(
            f"/chat/sessions/{session.id}", headers=headers
        )
    assert allowed_list.status_code == 200
    assert allowed_detail.status_code == 200
    assert compensation_canaries[0] in allowed_list.text
    for canary in compensation_canaries:
        assert canary in allowed_detail.text

    policy = (
        await db.execute(
            select(EnterprisePolicy).where(
                EnterprisePolicy.organization_id == organization.id
            )
        )
    ).scalar_one()
    policy.field_visibility = {
        **policy.field_visibility,
        FieldClass.COMPENSATION.value: ["admin"],
    }
    await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied_responses = [
            await client.get("/chat/sessions", headers=headers),
            await client.get(f"/chat/sessions/{session.id}", headers=headers),
            await client.post(
                f"/chat/sessions/{session.id}/messages",
                headers=headers,
                json={"content": "Do not reveal the salary"},
            ),
            await client.post(
                f"/chat/sessions/{session.id}/messages/stream",
                headers=headers,
                json={"content": "Do not stream the salary"},
            ),
            await client.get("/ai/v1/sessions", headers=headers),
        ]
    assert {response.status_code for response in denied_responses} == {403}
    denied_text = "\n".join(response.text for response in denied_responses)
    for canary in compensation_canaries:
        assert canary not in denied_text


async def test_position_compensation_is_redacted_and_pay_writes_fail_closed(
    db, monkeypatch
):
    organization = Organization(
        name="Position compensation tenant",
        slug=f"position-compensation-{uuid4().hex[:10]}",
    )
    db.add(organization)
    await db.flush()
    admin = await _user(db, organization.id, "admin", "position-comp-admin")
    recruiter = await _user(
        db, organization.id, "recruiter", "position-comp-recruiter"
    )
    position = Position(
        organization_id=organization.id,
        title="Platform engineer",
        description="Pay is USD 120000",
        full_description=(
            "оклад 200000 TJS; base 120k; 120,000 per annum"
        ),
        internal_notes="$120k; six-figure package; зп 200к",
        requirements={
            "skills": ["Python", "PostgreSQL"],
            "salary_range": "USD 120000",
            "nested": {
                "compensation": {"amount": 200000, "currency": "TJS"},
                "safe": "distributed systems",
                "details": "$120k",
                "base_amount": "120k",
                "annual_package": "six figures",
            },
        },
        candidate_profile="Target profile; salary USD 120000",
        public_description="Public role with pay $120k",
        status="open",
        created_by=admin.id,
    )
    db.add_all(
        [
            position,
            EnterprisePolicy(
                organization_id=organization.id,
                field_visibility={FieldClass.COMPENSATION.value: ["admin"]},
            ),
        ]
    )
    await db.commit()
    await db.refresh(position)

    llm_calls = 0

    async def fake_draft(*_args, **_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return {
            "assistant_message": "Authorized compensation draft",
            "draft": {"title": "Platform engineer", "salary_range": "$120k"},
            "is_complete": False,
            "missing_required": [],
            "quick_replies": [],
            "model_used": "test-model",
        }

    monkeypatch.setattr("app.routers.positions.draft_converse", fake_draft)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        admin_list = await client.get("/positions", headers=_auth(admin))
        admin_detail = await client.get(
            f"/positions/{position.id}", headers=_auth(admin)
        )
        recruiter_list = await client.get("/positions", headers=_auth(recruiter))
        recruiter_detail = await client.get(
            f"/positions/{position.id}", headers=_auth(recruiter)
        )
        denied_unknown_create = await client.post(
            "/positions",
            headers=_auth(recruiter),
            json={
                "title": "Unknown compensation phrasing",
                "description": "Total rewards band is 120k",
                "requirements": {"cash_target": "200k"},
            },
        )
        allowed_unknown_create = await client.post(
            "/positions",
            headers=_auth(admin),
            json={
                "title": "Authorized compensation phrasing",
                "description": "Total rewards band is 120k",
                "requirements": {"cash_target": "200k"},
            },
        )
        denied_structured_create = await client.post(
            "/positions",
            headers=_auth(recruiter),
            json={
                "title": "Private package role",
                "requirements": {"nested": {"salary_band": "USD 120000"}},
            },
        )
        denied_free_text_create = await client.post(
            "/positions",
            headers=_auth(recruiter),
            json={
                "title": "Private package role",
                "description": "Pay is USD 120000",
            },
        )
        denied_unknown_patch = await client.patch(
            f"/positions/{position.id}",
            headers=_auth(recruiter),
            json={"description": "Total rewards band is 120k"},
        )
        safe_structural_patch = await client.patch(
            f"/positions/{position.id}",
            headers=_auth(recruiter),
            json={"public_apply_enabled": False},
        )
        denied_patch = await client.patch(
            f"/positions/{position.id}",
            headers=_auth(recruiter),
            json={"requirements": {"skills": ["Go"]}},
        )
        denied_project = await client.post(
            "/positions/project",
            headers=_auth(recruiter),
            json={
                "folder": "Compensation project",
                "positions": [
                    {"title": "Data engineer", "salary_range": "200000 TJS"}
                ],
            },
        )
        denied_ai = await client.post(
            "/positions/draft",
            headers=_auth(recruiter),
            json={
                "messages": [{"role": "user", "content": "Pay is USD 120000"}]
            },
        )
        allowed_ai = await client.post(
            "/positions/draft",
            headers=_auth(admin),
            json={
                "messages": [{"role": "user", "content": "Pay is USD 120000"}]
            },
        )

    assert admin_list.status_code == admin_detail.status_code == 200
    for canary in (
        "Pay is USD 120000",
        "оклад 200000 TJS",
        "$120k",
        "base 120k",
        "120,000 per annum",
        "six-figure package",
        "зп 200к",
        "base_amount",
        "annual_package",
    ):
        assert canary in admin_detail.text
    assert recruiter_list.status_code == recruiter_detail.status_code == 200
    recruiter_payload = recruiter_detail.json()
    assert recruiter_payload["description"] is None
    assert recruiter_payload["full_description"] is None
    assert recruiter_payload["internal_notes"] is None
    assert recruiter_payload["candidate_profile"] is None
    assert recruiter_payload["public_description"] is None
    assert recruiter_payload["title"] == "Restricted position"
    assert recruiter_payload["folder"] is None
    assert recruiter_payload["requirements"] is None
    denied_reads = recruiter_list.text + recruiter_detail.text
    for canary in (
        "Pay is USD 120000",
        "оклад 200000 TJS",
        "$120k",
        "base 120k",
        "120,000 per annum",
        "six-figure package",
        "зп 200к",
        "salary_range",
        "compensation",
        "base_amount",
        "annual_package",
    ):
        assert canary not in denied_reads

    assert allowed_unknown_create.status_code == 201
    assert safe_structural_patch.status_code == 200
    for denied in (
        denied_unknown_create,
        denied_structured_create,
        denied_free_text_create,
        denied_unknown_patch,
        denied_patch,
        denied_project,
        denied_ai,
    ):
        assert denied.status_code == 403, denied.text
    assert allowed_ai.status_code == 200
    assert llm_calls == 1

    audits = list(
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == organization.id,
                    AuditLog.actor_user_id == recruiter.id,
                    AuditLog.status_code < 400,
                )
            )
        ).scalars()
    )
    audited_paths = {
        row.path_template: set(row.metadata_json.get("field_classes", []))
        for row in audits
        if row.metadata_json.get("sensitive_read")
    }
    assert "compensation" in audited_paths["/positions"]
    assert "compensation" in audited_paths["/positions/{position_id}"]


async def test_position_llm_routes_require_sensitive_context_before_gateway_call(
    db, monkeypatch
):
    organization, recruiter, _candidate, _position = await _ai_policy_graph(db)
    db.add_all(
        [
            EnterprisePolicy(
                organization_id=organization.id,
                field_visibility={
                    FieldClass.AI_CONTEXT.value: ["recruiter"],
                    FieldClass.COMPENSATION.value: ["recruiter"],
                },
            ),
            PermissionOverride(
                organization_id=organization.id,
                role="recruiter",
                permission="requisitions.compensation",
                allowed=True,
            ),
        ]
    )
    await db.commit()
    calls = {"draft": 0, "project": 0}

    async def fake_draft(*_args, **_kwargs):
        calls["draft"] += 1
        return {
            "assistant_message": "Drafted safely",
            "draft": {"title": "Platform engineer"},
            "is_complete": False,
            "missing_required": [],
            "quick_replies": [],
            "model_used": "test-model",
        }

    async def fake_project(*_args, **kwargs):
        calls["project"] += 1
        return {
            "project_name": "Confidential project",
            "folder": "Confidential",
            "positions": [{"title": "Platform engineer"}],
            "source_filename": kwargs.get("source_filename"),
            "source_chars": len(kwargs["text"]),
            "model_used": "test-model",
        }

    monkeypatch.setattr("app.routers.positions.draft_converse", fake_draft)
    monkeypatch.setattr("app.routers.positions.extract_project", fake_project)
    for path in ("/positions", "/positions/project"):
        policy = operation_policy("POST", path)
        assert policy is not None
        assert policy.actions == (
            "candidates.write",
            "requisitions.compensation",
        )
        assert policy.fields == frozenset({FieldClass.COMPENSATION})
    patch_policy = operation_policy("PATCH", "/positions/{position_id}")
    assert patch_policy is not None
    assert patch_policy.actions == ("candidates.write",)
    assert patch_policy.fields == frozenset()
    for path in ("/positions/draft", "/positions/project/extract"):
        policy = operation_policy("POST", path)
        assert policy is not None
        assert "ai.sensitive_context" in policy.actions
        assert "requisitions.compensation" in policy.actions
        assert FieldClass.AI_CONTEXT in policy.fields
        assert FieldClass.COMPENSATION in policy.fields
    text_policy = operation_policy("POST", "/positions/draft/extract-text")
    assert text_policy is not None
    assert text_policy.actions == ("candidates.write",)
    assert text_policy.fields == frozenset()

    headers = _auth(recruiter)
    confidential_text = (
        "Confidential launch plan with platform, security, and data roles. " * 3
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        authorized_draft = await client.post(
            "/positions/draft",
            headers=headers,
            json={
                "messages": [
                    {"role": "user", "content": "Confidential hiring plan"}
                ]
            },
        )
        authorized_project = await client.post(
            "/positions/project/extract",
            headers=headers,
            files={"file": ("confidential.txt", confidential_text, "text/plain")},
        )
    assert authorized_draft.status_code == 200
    assert authorized_project.status_code == 200
    assert calls == {"draft": 1, "project": 1}

    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="recruiter",
            permission="ai.sensitive_context",
            allowed=False,
        )
    )
    await db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied_draft = await client.post(
            "/positions/draft",
            headers=headers,
            json={
                "messages": [
                    {"role": "user", "content": "Must never reach the gateway"}
                ]
            },
        )
        denied_project = await client.post(
            "/positions/project/extract",
            headers=headers,
            files={"file": ("denied.txt", confidential_text, "text/plain")},
        )
        parsed_only = await client.post(
            "/positions/draft/extract-text",
            headers=headers,
            files={"file": ("parse-only.txt", confidential_text, "text/plain")},
        )
    assert denied_draft.status_code == 403
    assert denied_project.status_code == 403
    assert calls == {"draft": 1, "project": 1}
    assert parsed_only.status_code == 200
    assert parsed_only.json()["text"] == confidential_text.strip()


async def test_position_llm_usage_has_stable_provenance_without_raw_job_text(
    db, monkeypatch
):
    organization, _recruiter, _candidate, _position = await _ai_policy_graph(db)
    draft_canary = "RAW-DRAFT-JOB-CANARY salary USD 120000"
    project_canary = "RAW-PROJECT-JOB-CANARY оклад 200000 TJS"
    completions = iter(
        (
            {
                "assistant_message": "Captured",
                "draft": {"title": "Platform engineer"},
                "is_complete": False,
                "quick_replies": [],
            },
            {
                "project_name": "Private initiative",
                "folder": "private-initiative",
                "positions": [{"title": "Data engineer"}],
            },
        )
    )

    class LoggingProvider:
        async def complete(self, messages, **kwargs):
            result = CompletionResult(
                content=str(__import__("json").dumps(next(completions))),
                model_used="private/test-model",
                tokens_in=31,
                tokens_out=17,
                provider_used="private-test",
            )
            log = kwargs.get("log")
            assert log is not None
            await log_completion(
                log,
                result,
                "\n".join(message.content for message in messages),
            )
            return result

    async def routed_client(routed_db, organization_id, **_kwargs):
        assert routed_db is db
        assert organization_id == organization.id
        return BudgetedLLMClient(
            LoggingProvider(),
            db=routed_db,
            organization_id=organization_id,
            provider="vllm",
            managed=False,
        )

    monkeypatch.setattr(
        "app.services.position_draft.organization_llm_client", routed_client
    )
    await converse(
        [DraftMessage(role="user", content=draft_canary)],
        None,
        db=db,
        organization_id=organization.id,
    )
    await extract_project(
        text=project_canary,
        source_filename="private-project.txt",
        db=db,
        organization_id=organization.id,
    )
    await db.commit()

    ledger_rows = list(
        (
            await db.execute(
                select(UsageLedgerEntry).where(
                    UsageLedgerEntry.organization_id == organization.id,
                    UsageLedgerEntry.metric == "ai_tokens",
                )
            )
        ).scalars()
    )
    metadata = [row.metadata_json for row in ledger_rows]
    expected_provenance = {
        "position_draft_conversation": "position-draft-conversation-v1",
        "position_project_extract": "position-project-extract-v1",
    }
    for purpose, prompt_version in expected_provenance.items():
        purpose_rows = [item for item in metadata if item.get("purpose") == purpose]
        assert purpose_rows
        assert {item.get("prompt_version") for item in purpose_rows} == {
            prompt_version
        }

    general_messages = list(
        (
            await db.execute(
                select(Message).where(Message.organization_id == organization.id)
            )
        ).scalars()
    )
    assert {message.purpose for message in general_messages} == set(
        expected_provenance
    )
    serialized_general_log = " ".join(message.content for message in general_messages)
    serialized_ledger = " ".join(str(item) for item in metadata)
    for canary in (draft_canary, project_canary):
        assert canary not in serialized_general_log
        assert canary not in serialized_ledger


async def test_project_create_reuses_casefolded_tenant_folder_and_is_race_safe(db):
    organization_a, recruiter_a, _candidate, _position = await _ai_policy_graph(db)
    db.add_all(
        [
            EnterprisePolicy(
                organization_id=organization_a.id,
                field_visibility={FieldClass.COMPENSATION.value: ["recruiter"]},
            ),
            PermissionOverride(
                organization_id=organization_a.id,
                role="recruiter",
                permission="requisitions.compensation",
                allowed=True,
            ),
        ]
    )
    organization_b = Organization(
        name="Project grouping tenant B",
        slug=f"project-grouping-b-{uuid4().hex[:10]}",
    )
    db.add(organization_b)
    await db.flush()
    recruiter_b = await _user(
        db, organization_b.id, "recruiter", "project-grouping-b"
    )
    db.add_all(
        [
            EnterprisePolicy(
                organization_id=organization_b.id,
                field_visibility={FieldClass.COMPENSATION.value: ["recruiter"]},
            ),
            PermissionOverride(
                organization_id=organization_b.id,
                role="recruiter",
                permission="requisitions.compensation",
                allowed=True,
            ),
        ]
    )
    await db.commit()

    def payload(folder: str, title: str) -> dict:
        return {
            "folder": folder,
            "positions": [
                {
                    "title": title,
                    "description": "A reviewed project position",
                    "requirements": {"skills": ["Python"]},
                }
            ],
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/positions/project",
            headers=_auth(recruiter_a),
            json=payload("Launch Team", "Backend engineer"),
        )
        reused = await client.post(
            "/positions/project",
            headers=_auth(recruiter_a),
            json=payload("launch team", "Frontend engineer"),
        )
        cross_tenant = await client.post(
            "/positions/project",
            headers=_auth(recruiter_b),
            json=payload("LAUNCH TEAM", "Tenant B engineer"),
        )
        raced_a, raced_b = await asyncio.gather(
            client.post(
                "/positions/project",
                headers=_auth(recruiter_a),
                json=payload("Concurrent Group", "Data engineer"),
            ),
            client.post(
                "/positions/project",
                headers=_auth(recruiter_a),
                json=payload("concurrent group", "QA engineer"),
            ),
        )

    for response in (first, reused, cross_tenant, raced_a, raced_b):
        assert response.status_code == 201, response.text
        assert response.json()["folder_id"]
    assert first.json()["folder_id"] == reused.json()["folder_id"]
    assert cross_tenant.json()["folder_id"] != first.json()["folder_id"]
    assert raced_a.json()["folder_id"] == raced_b.json()["folder_id"]

    roots_a = list(
        (
            await db.execute(
                select(Folder).where(
                    Folder.organization_id == organization_a.id,
                    Folder.parent_id.is_(None),
                )
            )
        ).scalars()
    )
    assert sum(folder.name.casefold() == "launch team" for folder in roots_a) == 1
    assert sum(folder.name.casefold() == "concurrent group" for folder in roots_a) == 1

    response_bodies = [
        first.json(),
        reused.json(),
        cross_tenant.json(),
        raced_a.json(),
        raced_b.json(),
    ]
    created_ids = [UUID(item["created"][0]) for item in response_bodies]
    created_positions = list(
        (
            await db.execute(select(Position).where(Position.id.in_(created_ids)))
        ).scalars()
    )
    assert len(created_positions) == len(created_ids)
    expected_folder_by_position = {
        UUID(item["created"][0]): UUID(item["folder_id"]) for item in response_bodies
    }
    for position in created_positions:
        assert position.folder_id == expected_folder_by_position[position.id]
        folder = await db.get(Folder, position.folder_id)
        assert folder is not None
        assert position.folder == folder.name
