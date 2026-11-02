"""One coherent private-pilot hiring journey against PostgreSQL.

The test deliberately crosses the same domain boundaries as a pilot customer:
approved headcount becomes a public position, a duplicate-safe public applicant
becomes a first-class application, structured evidence is collected, an offer
is approved and accepted through a capability link, and event analytics then
reconcile the resulting hire.

Synchronous AI interview generation is unavailable in the pilot.  The fixture
therefore installs a versioned human-authored interview kit and exercises the
real assignment, access-control, scoring, and completion operations from that
point onward.  No external provider delivery is mocked or asserted.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.auth.jwt import encode_access_token
from app.main import app
from app.models.application import Application, ApplicationStageEvent
from app.models.assessment import AssessmentInvitation
from app.models.candidate import Candidate
from app.models.enterprise import PermissionOverride
from app.models.interview import (
    Interview,
    InterviewAssignment,
    InterviewKitVersion,
    InterviewQuestion,
)
from app.models.organization import Organization
from app.models.position import Position
from app.models.requisition import CandidateOffer
from app.models.score import Score
from app.models.user import User
from app.routers.interviews import complete_interview, get_interview, score_question
from app.routers.requisitions import (
    create_candidate_offer,
    create_offer_template,
    create_requisition,
    list_candidate_handoffs,
    open_requisition,
    send_offer,
    submit_offer,
    submit_requisition,
    update_candidate_handoff,
    vote_offer,
    vote_requisition,
)
from app.routers.scorecards import (
    assign_candidate_scorecards,
    create_scorecard_template,
    get_candidate_scorecards,
    submit_scorecard,
    update_scorecard_draft,
)
from app.schemas.applications import ApplicationStageTransition
from app.schemas.assessments import (
    AssessmentInvitationCreate,
    AssessmentOption,
    AssessmentQuestionCreate,
    AssessmentScoringRule,
    AssessmentSectionCreate,
    AssessmentTemplateCreate,
    AssessmentValidationRule,
    AssessmentVersionCreate,
)
from app.schemas.interviews import InterviewCompleteRequest, QuestionScoreRequest
from app.schemas.requisitions import (
    ApprovalVoteRequest,
    CandidateHandoffUpdate,
    CandidateOfferCreate,
    OfferTemplateCreate,
    RequisitionCreate,
    RequisitionOpenRequest,
)
from app.schemas.scorecards import (
    ScorecardAssignRequest,
    ScorecardCriterionCreate,
    ScorecardDraftUpdate,
    ScorecardRatingInput,
    ScorecardTemplateCreate,
)
from app.services.application_analytics import application_analytics
from app.services.applications import (
    get_application,
    transition_application_stage,
)
from app.services.assessments import (
    create_invitation,
    create_template,
    create_version,
    list_invitation_attempts,
)

pytestmark = pytest.mark.asyncio


def _auth(user: User) -> dict[str, str]:
    token = encode_access_token(
        user.id,
        user.role,
        user.locale,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


def _assessment_version() -> AssessmentVersionCreate:
    return AssessmentVersionCreate(
        title="Pilot engineering screen",
        instructions="Complete every required question.",
        passing_percentage=Decimal("70"),
        time_limit_minutes=45,
        sections=[
            AssessmentSectionCreate(
                title="Core evidence",
                questions=[
                    AssessmentQuestionCreate(
                        key="eligible",
                        question_type="boolean",
                        prompt="Can you work in the advertised location?",
                        required=True,
                        scoring=AssessmentScoringRule(correct=True),
                        max_score=1,
                    ),
                    AssessmentQuestionCreate(
                        key="specialty",
                        question_type="single",
                        prompt="Choose your primary engineering specialty.",
                        required=True,
                        options=[
                            AssessmentOption(value="backend", label="Backend"),
                            AssessmentOption(value="frontend", label="Frontend"),
                        ],
                        scoring=AssessmentScoringRule(correct="backend"),
                        max_score=2,
                    ),
                    AssessmentQuestionCreate(
                        key="evidence",
                        question_type="short",
                        prompt="Name the database used in the role description.",
                        required=True,
                        validation=AssessmentValidationRule(
                            min_length=3,
                            max_length=100,
                        ),
                        scoring=AssessmentScoringRule(
                            accepted_values=["PostgreSQL"],
                        ),
                        max_score=2,
                    ),
                ],
            )
        ],
    )


async def _install_human_interview_kit(
    db,
    *,
    organization_id,
    application_id,
    candidate_id,
    position_id,
    recruiter_id,
    interviewer_id,
) -> Interview:
    interview = Interview(
        organization_id=organization_id,
        application_id=application_id,
        candidate_id=candidate_id,
        position_id=position_id,
        created_by=recruiter_id,
        status="draft",
        current_kit_version=1,
    )
    db.add(interview)
    await db.flush()
    questions = [
        InterviewQuestion(
            organization_id=organization_id,
            interview_id=interview.id,
            position=0,
            category="technical",
            question_text="How would you make a PostgreSQL-backed API reliable?",
            rationale="Tests architecture depth",
            expected_signals="Transactions, observability, and failure recovery",
            is_required=True,
        ),
        InterviewQuestion(
            organization_id=organization_id,
            interview_id=interview.id,
            position=1,
            category="behavioral",
            question_text="Describe a difficult production incident you resolved.",
            rationale="Tests structured incident learning",
            expected_signals="Ownership, evidence, and follow-through",
            is_required=True,
        ),
    ]
    db.add_all(questions)
    await db.flush()
    snapshot = [
        {
            "id": str(question.id),
            "position": question.position,
            "category": question.category,
            "question_text": question.question_text,
            "rationale": question.rationale,
            "expected_signals": question.expected_signals,
            "is_required": question.is_required,
        }
        for question in questions
    ]
    db.add_all(
        [
            InterviewKitVersion(
                organization_id=organization_id,
                interview_id=interview.id,
                version=1,
                questions_snapshot=snapshot,
                created_by_user_id=recruiter_id,
            ),
            InterviewAssignment(
                organization_id=organization_id,
                interview_id=interview.id,
                interviewer_user_id=interviewer_id,
                assigned_by_user_id=recruiter_id,
            ),
        ]
    )
    await db.commit()
    return interview


async def _transition(db, user: User, application_id, stage: str, reason: str):
    application = await db.get(Application, application_id)
    assert application is not None
    await db.refresh(application)
    return await transition_application_stage(
        db,
        user,
        application.id,
        ApplicationStageTransition(
            stage=stage,
            expected_version=application.updated_at,
            reason=reason,
        ),
    )


async def test_private_pilot_golden_hiring_journey(db):
    journey_started = datetime.now(UTC) - timedelta(minutes=1)
    organization = Organization(
        name="Golden Pilot Telecom",
        slug=f"golden-pilot-{uuid4().hex[:8]}",
    )
    other_organization = Organization(
        name="Isolated Pilot Tenant",
        slug=f"isolated-pilot-{uuid4().hex[:8]}",
    )
    db.add_all([organization, other_organization])
    await db.flush()
    admin = User(
        organization_id=organization.id,
        email=f"admin-golden-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    recruiter = User(
        organization_id=organization.id,
        email=f"recruiter-golden-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="recruiter",
        locale="en",
    )
    manager = User(
        organization_id=organization.id,
        email=f"manager-golden-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="hiring_manager",
        locale="ru",
    )
    interviewer = User(
        organization_id=organization.id,
        email=f"interviewer-golden-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="interviewer",
        locale="ru",
    )
    unassigned_interviewer = User(
        organization_id=organization.id,
        email=f"unassigned-golden-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="interviewer",
        locale="en",
    )
    outsider = User(
        organization_id=other_organization.id,
        email=f"outsider-isolated-{uuid4().hex[:8]}@example.com",
        password_hash="unused",
        role="admin",
        locale="en",
    )
    db.add_all(
        [
            admin,
            recruiter,
            manager,
            interviewer,
            unassigned_interviewer,
            outsider,
        ]
    )
    await db.commit()

    # RU/EN preferences survive the authenticated API representation, and the
    # public careers preview carries either requested launch locale.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://golden.test",
    ) as client:
        en_me = await client.get("/auth/me", headers=_auth(recruiter))
        ru_me = await client.get("/auth/me", headers=_auth(manager))
        recruiter_permissions = await client.get(
            "/enterprise/permissions/me", headers=_auth(recruiter)
        )
        manager_permissions = await client.get(
            "/enterprise/permissions/me", headers=_auth(manager)
        )
        assert en_me.status_code == ru_me.status_code == 200
        assert recruiter_permissions.status_code == manager_permissions.status_code == 200
        assert en_me.json()["locale"] == "en"
        assert ru_me.json()["locale"] == "ru"
        assert recruiter_permissions.json()["permissions"][
            "requisitions.compensation"
        ] is True
        assert manager_permissions.json()["permissions"][
            "requisitions.compensation"
        ] is True

    requisition = await create_requisition(
        RequisitionCreate(
            title="Senior Platform Engineer",
            department="Engineering",
            business_justification="Approved pilot growth headcount",
            hiring_manager_user_id=manager.id,
            required_approvals=1,
        ),
        db=db,
        user=admin,
    )
    assert (await submit_requisition(requisition.id, db=db, user=admin)).status == (
        "pending"
    )
    approved_requisition = await vote_requisition(
        requisition.id,
        ApprovalVoteRequest(vote="approve", comment="Подтверждено менеджером"),
        db=db,
        user=manager,
    )
    assert approved_requisition.status == "approved"
    opened = await open_requisition(
        requisition.id,
        RequisitionOpenRequest(
            description="Build reliable APIs for the private B2B pilot.",
            requirements={
                "location": "Dushanbe",
                "employment_type": "Full-time",
                "skills": ["Python", "PostgreSQL"],
            },
            public_apply_enabled=True,
        ),
        db=db,
        user=admin,
    )
    assert opened.status == "opened"
    assert opened.position_id is not None
    position = await db.get(Position, opened.position_id)
    assert position is not None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://golden.test",
    ) as client:
        recruiter_position = await client.get(
            f"/positions/{position.id}", headers=_auth(recruiter)
        )
    assert recruiter_position.status_code == 200
    assert recruiter_position.json()["title"] == position.title
    assert position.status == "open"
    assert position.public_apply_enabled is True

    application_payload = {
        "name": "Amina Rahmonova",
        "phone": "+992 900 111 222",
        "email": "amina.golden@example.com",
        "source": "company_website",
        "raw_text": (
            "Platform engineer experienced with Python, PostgreSQL, reliable "
            "distributed systems, observability, and incident response. "
        )
        * 2,
    }
    apply_path = (
        f"/public/v1/careers/{organization.slug}/positions/{position.id}/apply"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://golden.test",
    ) as client:
        en_preview = await client.get(
            f"/public/v1/careers/{organization.slug}/preview",
            params={"locale": "en"},
        )
        ru_preview = await client.get(
            f"/public/v1/careers/{organization.slug}/preview",
            params={"locale": "ru"},
        )
        assert en_preview.status_code == ru_preview.status_code == 200
        assert 'data-locale="en"' in en_preview.text
        assert 'data-locale="ru"' in ru_preview.text

        first_apply = await client.post(
            apply_path,
            data=application_payload,
            headers={"Idempotency-Key": "golden-public-application"},
        )
        duplicate_apply = await client.post(
            apply_path,
            data=application_payload,
            headers={"Idempotency-Key": "golden-public-application"},
        )
        assert first_apply.status_code == duplicate_apply.status_code == 201
        assert first_apply.json() == duplicate_apply.json() == {
            "accepted": True,
            "message": "Application received. We'll be in touch.",
        }
        wrong_tenant_apply = await client.post(
            f"/public/v1/careers/{other_organization.slug}/positions/"
            f"{position.id}/apply",
            data=application_payload,
        )
        assert wrong_tenant_apply.status_code == 404

    candidate = (
        await db.execute(
            select(Candidate).where(
                Candidate.organization_id == organization.id,
                Candidate.email_normalized == "amina.golden@example.com",
            )
        )
    ).scalar_one()
    application = (
        await db.execute(
            select(Application).where(
                Application.organization_id == organization.id,
                Application.candidate_id == candidate.id,
                Application.position_id == position.id,
            )
        )
    ).scalar_one()
    assert application.stage == "new"
    assert application.source == "company_website"
    assert (
        await db.scalar(
            select(func.count(Application.id)).where(
                Application.candidate_id == candidate.id,
                Application.position_id == position.id,
                Application.closed_at.is_(None),
            )
        )
        == 1
    )
    assert (
        await db.scalar(
            select(func.count(Score.id)).where(
                Score.candidate_id == candidate.id,
                Score.position_id == position.id,
            )
        )
        == 1
    )
    first_class = await get_application(db, organization.id, application.id)
    assert first_class.id == application.id
    assert first_class.legacy_score_id is not None
    with pytest.raises(HTTPException) as hidden_application:
        await get_application(db, other_organization.id, application.id)
    assert hidden_application.value.status_code == 404

    assessment_template = await create_template(
        db,
        recruiter,
        AssessmentTemplateCreate(
            name="Golden engineering assessment",
            description="Deterministic native screening evidence",
        ),
    )
    assessment_version = await create_version(
        db,
        recruiter,
        assessment_template.id,
        _assessment_version(),
    )
    invitation = await create_invitation(
        db,
        recruiter,
        application.id,
        AssessmentInvitationCreate(
            template_version_id=assessment_version.id,
            recipient_email="amina.golden@example.com",
            expires_at=datetime.now(UTC) + timedelta(days=2),
        ),
    )
    stored_invitation = await db.get(AssessmentInvitation, invitation.id)
    assert stored_invitation is not None
    assert invitation.token not in stored_invitation.token_digest

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://golden.test",
    ) as client:
        launch = await client.get(f"/public/assessments/{invitation.token}")
        assert launch.status_code == 200
        assert launch.headers["cache-control"].startswith("no-store")
        public_questions = {
            question["key"]: question
            for section in launch.json()["sections"]
            for question in section["questions"]
        }
        attempt_response = await client.post(
            f"/public/assessments/{invitation.token}/attempts"
        )
        assert attempt_response.status_code == 200
        attempt_id = attempt_response.json()["id"]
        autosaved = await client.patch(
            f"/public/assessments/{invitation.token}/attempts/"
            f"{attempt_id}/answers",
            json={
                "answers": [
                    {
                        "question_id": public_questions["eligible"]["id"],
                        "value": True,
                    },
                    {
                        "question_id": public_questions["specialty"]["id"],
                        "value": "backend",
                    },
                    {
                        "question_id": public_questions["evidence"]["id"],
                        "value": "PostgreSQL",
                    },
                ]
            },
        )
        assert autosaved.status_code == 200
        submitted_assessment = await client.post(
            f"/public/assessments/{invitation.token}/attempts/{attempt_id}/submit"
        )
        assert submitted_assessment.status_code == 200
        assessment_result = submitted_assessment.json()["result"]
        assert "application_id" not in assessment_result
        assert "knockout_reason" not in assessment_result
        assert "breakdown" not in assessment_result
        assert assessment_result["attempt_id"] == attempt_id
        assert assessment_result["percentage"] == "100.0000"
        assert assessment_result["passed"] is True
        assert assessment_result["needs_manual_review"] is False

    attempts = await list_invitation_attempts(
        db,
        organization.id,
        invitation.id,
    )
    assert len(attempts.items) == 1
    assert attempts.items[0].status == "submitted"
    with pytest.raises(HTTPException) as hidden_attempts:
        await list_invitation_attempts(
            db,
            other_organization.id,
            invitation.id,
        )
    assert hidden_attempts.value.status_code == 404

    application = await _transition(
        db,
        recruiter,
        application.id,
        "screening",
        "native_assessment_passed",
    )
    assert application.stage == "screening"
    application = await _transition(
        db,
        recruiter,
        application.id,
        "interview",
        "screening_evidence_reviewed",
    )
    assert application.stage == "interview"

    interview = await _install_human_interview_kit(
        db,
        organization_id=organization.id,
        application_id=application.id,
        candidate_id=candidate.id,
        position_id=position.id,
        recruiter_id=recruiter.id,
        interviewer_id=interviewer.id,
    )
    with pytest.raises(HTTPException) as unassigned_interview:
        await get_interview(
            interview.id,
            db=db,
            user=unassigned_interviewer,
        )
    assert unassigned_interview.value.status_code == 404
    assigned_interview = await get_interview(
        interview.id,
        db=db,
        user=interviewer,
    )
    assert assigned_interview.application_id == application.id
    for question in assigned_interview.questions:
        await score_question(
            interview.id,
            question.id,
            QuestionScoreRequest(score=5, notes="Structured interview evidence"),
            db=db,
            user=interviewer,
        )
    completed_interview = await complete_interview(
        interview.id,
        InterviewCompleteRequest(notes="All required evidence captured"),
        db=db,
        user=interviewer,
    )
    assert completed_interview.status == "scored"
    assert completed_interview.overall_score == 5

    scorecard_template = await create_scorecard_template(
        ScorecardTemplateCreate(
            name="Golden structured scorecard",
            description="Versioned pilot hiring rubric",
            criteria=[
                ScorecardCriterionCreate(
                    key="technical_depth",
                    label="Technical depth",
                    weight=2,
                    is_required=True,
                ),
                ScorecardCriterionCreate(
                    key="collaboration",
                    label="Collaboration",
                    weight=1,
                    is_required=True,
                ),
            ],
        ),
        db=db,
        user=recruiter,
    )
    scorecard_panel = await assign_candidate_scorecards(
        candidate.id,
        ScorecardAssignRequest(
            application_id=application.id,
            position_id=position.id,
            template_id=scorecard_template.id,
            user_ids=[interviewer.id],
        ),
        db=db,
        user=recruiter,
    )
    assignment = scorecard_panel.assignments[0]
    scorecard_ratings = [
        ScorecardRatingInput(
            criterion_id=criterion.id,
            rating=5 if criterion.key == "technical_depth" else 4,
            comment=f"Evidence for {criterion.key}",
        )
        for criterion in scorecard_template.criteria
    ]
    with pytest.raises(HTTPException) as unassigned_scorecard:
        await update_scorecard_draft(
            assignment.id,
            ScorecardDraftUpdate(ratings=scorecard_ratings),
            db=db,
            user=unassigned_interviewer,
        )
    assert unassigned_scorecard.value.status_code == 404
    await update_scorecard_draft(
        assignment.id,
        ScorecardDraftUpdate(ratings=scorecard_ratings),
        db=db,
        user=interviewer,
    )
    submitted_panel = await submit_scorecard(
        assignment.id,
        db=db,
        user=interviewer,
    )
    assert submitted_panel.revealed is True
    assert submitted_panel.completion_count == submitted_panel.assignment_count == 1
    assert submitted_panel.aggregate_average == 4.67
    with pytest.raises(HTTPException) as hidden_scorecards:
        await get_candidate_scorecards(
            candidate.id,
            position_id=position.id,
            application_id=application.id,
            db=db,
            user=outsider,
        )
    assert hidden_scorecards.value.status_code == 404

    application = await _transition(
        db,
        recruiter,
        application.id,
        "offer",
        "structured_interview_approved",
    )
    assert application.stage == "offer"
    offer_template = await create_offer_template(
        OfferTemplateCreate(
            name="Golden pilot offer",
            subject_template="Offer for {{position.title}}",
            body_template=(
                "Hello {{candidate.full_name}}. Package: {{offer.salary}}. "
                "Start date: {{offer.start_date}}."
            ),
        ),
        db=db,
        user=admin,
    )
    offer = await create_candidate_offer(
        candidate.id,
        CandidateOfferCreate(
            application_id=application.id,
            position_id=position.id,
            template_id=offer_template.id,
            salary_amount=Decimal("18000"),
            salary_currency="TJS",
            start_date=datetime.now(UTC).date() + timedelta(days=30),
            expires_at=datetime.now(UTC) + timedelta(days=7),
            required_approvals=1,
        ),
        db=db,
        user=admin,
    )
    db.add(
        PermissionOverride(
            organization_id=organization.id,
            role="hiring_manager",
            permission="offers.compensation",
            allowed=True,
        )
    )
    await db.commit()
    assert (await submit_offer(offer.id, db=db, user=admin)).status == "pending"
    approved_offer = await vote_offer(
        offer.id,
        ApprovalVoteRequest(vote="approve", comment="Compensation approved"),
        db=db,
        user=manager,
    )
    assert approved_offer.status == "approved"
    sent_offer = await send_offer(offer.id, db=db, user=admin)
    assert sent_offer.offer.status == "sent"
    stored_offer = await db.get(CandidateOffer, offer.id)
    assert stored_offer is not None
    assert sent_offer.token not in (stored_offer.token_digest or "")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://golden.test",
    ) as client:
        public_offer = await client.get(f"/public/offers/{sent_offer.token}")
        assert public_offer.status_code == 200
        assert public_offer.headers["cache-control"] == "private, no-store"
        accepted_offer = await client.post(
            f"/public/offers/{sent_offer.token}/respond",
            json={
                "response": "accept",
                "signature_name": "Amina Rahmonova",
            },
        )
        assert accepted_offer.status_code == 200
        assert accepted_offer.json()["status"] == "accepted"
        consumed_offer = await client.get(f"/public/offers/{sent_offer.token}")
        assert consumed_offer.status_code == 410

    await db.refresh(stored_offer)
    assert stored_offer.status == "accepted"
    application = await db.get(
        Application,
        application.id,
        populate_existing=True,
    )
    assert application is not None
    assert application.stage == "hired"
    score = (
        await db.execute(
            select(Score).where(
                Score.candidate_id == candidate.id,
                Score.position_id == position.id,
            )
        )
    ).scalar_one()
    assert score.stage == "hired"
    hired_event = (
        await db.execute(
            select(ApplicationStageEvent).where(
                ApplicationStageEvent.application_id == application.id,
                ApplicationStageEvent.to_stage == "hired",
            )
        )
    ).scalar_one()
    assert hired_event.actor_kind == "public"
    assert hired_event.reason == "offer_accepted"

    handoffs = await list_candidate_handoffs(candidate.id, db=db, user=admin)
    assert len(handoffs.items) == 1
    handoff = await update_candidate_handoff(
        handoffs.items[0].id,
        CandidateHandoffUpdate(
            checklist={
                "documents": True,
                "equipment": True,
                "accounts": True,
                "first_day": True,
            }
        ),
        db=db,
        user=admin,
    )
    assert handoff.status == "completed"
    assert handoff.application_id == application.id

    report = await application_analytics(
        db,
        organization.id,
        start_at=journey_started,
        end_at=datetime.now(UTC) + timedelta(minutes=1),
        timezone_name="Asia/Dushanbe",
        position_id=position.id,
        drilldown_dimension="all",
    )
    assert report.total_applications == 1
    assert report.unique_candidates == 1
    assert {item.key: item.count for item in report.by_source} == {
        "company_website": 1
    }
    stages = {item.stage: item for item in report.stages}
    for stage in ("new", "screening", "interview", "offer", "hired"):
        assert stages[stage].entered == 1
    assert stages["hired"].current == 1
    assert report.assessments.invitations == 1
    assert report.assessments.started == 1
    assert report.assessments.completed == 1
    assert report.assessments.dropped_off == 0
    assert report.offers.sent == 1
    assert report.offers.accepted == 1
    assert report.offers.acceptance_rate_percent == 100
    assert report.reconciliation.applications_checked == 1
    assert report.reconciliation.applications_without_initial_event == 0
    assert report.reconciliation.duplicate_initial_events == 0
    assert report.reconciliation.stage_mismatches == 0
    assert report.drilldown is not None
    assert report.drilldown.total == 1
    assert report.drilldown.items[0].application_id == application.id
    assert report.drilldown.items[0].current_stage == "hired"
